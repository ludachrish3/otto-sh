"""
Unit tests for the ``otto monitor`` subcommand.

Covers:
  - Argument and option parsing / constraint validation
  - ``build_monitor_collector()`` factory (parser selection, host.log suppression)
  - Routing: ``--live`` vs a ``<source>`` review positional inside ``monitor()``

The monitor command is tested via ``monitor_app`` directly so the main
callback (which requires a real lab) is not involved.

The CLI-shape tests (bare invocation, ``--live``/source mutual exclusion,
source-suffix dispatch, reservation-gate ordering) live in the e2e suite —
``tests/e2e/cli/test_monitor_cli.py`` — per spec 2026-07-12's CLI split.
This file keeps the finer-grained option-parsing and factory/collector
coverage.
"""

import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
import typer
from typer.testing import CliRunner

from otto.cli.monitor import monitor, monitor_app
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB
from otto.monitor.export import build_db_export
from otto.monitor.factory import build_monitor_collector
from otto.monitor.parsers import LoadParser, MemParser
from otto.monitor.session import new_frame
from otto.reservations import ReservationGateResult
from otto.result import CommandResult, Results
from otto.utils import Status

runner = CliRunner()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_host(name: str = "box") -> UnixHost:
    """Return a real UnixHost (no connection is made on construction)."""
    return UnixHost(
        ip="10.0.0.1",
        element=name,
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
    )


def _make_cert(tmp_path: Path) -> Path:
    """Mint a throwaway self-signed PEM (cert+key bundled into one file) via openssl.

    Same approach as ``tests/unit/monitor/test_server_tls.py``: universally
    present on the Linux targets this repo supports, no extra dev-dep needed.
    Cert and key are generated to separate files (openssl can't safely write
    both to one ``-out``/``-keyout`` path — the second write clobbers the
    first) then concatenated, exercising the ``tls_key is None`` "cert
    bundles the key" path documented on ``MonitorSettings``.
    """
    key, crt = tmp_path / "key.pem", tmp_path / "crt.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    bundle = tmp_path / "cert.pem"
    bundle.write_text(crt.read_text() + key.read_text())
    return bundle


def _close_coro(coro):
    """Close a coroutine without running it (suppresses 'never awaited' warnings)."""
    if hasattr(coro, "close"):
        coro.close()


@pytest.fixture
def live_mode_mocks():
    """
    Patch everything monitor() touches in live mode so no real SSH connections
    or event loops are started, and no active OttoContext/lab is required
    (``--live`` now always builds a session frame + lab snapshot, which needs
    ``get_lab()`` — a bare ``CliRunner`` invocation has no active context).
    """
    mock_host = _make_host()
    mock_collector = MagicMock()
    mock_server = MagicMock()
    mock_lab = MagicMock()
    mock_lab.links = []

    with (
        patch("otto.cli.monitor.all_hosts", return_value=iter([mock_host])),
        patch("otto.cli.monitor.get_lab", return_value=mock_lab),
        patch("otto.cli.monitor.build_monitor_collector", return_value=mock_collector),
        patch("otto.monitor.server.MonitorServer", return_value=mock_server),
        patch("asyncio.run", side_effect=_close_coro),
    ):
        yield {
            "host": mock_host,
            "collector": mock_collector,
            "server": mock_server,
        }


# ── Help / basic smoke ────────────────────────────────────────────────────────


class TestMonitorHelp:
    def test_help_flag(self):
        result = runner.invoke(monitor_app, ["--help"])
        assert result.exit_code == 0

    def test_help_mentions_interval(self):
        result = runner.invoke(monitor_app, ["--help"])
        assert "--interval" in result.output or "-i" in result.output


# ── --interval validation ─────────────────────────────────────────────────────


