"""What `otto docker build` prints is what the daemon holds.

Each test runs the verb, then asks the same host's daemon through the
passthrough (`otto host <id> exec "docker ..."`), which shares no code with
the docker verbs, and compares. The two parsers are in `_honesty`, pinned
against otto's real output by a hostless unit test.
"""

import re
import tarfile
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo

from ._cli import _WIDE, _run_otto
from ._honesty import DAEMON_LIST_COMMAND, parse_built_line, parse_daemon_rows

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("docker_e2e")]

_IMAGE = "repo1/repo1-api"
_LATEST = "repo1-api:latest"


def _daemon_images(host: str, xdir, *, allow_empty: bool = False) -> "dict[str, str]":
    """reference -> short id, as the daemon on *host* lists them.

    A daemon with no images lists nothing, which is only believable as the
    state *before* a build; *allow_empty* says the caller expects that.
    """
    out = _run_otto("host", host, "exec", DAEMON_LIST_COMMAND, xdir=xdir, env=_WIDE)
    assert out.returncode == 0, out.stderr
    rows = parse_daemon_rows(out.stdout + out.stderr)
    assert rows or allow_empty, f"the daemon listed no image rows:\n{out.stdout}"
    return rows


def _rmi(host: str, xdir, *refs: str) -> None:
    """Remove images from the daemon on *host*, best effort: the exit code is ignored."""
    _run_otto("host", host, "exec", f"docker rmi {' '.join(refs)}", xdir=xdir)


def _references_of(daemon: "dict[str, str]", image_id: str) -> "set[str]":
    return {reference for reference, listed in daemon.items() if listed == image_id}


def _build(host: str, xdir, *flags: str):
    built = _run_otto("docker", "build", "repo1-api", "--on", host, *flags, xdir=xdir, env=_WIDE)
    assert built.returncode == 0, built.stdout + built.stderr
    return built


def test_every_reference_and_id_build_prints_is_the_daemons(docker_host, tmp_path):
    built = _build(docker_host, tmp_path)
    line = parse_built_line(built.stdout, _IMAGE)
    assert line, built.stdout

    daemon = _daemon_images(docker_host, tmp_path)
    for reference in line.refs:
        assert daemon.get(reference) == line.image_id, (reference, line.image_id, daemon)


def test_build_leaves_no_tag_nobody_asked_for(docker_host, tmp_path):
    before = _daemon_images(docker_host, tmp_path, allow_empty=True)
    built = _build(docker_host, tmp_path)
    line = parse_built_line(built.stdout, _IMAGE)
    assert line, built.stdout
    assert line.refs == [_LATEST], line

    # Keyed on the image just built, under any repository name: a reference on
    # this id that is neither asked for nor already there before is one build added.
    # This is the weak form (a cached build reuses an id that may already carry a
    # stable extra tag); the strict form is in the two fresh-id tests below.
    after = _references_of(_daemon_images(docker_host, tmp_path), line.image_id)
    assert set(line.refs) <= after, (line, after)
    assert after <= set(line.refs) | _references_of(before, line.image_id), (line, after)


def test_a_typed_tag_is_the_tag_and_nothing_else_is_added(docker_host, tmp_path):
    tag = "repo1-api:honesty-typed"
    line = None
    try:
        # --no-cache gives the image a fresh id, so no reference was ever on it:
        # whatever the daemon lists for it now, this build put there.
        built = _build(docker_host, tmp_path, "-t", tag, "--no-cache")
        line = parse_built_line(built.stdout, _IMAGE)
        assert line, built.stdout
        assert line.refs == [tag], line

        daemon = _daemon_images(docker_host, tmp_path)
        assert daemon.get(tag) == line.image_id, (tag, line.image_id, daemon)
        assert _references_of(daemon, line.image_id) == {tag}, (line, daemon)
    finally:
        # By tag, then by id: removing the last tag of an image removes it, but a
        # failed build or an extra tag would leave the id dangling.
        _rmi(docker_host, tmp_path, tag)
        if line:
            _rmi(docker_host, tmp_path, line.image_id)


