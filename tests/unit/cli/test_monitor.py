"""
Unit tests for the ``otto monitor`` subcommand.

Covers:
  - Argument and option parsing / constraint validation
  - ``_load_historical()`` helper for .db / .json / .csv files
  - ``_build_collector()`` helper (parser selection, host.log suppression)
  - Routing: live mode vs historical mode inside ``monitor()``

The monitor command is tested via ``monitor_app`` directly so the main
callback (which requires a real lab) is not involved.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.monitor import _build_collector, _load_historical, monitor_app
from otto.host import RunResult
from otto.host.remoteHost import RemoteHost
from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import LoadParser, MemParser
from otto.utils import CommandStatus, Status

runner = CliRunner()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_host(name: str = 'box') -> RemoteHost:
    """Return a real RemoteHost (no connection is made on construction)."""
    return RemoteHost(ip='10.0.0.1', ne=name, creds={'admin': 'secret'}, log=True)


def _close_coro(coro):
    """Close a coroutine without running it (suppresses 'never awaited' warnings)."""
    if hasattr(coro, 'close'):
        coro.close()


@pytest.fixture
def live_mode_mocks():
    """
    Patch everything monitor() touches in live mode so no real SSH connections
    or event loops are started.
    """
    mock_host = _make_host()
    mock_collector = MagicMock()
    mock_server = MagicMock()

    with (
        patch('otto.cli.monitor.all_hosts', return_value=iter([mock_host])),
        patch('otto.cli.monitor.get_host', return_value=mock_host),
        patch('otto.cli.monitor.MetricCollector', return_value=mock_collector),
        patch('otto.cli.monitor.MonitorServer', return_value=mock_server),
        patch('asyncio.run', side_effect=_close_coro),
    ):
        yield {
            'host': mock_host,
            'collector': mock_collector,
            'server': mock_server,
        }


# ── Help / basic smoke ────────────────────────────────────────────────────────

class TestMonitorHelp:
    def test_help_flag(self):
        result = runner.invoke(monitor_app, ['--help'])
        assert result.exit_code == 0

    def test_help_mentions_interval(self):
        result = runner.invoke(monitor_app, ['--help'])
        assert '--interval' in result.output or '-i' in result.output


# ── --interval validation ─────────────────────────────────────────────────────

class TestIntervalOption:
    def test_default_interval_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, [])
        # Default (5.0) is above min (1.0), so parsing succeeds
        assert result.exit_code == 0

    def test_custom_interval_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ['--interval', '10'])
        assert result.exit_code == 0

    def test_interval_short_flag(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ['-i', '3'])
        assert result.exit_code == 0

    def test_interval_below_min_rejected(self):
        result = runner.invoke(monitor_app, ['--interval', '0.5'])
        assert result.exit_code == 2

    def test_interval_at_min_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ['--interval', '1.0'])
        assert result.exit_code == 0


# ── Hosts positional argument ─────────────────────────────────────────────────

class TestHostsArgument:
    def test_single_host_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ['host1'])
        assert result.exit_code == 0

    def test_comma_separated_hosts_accepted(self, live_mode_mocks):
        result = runner.invoke(monitor_app, ['host1,host2'])
        assert result.exit_code == 0

    def test_specific_hosts_routed_to_get_host(self, live_mode_mocks):
        """When host IDs are given, get_host() is called for each one."""
        with patch('otto.cli.monitor.get_host', return_value=live_mode_mocks['host']) as p:
            runner.invoke(monitor_app, ['router1'])
        p.assert_called()

    def test_no_hosts_uses_all_hosts(self, live_mode_mocks):
        """With no positional hosts, all_hosts() provides the list."""
        with patch('otto.cli.monitor.all_hosts', return_value=iter([])) as p:
            runner.invoke(monitor_app, [])
        p.assert_called_once()


# ── --db option ───────────────────────────────────────────────────────────────

class TestDbOption:
    def test_db_option_accepted(self, live_mode_mocks, tmp_path):
        db_file = tmp_path / 'metrics.db'
        result = runner.invoke(monitor_app, ['--db', str(db_file)])
        assert result.exit_code == 0


# ── --file option (historical mode) ──────────────────────────────────────────

class TestFileOption:
    def test_nonexistent_file_rejected(self):
        result = runner.invoke(monitor_app, ['--file', '/nonexistent/path/data.db'])
        assert result.exit_code != 0

    def test_json_file_accepted(self, tmp_path):
        json_file = tmp_path / 'metrics.json'
        json_file.write_text('{"metrics": [], "events": []}')
        with patch('asyncio.run', side_effect=_close_coro):
            result = runner.invoke(monitor_app, ['--file', str(json_file)])
        assert result.exit_code == 0

    def test_db_file_accepted(self, tmp_path):
        db_file = tmp_path / 'metrics.db'
        # Create a valid SQLite file with the expected schema
        with sqlite3.connect(str(db_file)) as conn:
            conn.executescript("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL, value REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, end_ts TEXT,
                    label TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
                    color TEXT NOT NULL DEFAULT '#888888', dash TEXT NOT NULL DEFAULT 'dash'
                );
            """)
        with patch('asyncio.run', side_effect=_close_coro):
            result = runner.invoke(monitor_app, ['--file', str(db_file)])
        assert result.exit_code == 0


