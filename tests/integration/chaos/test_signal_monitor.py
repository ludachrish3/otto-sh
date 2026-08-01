"""SIGTERM during ``otto monitor --live`` serve: otto's policy, not uvicorn's.

Before plan 3's ownership fix, uvicorn's ``capture_signals`` displaced the
lifecycle handlers for the whole serve window — no banner, uvicorn's own
two-stage policy, and a racy exit code from the post-drain signal
re-raise. This pins the uniform contract: banner + 143, same as every
other command.
"""

import signal

import pytest

from ._driver import BANNER, spawn_otto

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]


def _monitor_argv(chaos_target, db_path) -> "list[str]":
    return [
        "monitor",
        "--live",
        "--hosts",
        chaos_target.host_id,
        "--interval",
        "1",
        "--db",
        str(db_path),
    ]


def _sigterm_monitor(
    chaos_target, tmp_path, *, db_name: str, extra_env: "dict[str, str] | None"
) -> None:
    p = spawn_otto(
        _monitor_argv(chaos_target, tmp_path / db_name),
        xdir=tmp_path,
        target=chaos_target,
        extra_env=extra_env,
    )
    p.wait_for_log("Monitor dashboard started on", timeout=60)  # phase: serving
    p.signal(signal.SIGTERM)
    p.wait_for_stderr(BANNER, timeout=15)
    rc = p.wait(timeout=30)
    assert rc == 143, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()


def test_sigterm_during_monitor_serve_exits_143(chaos_target, tmp_path) -> None:
    _sigterm_monitor(chaos_target, tmp_path, db_name="chaos-monitor.db", extra_env=None)


def test_forced_sigterm_during_monitor_serve_still_exits_143(chaos_target, tmp_path) -> None:
    """Deadline 0 forces past the uvicorn drain.

    Task 2 shields the serving await but leaves the drain await bare
    precisely so the force path can abandon it — this is the end-to-end
    proof (Task 2's unit tests never issue a second cancellation). A hang
    here means the drain became unabandonable.
    """
    _sigterm_monitor(
        chaos_target,
        tmp_path,
        db_name="chaos-monitor-forced.db",
        extra_env={"OTTO_TEARDOWN_DEADLINE": "0"},
    )
