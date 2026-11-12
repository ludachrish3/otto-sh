"""The coverage counter reset and the cleanup sweep under every BusyBox row (#842, #777).

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

import os
import subprocess
import time
from pathlib import Path

import pytest

from otto.host import cov_sweep
from otto.host.product import (
    GcdaFindError,
    classify_gcda_check,
    classify_gcda_listing,
    gcda_check_cmd,
    gcda_delete_cmd,
    gcda_list_cmd,
)
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
    # The dir it emptied goes (#722); the one holding a user file, and the
    # cov_dir holding it, stay.
    assert sorted(str(p.relative_to(cov)) for p in cov.rglob("*")) == ["sub", "sub/keep.txt"]


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_reset_removes_the_cov_dir_it_emptied_and_nothing_above(
    sweep_release, applet_dir, tmp_path
):
    # The quoting is load-bearing: an unquoted `rmdir c[x]*` would also take
    # the empty sibling `cxy`.
    above = tmp_path / "above"
    cov = above / "c[x]*"
    (cov / "sub").mkdir(parents=True)
    (cov / "sub" / "a.gcda").write_bytes(b"")
    (above / "cxy").mkdir()
    link = tmp_path / "link"
    (tmp_path / "target").mkdir()
    link.symlink_to(tmp_path / "target")
    d = applet_dir(sweep_release, SWEEP_APPLETS)

    done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(cov)))
    assert done.returncode == 0, f"rc {done.returncode}: {_first_line(done)!r}"
    assert sorted(p.name for p in above.iterdir()) == ["cxy"]
    # The check's guard answers the removed cov_dir, whatever its name.
    assert _check(sweep_release, d, str(cov)) == "absent"
    for name in ("caf\u00e9", "t\tab"):
        named = above / name
        named.mkdir()
        (named / "a.gcda").write_bytes(b"")
        done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(named)))
        assert done.returncode == 0, f"rc {done.returncode}: {_first_line(done)!r}"
        assert not named.exists()
        assert _check(sweep_release, d, str(named)) == "absent"

    # A cov_dir that is a symlink: rmdir refuses it, and never follows it.
    done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(link)))
    assert done.returncode == 0, f"rc {done.returncode}: {_first_line(done)!r}"
    assert link.is_symlink()
    assert (tmp_path / "target").is_dir()


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_reset_never_walks_a_counter_path_holding_a_newline(
    sweep_release, applet_dir, tmp_path
):
    # Every row's find must honour `! -path '*<newline>*'` (a literal newline:
    # 1.16.1 and 1.21.1 accept `[[:cntrl:]]` and never match it): a forged
    # second line through a symlink would otherwise rmdir a dir outside cov_dir.
    cov = tmp_path / "cov"
    outside = tmp_path / "outside"
    (outside / "victim").mkdir(parents=True)
    cov.mkdir()
    (cov / "link").symlink_to(outside)
    forged = Path(f"{cov}/x\n{cov}/link/victim/z.gcda")
    forged.parent.mkdir(parents=True)
    forged.write_bytes(b"")
    (cov / "plain").mkdir()
    (cov / "plain" / "a.gcda").write_bytes(b"")
    d = applet_dir(sweep_release, SWEEP_APPLETS)

    done = run_in_busybox_sh(sweep_release, d, gcda_delete_cmd(str(cov)))

    assert done.returncode == 0, f"rc {done.returncode}: {_first_line(done)!r}"
    assert not forged.exists()
    assert (outside / "victim").is_dir()
    assert not (cov / "plain").exists()  # an ordinary counter's dir still goes


def _check(release, applets: Path, cov_dir: str):
    """Run the reset's check under *release* and classify it as otto does."""
    done = run_in_busybox_sh(release, applets, gcda_check_cmd(cov_dir))
    status = Status.Success if done.returncode == 0 else Status.Failed
    cmd = CommandResult(status=status, value=done.stdout, command="find", retcode=done.returncode)
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

    # An absent cov_dir, however its name must be quoted, reads as absent:
    # from the check's guard, and from find's own line, BusyBox's leg of the
    # `enoent_for_start` rendering pin (a dir that vanished between the two).
    for name in ("plain", "with space", "it's gone", "caf\u00e9", "t\tab"):
        absent = str(tmp_path / "absent" / name)
        assert _check(sweep_release, d, absent) == "absent", (sweep_release.version, name)
        done = run_in_busybox_sh(sweep_release, d, f"{gcda_list_cmd(absent)} 2>&1")
        lines = [line for line in done.stdout.splitlines() if line]
        answer = classify_gcda_listing(absent, lines, done.returncode)
        assert answer == "absent", (sweep_release.version, name, done.stdout)


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
    # Every dir the removal emptied went too (#722), the cov_dir included.
    assert not cov.exists()


