"""End-to-end tests for the ``otto monitor`` CLI split (spec 2026-07-12).

``otto monitor`` is a single command with two mutually-exclusive entry
points: ``--live`` (collect from lab hosts, explicit opt-in) and a
``<source>`` positional (review a saved format:1 export). These tests drive
that dispatch/validation surface through Typer's ``CliRunner`` — no uvicorn
server is ever started here (the Playwright task covers serving behavior),
and ``--live`` collection itself is never reached, so no real lab or hosts
are touched, matching this directory's hostless CLI-shape testing pattern.

What the leaf hands the monitor library, and how it reports the library's
refusals, is pinned by ``tests/unit/cli/test_monitor_differential.py``. The
shape-only tests at the top (bare invocation, mutual exclusion, a missing
file) are refused before the command body runs, so they invoke the bare
``monitor_app``. Everything that reaches the body dispatches through the FULL
``otto.cli.main.app``: that is the only path that runs the real
``CommandSpec``/``command_preamble`` dispatch machinery, which the body's
per-mode preamble (``ensure_cli_session`` for review) relies on, and where
the lab-requirement bug the tests near the bottom guard against actually
lived.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.monitor import monitor_app
from otto.monitor.db import MetricDB
from otto.monitor.session import new_frame

pytestmark = pytest.mark.hostless

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse rich's wrapped error panel so a message can be matched as one string."""
    return " ".join(text.replace("│", " ").split())


def _review(tmp_path: Path, source: Path):
    """Run ``otto monitor <source>`` through the full app, with no lab configured."""
    from otto.cli.main import app

    return runner.invoke(
        app,
        ["monitor", str(source)],
        env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
    )


# ── 1. Bare invocation: usage, exit 2 ────────────────────────────────────────


def test_bare_monitor_prints_usage_exit_2() -> None:
    """Neither ``--live`` nor a ``<source>``: usage help, exit 2, names both."""
    result = runner.invoke(monitor_app, [])

    assert result.exit_code == 2
    assert "--live" in result.output
    assert "source" in result.output.lower()


# ── 2. --live and <source> are mutually exclusive ───────────────────────────


def test_live_and_source_mutually_exclusive(tmp_path: Path) -> None:
    db_file = tmp_path / "x.db"
    db_file.write_bytes(b"")  # only needs to exist — Typer's exists=True gate

    result = runner.invoke(monitor_app, ["--live", str(db_file)])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


# ── 3. Unknown source suffix ─────────────────────────────────────────────────
#
# A source that is not a loadable export is a bad argument, so it is a usage
# error naming SOURCE: exit 2, like every other refused input.


def test_source_rejects_unknown_suffix(tmp_path: Path) -> None:
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("not a monitor export")

    result = _review(tmp_path, csv_file)

    assert result.exit_code == 2, result.output
    flat = _flat(result.output)
    assert "Invalid value for SOURCE" in flat
    assert ".json" in flat
    assert ".db" in flat


# ── 4. Legacy (pre-format:1) JSON is rejected ────────────────────────────────


def test_source_rejects_legacy_json(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"metrics": [], "events": []}))

    result = _review(tmp_path, legacy)

    assert result.exit_code == 2, result.output
    assert "format" in result.output.lower()


# ── 4b. Corrupted / non-SQLite .db file ──────────────────────────────────────
#
# Reproduces the hand-carried-archive path the guide advertises (a truncated
# `scp` copy): sqlite3.connect() is lazy, so a garbage-bytes file only fails
# on the first PRAGMA, raising sqlite3.DatabaseError — the PARENT class of
# OperationalError. Must surface as otto's own fail-loud message, exit 2, and
# critically NO raw traceback on stderr/stdout.


