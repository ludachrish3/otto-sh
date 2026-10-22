"""End-to-end: a shell product inside a compose container, collected by a container-only pattern.

    otto -l unix docker compose up covctr                      (project_ctr)
    otto -l unix test --cov --cov-clean TestCovCtr
    otto -l unix cov report <run> --dir <report>
    otto -l unix test --no-cov TestCovCtr                      (exercise again)
    otto -l unix cov get --output <get>
    otto -l unix docker compose down covctr

project_ctr's ``[coverage] hosts`` fullmatches only ``test3.covctr.app``,
so every coverage walk must include container hosts to find anything.

Prerequisites: test3 up with docker; docker on the dev VM with the kmodcov
5.15 column's compiler image and ``arm64v8/ubuntu:22.04`` (both already
provisioned for the kmodcov matrix). ``tests/project_ctr/build.sh`` builds
the product and the runtime image's tarball here when missing or stale;
test3 has no internet, so the module fixture ``docker load``\\ s that
tarball there unless test3 already holds the same image id. The stack's
compose project carries a fresh ``e2e-<hex>`` suffix, and its teardown —
``compose down``, then a label-scoped sweep of containers and networks —
is armed before ``compose up``, while the test3 lease is held. The
product's counters live inside the container, never in test3's own
``/var/cov`` (``[coverage] hosts`` excludes test3); the module joins the
``coverage_e2e`` xdist group so it runs serially with the other coverage
e2es, which also drive test3's docker daemon and lab hosts.
"""

import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture
from otto.coverage.store.model import CoverageStore
from otto.docker.compose import use_case_project
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.gitrepo import git_env
from tests.e2e._otto_subprocess import PROJECT_CTR, run_otto
from tests.e2e.cov._project5_build import _artifacts_are_stale, _hits, _line_of, _record

_LAB = "unix"
_PARENT = "test3"
_USE_CASE = "covctr"
CONTAINER = "test3.covctr.app"
PRODUCT = "counter"
BUILD = PROJECT_CTR / "build"
RUNTIME_IMAGE = "otto-cov-ctr-runtime:e2e"
RUNTIME_TAR = BUILD / "otto-cov-ctr-runtime.tar"
SOURCES = [
    *(PROJECT_CTR / "src").glob("*.[ch]"),
    PROJECT_CTR / "docker" / "Dockerfile",
    PROJECT_CTR / "build.sh",
]
_COMMITTED = ["src", "docker", "build.sh", ".otto", "tests"]
ARTIFACTS = [BUILD / "counter", BUILD / "main.gcno", BUILD / "ops.gcno", RUNTIME_TAR]
# `name=<value>`: never `[...]`, which otto's Rich log console reads as
# markup and drops, and never matched by the command's own echoed line,
# which shows `=<$(` instead of a value.
_MARKED = re.compile(r"(\w+)=<([^>$]*)>")
_CLEAN = {"containers": "", "networks": ""}
"""A compose project with no container and no network left on test3."""


def _ensure_artifacts(tmp_path_factory) -> None:
    """Build the product and the runtime tarball when missing or stale; refuse uncommitted sources.

    A capture anchors every measured file to a committed git blob at HEAD,
    so an uncommitted source makes the capture and ``_line_of`` disagree.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *_COMMITTED],
        cwd=PROJECT_CTR,
        env=git_env(tmp_path_factory.mktemp("githome")),
        check=False,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, f"git status failed: {status.stderr}"
    assert not status.stdout.strip(), f"commit project_ctr's sources first:\n{status.stdout}"
    # A pruned local runtime image is as stale as an old tarball: the
    # fixture compares test3's image id against it.
    if _artifacts_are_stale(ARTIFACTS, SOURCES) or not _local_image_id():
        build = subprocess.run(
            [str(PROJECT_CTR / "build.sh")], check=False, capture_output=True, text=True
        )
        assert build.returncode == 0, f"build.sh failed:\n{build.stdout}\n{build.stderr}"


def _local_image_id() -> str:
    """Return the dev VM's runtime image id, or ``""`` when the image is not there."""
    out = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", RUNTIME_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