def test_no_cache_makes_docker_rebuild_every_step(docker_host, tmp_path):
    _build(docker_host, tmp_path)
    warm = _daemon_images(docker_host, tmp_path)[_LATEST]
    try:
        _build(docker_host, tmp_path, "--no-cache")
        daemon = _daemon_images(docker_host, tmp_path)
        forced = daemon[_LATEST]

        # A build served from docker's layer cache yields the same image id; one
        # that re-ran every step yields a new one.
        assert forced != warm, f"--no-cache rebuilt to the same image id {warm}"
        # And the new id carries exactly the one reference the build was asked for.
        assert _references_of(daemon, forced) == {_LATEST}, (forced, daemon)
    finally:
        # `:latest` moved to the forced image; the warm one it left is dangling.
        _rmi(docker_host, tmp_path, warm)


_ARCHIVE_IMAGE = "honesty-archive"
_ARCHIVE_REPO = "honesty"
_LAB_DATA = PROJECT_ROOT / "tests" / "_fixtures" / "lab_data" / "tech1"
_REPO1_DOCKER = PROJECT_ROOT / "tests" / "repo1" / "docker"


def _archive_repo(root: Path, dockerfile: str) -> Path:
    """A throwaway SUT repo whose one image is `tests/repo1/docker` as a .tar.gz.

    The Dockerfile and its files sit at the archive root; *dockerfile* is the
    path inside the archive. The lab is the same one repo1 reads, by absolute
    path, so `--on <host>` resolves the same hosts.
    """
    archive = root / "ctx.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for member in sorted(_REPO1_DOCKER.iterdir()):
            tar.add(member, arcname=member.name)
    return make_sut_repo(
        root / "sut",
        name=_ARCHIVE_REPO,
        extra=f"""\
[[lab.sources]]
backend = "json"
paths = ["{_LAB_DATA}"]

[docker]

[[docker.images]]
name = "{_ARCHIVE_IMAGE}"
dockerfile = "{dockerfile}"
context = "{archive}"
""",
    )


def _build_archive_image(host: str, sut: Path, xdir):
    return _run_otto(
        "docker", "build", _ARCHIVE_IMAGE, "--on", host, sut_dirs=str(sut), xdir=xdir, env=_WIDE
    )


def _archive_left_on_host(host: str, xdir) -> bool:
    """Whether the build directory on *host* still holds an uploaded archive of the image."""
    listing = _run_otto(
        "host", host, "exec", f"ls /tmp/otto-docker/{_ARCHIVE_REPO}/build", xdir=xdir, env=_WIDE
    )
    return re.search(rf"{_ARCHIVE_IMAGE}\..*\.tar\.gz", listing.stdout + listing.stderr) is not None


def test_an_archive_context_builds_what_the_daemon_lists(docker_host, tmp_path):
    sut = _archive_repo(tmp_path, "Dockerfile")
    line = None
    try:
        built = _build_archive_image(docker_host, sut, tmp_path)
        assert built.returncode == 0, built.stdout + built.stderr
        line = parse_built_line(built.stdout, f"{_ARCHIVE_REPO}/{_ARCHIVE_IMAGE}")
        assert line, built.stdout

        daemon = _daemon_images(docker_host, tmp_path)
        for reference in line.refs:
            assert daemon.get(reference) == line.image_id, (reference, line.image_id, daemon)
        # The uploaded archive is removed once docker has read it.
        assert not _archive_left_on_host(docker_host, tmp_path)
    finally:
        if line:
            _rmi(docker_host, tmp_path, line.image_id)


def test_a_wrong_dockerfile_path_inside_the_archive_is_dockers_error(docker_host, tmp_path):
    sut = _archive_repo(tmp_path, "nope/Dockerfile")
    built = _build_archive_image(docker_host, sut, tmp_path)
    output = built.stdout + built.stderr

    assert built.returncode == 1, output
    # buildx says "failed to read dockerfile", the classic builder "Cannot locate
    # specified Dockerfile": docker's own words either way, relayed. Not just the
    # word "dockerfile", which otto's own echo of `-f nope/Dockerfile` also carries.
    low = output.lower()
    assert (
        "cannot locate specified dockerfile" in low
        or "failed to read dockerfile" in low
        or "no such file" in low
    ), output
    assert not _archive_left_on_host(docker_host, tmp_path)