class TestIntervalOption:
    def test_default_interval_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live"])
        # Default (5.0) is above min (1.0), so parsing succeeds
        assert result.exit_code == 0

    def test_custom_interval_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live", "--interval", "10"])
        assert result.exit_code == 0

    def test_interval_short_flag(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live", "-i", "3"])
        assert result.exit_code == 0

    def test_interval_below_min_rejected(self):
        # Typer's own `min=1.0` parameter validation fires during parsing,
        # before monitor()'s body runs — --live is irrelevant here.
        result = runner.invoke(monitor_app, ["--interval", "0.5"])
        assert result.exit_code == 2

    def test_interval_at_min_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live", "--interval", "1.0"])
        assert result.exit_code == 0


# ── --hosts regex option ──────────────────────────────────────────────────────


class TestHostsArgument:
    def test_single_host_regex_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live", "--hosts", "host1"])
        assert result.exit_code == 0

    def test_alternation_regex_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ["--live", "--hosts", "host1|host2"])
        assert result.exit_code == 0

    def test_no_hosts_option_uses_all_hosts(self, live_mode_mocks):
        """Without --hosts, all_hosts() provides the list (called with pattern=None)."""
        with patch(
            "otto.cli.monitor.all_hosts",
            return_value=iter([live_mode_mocks["host"]]),
        ) as p:
            runner.invoke(monitor_app, ["--live"])
        p.assert_called_once()
        assert p.call_args.kwargs.get("pattern") is None

    def test_hosts_regex_passed_to_all_hosts(self, live_mode_mocks):
        """A --hosts regex is compiled and forwarded to all_hosts(pattern=...)."""
        with patch(
            "otto.cli.monitor.all_hosts",
            return_value=iter([live_mode_mocks["host"]]),
        ) as p:
            runner.invoke(monitor_app, ["--live", "--hosts", "router"])
        p.assert_called_once()
        pattern = p.call_args.kwargs.get("pattern")
        assert pattern is not None
        assert pattern.search("router1")
        assert pattern.search("switch1") is None

    def test_no_matching_hosts_exits_nonzero(self, live_mode_mocks):
        with patch("otto.cli.monitor.all_hosts", return_value=iter([])):
            result = runner.invoke(monitor_app, ["--live", "--hosts", "nope"])
        assert result.exit_code != 0


# ── --db option ───────────────────────────────────────────────────────────────


class TestDbOption:
    def test_db_option_accepted(self, live_mode_mocks, tmp_path):
        db_file = tmp_path / "metrics.db"
        result = runner.invoke(monitor_app, ["--live", "--db", str(db_file)])
        assert result.exit_code == 0

    def test_live_db_persists_real_meta_and_interval(self, tmp_path):
        """``--live --db`` writes a session row carrying the REAL parser catalog.

        The CALL-SITE guard for the ``meta_json`` seam. ``live_mode_mocks``
        cannot serve here: it patches ``build_monitor_collector`` into a
        MagicMock, so nothing the CLI hands to ``MetricDB`` is ever exercised
        and the whole ``meta_json`` expression is unobserved — reverting it to
        the raw ``get_meta_model().model_dump_json()`` dump (which silently
        drops every chart; see otto.monitor.export.session_meta) left the
        entire suite green. So drive the REAL collector/MetricDB the CLI
        builds, and assert on the artifact read back out of the file.

        Only the serve loop is replaced: ``_run_monitor`` is swapped for a stub
        that opens the DB (``init_db`` INSERTs the session row), finalizes, and
        closes — no uvicorn, no host I/O (``collector.run`` is never called).
        """
        db_file = tmp_path / "metrics.db"
        mock_lab = MagicMock()
        mock_lab.links = []

        # Signature mirrors _run_monitor's; `server`/`interval`/`duration` are
        # accepted and ignored — this stub replaces only the serve loop.
        async def _fake_run_monitor(collector, server, interval, db=None, duration=None):
            await collector.init_db()
            if db is not None:
                await db.finalize(datetime.now(tz=timezone.utc))
            await collector.close()

        with (
            patch("otto.cli.monitor.all_hosts", return_value=iter([_make_host("router1")])),
            patch("otto.cli.monitor.get_lab", return_value=mock_lab),
            patch("otto.monitor.server.MonitorServer", return_value=MagicMock()),
            patch("otto.cli.monitor._run_monitor", _fake_run_monitor),
        ):
            result = runner.invoke(
                monitor_app,
                ["--live", "--db", str(db_file), "--interval", "7"],
            )
        assert result.exit_code == 0, result.output

        (session,) = build_db_export(str(db_file)).sessions
        # The parser catalog survived the MonitorMeta -> SessionMeta reshape.
        assert session.meta.charts, "session persisted with no chart specs"
        assert "CPU" in [c.chart for c in session.meta.charts]
        assert session.meta.tabs, "session persisted with no tabs"
        # The interval the run was LAUNCHED with — the collector has not run at
        # meta-write time, so this can only be right if the CLI threads its
        # --interval through. A null here leaves derived health unresolvable on
        # replay (web/src/data/health.ts's cadenceMs).
        assert session.meta.interval == 7.0
        # And the lab snapshot rode along.
        assert [h.id for h in session.lab.hosts] == ["router1"]


