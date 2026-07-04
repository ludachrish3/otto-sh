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
from typing import NamedTuple

import pytest

from tests._fixtures._host_pool import UNIX_POOL, lease_unix_host
from tests.e2e._otto_subprocess import (
    COVERAGE_BOOTSTRAP,
    COVERAGERC,
    OTTO_BIN,
    PROJECT_ROOT,
    REPO1,
    assert_output_dir,
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
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Start ``otto --lab <lab> monitor <argv>`` as a non-blocking Popen.

    Mirrors the subprocess-coverage env from ``_run_otto`` in the docker e2e
    test so monitor subprocess runs contribute to the combined coverage report.
    ``extra_env`` overlays additional variables (e.g. an init-module toggle
    like ``OTTO_E2E_UPTIME_HOST``) onto the subprocess environment.
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
    if extra_env:
        env.update(extra_env)

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


def _tail(text: str, n: int = 20) -> str:
    """Return the last *n* lines of *text*, for compact failure diagnostics."""
    return "\n".join(text.splitlines()[-n:])


class _MonitorRunResult(NamedTuple):
    """Outcome of one :func:`_run_monitor_briefly` run."""

    returncode: int
    """Exit code (or negative signal number) reported by the subprocess."""

    stdout: str
    """Captured stdout, for diagnostics on assertion failure."""

    stderr: str
    """Captured stderr, for diagnostics on assertion failure."""

    rows_found: bool
    """Whether ``metrics`` had any rows by the time polling stopped."""


def _run_monitor_briefly(
    host: str,
    db_path: Path,
    extra_env: dict[str, str] | None = None,
) -> _MonitorRunResult:
    """Start, tick, and cleanly stop ``otto monitor`` against *host*.

    Shared choreography for the monitor e2e tests: start the subprocess with
    ``--hosts <host> --interval 1 --db <db_path>``, poll the DB for up to 6 s
    waiting for the first collection tick, send SIGINT, and wait up to 30 s
    for a clean exit — failing loudly (never skipping) if the live-bed
    process wedges. Returns the process's exit status and captured output so
    callers apply their own assertions.
    """
    proc = _start_monitor(
        [
            "--hosts",
            host,
            "--interval",
            "1",
            "--db",
            str(db_path),
        ],
        xdir=db_path.parent,
        extra_env=extra_env,
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
            f"host={host!r}, db={db_path}\n"
            f"stdout:\n{stdout_buf}\nstderr:\n{stderr_buf}"
        )

    # Capture remaining output for diagnostics.
    try:
        stdout_out, stderr_out = proc.communicate()
    except ValueError:
        # communicate() called after wait() — stdout/stderr already consumed.
        stdout_out = ""
        stderr_out = ""

    return _MonitorRunResult(
        returncode=returncode,
        stdout=stdout_out,
        stderr=stderr_out,
        rows_found=rows_found,
    )


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

    result = _run_monitor_briefly(element, db_path, extra_env=None)

    # ── Exit code ───────────────────────────────────────────────────────────
    # SIGINT handling by otto monitor / typer / asyncio produces one of:
    #   0    — clean asyncio cancellation path exits 0 in some Python builds
    #  -2    — process killed by SIGINT (Linux: Python subprocess reports -signal)
    #  130   — shell convention: 128 + SIGINT (2); typer's default when asyncio
    #           receives KeyboardInterrupt and sys.exit(130) is called
    # Any other code is an unexpected failure.
    _sigint_negative = -int(signal.SIGINT)  # -2
    _sigint_shell = 128 + int(signal.SIGINT)  # 130
    assert result.returncode in (0, _sigint_negative, _sigint_shell), (
        f"otto monitor exited with unexpected returncode {result.returncode}\n"
        f"(expected 0, {_sigint_negative}, or {_sigint_shell} for SIGINT)\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # ── Row count ───────────────────────────────────────────────────────────
    assert db_path.exists(), (
        f"DB file was not created at {db_path}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        all_rows = conn.execute("SELECT host, label, value FROM metrics").fetchall()
    finally:
        conn.close()

    assert all_rows, (
        f"metrics table has no rows after monitor ran for up to 6 s against host {element!r}.\n"
        f"rows_found_during_poll={result.rows_found}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Assert at least one row's host column contains the element name
    # (e.g. "carrot_seed" contains "carrot").
    matching = [r for r in all_rows if element in r[0]]
    assert matching, (
        f"No metrics row has host containing {element!r}.\n"
        f"Rows found: {all_rows[:10]!r}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # monitor collects host metrics → output dir created
    assert_output_dir(tmp_path, "monitor")


def _uptime_rows(db_path: Path, host: str) -> int:
    """Count persisted Uptime points for *host* (schema per ``_has_metric_rows``).

    Matches ``host`` by substring rather than equality, mirroring
    ``test_monitor_collects_and_persists``'s own ``element in r[0]`` check:
    the ``metrics.host`` column holds ``RemoteHost.name`` — the
    auto-generated, space-joined *name* (e.g. ``"carrot seed"``), which is
    neither the leased pool element (``"carrot"``) nor the underscore-joined
    ``.id`` (``"carrot_seed"``) used to key parser registration — but all
    three share the element as a substring.
    """
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE host LIKE ? AND label = 'Uptime'",
            (f"%{host}%",),
        ).fetchone()[0]


def test_per_host_parser_scoping_via_init_module(monitor_host: str, tmp_path: Path) -> None:
    """UptimeParser registered (via repo1's init module) for the leased host id
    produces Uptime rows; registered for a non-existent id, the same host gets
    only defaults. Two runs = both halves of the scoping guarantee through the
    real subprocess path (settings.toml init -> registry -> collector -> DB).
    """
    # repo1_monitor_uptime.py's register_host_parsers() call keys on the exact
    # host *id* (otto/monitor/factory.py: get_host_parsers(host.id)), not the
    # short pool element --hosts matches by regex. The tech1 lab fixture (see
    # tests/_fixtures/lab_data/tech1/hosts.json) gives every veggies-pool host
    # board="seed", so RemoteHost._generate_id() composes "<element>_seed" —
    # e.g. "carrot_seed" for the leased element "carrot" (verified against
    # UnixHost directly; also assumed elsewhere, e.g.
    # tests/e2e/configmodule/test_completion_cache.py's host-id tuple).
    host_id = f"{monitor_host}_seed"

    # Run 1: registration targets the leased host -> Uptime present.
    # Same Popen->ticks->SIGINT choreography as the existing test.
    db_registered = tmp_path / "registered.db"
    result_registered = _run_monitor_briefly(
        monitor_host, db_registered, extra_env={"OTTO_E2E_UPTIME_HOST": host_id}
    )
    assert _uptime_rows(db_registered, monitor_host) > 0, (
        f"host {monitor_host} registered UptimeParser but produced no Uptime rows\n"
        f"rows_found_during_poll={result_registered.rows_found}\n"
        f"stdout(tail):\n{_tail(result_registered.stdout)}\n"
        f"stderr(tail):\n{_tail(result_registered.stderr)}"
    )

    # Run 2: registration targets a host id that matches nothing -> no Uptime,
    # defaults intact.
    db_unregistered = tmp_path / "unregistered.db"
    result_unregistered = _run_monitor_briefly(monitor_host, db_unregistered, extra_env=None)
    assert _uptime_rows(db_unregistered, monitor_host) == 0, (
        "unregistered host must NOT get the custom parser\n"
        f"rows_found_during_poll={result_unregistered.rows_found}\n"
        f"stdout(tail):\n{_tail(result_unregistered.stdout)}\n"
        f"stderr(tail):\n{_tail(result_unregistered.stderr)}"
    )
    assert _has_metric_rows(db_unregistered), (
        "default parsers must still produce rows\n"
        f"rows_found_during_poll={result_unregistered.rows_found}\n"
        f"stdout(tail):\n{_tail(result_unregistered.stdout)}\n"
        f"stderr(tail):\n{_tail(result_unregistered.stderr)}"
    )
