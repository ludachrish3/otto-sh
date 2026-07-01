"""End-to-end CLI test for `otto monitor` collect → store → read-back.

Drives the real ``otto monitor`` binary as a subprocess (Popen, not run, because
monitor runs until interrupted), lets the collector produce a few ticks against a
live Vagrant Unix host, sends SIGINT, waits for a clean exit, then reads the
SQLite DB to assert that metrics rows were persisted.

Requirements:
    vagrant up carrot (or tomato or pepper) — any one veggies-lab Unix host.

The test leases one host from the UNIX_POOL via the same fd-flock mechanism as
the docker e2e and transfer e2e tests, so it distributes across the lab without
contending with docker tests that also lease hosts.

Host-failure-mid-collection deferral note
-----------------------------------------
The spec asked for a stretch case: monitor targeting TWO hosts, stop one mid-run,
assert the other keeps producing rows.  This is deferred because:
  * ``otto monitor`` selects UnixHosts (shell metrics) — docker container hosts
    are not monitorable via the shell path.
  * Stopping a Vagrant VM mid-run is explicitly forbidden by the bed policy
    (never power/reboot real VMs).
  * There is no safe way to make a UnixHost temporarily unreachable without
    powering it off or stopping the VM.
The deferral would require a mock-unreachable host path or a container host that
exposes a shell — neither is in scope here.
"""

import contextlib
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from tests._fixtures._host_pool import UNIX_POOL, lease_unix_host
from tests.e2e._otto_subprocess import (
    COVERAGE_BOOTSTRAP,
    COVERAGERC,
    OTTO_BIN,
    PROJECT_ROOT,
    REPO1,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("monitor_e2e")]

# The monitor polls shell metrics from Unix hosts (carrot/tomato/pepper all work).
# We reuse the full UNIX_POOL since monitor doesn't need docker capability.
_MONITOR_POOL = UNIX_POOL


# ---------------------------------------------------------------------------
# Subprocess helper for otto monitor (Popen, not blocking run)
# ---------------------------------------------------------------------------


def _start_monitor(
    argv: list[str],
    *,
    lab: str = "veggies",
    xdir: Path,
    sut_dirs: Path = REPO1,
) -> subprocess.Popen[str]:
    """Start ``otto --lab <lab> monitor <argv>`` as a non-blocking Popen.

    Mirrors the subprocess-coverage env from ``_run_otto`` in the docker e2e
    test so monitor subprocess runs contribute to the combined coverage report.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(sut_dirs),
        "OTTO_XDIR": str(xdir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }

    cmd: list[str] = [str(OTTO_BIN), "--lab", lab, "monitor", *argv]
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
        # Isolate from the xdist worker's process group so that pytest's own
        # SIGINT (e.g. KeyboardInterrupt on Ctrl+C) does not propagate to the
        # monitor subprocess before we are ready to stop it ourselves.
        start_new_session=True,
    )


def _has_metric_rows(db_path: Path) -> bool:
    """Return True if the metrics table exists and has at least one row."""
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()
            return rows is not None and rows[0] > 0
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — DB file created but schema not written.
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def monitor_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one Unix host from the pool for the monitor test's duration.

    Yields the host's *element* name (e.g. ``"carrot"``).  The ``--hosts``
    regex passed to ``otto monitor`` matches this element against the host id
    (``carrot_seed``) via ``re.search``, so ``"carrot"`` is a valid regex that
    matches ``"carrot_seed"``.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _MONITOR_POOL) as element:
        yield element


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_monitor_collects_and_persists(monitor_host: str, tmp_path: Path) -> None:
    """otto monitor writes metrics rows to the SQLite DB for a live Unix host.

    Steps:
    1. Start ``otto monitor --hosts <element> --interval 1 --db <tmp/monitor.db>``.
    2. Poll the DB every 0.5 s until rows appear OR 6 s elapse.
    3. Send SIGINT; wait up to 30 s for a clean exit.
    4. Assert the process exited without SIGKILL (returncode 0 or -SIGINT/-2).
    5. Assert the ``metrics`` table has ≥1 row where the host matches the leased element.
    """
    db_path = tmp_path / "monitor.db"
    element = monitor_host  # e.g. "carrot" — matches "carrot_seed" via re.search

    proc = _start_monitor(
        [
            "--hosts",
            element,
            "--interval",
            "1",
            "--db",
            str(db_path),
        ],
        xdir=tmp_path,
    )

    # Poll for up to 6 s, checking every 0.5 s — give the first collection tick
    # time to complete (SSH connect + shell commands + DB write).
    deadline = time.monotonic() + 6.0
    rows_found = False
    while time.monotonic() < deadline:
        if _has_metric_rows(db_path):
            rows_found = True
            break
        # Also check whether the process has already exited unexpectedly.
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    # ── Stop the monitor ────────────────────────────────────────────────────
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)

    try:
        returncode = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        stdout_buf = ""
        stderr_buf = ""
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            stdout_buf, stderr_buf = proc.communicate(timeout=1)
        proc.kill()
        # Bounded wait after SIGKILL — never block the run indefinitely even if
        # the pipes somehow stay open (pipes were drained by communicate() above).
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        pytest.fail(
            f"otto monitor did not exit within 30 s after SIGINT — this is a real bug.\n"
            f"host={element!r}, db={db_path}\n"
            f"stdout:\n{stdout_buf}\nstderr:\n{stderr_buf}"
        )

    # Capture remaining output for diagnostics.
    try:
        stdout_out, stderr_out = proc.communicate()
    except ValueError:
        # communicate() called after wait() — stdout/stderr already consumed.
        stdout_out = ""
        stderr_out = ""

    # ── Exit code ───────────────────────────────────────────────────────────
    # SIGINT handling by otto monitor / typer / asyncio produces one of:
    #   0    — clean asyncio cancellation path exits 0 in some Python builds
    #  -2    — process killed by SIGINT (Linux: Python subprocess reports -signal)
    #  130   — shell convention: 128 + SIGINT (2); typer's default when asyncio
    #           receives KeyboardInterrupt and sys.exit(130) is called
    # Any other code is an unexpected failure.
    _sigint_negative = -int(signal.SIGINT)  # -2
    _sigint_shell = 128 + int(signal.SIGINT)  # 130
    assert returncode in (0, _sigint_negative, _sigint_shell), (
        f"otto monitor exited with unexpected returncode {returncode}\n"
        f"(expected 0, {_sigint_negative}, or {_sigint_shell} for SIGINT)\n"
        f"stdout:\n{stdout_out}\nstderr:\n{stderr_out}"
    )

    # ── Row count ───────────────────────────────────────────────────────────
    assert db_path.exists(), (
        f"DB file was not created at {db_path}\nstdout:\n{stdout_out}\nstderr:\n{stderr_out}"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        all_rows = conn.execute("SELECT host, label, value FROM metrics").fetchall()
    finally:
        conn.close()

    assert all_rows, (
        f"metrics table has no rows after monitor ran for up to 6 s against host {element!r}.\n"
        f"rows_found_during_poll={rows_found}\n"
        f"stdout:\n{stdout_out}\nstderr:\n{stderr_out}"
    )

    # Assert at least one row's host column contains the element name
    # (e.g. "carrot_seed" contains "carrot").
    matching = [r for r in all_rows if element in r[0]]
    assert matching, (
        f"No metrics row has host containing {element!r}.\n"
        f"Rows found: {all_rows[:10]!r}\n"
        f"stdout:\n{stdout_out}\nstderr:\n{stderr_out}"
    )