# ── the cleanup sweep (#777): the probe and the removal scripts, verbatim ─────


def _probe(release, applets: Path, dirs: list[str]):
    """Run the sweep's probe script under *release*; parse it as otto does."""
    done = run_in_busybox_sh(release, applets, cov_sweep._probe_script(dirs))
    return cov_sweep._parse_probe(done.stdout, dirs, done.returncode)


def _remove(release, applets: Path, dirs: list[str]):
    """Run the sweep's removal script under *release*; parse it as otto does."""
    done = run_in_busybox_sh(release, applets, cov_sweep._remove_script(dirs))
    return cov_sweep._parse_removal(done.stdout, dirs, done.returncode)


@pytest.mark.busybox
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_sweep_removes_counters_and_emptied_dirs_and_nothing_else(
    sweep_release, applet_dir, tmp_path
):
    only = tmp_path / "only counters"
    (only / "sub").mkdir(parents=True)
    (only / "a.gcda").write_bytes(b"")
    (only / "sub" / "b.gcda").write_bytes(b"")
    user = _coverage_tree(tmp_path)
    (user / "emptied").mkdir()
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    (healthy / "notes.txt").write_text("user data")
    absent = tmp_path / "absent" / "it's gone"
    dirs = [str(only), str(user), str(healthy), str(absent)]
    d = applet_dir(sweep_release, SWEEP_APPLETS)

    before = _probe(sweep_release, d, dirs)
    assert sorted(before[0]) == [str(only / "a.gcda"), str(only / "sub" / "b.gcda")]
    assert set(before[1]) == {
        str(user / "a.gcda"),
        str(user / "sub" / "b c.gcda"),
        str(user / "sub" / "deeper" / "d.gcda"),
        str(user / "emptied"),
    }
    # The empty-dir probe is silent on a healthy non-empty dir, and BusyBox's
    # ENOENT rendering parses as absent.
    assert before[2:] == [[], "absent"]

    assert _remove(sweep_release, d, dirs) == [None, None, None, None]

    assert not only.exists()
    assert (user / "sub" / "keep.txt").read_text() == "user data"
    assert _gcda_left(user) == []
    assert (healthy / "notes.txt").exists()
    assert _probe(sweep_release, d, dirs) == ["absent", [], [], "absent"]


@pytest.mark.busybox
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 dir")
@pytest.mark.parametrize("sweep_release", _SWEEP_ROWS, indirect=True)
def test_the_sweep_probe_reports_an_unreadable_dir(sweep_release, applet_dir, tmp_path):
    cov = tmp_path / "cov"
    (cov / "locked").mkdir(parents=True)
    (cov / "locked").chmod(0)
    d = applet_dir(sweep_release, SWEEP_APPLETS)
    try:
        [answer] = _probe(sweep_release, d, [str(cov)])
    finally:
        (cov / "locked").chmod(0o755)
    # BusyBox's find never runs -exec on a dir it cannot read under -depth,
    # so no `otto-ls-failed` line appears (GNU's does, tests/unit/host); its
    # own diagnostic and status make the answer an error all the same.
    assert isinstance(answer, GcdaFindError), answer
    assert str(cov / "locked") in answer.detail