# ── _load_historical() unit tests ─────────────────────────────────────────────

class TestLoadHistorical:
    """Direct unit tests for the private _load_historical() helper."""

    @pytest.mark.asyncio
    async def test_json_extension_uses_from_json(self, tmp_path):
        json_file = tmp_path / 'data.json'
        json_file.write_text('{"metrics": [], "events": []}')
        collector = await _load_historical(json_file)
        assert collector is not None
        assert collector.get_series() == {}

    @pytest.mark.asyncio
    async def test_sqlite_extension_uses_from_sqlite(self, tmp_path):
        db_file = tmp_path / 'data.db'
        with sqlite3.connect(str(db_file)) as conn:
            conn.executescript("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL, value REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                    end_ts TEXT, label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    color TEXT NOT NULL DEFAULT '#888888',
                    dash TEXT NOT NULL DEFAULT 'dash'
                );
            """)
        collector = await _load_historical(db_file)
        assert collector is not None

    @pytest.mark.asyncio
    async def test_unsupported_extension_raises_exit(self, tmp_path):
        import click
        txt_file = tmp_path / 'data.txt'
        txt_file.write_text('garbage')
        with pytest.raises(click.exceptions.Exit):
            await _load_historical(txt_file)

    @pytest.mark.asyncio
    async def test_json_with_data_loads_metrics(self, tmp_path):
        ts = '2024-01-01T12:00:00'
        json_file = tmp_path / 'data.json'
        json_file.write_text(json.dumps({
            'metrics': [
                {'timestamp': ts, 'host': 'router1', 'label': 'CPU %', 'value': 42.0},
            ],
            'events': [],
        }))
        collector = await _load_historical(json_file)
        series = collector.get_series()
        assert 'router1/CPU %' in series
        assert len(series['router1/CPU %']) == 1
        _, value, _ = series['router1/CPU %'][0]
        assert value == 42.0

    @pytest.mark.asyncio
    async def test_sqlite_with_data_loads_metrics(self, tmp_path):
        db_file = tmp_path / 'data.db'
        ts = datetime(2024, 1, 1, 12, 0, 0).isoformat()
        with sqlite3.connect(str(db_file)) as conn:
            conn.executescript("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL, value REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                    end_ts TEXT, label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    color TEXT NOT NULL DEFAULT '#888888',
                    dash TEXT NOT NULL DEFAULT 'dash'
                );
            """)
            conn.execute(
                'INSERT INTO metrics (ts, host, label, value) VALUES (?, ?, ?, ?)',
                (ts, 'host1', 'CPU %', 55.0),
            )
        collector = await _load_historical(db_file)
        series = collector.get_series()
        assert 'host1/CPU %' in series
        _, value, _ = series['host1/CPU %'][0]
        assert value == 55.0


# ── _build_collector() unit tests ─────────────────────────────────────────────