class TestTlsResolvedBeforeDbCreation:
    """A doomed [monitor] TLS config must exit BEFORE a ``--db`` archive is built.

    ``_resolve_monitor_tls()`` used to run after ``build_session_metric_db()``
    in the ``--live`` branch, so a bad TLS declaration left a half-created
    ``--db`` file on disk even though the run never actually served anything.
    """

    def test_doomed_tls_config_exits_before_build_session_metric_db(
        self, live_mode_mocks, tmp_path
    ):
        db_file = tmp_path / "metrics.db"
        ctx = _make_ctx({"_otto_root_options": object(), "_otto_lab_ready": True})
        with (
            patch("otto.cli.monitor._resolve_monitor_tls", side_effect=typer.Exit(1)),
            patch("otto.cli.monitor.build_session_metric_db") as mock_build_db,
            pytest.raises(typer.Exit),
        ):
            monitor(ctx, live=True, db=db_file)
        mock_build_db.assert_not_called()
        assert not db_file.exists()


# ── <source> positional (review mode) ────────────────────────────────────────
#
# Rejection-path coverage (unknown suffix, legacy-flat JSON, missing file,
# --live/source mutual exclusion) lives in tests/e2e/cli/test_monitor_cli.py.
# These cover the ACCEPTANCE path: a well-formed .json/.db export dispatches
# through to serving (asyncio.run mocked out — no uvicorn is started here).


class TestSourceArgument:
    def test_json_file_accepted(self, tmp_path):
        json_file = tmp_path / "metrics.json"
        # A minimal, valid format:1 document (spec 2026-07-10 §3) — the flat
        # legacy shape ({"metrics": [], "events": []}) is now REJECTED; see
        # test_source_rejects_legacy_json in the e2e suite.
        json_file.write_text(json.dumps({"format": 1, "sessions": []}))
        with patch("asyncio.run", side_effect=_close_coro):
            result = runner.invoke(monitor_app, [str(json_file)])
        assert result.exit_code == 0

    def test_db_file_accepted(self, tmp_path):
        import asyncio

        db_file = tmp_path / "metrics.db"

        async def _seed() -> None:
            # A real (empty) schema-v2 session archive — the legacy flat
            # sqlite shape is now refused loud (UnsupportedDBError); see
            # test_source_rejects_unknown_suffix / test_db_v2.py's refusal
            # tests for that path.
            db = MetricDB(str(db_file), new_frame(label=None, note=None), "{}", "{}")
            await db.open()
            await db.close()

        asyncio.run(_seed())

        with patch("asyncio.run", side_effect=_close_coro):
            result = runner.invoke(monitor_app, [str(db_file)])
        assert result.exit_code == 0


