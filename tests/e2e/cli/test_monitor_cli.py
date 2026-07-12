"""End-to-end tests for the ``otto monitor`` CLI split (spec 2026-07-12).

``otto monitor`` is a single command with two mutually-exclusive entry
points: ``--live`` (collect from lab hosts, explicit opt-in) and a
``<source>`` positional (review a saved format:1 export). These tests drive
that dispatch/validation surface through Typer's ``CliRunner`` — no uvicorn
server is ever started here (the Playwright task covers serving behavior),
and ``--live`` collection itself is mocked out so no real lab or hosts are
touched, matching this directory's hostless CLI-shape testing pattern.

Finer-grained option-parsing and factory/collector coverage lives in
``tests/unit/cli/test_monitor.py``. The lab-requirement tests near the
bottom of this file (``--lab`` optional for review, mandatory for
``--live``) dispatch through the FULL ``otto.cli.main.app`` rather than the
bare ``monitor_app`` used everywhere else here: that is the only path that
runs the real ``CommandSpec``/``command_preamble`` dispatch machinery, which
is where the bug they guard against actually lived (a bare ``monitor_app``
invocation bypasses ``ensure_lab_context`` entirely and cannot see it).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
import typer
from typer.testing import CliRunner

from otto.cli.monitor import monitor, monitor_app
from otto.monitor.db import MetricDB
from otto.monitor.session import new_frame
from otto.reservations import ReservationGateResult

pytestmark = pytest.mark.hostless

runner = CliRunner()


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


def test_source_rejects_unknown_suffix(tmp_path: Path) -> None:
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("not a monitor export")

    result = runner.invoke(monitor_app, [str(csv_file)])

    assert result.exit_code == 1
    assert ".json" in result.output
    assert ".db" in result.output


# ── 4. Legacy (pre-format:1) JSON is rejected ────────────────────────────────


def test_source_rejects_legacy_json(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"metrics": [], "events": []}))

    result = runner.invoke(monitor_app, [str(legacy)])

    assert result.exit_code == 1
    assert "format" in result.output.lower()


# ── 4b. Corrupted / non-SQLite .db file ──────────────────────────────────────
#
# Reproduces the hand-carried-archive path the guide advertises (a truncated
# `scp` copy): sqlite3.connect() is lazy, so a garbage-bytes file only fails
# on the first PRAGMA, raising sqlite3.DatabaseError — the PARENT class of
# OperationalError. Must surface as otto's own fail-loud message, exit 1, and
# critically NO raw traceback on stderr/stdout.


def test_source_rejects_corrupted_db_no_traceback(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database at all, just garbage bytes")

    result = runner.invoke(monitor_app, [str(garbage)])

    assert result.exit_code == 1
    assert "not a monitor database" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ── 5. Missing source file ───────────────────────────────────────────────────


def test_source_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"

    result = runner.invoke(monitor_app, [str(missing)])

    # Typer's own `exists=True` on the positional rejects this during
    # argument parsing, before monitor()'s body ever runs.
    assert result.exit_code != 0


# ── 6. --live gates before host selection ────────────────────────────────────


def _make_ctx(meta: dict) -> typer.Context:
    """Build a typer.Context (backed by click.Context) with the given meta.

    Mirrors the established fixture in tests/unit/cli/test_monitor.py /
    tests/unit/cli/test_preamble_reservation_gate.py: monitor() reads
    ctx.meta["otto_reservation"] and calls .evaluate() inline (there is no
    standalone gate(ctx) callable to patch), so exercising gate ORDER means
    calling monitor() directly with a hand-built context rather than going
    through CliRunner.
    """
    cmd = click.Command("monitor")
    ctx = click.Context(cmd)
    ctx.meta.update(meta)
    return ctx  # type: ignore[return-value]


def test_live_requires_reservation_gate_before_host_selection() -> None:
    """The reservation gate must be consulted BEFORE hosts are selected.

    Records the order two mocked calls happen in: the gate's .evaluate() and
    otto.cli.monitor.all_hosts(). No hosts match (all_hosts returns empty),
    so monitor() exits 1 right after selection — this test only cares about
    what happened, and in what order, up to that point.
    """
    order: list[str] = []

    mock_res = MagicMock()

    def _evaluate() -> ReservationGateResult:
        order.append("gate")
        return ReservationGateResult(checked=True, skipped=False, warning=None)

    mock_res.evaluate.side_effect = _evaluate

    def _fake_all_hosts(*_args, **_kwargs):
        order.append("hosts")
        return iter([])

    ctx = _make_ctx({"otto_reservation": mock_res})

    with (
        patch("otto.cli.monitor.all_hosts", side_effect=_fake_all_hosts),
        pytest.raises(typer.Exit),
    ):
        monitor(ctx, live=True)  # type: ignore[arg-type]

    assert order == ["gate", "hosts"]
    mock_res.evaluate.assert_called_once()


# ── 7. Lab requirement: optional for review, mandatory for --live ───────────
#
# Regression coverage for the live-bed-caught bug: monitor's spec set
# gate=False but not lab_free=True, so the shared root preamble still
# hard-required --lab even for review mode, which never loads a lab at all.
# `otto monitor <source>` — the exact command docs/guide/monitor.md
# documents — failed with "Error: Missing option '--lab'" even against a
# fully self-contained archive. See builtin_commands.py's monitor
# registration (lab_free=True) and monitor.py's --live branch (which now
# pulls the lab in itself via otto.cli.invoke.ensure_lab_session).


def test_review_mode_reaches_server_without_lab(tmp_path: Path) -> None:
    """``otto monitor <source>`` with no ``--lab`` and no lab configured must succeed.

    Builds a real (empty) schema-v2 ``.db`` the way
    ``tests/unit/cli/test_monitor.py::TestSourceArgument.test_db_file_accepted``
    does (a real ``MetricDB``, not hand-crafted SQLite), then dispatches
    through the full app with ``OTTO_LAB`` cleared and no ``--lab`` anywhere
    on the command line — the exact scenario the live-bed report reproduced.
    Only ``MonitorServer.serve()`` is stubbed (an ``AsyncMock``, so the real
    ``asyncio.run(_serve_review(...))`` coroutine actually runs to the point
    of constructing the server, then returns immediately instead of starting
    a real uvicorn server) — everything up to and including server
    construction runs for real, so this fails RED against the pre-fix code
    (exit 2, "Missing option '--lab'") and passes GREEN after it.
    """
    db_file = tmp_path / "metrics.db"

    async def _seed() -> None:
        db = MetricDB(str(db_file), new_frame(label=None, note=None), "{}", "{}")
        await db.open()
        await db.close()

    asyncio.run(_seed())

    mock_server = MagicMock()
    mock_server.serve = AsyncMock()

    from otto.cli.main import app

    with patch("otto.monitor.server.MonitorServer", return_value=mock_server) as mock_cls:
        result = runner.invoke(
            app,
            ["monitor", str(db_file)],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )

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


# ── 8. Review mode prints the server URL to the console ─────────────────────
#
# Regression coverage for the live-bed-caught bug: `lab_free=True` (added to
# fix #7 above) makes `command_preamble` early-return entirely for BOTH of
# monitor's branches, skipping `ensure_cli_session` (banner + `init_cli_logging`)
# for review mode too — not just the lab load. With no handler attached to the
# `'otto'` logger, `MonitorServer.serve()`'s `logger.info(f"Server running at
# {url}")` (otto/monitor/server.py) vanished into Python's `lastResort`
# handler (WARNING+ only): `otto monitor <source>` printed NOTHING, not even
# the URL a review-mode user needs to open. Fixed by having monitor's review
# branch call `ensure_cli_session` itself, the same way its `--live` branch
# already calls `ensure_lab_session` for the lab-requiring piece.


def test_review_mode_logs_server_url_to_console(
    tmp_path: Path, hermetic_monitor_dist: Path
) -> None:
    """``otto monitor <source>`` (review) must print the server URL to the console.

    Real end-to-end: dispatches through the full ``otto.cli.main.app`` (the
    real ``CommandSpec``/``command_preamble`` dispatch) against a real
    schema-v2 ``.db`` export, and lets the real ``MonitorServer.serve()``
    method run unmodified — including its actual ``logger.info`` calls that
    the fix depends on reaching the console. The only stub is uvicorn's own
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

    from otto.cli.main import app

    with patch.object(uvicorn.Server, "serve", _fake_uvicorn_serve):
        result = runner.invoke(
            app,
            ["monitor", str(db_file)],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )

    assert result.exit_code == 0, result.output
    assert "Server running at" in result.output, result.output