class TestBuildCollector:
    """Direct unit tests for the private _build_collector() helper."""

    def test_disables_host_logging(self):
        host = _make_host()
        assert host.log is True
        _build_collector(hosts=[host])
        assert host.log is False

    @pytest.mark.asyncio
    async def test_db_path_forwarded(self, tmp_path):
        host = _make_host()
        db_file = tmp_path / 'out.db'
        collector = _build_collector(hosts=[host], db_path=db_file)
        # DB is created lazily on init_db(), not on construction
        await collector.init_db()
        assert db_file.exists()
        await collector.close_db()

    def test_no_db_path_leaves_no_file(self):
        host = _make_host()
        collector = _build_collector(hosts=[host], db_path=None)
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


def _make_monitor_host(name: str = 'router1') -> MagicMock:
    """Return a mock RemoteHost whose run returns canned metric output.

    The mock boundary is at the host I/O layer — the collector, parsers,
    and storage all run for real.
    """
    host = MagicMock(spec=RemoteHost)
    host.name = name
    host.id = name
    host.log = True

    responses: dict[str, CommandStatus] = {
        'grep -c ^processor /proc/cpuinfo': CommandStatus(
            command='grep -c ^processor /proc/cpuinfo',
            output=_CPUINFO_OUTPUT,
            status=Status.Success,
            retcode=0,
        ),
        'free -b': CommandStatus(
            command='free -b',
            output=_FREE_OUTPUT,
            status=Status.Success,
            retcode=0,
        ),
        'cat /proc/loadavg': CommandStatus(
            command='cat /proc/loadavg',
            output=_LOADAVG_OUTPUT,
            status=Status.Success,
            retcode=0,
        ),
    }

    async def fake_run_cmds(cmds: list[str] | str, timeout: float | None = None) -> RunResult:
        if isinstance(cmds, str):
            cmds = [cmds]
        results = []
        for cmd in cmds:
            if cmd in responses:
                results.append(responses[cmd])
            else:
                results.append(CommandStatus(cmd, '', Status.Failed, 1))
        overall = Status.Success if all(r.status == Status.Success for r in results) else Status.Failed
        return RunResult(status=overall, statuses=results)

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
        host = _make_monitor_host('router1')
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
        assert 'router1/Memory Usage' in series
        _, mem_value, mem_meta = series['router1/Memory Usage'][0]
        assert abs(mem_value - 62.5) < 0.1  # 10B / 16B = 62.5%
        assert mem_meta is not None

        # LoadParser produces three load average series
        assert 'router1/Load (1m)' in series
        assert 'router1/Load (5m)' in series
        assert 'router1/Load (15m)' in series
        _, load_1m, _ = series['router1/Load (1m)'][0]
        assert abs(load_1m - 0.52) < 0.01

    @pytest.mark.asyncio
    async def test_collection_stores_to_sqlite(self, tmp_path):
        host = _make_monitor_host('router1')
        db_file = tmp_path / 'test_metrics.db'
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
            db_path=str(db_file),
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )
        await collector.close_db()

        assert db_file.exists()
        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute('SELECT host, label, value FROM metrics').fetchall()
        # 1 Memory Usage + 3 Load averages = 4 rows
        assert len(rows) >= 4
        labels = {row[1] for row in rows}
        assert 'Memory Usage' in labels
        assert 'Load (1m)' in labels

    @pytest.mark.asyncio
    async def test_multiple_hosts_collected(self):
        host1 = _make_monitor_host('host1')
        host2 = _make_monitor_host('host2')
        collector = MetricCollector(
            hosts=[host1, host2],
            parsers=[LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        series = collector.get_series()
        assert 'host1/Load (1m)' in series
        assert 'host2/Load (1m)' in series

    @pytest.mark.asyncio
    async def test_failed_command_does_not_crash_collector(self):
        host = _make_monitor_host('router1')
        # Override host.run to always return failures with empty output
        host.run = AsyncMock(return_value=RunResult(
            status=Status.Failed,
            statuses=[
                CommandStatus('free -b', '', Status.Failed, 1),
                CommandStatus('cat /proc/loadavg', '', Status.Failed, 1),
            ],
        ))
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
