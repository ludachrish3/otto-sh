"""What ``otto docker image ls|rm|prune`` print and do is what the daemon holds and loses.

Each test runs the verb through the CLI on the leased host, then asks the same
daemon through the passthrough (``otto host <id> exec "docker ..."``), which
shares no code with the docker verbs, and compares.

SAFETY: the bed's daemons hold images other tests need. Every image here is a
throwaway built from ``scratch`` with a per-test label
(``otto-e2e-699=<uuid>``) and a per-test reference; ``image rm`` names only
those references, ``image prune`` always runs with that label as its filter and
never with ``-a``, and the finalizer removes whatever carries the label.
"""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ._cli import _WIDE, _flat, _run_otto
from ._honesty import DAEMON_IMAGE_PAIRS_COMMAND, MARK, parse_daemon_rows, parse_images_rows

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("docker_e2e")]

_LABEL = "otto-e2e-699"
_FULL_ID = re.compile(rf"{MARK} (sha256:[0-9a-f]{{64}})")


@dataclass
class Throwaway:
    """The test's own images on *host*: one label, one repository, nothing else touched."""

    host: str
    xdir: Path
    token: str

    @property
    def label(self) -> str:
        """The ``--filter`` value that matches this test's images and no other."""
        return f"label={_LABEL}={self.token}"

    @property
    def repository(self) -> str:
        """The repository every reference of this test lives under."""
        return f"{_LABEL}-{self.token}"

    def exec(self, command: str) -> str:
        """Run *command* on the host through the passthrough; return its console text."""
        out = _run_otto("host", self.host, "exec", command, xdir=self.xdir, env=_WIDE)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout + out.stderr

    def build(self, tag: str, rev: int) -> str:
        """Build a scratch image tagged ``<repository>:<tag>``; return its full id.

        One ``LABEL`` instruction, so no intermediate image is left behind. A
        second build of the same tag with another *rev* leaves the first dangling.
        """
        ref = f"{self.repository}:{tag}"
        self.exec(
            f"printf 'FROM scratch\\nLABEL {_LABEL}={self.token} rev={rev}\\n' "
            f"| docker build -q -t {ref} -"
        )
        return self.full_id(ref)

    def full_id(self, ref: str) -> str:
        """The daemon's own full id for *ref*."""
        found = _FULL_ID.findall(
            self.exec(f"docker image inspect --format '{MARK} {{{{.Id}}}}' {ref}")
        )
        assert len(found) == 1, f"no single id for {ref}: {found}"
        return found[0]

    def held_ids(self, *, dangling: bool = False) -> "set[str]":
        """Every full id the daemon holds with this test's label (only the dangling ones)."""
        extra = " --filter dangling=true" if dangling else ""
        text = self.exec(
            f"docker image ls -a --no-trunc --filter {self.label}{extra} "
            f"--format '{MARK} {{{{.ID}}}}'"
        )
        return set(_FULL_ID.findall(text))


@pytest.fixture
def throwaway(docker_host, tmp_path) -> Iterator[Throwaway]:
    """Yield the test's image namespace; remove every image carrying its label on exit."""
    images = Throwaway(docker_host, tmp_path, uuid.uuid4().hex[:12])
    yield images
    # By id, with the label as the only selector: nothing another test owns can match.
    images.exec(
        f"for i in $(docker image ls -aq --no-trunc --filter {images.label}); "
        f"do docker image rm -f -- $i; done; true"
    )
    assert images.held_ids() == set(), "the finalizer left a throwaway image behind"


def test_image_ls_and_its_alias_print_the_daemons_references_and_ids(throwaway):
    ref = f"{throwaway.repository}:a"
    full = throwaway.build("a", 1)
    listed = {}
    for argv in (["image", "ls"], ["images"]):
        out = _run_otto("docker", *argv, "--parent", throwaway.host, xdir=throwaway.xdir, env=_WIDE)
        assert out.returncode == 0, out.stdout + out.stderr
        assert out.stdout.startswith(f"== {throwaway.host} ==\n"), out.stdout
        listed[" ".join(argv)] = parse_images_rows(out.stdout)[throwaway.host]
    # The throwaway is what makes the comparison non-vacuous.
    assert listed["image ls"].get(ref) == full.removeprefix("sha256:")[:12], listed["image ls"]
    daemon = parse_daemon_rows(throwaway.exec(DAEMON_IMAGE_PAIRS_COMMAND))
    for reference, short in listed["image ls"].items():
        assert daemon.get(reference) == short, f"{reference}: {short} is not the daemon's"
    assert listed["images"][ref] == listed["image ls"][ref]


def test_image_rm_dry_run_names_the_command_and_removes_nothing(throwaway):
    ref = f"{throwaway.repository}:a"
    full = throwaway.build("a", 1)
    dry = _run_otto(
        "-n",
        "docker",
        "image",
        "rm",
        ref,
        "--parent",
        throwaway.host,
        xdir=throwaway.xdir,
        env=_WIDE,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert f"on {throwaway.host}: docker image rm -- {ref}" in _flat(dry.stdout + dry.stderr)
    assert throwaway.held_ids() == {full}


def test_image_rm_prints_what_docker_did_and_the_daemon_no_longer_holds_it(throwaway):
    ref = f"{throwaway.repository}:a"
    full = throwaway.build("a", 1)
    out = _run_otto(
        "docker", "image", "rm", ref, "--parent", throwaway.host, xdir=throwaway.xdir, env=_WIDE
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith(f"== {throwaway.host} ==\n"), out.stdout
    assert f"Untagged: {ref}\n" in out.stdout, out.stdout
    assert f"Deleted: {full}\n" in out.stdout, out.stdout
    assert throwaway.held_ids() == set()


def test_image_rm_of_an_image_the_daemon_lacks_is_dockers_own_error(throwaway):
    missing = f"{throwaway.repository}:missing"
    out = _run_otto(
        "docker",
        "image",
        "rm",
        missing,
        "--parent",
        throwaway.host,
        xdir=throwaway.xdir,
        env=_WIDE,
    )
    assert out.returncode == 1, out.stdout + out.stderr
    assert out.stdout.startswith(f"== {throwaway.host} ==\n"), out.stdout
    assert "No such image" in out.stdout, out.stdout


def test_image_prune_removes_exactly_the_dangling_images_its_filter_names(throwaway):
    first = throwaway.build("a", 1)
    second = throwaway.build("a", 2)  # takes the tag: `first` is now dangling
    assert first != second
    assert throwaway.held_ids(dangling=True) == {first}

    # Without -f: refused, nothing runs.
    unforced = _run_otto(
        "docker",
        "image",
        "prune",
        "--filter",
        throwaway.label,
        "--parent",
        throwaway.host,
        xdir=throwaway.xdir,
        env=_WIDE,
    )
    assert unforced.returncode == 2, unforced.stdout + unforced.stderr
    assert "needs -f" in _flat(unforced.stdout + unforced.stderr)
    assert throwaway.held_ids() == {first, second}

    out = _run_otto(
        "docker",
        "image",
        "prune",
        "-f",
        "--filter",
        throwaway.label,
        "--parent",
        throwaway.host,
        xdir=throwaway.xdir,
        env=_WIDE,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith(f"== {throwaway.host} ==\n"), out.stdout
    assert f"deleted: {first}" in out.stdout, out.stdout
    assert second not in out.stdout, out.stdout
    assert throwaway.held_ids() == {second}
