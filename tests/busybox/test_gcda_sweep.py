"""The coverage counter reset under every BusyBox row (#842).

otto's counter reset (``otto.host.product.gcda_delete_cmd``) runs on whatever
userland the device has. ``find -delete`` is a GNU/BusyBox extension that a
BusyBox build may compile out: the distro's own BusyBox 1.36.1 (the ``system``
row) lacks it, so a reset spelled with it fails there and leaves every
counter behind. These tests run the reset's real command line under each
row's own ``busybox sh``, with PATH holding only that row's applets (see
``tests/busybox/_rows.py``'s ``run_in_busybox_sh``), and judge it by the
filesystem afterwards, never by the exit status alone.

DRIVEN FROM ``busybox sh``, unlike ``test_applet_contracts.py``: these tests
ask what a BusyBox userland does with otto's command, not how one applet
parses its argv, and every name the script runs resolves to a BusyBox applet
either way.

The ``system`` row is never skipped. It is the one row measured to lack
``-delete``, so it is #842's RED; a host without ``busybox`` on PATH fails it
by name, at setup of a selected test (``sweep_release``), so deselecting it or
collecting this module elsewhere stays clean.
"""

import subprocess
import time
from pathlib import Path

import pytest

from otto.host.product import classify_gcda_check, gcda_check_cmd, gcda_delete_cmd
from otto.result import CommandResult
from otto.utils import Status
from tests.busybox._rows import ARTIFACT_ROWS, SWEEP_APPLETS, SYSTEM_BUILD, run_in_busybox_sh

_SWEEP_ROWS = [*ARTIFACT_ROWS, pytest.param(SYSTEM_BUILD, id="system")]
"""Every row, the ``system`` one unmarked: its absence fails in ``sweep_release``."""

_SYSTEM_ONLY = [row for row in _SWEEP_ROWS if row.id == "system"]


@pytest.fixture
def sweep_release(request):
    """The row under test; a missing system ``busybox`` fails the selected test by name."""
    release = request.param
    if release is None:
        pytest.fail(
            "no `busybox` on PATH: install busybox-static; this row is #842's RED "
            "(BusyBox 1.36.1 lacks find -delete), so it is never skipped"
        )
    return release


def _gcda_left(root: Path) -> list[Path]:
    """Every ``.gcda`` regular file under *root*, walked from Python."""
    return sorted(p for p in root.rglob("*.gcda") if p.is_file())


def _first_line(done: subprocess.CompletedProcess) -> str:
    """The diagnostic's first line: BusyBox follows a rejected primary with its whole usage."""
    text = (done.stderr or done.stdout).strip()
    return text.splitlines()[0] if text else ""


def _coverage_tree(root: Path) -> Path:
    """A cov_dir with nested counters, a name with a space, and a user file."""
    cov = root / "my cov"  # a space: the quoting is load-bearing
    (cov / "sub" / "deeper").mkdir(parents=True)
    (cov / "a.gcda").write_bytes(b"")
    (cov / "sub" / "b c.gcda").write_bytes(b"")
    (cov / "sub" / "deeper" / "d.gcda").write_bytes(b"")
    (cov / "sub" / "keep.txt").write_text("user data")
    return cov


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_reset_removes_every_counter_without_find_delete(sweep_release, applet_dir, tmp_path):
    cov = _coverage_tree(tmp_path)
    assert len(_gcda_left(cov)) == 3  # premise: the tree holds counters to remove
    d = applet_dir(sweep_release, SWEEP_APPLETS)

    done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(cov)))

    rc = done.returncode
    assert rc == 0, (
        f"BusyBox {sweep_release.version} rejected the reset (rc {rc}): {_first_line(done)!r}"
    )
    assert _gcda_left(cov) == [], f"counters survived under BusyBox {sweep_release.version}"
    assert (cov / "sub" / "keep.txt").read_text() == "user data"


def _check(release, applets: Path, cov_dir: str):
    """Run the reset's check under *release* and classify it as otto does."""
    done = run_in_busybox_sh(release, applets, gcda_check_cmd(cov_dir))
    status = Status.Success if done.returncode == 0 else Status.Failed
    cmd = CommandResult(status, value=done.stdout, command="find", retcode=done.returncode)
    return classify_gcda_check(cov_dir, cmd)


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_reset_check_sees_what_the_removal_left(sweep_release, applet_dir, tmp_path):
    cov = _coverage_tree(tmp_path)
    d = applet_dir(sweep_release, SWEEP_APPLETS)
    assert run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(cov))).returncode == 0

    # Clean after the removal.
    assert _check(sweep_release, d, str(cov)) == []

    # A counter put back is seen, by its path.
    back = cov / "sub" / "back again.gcda"
    back.write_bytes(b"")
    assert _check(sweep_release, d, str(cov)) == [str(back)]

    # BusyBox's leg of the `enoent_for_start` rendering pin: an absent
    # cov_dir, however its name must be quoted, reads as absent.
    for name in ("plain", "with space", "it's gone"):
        absent = str(tmp_path / "absent" / name)
        assert _check(sweep_release, d, absent) == "absent", (sweep_release.version, name)


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SYSTEM_ONLY, indirect=True)
def test_the_reset_survives_more_paths_than_one_command_line_holds(
    sweep_release, applet_dir, tmp_path
):
    # BusyBox 1.36.1's `{} +` ignores ARG_MAX: past it, it passes a fraction of
    # the paths and still exits 0. `{} \;` cannot exceed the limit. 22,000
    # unique ~200-byte names is several times Linux's 2 MiB argument limit.
    cov = tmp_path / "cov"
    for sub in range(22):
        (cov / f"d{sub:02d}").mkdir(parents=True)
    for i in range(22_000):
        name = f"{i:05d}_" + "x" * 190 + ".gcda"
        (cov / f"d{i % 22:02d}" / name).write_bytes(b"")
    assert len(_gcda_left(cov)) == 22_000  # premise: no generator collision
    d = applet_dir(sweep_release, SWEEP_APPLETS)

    started = time.monotonic()
    done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(cov)), timeout=600)
    elapsed = time.monotonic() - started

    rc = done.returncode
    assert rc == 0, f"rc {rc}: {_first_line(done)!r}"
    left = _gcda_left(cov)
    assert left == [], f"{len(left)} of 22000 counters survived ({elapsed:.0f}s)"
