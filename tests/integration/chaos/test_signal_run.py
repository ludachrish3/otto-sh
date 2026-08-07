"""Real SIGINT/SIGTERM against a mid-flight ``otto … run`` (chaos spec, Tier 2).

Phase gating: the ``@<host>   | <cmd>`` INFO line in verbose.log marks
"command running" (the one reliable INFO-level marker; connection lines
are DEBUG — the driver passes --log-level DEBUG anyway); the stderr
banner marks "teardown running". Remote hygiene is asserted only on
graceful paths — a forced teardown abandons the sweep by design, and its
leftovers are tier-3 recovery material.

The pgrep pattern brackets its first character (``[s]leep``) so the probe
shell's own command line never matches itself.
"""

import os
import re
import signal
import time

import pytest

from tests._fixtures.bed_hygiene import argv_pattern

from ._driver import BANNER, spawn_otto
from ._target import probe

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]

_MARKER_TIMEOUT = 60.0
_EXIT_TIMEOUT = 30.0


def _sleep_cmd(tag: str) -> str:
    """A unique, greppable long command: per-test tag + parent pid uniquify."""
    return f"sleep 3{tag}.{os.getpid() % 100000}"


def _remote_has(target, cmd: str) -> bool:
    status, _ = probe(target, f"pgrep -f '{argv_pattern(cmd)}'")
    return status == 0


def _wait_remote_reaped(target, cmd: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _remote_has(target, cmd):
            return
        time.sleep(0.2)
    raise AssertionError(f"remote command survived teardown: {cmd!r}")


def _wait_remote_running(target, cmd: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _remote_has(target, cmd):
            return
        time.sleep(0.2)
    raise AssertionError(f"remote command never appeared: {cmd!r}")


def _interrupt_mid_run(chaos_target, tmp_path, *, tag: str, sig: int, expected_rc: int) -> None:
    cmd = _sleep_cmd(tag)
    p = spawn_otto(["host", chaos_target.host_id, "run", cmd], xdir=tmp_path, target=chaos_target)
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)  # phase: command running
    _wait_remote_running(chaos_target, cmd)
    p.signal(sig)
    p.wait_for_stderr(BANNER, timeout=15)  # phase: teardown running
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc == expected_rc, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()
    _wait_remote_reaped(chaos_target, cmd)


def test_sigint_mid_run_cleans_up_and_exits_130(chaos_target, tmp_path) -> None:
    _interrupt_mid_run(chaos_target, tmp_path, tag="01", sig=signal.SIGINT, expected_rc=130)


def test_sigterm_mid_run_cleans_up_and_exits_143(chaos_target, tmp_path) -> None:
    _interrupt_mid_run(chaos_target, tmp_path, tag="02", sig=signal.SIGTERM, expected_rc=143)


def _forced_mid_run(chaos_target, tmp_path, *, tag: str, sig: int, expected_rc: int) -> None:
    """Deadline 0 ⇒ teardown always loses the race ⇒ deterministic force path.

    No remote assertion: the force path abandons the sweep by design.
    """
    cmd = _sleep_cmd(tag)
    p = spawn_otto(
        ["host", chaos_target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "0"},
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)
    p.signal(sig)
    p.wait_for_stderr(BANNER, timeout=15)
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc == expected_rc, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()


def test_forced_sigint_exits_130(chaos_target, tmp_path) -> None:
    _forced_mid_run(chaos_target, tmp_path, tag="03", sig=signal.SIGINT, expected_rc=130)


def test_forced_sigterm_exits_143(chaos_target, tmp_path) -> None:
    _forced_mid_run(chaos_target, tmp_path, tag="04", sig=signal.SIGTERM, expected_rc=143)


def test_second_signal_still_exits_promptly(chaos_target, tmp_path) -> None:
    """Double-signal smoke: banner-gated second SIGINT; prompt exit, no wedge.

    'Forced' means teardown lost the race, not signal count (plan 1). Three
    outcomes are physically possible for the second signal, and the first
    two exit 130: it lands during teardown (forces it), or after teardown
    won but before handler removal (idempotent ``_force.set()``). The third
    is unavoidable: on a loopback target teardown takes milliseconds, so
    under load the second signal can land AFTER ``_main`` removed its
    handlers — the process dies to the OS default disposition, exit ``-2``
    (the documented third-signal window, reached at signal #2). The product
    cannot atomically exit-and-keep-handlers; the contract this test pins
    is "prompt exit, never a wedge, 130 or signal-death — never 143, 1, or
    a hang". Discovered as a 2/2 load repro during Task 5's gate runs.
    """
    cmd = _sleep_cmd("05")
    p = spawn_otto(
        ["host", chaos_target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "600"},
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)
    p.signal(signal.SIGINT)
    p.wait_for_stderr(BANNER, timeout=15)
    p.signal(signal.SIGINT)
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc in (130, -int(signal.SIGINT)), f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()
