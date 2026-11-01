"""The bed lease through real `make`: the convenience targets, nesting, competition, SIGKILL.

`tests/unit/scripts/test_bed_lease.py` pins the script; this module pins what the
Makefile does with it, with real `$$PPID`s and real process groups. Every run
points `BED_LEASE_ROOT` at a root of its own, so nothing here touches the cell's
lease, and a scratch makefile included beside the real one (`-f Makefile -f
extra.mk`) supplies lane-shaped targets that run no suite.
"""

import os
import signal
import subprocess
from pathlib import Path

import pytest

from otto.utils import wait_for
from scripts.process_group import group_alive, signal_group
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_DROPPED = ("MAKEFLAGS", "MFLAGS", "MAKELEVEL")


def _env(root: Path) -> "dict[str, str]":
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    env["BED_LEASE_ROOT"] = str(root)
    env["BED_LEASE_DRAIN"] = "1"
    return env


def _make(root: Path, *args: str, extra: "Path | None" = None, **kw):
    argv = ["make", "--no-print-directory"]
    if extra is not None:
        argv += ["-f", "Makefile", "-f", str(extra)]
    argv += list(args)
    return subprocess.run(
        argv,
        cwd=CHECKOUT_ROOT,
        env=_env(root),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        **kw,
    )


def _group_has(pgid: int, comm: str) -> bool:
    """Whether a process named *comm* is a member of group *pgid* right now."""
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)
        except OSError:
            continue
        if fields[0].endswith(f"({comm}") and int(fields[1].split()[2]) == pgid:
            return True
    return False


def _wait_for_member(pgid: int, comm: str) -> None:
    wait_for(
        lambda: _group_has(pgid, comm),
        30.0,
        interval=0.05,
        on_timeout=f"no {comm} joined group {pgid}",
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lease"


@pytest.fixture
def extra(tmp_path: Path) -> Path:
    """Lane-shaped targets over the real macros: a hold, and an outer/inner nesting."""
    status = tmp_path / "inner-status.txt"
    makefile = tmp_path / "extra.mk"
    makefile.write_text(
        "hold:\n"
        "\t$(call bed_acquire,hold)\n"
        "\t@sleep $(HOLD)\n"
        "\t$(call bed_release)\n"
        "\n"
        "outer:\n"
        "\t$(call bed_acquire,outer)\n"
        f"\t@setsid $(MAKE) --no-print-directory -f Makefile -f {makefile} inner\n"
        "\t$(call bed_release)\n"
        "\n"
        "inner:\n"
        "\t$(call bed_acquire,inner)\n"
        f"\t@$(BED_LEASE) status > {status}\n"
        "\t$(call bed_release)\n"
    )
    return makefile


@pytest.mark.timeout(300)
def test_bed_lease_status_and_release_from_a_shell(root):
    """`make bed-lease` holds for the shell that ran it; `bed-release` evicts and names it."""
    shell = subprocess.Popen(
        ["sh", "-c", 'make --no-print-directory bed-lease PURPOSE="by hand"; sleep 60'],
        cwd=CHECKOUT_ROOT,
        env=_env(root),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert shell.stdout is not None
        first = shell.stdout.readline()
        assert f"(pid {shell.pid})" in first, first
        status = _make(root, "bed-status")
        assert status.returncode == 0, status.stderr
        assert f"pid {shell.pid} (holder running)" in status.stdout, status.stdout
        assert "'by hand'" in status.stdout
        released = _make(root, "bed-release")
        assert released.returncode == 0, released.stderr
        assert "evicted" in released.stdout
        assert "'by hand'" in released.stdout
        assert "free" in _make(root, "bed-status").stdout
    finally:
        signal_group(shell.pid, signal.SIGKILL)
        shell.wait()
        if shell.stdout is not None:
            shell.stdout.close()


@pytest.mark.timeout(300)
def test_a_nested_make_re_enters_the_outer_hold(root, extra, tmp_path):
    """The gate-tip shape: the outer make holds, the inner (own session) registers its group."""
    done = _make(root, "outer", extra=extra, start_new_session=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("taken by") == 1, done.stdout
    assert "held by this run" in done.stdout, done.stdout
    status = (tmp_path / "inner-status.txt").read_text()
    assert "for 'outer'" in status, status
    assert status.count("group ") == 2, status
    assert "released by pid" in done.stdout
    assert "free" in _make(root, "bed-status").stdout


@pytest.mark.timeout(300)
def test_two_competing_lanes_the_second_fails_then_succeeds(root, extra):
    first = subprocess.Popen(
        ["make", "--no-print-directory", "-f", "Makefile", "-f", str(extra), "hold", "HOLD=60"],
        cwd=CHECKOUT_ROOT,
        env=_env(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert first.stdout is not None
        assert "taken by" in first.stdout.readline()
        second = _make(root, "hold", "HOLD=0", extra=extra, start_new_session=True)
        assert second.returncode == 2, second.stdout + second.stderr
        assert f"pid {first.pid}," in second.stdout, second.stdout
        assert "'hold'" in second.stdout
        assert "holder running" in second.stdout
        signal_group(first.pid, signal.SIGKILL)  # the first run ends (no release ran)
        first.wait(timeout=60)
        wait_for(lambda: not group_alive(first.pid), 10.0, on_timeout="the hold outlived its kill")
        third = _make(root, "hold", "HOLD=0", extra=extra, start_new_session=True)
        assert third.returncode == 0, third.stdout + third.stderr
        assert "taken over" in third.stdout, third.stdout
        assert "released by pid" in third.stdout
    finally:
        if first.poll() is None:
            signal_group(first.pid, signal.SIGKILL)
            first.wait()
        if first.stdout is not None:
            first.stdout.close()


@pytest.mark.timeout(300)
def test_a_killed_lane_keeps_the_bed_while_its_group_survives(root, extra):
    lane = subprocess.Popen(
        ["make", "--no-print-directory", "-f", "Makefile", "-f", str(extra), "hold", "HOLD=60"],
        cwd=CHECKOUT_ROOT,
        env=_env(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert lane.stdout is not None
        assert "taken by" in lane.stdout.readline()
        _wait_for_member(lane.pid, "sleep")  # the acquire said so before make forked it
        os.kill(lane.pid, signal.SIGKILL)
        lane.wait()
        assert group_alive(lane.pid), "the sleep survived its make"
        refused = _make(root, "hold", "HOLD=0", extra=extra, start_new_session=True)
        assert refused.returncode == 2, refused.stdout
        assert "holder pid gone, 1 group(s) still running" in refused.stdout, refused.stdout
        signal_group(lane.pid, signal.SIGKILL)
        wait_for(lambda: not group_alive(lane.pid), 10.0, on_timeout="the group outlived SIGKILL")
        taken = _make(root, "hold", "HOLD=0", extra=extra, start_new_session=True)
        assert taken.returncode == 0, taken.stdout + taken.stderr
        assert "taken over" in taken.stdout, taken.stdout
    finally:
        signal_group(lane.pid, signal.SIGKILL)
        if lane.stdout is not None:
            lane.stdout.close()
