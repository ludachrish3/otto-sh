"""
Unit/component tests for MetricCollector SQLite and JSON persistence.

These tests exercise the full read/write round-trip without any SSH connections
or running event loops.  They are the primary coverage for the database
integration the user noted had never been tested.

Covers:
  - Schema initialisation when a db_path is provided
  - Incremental metric writes via _db_write_point()
  - Event writes, updates, and deletes via add_event() / update_event() / delete_event()
  - Span events (end_timestamp)
  - Schema migration (old tables without the end_ts column)
  - from_sqlite() class method
  - JSON export/import round-trip (export_json / from_json)
  - get_meta() reports live=False for historical collectors
  - WAL mode and busy_timeout are set correctly
  - Instance locking prevents two writers on the same database
  - Display host resolution for MonitorServer
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.events import MonitorEvent
from otto.monitor.parsers import TopCpuParser, MemParser


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_collector(db_path: str | None = None) -> MetricCollector:
    """Return a MetricCollector with no hosts (historical / test mode).

    DB is NOT initialised — call ``await collector.init_db()`` separately
    for tests that need the database.
    """
    return MetricCollector(hosts=[], parsers=[TopCpuParser(), MemParser()], db_path=db_path)


async def _empty_collector_with_db(db_path: str) -> MetricCollector:
    """Return a MetricCollector with its async DB connection initialised."""
    collector = _empty_collector(db_path)
    await collector.init_db()
    return collector


async def _inject_point(
    collector: MetricCollector,
    host: str,
    label: str,
    value: float,
    ts: datetime | None = None,
) -> None:
    """Manually inject a data point as if _process_host_results had stored it."""
    ts = ts or datetime.now()
    key = f'{host}/{label}'
    from collections import deque
    if key not in collector._series:
        collector._series[key] = deque()
    collector._series[key].append((ts, value, None))
    await collector._db_write_point(ts, host, label, value)


# ── Schema initialisation ─────────────────────────────────────────────────────

class TestSchemaInit:
    @pytest.mark.asyncio
    async def test_creates_metrics_table(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        with sqlite3.connect(db_path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'metrics' in tables
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_creates_events_table(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        with sqlite3.connect(db_path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'events' in tables
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_metrics_table_has_expected_columns(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute('PRAGMA table_info(metrics)')}
        assert {'id', 'ts', 'host', 'label', 'value'}.issubset(cols)
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_events_table_has_end_ts_column(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
        assert 'end_ts' in cols
        await collector.close_db()


# ── WAL mode and busy_timeout ────────────────────────────────────────────────

class TestWalMode:
    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        with sqlite3.connect(db_path) as conn:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        assert mode == 'wal'
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_busy_timeout_set(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        assert collector._db_conn is not None
        cursor = await collector._db_conn.execute('PRAGMA busy_timeout')
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 5000
        await collector.close_db()


# ── Instance locking ─────────────────────────────────────────────────────────

class TestInstanceLock:
    @pytest.mark.asyncio
    async def test_second_instance_raises(self, tmp_path):
        db_path = str(tmp_path / 'locked.db')
        collector_a = await _empty_collector_with_db(db_path)
        with pytest.raises(RuntimeError, match='Another otto monitor instance'):
            await _empty_collector_with_db(db_path)
        await collector_a.close_db()

    @pytest.mark.asyncio
    async def test_lock_released_on_close(self, tmp_path):
        db_path = str(tmp_path / 'locked.db')
        collector_a = await _empty_collector_with_db(db_path)
        await collector_a.close_db()
        # Should succeed now that the lock is released
        collector_b = await _empty_collector_with_db(db_path)
        await collector_b.close_db()

    @pytest.mark.asyncio
    async def test_read_only_loader_ignores_lock(self, tmp_path):
        db_path = str(tmp_path / 'locked.db')
        collector_a = await _empty_collector_with_db(db_path)
        # from_sqlite is read-only and should not be blocked by the write lock
        historical = await MetricCollector.from_sqlite(db_path)
        assert historical.get_series() == {}
        await collector_a.close_db()


# ── Metric persistence ────────────────────────────────────────────────────────

class TestMetricPersistence:
    @pytest.mark.asyncio
    async def test_metric_written_to_db(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        ts = datetime(2024, 6, 1, 12, 0, 0)
        await _inject_point(collector, 'router1', 'CPU %', 42.5, ts)
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            rows = list(conn.execute('SELECT ts, host, label, value FROM metrics'))
        assert len(rows) == 1
        assert rows[0][1] == 'router1'
        assert rows[0][2] == 'CPU %'
        assert rows[0][3] == pytest.approx(42.5)

    @pytest.mark.asyncio
    async def test_multiple_metrics_written(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        await _inject_point(collector, 'h1', 'CPU %', 10.0)
        await _inject_point(collector, 'h1', 'Memory %', 60.0)
        await _inject_point(collector, 'h2', 'CPU %', 20.0)
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]
        assert count == 3

    @pytest.mark.asyncio
    async def test_no_db_does_not_raise(self):
        collector = _empty_collector(db_path=None)
        # Should silently do nothing when no db_path
        await _inject_point(collector, 'h1', 'CPU %', 50.0)
        assert collector.get_series()


# ── Event persistence ─────────────────────────────────────────────────────────

class TestEventPersistence:
    @pytest.mark.asyncio
    async def test_event_written_to_db(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        ts = datetime(2024, 6, 1, 13, 0, 0)
        await collector.add_event(label='test start', timestamp=ts, color='#888888', source='auto')
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            rows = list(conn.execute('SELECT label, source, color FROM events'))
        assert len(rows) == 1
        assert rows[0][0] == 'test start'
        assert rows[0][1] == 'auto'

    @pytest.mark.asyncio
    async def test_span_event_written_with_end_ts(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        start = datetime(2024, 6, 1, 13, 0, 0)
        end = datetime(2024, 6, 1, 13, 5, 0)
        await collector.add_event(label='my span', timestamp=start, end_timestamp=end)
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            rows = list(conn.execute('SELECT end_ts FROM events'))
        assert rows[0][0] == end.isoformat()

    @pytest.mark.asyncio
    async def test_event_updated_in_db(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        event = await collector.add_event(label='old label', color='#888888', dash='dash')
        await collector.update_event(event.id, label='new label', color='#ff0000', dash='solid')
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute('SELECT label, color, dash FROM events WHERE id = ?', (event.id,)).fetchone()
        assert row[0] == 'new label'
        assert row[1] == '#ff0000'
        assert row[2] == 'solid'

    @pytest.mark.asyncio
    async def test_event_deleted_from_db(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        collector = await _empty_collector_with_db(db_path)
        event = await collector.add_event(label='to delete')
        await collector.delete_event(event.id)
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        assert count == 0


# ── Schema migration ──────────────────────────────────────────────────────────

class TestSchemaMigration:
    @pytest.mark.asyncio
    async def test_old_schema_without_end_ts_gets_migrated(self, tmp_path):
        """
        A database created before span-event support (no end_ts column on events)
        should be silently migrated when MetricCollector opens it.
        """
        db_path = str(tmp_path / 'old.db')
        # Create old-style schema without end_ts
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL, value REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    color TEXT NOT NULL DEFAULT '#888888',
                    dash TEXT NOT NULL DEFAULT 'dash'
                );
            """)
        # Opening via MetricCollector should migrate the schema
        collector = await _empty_collector_with_db(db_path)
        await collector.close_db()
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
        assert 'end_ts' in cols