class _Otto:
    """``otto --lab unix`` against project_ctr, with one compose suffix on every call."""

    def __init__(self, xdir: Path, suffix: str) -> None:
        self.xdir = xdir
        self.env = {"OTTO_COMPOSE_SUFFIX": suffix, "COLUMNS": "200"}

    def __call__(self, *argv: str, timeout: int = 180, check: bool = True):
        result = run_otto(
            list(argv),
            xdir=self.xdir,
            roots=PROJECT_CTR,
            lab=_LAB,
            extra_env=self.env,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def marked(self, cmd: str) -> dict[str, str]:
        """Run *cmd* on test3 and return each ``name=<value>`` marker it echoed."""
        result = self("host", _PARENT, "exec", cmd, timeout=120)
        return {k: v.strip() for k, v in _MARKED.findall(result.stdout + result.stderr)}


def _ensure_runtime_image(otto: _Otto) -> None:
    """Make test3 hold the runtime image the dev VM built, leaving no tarball behind."""
    want = _local_image_id()
    assert want, f"{RUNTIME_IMAGE} is missing on the dev VM after the build"
    have = otto.marked(
        f"echo \"image=<$(docker image inspect --format '{{{{.Id}}}}' {RUNTIME_IMAGE} "
        '2>/dev/null)>"'
    )
    if have.get("image") == want:
        return
    remote = f"/tmp/{RUNTIME_TAR.name}"
    try:
        otto("host", _PARENT, "put", str(RUNTIME_TAR), "/tmp", timeout=600)
        loaded = otto.marked(
            f"docker load -i {remote} >/dev/null; "
            f"echo \"image=<$(docker image inspect --format '{{{{.Id}}}}' {RUNTIME_IMAGE} "
            '2>/dev/null)>"'
        )
    finally:
        # A put that lands and a load that fails must not leave ~27 MB in test3's /tmp.
        otto("host", _PARENT, "exec", f"rm -f {remote}", timeout=120, check=False)
    assert loaded.get("image") == want, f"test3 did not load {RUNTIME_IMAGE}: {loaded}"


def _leftovers(otto: _Otto, project: str) -> dict[str, str]:
    label = f"label=com.docker.compose.project={project}"
    return otto.marked(
        f'echo "containers=<$(docker ps -aq --filter {label} | tr "\\n" " ")>"; '
        f'echo "networks=<$(docker network ls -q --filter {label} | tr "\\n" " ")>"'
    )


@dataclass
class _Teardown:
    after_down: dict[str, str]
    """What ``compose down`` alone left — the hygiene test's subject."""
    after_sweep: dict[str, str]
    """What the label sweep left; anything here is a leak on test3."""


def _teardown(otto: _Otto, project: str) -> _Teardown:
    """Bring the stack down, sweep anything left, and report both listings.

    It asserts nothing: the caller decides whether a leftover is the headline
    or a footnote to an earlier failure.
    """
    otto("docker", "compose", "down", _USE_CASE, check=False)
    after_down = _leftovers(otto, project)
    label = f"label=com.docker.compose.project={project}"
    otto.marked(
        f"docker ps -aq --filter {label} | xargs -r docker rm -f >/dev/null; "
        f"docker network ls -q --filter {label} | xargs -r docker network rm >/dev/null; "
        'echo "swept=<1>"'
    )
    return _Teardown(after_down=after_down, after_sweep=_leftovers(otto, project))


def _teardown_after_failure(otto: _Otto, project: str) -> None:
    """Clean up after a failed body without replacing its error.

    Whatever the cleanup raises or leaves goes to stderr, which pytest shows
    beside the body's traceback — so the first failure stays the headline.
    """
    try:
        torn = _teardown(otto, project)
    except Exception as e:  # noqa: BLE001 — reported, never allowed to mask the body's error
        sys.stderr.write(f"teardown after the failure also failed: {e!r}\n")
        return
    if torn.after_sweep != _CLEAN:
        sys.stderr.write(f"{project} still on test3 after the sweep: {torn.after_sweep}\n")


@dataclass
class Outcome:
    log_dir: Path
    store: CoverageStore
    get_dir: Path
    while_up: dict[str, str]
    after_down: dict[str, str]


@pytest.fixture(scope="module")
def ctr_run(tmp_path_factory):
    _ensure_artifacts(tmp_path_factory)
    tmp = tmp_path_factory.mktemp("container_host_e2e")
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    get_dir = tmp / "get"
    suffix = "e2e-" + uuid.uuid4().hex[:8]
    project = use_case_project(_LAB, _USE_CASE, suffix)
    otto = _Otto(xdir, suffix)
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, [_PARENT]):
        _ensure_runtime_image(otto)
        # Cleanup always runs; a leftover is asserted only when the body
        # succeeded, so it never masks the body's own failure.
        try:
            otto("docker", "compose", "up", _USE_CASE, timeout=300)
            # The positive control for the label the teardown sweeps by.
            while_up = _leftovers(otto, project)
            otto("test", "--cov", "--cov-clean", "TestCovCtr", timeout=900)
            (log_dir,) = sorted((xdir / "test").glob("*"))
            otto("cov", "report", str(log_dir), "--dir", str(report_dir), timeout=120)
            # Collection clears the counters it captured, so exercise the
            # product again — --no-cov, since collection is otherwise on by
            # default for an instrumented lab — before `otto cov get`.
            otto("test", "--no-cov", "TestCovCtr", timeout=600)
            otto("cov", "get", "--output", str(get_dir), timeout=300)
        except BaseException:
            _teardown_after_failure(otto, project)
            raise
        torn = _teardown(otto, project)
        assert torn.after_sweep == _CLEAN, f"{project} still on test3 after the sweep: {torn}"
    return Outcome(
        log_dir=log_dir,
        store=CoverageStore.load(report_dir / "store.json"),
        get_dir=get_dir,
        while_up=while_up,
        after_down=torn.after_down,
    )