# ── Reservation gate: per-branch, not uniform ────────────────────────────────
#
# monitor registers gate=False (see builtin_commands.py) and gates itself:
# reviewing a saved <source> reads a local file and never touches live
# hardware, so it is gate-exempt by design; --live collection still gates.
# (Gate-ordering-before-host-selection is covered end to end in
# tests/e2e/cli/test_monitor_cli.py.)
#
# monitor() reads ctx.meta["otto_reservation"] and calls .evaluate() inline
# (there is no more standalone `gate(ctx)` callable to patch — see
# otto.reservations.check.ReservationGate — so these tests call monitor()
# directly with a hand-built click.Context, the same pattern
# tests/unit/cli/test_reservation.py uses for its ctx-driven commands).


def _make_ctx(meta: dict) -> typer.Context:
    """Build a typer.Context (backed by click.Context) with the given meta."""
    cmd = click.Command("monitor")
    ctx = click.Context(cmd)
    ctx.meta.update(meta)
    return ctx  # type: ignore[return-value]


class TestGatePerBranch:
    def test_source_review_does_not_invoke_gate(self, tmp_path):
        json_file = tmp_path / "metrics.json"
        json_file.write_text(json.dumps({"format": 1, "sessions": []}))
        mock_res = MagicMock()
        ctx = _make_ctx({"otto_reservation": mock_res})
        with patch("asyncio.run", side_effect=_close_coro):
            monitor(ctx, source=json_file)
        mock_res.evaluate.assert_not_called()

    def test_live_mode_invokes_gate(self, live_mode_mocks):
        mock_res = MagicMock()
        mock_res.evaluate.return_value = ReservationGateResult(
            checked=True, skipped=False, warning=None
        )
        ctx = _make_ctx({"otto_reservation": mock_res})

        monitor(ctx, live=True)

        mock_res.evaluate.assert_called_once()

    def test_live_mode_prints_warning_from_gate(self, live_mode_mocks, capsys):
        warning = (
            "\N{WARNING SIGN}  Reservation check SKIPPED for user 'alice' "
            "on lab 'x'. Required resources: []"
        )
        mock_res = MagicMock()
        mock_res.evaluate.return_value = ReservationGateResult(
            checked=False, skipped=True, warning=warning
        )
        ctx = _make_ctx({"otto_reservation": mock_res})

        monitor(ctx, live=True)

        out = capsys.readouterr().out
        # rich strips the [bold red] markup and may word-wrap at the console
        # width when rendering to the captured (non-tty) stream — compare
        # with whitespace normalized so a wrap point can't fail the assertion.
        assert " ".join(warning.split()) in " ".join(out.split())

    def test_live_mode_without_reservation_state_is_noop(self, live_mode_mocks, capsys):
        """No otto_reservation in ctx.meta (e.g. monitor invoked directly in tests, no preamble) -> no crash, no print."""  # noqa: E501 — descriptive docstring
        ctx = _make_ctx({})

        monitor(ctx, live=True)  # must not raise

        assert capsys.readouterr().out == ""


# ── build_monitor_collector() unit tests ──────────────────────────────────────