def test_source_rejects_corrupted_db_no_traceback(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database at all, just garbage bytes")

    result = _review(tmp_path, garbage)

    assert result.exit_code == 2, result.output
    assert "not a monitor database" in _flat(result.output)
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ── 5. Missing source file ───────────────────────────────────────────────────


def test_source_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"

    result = runner.invoke(monitor_app, [str(missing)])

    # Typer's own `exists=True` on the positional rejects this during
    # argument parsing, before monitor()'s body ever runs.
    assert result.exit_code != 0


# ── 6. Lab requirement: optional for review, mandatory for --live ───────────
#
# Regression coverage for the live-bed-caught bug: monitor's spec set
# gate=False but not lab_free=True, so the shared root preamble still
# hard-required --lab even for review mode, which never loads a lab at all.
# `otto monitor <source>` — the exact command docs/cli/monitor/review.md
# documents — failed with "Error: Missing option '--lab'" even against a
# fully self-contained archive. See builtin_commands.py's monitor
# registration (lab_free=True) and monitor.py's --live branch (which pulls
# the lab in itself via otto.cli.invoke.ensure_lab_session).


def test_review_mode_reaches_server_without_lab(tmp_path: Path) -> None:
    """``otto monitor <source>`` with no ``--lab`` and no lab configured must succeed.

    Builds a real (empty) schema-v2 ``.db`` with a real ``MetricDB`` (not
    hand-crafted SQLite), then dispatches through the full app with
    ``OTTO_LAB`` cleared and no ``--lab`` anywhere on the command line — the
    exact scenario the live-bed report reproduced. Only
    ``MonitorServer.serve()`` is stubbed (an ``AsyncMock``, so the real
    ``serve_review`` coroutine runs to the point of constructing the server,
    then returns immediately instead of starting a real uvicorn server) —
    everything up to and including server construction runs for real, so this
    fails RED against the pre-fix code (exit 2, "Missing option '--lab'") and
    passes GREEN after it.
    """
    db_file = tmp_path / "metrics.db"

    async def _seed() -> None:
        db = MetricDB(str(db_file), new_frame(label=None, note=None), "{}", "{}")
        await db.open()
        await db.close()

    asyncio.run(_seed())

    mock_server = MagicMock()
    mock_server.serve = AsyncMock()

    with patch("otto.monitor.server.MonitorServer", return_value=mock_server) as mock_cls:
        result = _review(tmp_path, db_file)

    assert result.exit_code == 0, result.output
    mock_cls.assert_called_once()
    mock_server.serve.assert_awaited_once()


def test_live_without_lab_reports_missing_option(tmp_path: Path) -> None:
    """``otto monitor --live`` with no lab must still fail loud, naming ``--lab``.

    Making review mode lab-free must not silently make ``--live`` lab-free
    too: live collection touches real hosts, so it still needs a lab. Drives
    the full app (see ``test_review_mode_reaches_server_without_lab`` above)
    so the real ``command_preamble``/``CommandSpec`` dispatch — where
    ``--live``'s own lab pull now lives — is exercised end to end.
    """
    from otto.cli.main import app

    result = runner.invoke(
        app,
        ["monitor", "--live"],
        env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
    )

    assert result.exit_code != 0
    assert "Missing option '--lab'" in result.stderr


# ── 7. Review mode prints the server URL to the console ─────────────────────
#
# Regression coverage for the live-bed-caught bug: `lab_free=True` (added to
# fix #6 above) makes `command_preamble` early-return entirely for BOTH of
# monitor's branches, skipping `ensure_cli_session` (`init_cli_logging`)
# for review mode too — not just the lab load. With no handler attached to the
# `'otto'` logger, every `MonitorServer.serve()` record vanished into Python's
# `lastResort` handler (WARNING+ only): review mode's logged output was lost.
# Fixed by having monitor's review branch call `ensure_cli_session` itself, the
# same way its `--live` branch calls `ensure_lab_session` for the lab piece.
# (The keyed URL itself now prints via CONSOLE — terminal only, key kept out of
# the log files — so the guard rests on the keyless `Monitor dashboard started`
# line, which travels through the logging tree.) Since root capture (spec
# 2026-08-30 §3.1) the root callback raises the console handler, so review
# mode's records would survive even without that call; what this asserts is
# the user-facing guarantee — review mode's log trail reaches the console —
# rather than which install delivers it.


def test_review_mode_logs_server_url_to_console(
    tmp_path: Path, hermetic_monitor_dist: Path
) -> None:
    """``otto monitor <source>`` (review) must print the server URL to the console.

    Real end-to-end: dispatches through the full ``otto.cli.main.app`` (the
    real ``CommandSpec``/``command_preamble`` dispatch) against a real
    schema-v2 ``.db`` export, and lets the real ``MonitorServer.serve()``
    method run unmodified. serve() prints the keyed URL straight to the
    terminal via ``CONSOLE`` (so the access key never reaches the log files)
    and logs a keyless ``Monitor dashboard started`` line through the ``'otto'``
    logger — the latter is what needs a console handler to have been installed,
    so this test asserts on both. The only stub is uvicorn's own
    internal socket/request loop (``uvicorn.Server.serve``): replaced with a
    fake that flips ``started`` and fabricates a bound socket, so no real TCP
    listener opens and the invocation returns promptly (matching this
    module's "no uvicorn server is ever started here" design), plus the
    ``hermetic_monitor_dist`` stand-in the server's ``make web`` fail-fast
    demands (pytest never builds the dist; CI runs without one). This fails
    RED against the pre-fix code (no "Server running at" anywhere in
    output — review mode printed nothing) and passes GREEN once review mode
    calls ``ensure_cli_session``.
    """
    del hermetic_monitor_dist
    db_file = tmp_path / "metrics.db"

    async def _seed() -> None:
        db = MetricDB(str(db_file), new_frame(label=None, note=None), "{}", "{}")
        await db.open()
        await db.close()

    asyncio.run(_seed())

    import uvicorn

    class _FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("0.0.0.0", 54321)

    class _FakeUvicornServer:
        def __init__(self) -> None:
            self.sockets = [_FakeSocket()]

    async def _fake_uvicorn_serve(self, sockets: object = None) -> None:
        # Mirrors just enough of uvicorn.Server._serve()'s post-startup state
        # (self.started + self.servers) for MonitorServer.serve() to extract
        # a port and log — without ever binding a real socket.
        self.started = True
        self.servers = [_FakeUvicornServer()]

    with patch.object(uvicorn.Server, "serve", _fake_uvicorn_serve):
        result = _review(tmp_path, db_file)

    assert result.exit_code == 0, result.output
    # The keyed URL reaches the console via CONSOLE.print (terminal only)...
    assert "Server running at" in result.output, result.output
    # ...and the keyless audit line reaches it via the 'otto' logger, which is
    # live only because review mode called ensure_cli_session (the guarded bug).
    assert "Monitor dashboard started on" in result.output, result.output