def _gcda(tree: Path) -> list[str]:
    return sorted(str(p.relative_to(tree)) for p in tree.rglob("*.gcda"))


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestContainerHostCoverage:
    def test_the_container_is_the_only_collected_host(self, ctr_run):
        cov = ctr_run.log_dir / "cov"
        hosts = {d.name for d in cov.iterdir() if d.is_dir() and not d.name.startswith(".")}
        assert hosts == {CONTAINER}
        leaf = cov / CONTAINER / PRODUCT
        assert Capture.load(leaf / "capture.json").product == PRODUCT
        # Path-keeping: no GCOV_PREFIX_STRIP, so each counter sits under its
        # object's full /build/... path below the product's leaf.
        assert _gcda(leaf) == ["build/build/main.gcda", "build/build/ops.gcda"]

    def test_the_report_counts_the_exercised_function(self, ctr_run):
        rec = _record(ctr_run.store, "src/ops.c")
        src = PROJECT_CTR / "src" / "ops.c"
        assert _hits(rec, _line_of(src, "return a + b;")) == 1
        assert _hits(rec, _line_of(src, "return a - b;")) == 0

    def test_cov_get_collects_the_container(self, ctr_run):
        leaf = ctr_run.get_dir / "cov" / CONTAINER / PRODUCT
        assert Capture.load(leaf / "capture.json").product == PRODUCT
        assert _gcda(leaf) == ["build/build/main.gcda", "build/build/ops.gcda"]

    def test_compose_down_leaves_nothing_on_test3(self, ctr_run):
        # The compose-project label must have matched the running stack, or
        # the empty listing after `down` would prove nothing.
        assert ctr_run.while_up["containers"], f"the label matched nothing: {ctr_run.while_up}"
        assert ctr_run.while_up["networks"], f"the label matched nothing: {ctr_run.while_up}"
        assert ctr_run.after_down == _CLEAN