# ── from_sqlite() class method ────────────────────────────────────────────────

class TestFromSqlite:
    def _make_db(self, tmp_path, rows=None, events=None) -> str:
        db_path = str(tmp_path / 'data.db')
        with sqlite3.connect(db_path) as conn:
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
            for ts, host, label, value in (rows or []):
                conn.execute(
                    'INSERT INTO metrics (ts, host, label, value) VALUES (?, ?, ?, ?)',
                    (ts, host, label, value),
                )
            for ev in (events or []):
                conn.execute(
                    'INSERT INTO events (ts, end_ts, label, source, color, dash) VALUES (?, ?, ?, ?, ?, ?)',
                    ev,
                )
        return db_path

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_collector(self, tmp_path):
        db_path = self._make_db(tmp_path)
        collector = await MetricCollector.from_sqlite(db_path)
        assert collector.get_series() == {}
        assert collector.get_events() == []

    @pytest.mark.asyncio
    async def test_loads_metric_rows(self, tmp_path):
        ts = datetime(2024, 3, 1, 10, 0, 0).isoformat()
        db_path = self._make_db(
            tmp_path,
            rows=[(ts, 'router1', 'CPU %', 33.3), (ts, 'router1', 'Memory %', 55.0)],
        )
        collector = await MetricCollector.from_sqlite(db_path)
        series = collector.get_series()
        assert 'router1/CPU %' in series
        assert 'router1/Memory %' in series
        _, value, _ = series['router1/CPU %'][0]
        assert value == pytest.approx(33.3)

    @pytest.mark.asyncio
    async def test_loads_multiple_hosts(self, tmp_path):
        ts = datetime(2024, 3, 1, 10, 0, 0).isoformat()
        db_path = self._make_db(
            tmp_path,
            rows=[
                (ts, 'host1', 'CPU %', 10.0),
                (ts, 'host2', 'CPU %', 20.0),
            ],
        )
        collector = await MetricCollector.from_sqlite(db_path)
        series = collector.get_series()
        assert 'host1/CPU %' in series
        assert 'host2/CPU %' in series

    @pytest.mark.asyncio
    async def test_loads_events(self, tmp_path):
        ts = datetime(2024, 3, 1, 10, 0, 0).isoformat()
        db_path = self._make_db(
            tmp_path,
            events=[(ts, None, 'test start', 'auto', '#888888', 'dash')],
        )
        collector = await MetricCollector.from_sqlite(db_path)
        events = collector.get_events()
        assert len(events) == 1
        assert events[0].label == 'test start'
        assert events[0].source == 'auto'

    @pytest.mark.asyncio
    async def test_loads_span_events(self, tmp_path):
        ts = datetime(2024, 3, 1, 10, 0, 0).isoformat()
        end_ts = datetime(2024, 3, 1, 10, 5, 0).isoformat()
        db_path = self._make_db(
            tmp_path,
            events=[(ts, end_ts, 'test span', 'auto', '#2ca02c', 'solid')],
        )
        collector = await MetricCollector.from_sqlite(db_path)
        events = collector.get_events()
        assert events[0].end_timestamp is not None
        assert events[0].end_timestamp == datetime(2024, 3, 1, 10, 5, 0)

    @pytest.mark.asyncio
    async def test_get_meta_reports_live_false(self, tmp_path):
        db_path = self._make_db(tmp_path)
        collector = await MetricCollector.from_sqlite(db_path)
        meta = collector.get_meta()
        assert meta['live'] is False

    @pytest.mark.asyncio
    async def test_old_schema_without_host_column(self, tmp_path):
        """from_sqlite() should handle old DBs that lack the host column."""
        db_path = str(tmp_path / 'old.db')
        ts = datetime(2024, 3, 1, 10, 0, 0).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, label TEXT NOT NULL, value REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                    end_ts TEXT, label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    color TEXT NOT NULL DEFAULT '#888888', dash TEXT NOT NULL DEFAULT 'dash'
                );
            """)
            conn.execute(
                'INSERT INTO metrics (ts, label, value) VALUES (?, ?, ?)',
                (ts, 'CPU %', 75.0),
            )
        collector = await MetricCollector.from_sqlite(db_path)
        series = collector.get_series()
        # Without host, the key is just 'label' (no prefix)
        assert any('CPU %' in k for k in series)


# ── JSON round-trip ───────────────────────────────────────────────────────────

class TestJsonRoundTrip:
    def test_empty_round_trip(self, tmp_path):
        collector = _empty_collector()
        path = str(tmp_path / 'out.json')
        collector.export_json(path)
        loaded = MetricCollector.from_json(path)
        assert loaded.get_series() == {}
        assert loaded.get_events() == []

    @pytest.mark.asyncio
    async def test_metrics_preserved(self, tmp_path):
        collector = _empty_collector()
        ts = datetime(2024, 6, 1, 12, 0, 0)
        await _inject_point(collector, 'host1', 'CPU %', 77.7, ts)
        path = str(tmp_path / 'out.json')
        collector.export_json(path)
        loaded = MetricCollector.from_json(path)
        series = loaded.get_series()
        assert 'host1/CPU %' in series
        _, value, _ = series['host1/CPU %'][0]
        assert value == pytest.approx(77.7)

    @pytest.mark.asyncio
    async def test_events_preserved(self, tmp_path):
        collector = _empty_collector()
        ts = datetime(2024, 6, 1, 12, 0, 0)
        await collector.add_event(label='my event', timestamp=ts, color='#ff0000', source='user_code')
        path = str(tmp_path / 'out.json')
        collector.export_json(path)
        loaded = MetricCollector.from_json(path)
        events = loaded.get_events()
        assert len(events) == 1
        assert events[0].label == 'my event'
        assert events[0].color == '#ff0000'
        assert events[0].source == 'user_code'

    @pytest.mark.asyncio
    async def test_span_events_preserved(self, tmp_path):
        collector = _empty_collector()
        start = datetime(2024, 6, 1, 12, 0, 0)
        end = datetime(2024, 6, 1, 12, 10, 0)
        await collector.add_event(label='span', timestamp=start, end_timestamp=end)
        path = str(tmp_path / 'out.json')
        collector.export_json(path)
        loaded = MetricCollector.from_json(path)
        ev = loaded.get_events()[0]
        assert ev.end_timestamp == end

    @pytest.mark.asyncio
    async def test_multiple_hosts_preserved(self, tmp_path):
        collector = _empty_collector()
        ts = datetime(2024, 6, 1, 12, 0, 0)
        await _inject_point(collector, 'host1', 'CPU %', 10.0, ts)
        await _inject_point(collector, 'host2', 'CPU %', 20.0, ts)
        path = str(tmp_path / 'out.json')
        collector.export_json(path)
        loaded = MetricCollector.from_json(path)
        assert 'host1/CPU %' in loaded.get_series()
        assert 'host2/CPU %' in loaded.get_series()

    @pytest.mark.asyncio
    async def test_to_json_is_valid_json(self):
        collector = _empty_collector()
        await _inject_point(collector, 'h1', 'CPU %', 50.0)
        raw = collector.to_json()
        parsed = json.loads(raw)
        assert 'metrics' in parsed
        assert 'events' in parsed

    def test_invalid_rows_skipped_gracefully(self, tmp_path):
        """from_json() must skip malformed entries without raising."""
        bad_json = json.dumps({
            'metrics': [
                {'timestamp': 'bad-date', 'host': 'h1', 'label': 'CPU %', 'value': 1.0},
                {'host': 'h1', 'label': 'CPU %', 'value': 1.0},  # missing timestamp
                {'timestamp': '2024-01-01T00:00:00', 'host': 'h1', 'label': 'CPU %', 'value': 'NaN-bad'},
                {'timestamp': '2024-01-01T00:00:00', 'host': 'h1', 'label': 'OK', 'value': 3.0},
            ],
            'events': [],
        })
        path = str(tmp_path / 'partial.json')
        Path(path).write_text(bad_json)
        collector = MetricCollector.from_json(path)
        series = collector.get_series()
        # Only the last valid row should be loaded
        assert sum(len(pts) for pts in series.values()) == 1


# ── Full live-to-historical pipeline ─────────────────────────────────────────

class TestLiveToHistoricalPipeline:
    """
    Simulate the full pipeline: write data during a 'live' run, then reload
    it in historical mode (as the monitor --file flag would do).
    """

    @pytest.mark.asyncio
    async def test_sqlite_write_then_reload(self, tmp_path):
        db_path = str(tmp_path / 'session.db')

        # Simulate a live run writing data
        live = await _empty_collector_with_db(db_path)
        ts1 = datetime(2024, 6, 1, 12, 0, 0)
        ts2 = datetime(2024, 6, 1, 12, 0, 5)
        await _inject_point(live, 'dut', 'CPU %', 30.0, ts1)
        await _inject_point(live, 'dut', 'CPU %', 35.0, ts2)
        await live.add_event(label='test start', timestamp=ts1, source='auto', color='#888888')
        await live.close_db()

        # Reload as historical
        historical = await MetricCollector.from_sqlite(db_path)
        series = historical.get_series()
        events = historical.get_events()

        assert 'dut/CPU %' in series
        assert len(series['dut/CPU %']) == 2
        assert len(events) == 1
        assert events[0].label == 'test start'

    @pytest.mark.asyncio
    async def test_json_write_then_reload(self, tmp_path):
        json_path = str(tmp_path / 'session.json')

        live = _empty_collector()
        ts = datetime(2024, 6, 1, 12, 0, 0)
        await _inject_point(live, 'dut', 'Memory %', 60.0, ts)
        await live.add_event(label='test pass', timestamp=ts, source='auto', color='#2ca02c')
        live.export_json(json_path)

        historical = MetricCollector.from_json(json_path)
        series = historical.get_series()
        events = historical.get_events()

        assert 'dut/Memory %' in series
        assert len(events) == 1
        assert events[0].color == '#2ca02c'


# ── Display host / URL resolution ────────────────────────────────────────────

class TestDisplayHost:
    def test_get_all_ips_returns_no_loopback(self):
        from otto.monitor.server import _get_all_ips
        ips = _get_all_ips()
        for ip in ips:
            assert not ip.startswith('127.'), f'Loopback address {ip} should be excluded'

    def test_url_does_not_contain_0000(self):
        from otto.monitor.server import MonitorServer
        from otto.monitor.collector import MetricCollector
        server = MonitorServer(MetricCollector(hosts=[]), host='0.0.0.0', port=9999)
        assert '0.0.0.0' not in server.url

    def test_urls_returns_one_per_interface_when_bound_to_all(self):
        from otto.monitor.server import MonitorServer, _get_all_ips
        from otto.monitor.collector import MetricCollector
        server = MonitorServer(MetricCollector(hosts=[]), host='0.0.0.0', port=9999)
        ips = _get_all_ips()
        if ips:
            assert len(server.urls) == len(ips)
            for u in server.urls:
                assert '0.0.0.0' not in u

    def test_specific_bind_returns_single_url(self):
        from otto.monitor.server import MonitorServer
        from otto.monitor.collector import MetricCollector
        server = MonitorServer(MetricCollector(hosts=[]), host='10.0.0.1', port=9999)
        assert server.urls == ['http://10.0.0.1:9999']
        assert server.url == 'http://10.0.0.1:9999'