class TestResolveMonitorTls:
    """[monitor] TLS resolution: single source of truth, fail-loud (spec 'Runtime behavior')."""

    @staticmethod
    def _repo(name: str, cert=None, key=None):
        from types import SimpleNamespace

        from otto.config import MonitorSettings

        return SimpleNamespace(
            name=name, monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key)
        )

    def test_no_repo_declares_tls_returns_none(self, monkeypatch):
        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a"), self._repo("b")])
        assert _resolve_monitor_tls() is None

    def test_single_declaration_with_real_files_applies(self, monkeypatch, tmp_path):
        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        cert = _make_cert(tmp_path)
        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a", cert=cert)])
        resolved = _resolve_monitor_tls()
        assert resolved is not None
        assert resolved.tls_cert == cert

    def test_identical_declarations_apply(self, monkeypatch, tmp_path):
        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        cert = _make_cert(tmp_path)
        monkeypatch.setattr(
            otto.config,
            "get_repos",
            lambda: [self._repo("a", cert=cert), self._repo("b", cert=cert)],
        )
        assert _resolve_monitor_tls() is not None

    def test_disagreeing_declarations_exit_1_naming_repos(self, monkeypatch, tmp_path, capsys):
        import typer

        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        c1, c2 = tmp_path / "c1.pem", tmp_path / "c2.pem"
        c1.write_text("x"), c2.write_text("y")
        monkeypatch.setattr(
            otto.config,
            "get_repos",
            lambda: [self._repo("alpha", cert=c1), self._repo("beta", cert=c2)],
        )
        with pytest.raises(typer.Exit) as excinfo:
            _resolve_monitor_tls()
        assert excinfo.value.exit_code == 1
        err = capsys.readouterr().err
        assert "alpha" in err
        assert "beta" in err

    def test_missing_cert_file_exits_1_naming_path(self, monkeypatch, tmp_path, capsys):
        import typer

        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        ghost = tmp_path / "nope.pem"
        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a", cert=ghost)])
        with pytest.raises(typer.Exit) as excinfo:
            _resolve_monitor_tls()
        assert excinfo.value.exit_code == 1
        assert str(ghost) in capsys.readouterr().err

    def test_unparseable_cert_file_exits_1_naming_path(self, monkeypatch, tmp_path, capsys):
        """A cert file that exists (passes ``is_file()``) but isn't a real PEM

        must still fail loud at CLI startup, never silently proceed into
        uvicorn only to die later inside ``MonitorServer.serve()``'s
        background task (see ``test_serve_raises_instead_of_hanging_on_bad_cert``
        in ``tests/unit/monitor/test_server_tls.py`` for that failure mode).
        """
        import typer

        import otto.config
        from otto.cli.monitor import _resolve_monitor_tls

        garbage = tmp_path / "cert.pem"
        garbage.write_text("this is not a certificate")
        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a", cert=garbage)])
        with pytest.raises(typer.Exit) as excinfo:
            _resolve_monitor_tls()
        assert excinfo.value.exit_code == 1
        assert str(garbage) in capsys.readouterr().err


class TestBuildCollector:
    """Direct unit tests for the build_monitor_collector() factory."""

    def test_disables_host_logging(self):
        host = _make_host()
        assert host.log is LogMode.NORMAL
        build_monitor_collector(hosts=[host])
        assert host.log is LogMode.NEVER

    @pytest.mark.asyncio
    async def test_db_forwarded(self, tmp_path):
        host = _make_host()
        db_file = tmp_path / "out.db"
        db = MetricDB(
            str(db_file),
            new_frame(label=None, note=None),
            lab_json="{}",
            meta_json="{}",
        )
        collector = build_monitor_collector(hosts=[host], db=db)
        # DB is created lazily on init_db(), not on construction
        await collector.init_db()
        assert db_file.exists()
        await collector.close_db()

    def test_no_db_leaves_no_file(self):
        host = _make_host()
        collector = build_monitor_collector(hosts=[host], db=None)
        # Just verifies it doesn't raise
        assert collector is not None


# ── Helpers for live collection tests ────────────────────────────────────────

# Synthetic command outputs matching parser expected formats
_FREE_OUTPUT = (
    "              total        used        free      shared  buff/cache   available\n"
    "Mem:    16000000000  10000000000   3000000000           0  3000000000  6000000000\n"
    "Swap:    2048000000           0  2048000000"
)
_LOADAVG_OUTPUT = "0.52 0.58 0.59 1/432 12345"
_CPUINFO_OUTPUT = "4"


def _make_monitor_host(name: str = "router1") -> MagicMock:
    """Return a mock UnixHost whose run returns canned metric output.

    The mock boundary is at the host I/O layer — the collector, parsers,
    and storage all run for real.
    """
    host = MagicMock(spec=UnixHost)
    host.name = name
    host.id = name
    host.log = LogMode.NORMAL

    responses: dict[str, CommandResult] = {
        "grep -c ^processor /proc/cpuinfo": CommandResult(
            Status.Success,
            value=_CPUINFO_OUTPUT,
            command="grep -c ^processor /proc/cpuinfo",
            retcode=0,
        ),
        "free -b": CommandResult(
            Status.Success,
            value=_FREE_OUTPUT,
            command="free -b",
            retcode=0,
        ),
        "cat /proc/loadavg": CommandResult(
            Status.Success,
            value=_LOADAVG_OUTPUT,
            command="cat /proc/loadavg",
            retcode=0,
        ),
    }

    async def fake_run_cmds(cmds: list[str] | str, timeout: float | None = None) -> Results:
        if isinstance(cmds, str):
            cmds = [cmds]
        results = []
        for cmd in cmds:
            if cmd in responses:
                results.append(responses[cmd])
            else:
                results.append(CommandResult(Status.Failed, value="", command=cmd, retcode=1))
        return Results.collect(results)

    host.run = AsyncMock(side_effect=fake_run_cmds)
    return host


# ── MetricCollector live run tests ───────────────────────────────────────────


class TestCollectorLiveRun:
    """Tests that let MetricCollector.run() execute for real with mock hosts.

    Mock boundary: host.run (I/O layer).
    Exercises: parser selection, output parsing, series storage, DB writes.
    """

    @pytest.mark.asyncio
    async def test_single_cycle_parses_metrics(self):
        host = _make_monitor_host("router1")
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        series = collector.get_series()
        # MemParser produces "Memory Usage" keyed by chart name
        assert "router1/Memory Usage" in series
        mem_pt = series["router1/Memory Usage"][0]
        assert abs(mem_pt.value - 62.5) < 0.1  # 10B / 16B = 62.5%
        assert mem_pt.meta is not None

        # LoadParser produces three load average series
        assert "router1/Load (1m)" in series
        assert "router1/Load (5m)" in series
        assert "router1/Load (15m)" in series
        load_1m = series["router1/Load (1m)"][0].value
        assert abs(load_1m - 0.52) < 0.01

    @pytest.mark.asyncio
    async def test_collection_stores_to_sqlite(self, tmp_path):
        host = _make_monitor_host("router1")
        db_file = tmp_path / "test_metrics.db"
        db = MetricDB(
            str(db_file),
            new_frame(label=None, note=None),
            lab_json="{}",
            meta_json="{}",
        )
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
            db=db,
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )
        await collector.close_db()

        assert db_file.exists()
        with closing(sqlite3.connect(str(db_file))) as conn, conn:
            rows = conn.execute("SELECT host, label, value FROM metrics").fetchall()
        # 1 Memory Usage + 3 Load averages = 4 rows
        assert len(rows) >= 4
        labels = {row[1] for row in rows}
        assert "Memory Usage" in labels
        assert "Load (1m)" in labels

    @pytest.mark.asyncio
    async def test_multiple_hosts_collected(self):
        host1 = _make_monitor_host("host1")
        host2 = _make_monitor_host("host2")
        collector = MetricCollector(
            hosts=[host1, host2],
            parsers=[LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        series = collector.get_series()
        assert "host1/Load (1m)" in series
        assert "host2/Load (1m)" in series

    @pytest.mark.asyncio
    async def test_failed_command_does_not_crash_collector(self):
        host = _make_monitor_host("router1")
        # Override host.run to always return failures with empty output
        host.run = AsyncMock(
            return_value=Results.collect(
                [
                    CommandResult(Status.Failed, value="", command="free -b", retcode=1),
                    CommandResult(Status.Failed, value="", command="cat /proc/loadavg", retcode=1),
                ]
            )
        )
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        # Parsers return {} for empty/unparseable output — no series created
        assert collector.get_series() == {}
