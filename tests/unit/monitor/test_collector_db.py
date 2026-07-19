"""
Unit/component tests for MetricCollector's schema-v2 SQLite persistence.

These tests exercise the full read/write round-trip without any SSH connections
or running event loops.  They are the primary coverage for the database
integration the user noted had never been tested.

Covers:
  - Schema initialisation when a db_path is provided
  - Incremental metric writes via _db_write_point()
  - Event writes, updates, and deletes via add_event() / update_event() / delete_event()
  - Span events (end_timestamp)
  - WAL mode and busy_timeout are set correctly
  - Instance locking prevents two writers on the same database (and that the
    read-only v2 archive reader, read_sessions(), ignores that lock)
  - Display host resolution for MonitorServer

The legacy JSON/pre-session-SQLite persistence formats (``MetricCollector.from_json``/
``from_sqlite``/``export_json``/``to_json``, ``otto.monitor.history``) were retired in
the sessionized-producer cutover (spec 2026-07-12) — schema v2 round-tripping is
covered by ``test_db_v2.py`` and ``test_export_producer.py`` instead.
"""

import itertools
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.models import MetricPoint
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB, read_sessions
from otto.monitor.parsers import MemParser, PerCoreCpuParser
from otto.monitor.session import new_frame

# ── Helpers ───────────────────────────────────────────────────────────────────

_PROC_FD = Path("/proc/self/fd")


def _open_fd_count() -> int:
    """Number of fds this process currently holds open (Linux /proc)."""
    return sum(1 for _ in _PROC_FD.iterdir())


# new_frame's default (wall-clock) id has 1-second granularity; a test that
# opens+closes+reopens a MetricDB on the same path within one wall-clock
# second would otherwise mint two frames with the same id and collide on
# the sessions.id PRIMARY KEY. Anonymous test frames count off a fixed,
# always-advancing epoch instead so sequential opens never collide.
_frame_clock = itertools.count()
_FRAME_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _anon_frame():
    return new_frame(
        label=None, note=None, now=_FRAME_EPOCH + timedelta(seconds=next(_frame_clock))
    )


def _empty_collector(db_path: str | None = None) -> MetricCollector:
    """Return a MetricCollector with no hosts (historical / test mode).

    DB is NOT initialised — call ``await collector.init_db()`` separately
    for tests that need the database. *db_path*, when given, is wrapped in a
    session-bound MetricDB with an anonymous frame — these tests only care
    about the write/read plumbing, not session identity.
    """
    db = (
        MetricDB(db_path, _anon_frame(), lab_json="{}", meta_json="{}")
        if db_path is not None
        else None
    )
    return MetricCollector(hosts=[], parsers=[PerCoreCpuParser(), MemParser()], db=db)


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
    ts = ts or datetime.now(tz=timezone.utc)
    key = f"{host}/{label}"
    from collections import deque

    if key not in collector._store.series:
        collector._store.series[key] = deque()
    collector._store.series[key].append(MetricPoint(ts=ts, value=value, meta=None))
    if collector._db:
        await collector._db.write_point(ts, host, label, value)


# ── Schema initialisation ─────────────────────────────────────────────────────


class TestSchemaInit:
    @pytest.mark.asyncio
    async def test_creates_metrics_table(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "metrics" in tables
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_creates_events_table(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "events" in tables
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_metrics_table_has_expected_columns(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(metrics)")}
        assert {"id", "ts", "host", "label", "value"}.issubset(cols)
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_events_table_has_end_ts_column(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "end_ts" in cols
        await collector.close_db()


# ── WAL mode and busy_timeout ────────────────────────────────────────────────


class TestWalMode:
    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        await collector.close_db()

    @pytest.mark.asyncio
    async def test_busy_timeout_set(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        assert collector._db is not None
        cursor = await collector._db._conn.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 5000
        await collector.close_db()


# ── Instance locking ─────────────────────────────────────────────────────────


class TestInstanceLock:
    @pytest.mark.asyncio
    async def test_second_instance_raises(self, tmp_path):
        db_path = str(tmp_path / "locked.db")
        collector_a = await _empty_collector_with_db(db_path)
        with pytest.raises(RuntimeError, match="Another otto monitor instance"):
            await _empty_collector_with_db(db_path)
        await collector_a.close_db()

    @pytest.mark.skipif(not _PROC_FD.is_dir(), reason="fd accounting needs Linux /proc")
    @pytest.mark.asyncio
    async def test_refused_instance_does_not_leak_the_lock_fd(self, tmp_path):
        """The contended path opens an fd before flock fails — it must close it.

        ``self._lock_fd`` is only assigned on a SUCCESSFUL flock, so a leaked
        fd here is unreclaimable: ``close()`` never sees it and every refused
        instance burns one for the life of the process.
        """
        db_path = str(tmp_path / "locked.db")
        collector_a = await _empty_collector_with_db(db_path)
        try:
            before = _open_fd_count()
            for _ in range(5):
                with pytest.raises(RuntimeError, match="Another otto monitor instance"):
                    await _empty_collector_with_db(db_path)
            after = _open_fd_count()
            # Pre-fix this leaked exactly one fd per refusal (after == before + 5).
            assert after <= before + 1, f"leaked {after - before} fds across 5 refusals"
        finally:
            await collector_a.close_db()

    @pytest.mark.asyncio
    async def test_lock_released_on_close(self, tmp_path):
        db_path = str(tmp_path / "locked.db")
        collector_a = await _empty_collector_with_db(db_path)
        await collector_a.close_db()
        # Should succeed now that the lock is released
        collector_b = await _empty_collector_with_db(db_path)
        await collector_b.close_db()

    @pytest.mark.asyncio
    async def test_read_only_loader_ignores_lock(self, tmp_path):
        db_path = str(tmp_path / "locked.db")
        collector_a = await _empty_collector_with_db(db_path)
        # read_sessions() is a read-only, non-flock reader (plain sqlite3,
        # opened `mode=ro`) — it must not be blocked by the write lock
        # collector_a still holds via MetricDB's flock guard.
        sessions = read_sessions(db_path)
        assert len(sessions) == 1
        assert sessions[0].metrics == []
        await collector_a.close_db()

    @pytest.mark.asyncio
    async def test_open_failure_after_lock_releases_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure between lock-acquire and connect must not leak the flock."""
        from otto.monitor import db as db_mod

        # Save the original connect before patching
        real_connect = db_mod.aiosqlite.connect
        call_count = {"n": 0}

        async def _selective_boom(path: str) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("connect failed")
            # Second call and beyond: use the real connect
            return await real_connect(path)

        monkeypatch.setattr(db_mod.aiosqlite, "connect", _selective_boom)
        db_path = str(tmp_path / "m.db")
        failing = db_mod.MetricDB(
            db_path,
            new_frame(label=None, note=None),
            lab_json="{}",
            meta_json="{}",
        )
        with pytest.raises(RuntimeError, match="connect failed"):
            await failing.open()
        # After a failure during open(), the lock_fd must be cleaned up
        # so the caller (or a retry) can acquire the lock on the same path.
        assert failing._lock_fd is None

        # The lock must be free: a fresh open on the same path succeeds.
        db = db_mod.MetricDB(
            db_path,
            new_frame(label=None, note=None),
            lab_json="{}",
            meta_json="{}",
        )
        await db.open()
        await db.close()


# ── Metric persistence ────────────────────────────────────────────────────────


class TestMetricPersistence:
    @pytest.mark.asyncio
    async def test_metric_written_to_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        await _inject_point(collector, "router1", "CPU %", 42.5, ts)
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = list(conn.execute("SELECT ts, host, label, value FROM metrics"))
        assert len(rows) == 1
        assert rows[0][1] == "router1"
        assert rows[0][2] == "CPU %"
        assert rows[0][3] == pytest.approx(42.5)

    @pytest.mark.asyncio
    async def test_multiple_metrics_written(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        await _inject_point(collector, "h1", "CPU %", 10.0)
        await _inject_point(collector, "h1", "Memory %", 60.0)
        await _inject_point(collector, "h2", "CPU %", 20.0)
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            count = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        assert count == 3

    @pytest.mark.asyncio
    async def test_no_db_does_not_raise(self):
        collector = _empty_collector(db_path=None)
        # Should silently do nothing when no db_path
        await _inject_point(collector, "h1", "CPU %", 50.0)
        assert collector.get_series()


# ── Event persistence ─────────────────────────────────────────────────────────


class TestEventPersistence:
    @pytest.mark.asyncio
    async def test_event_written_to_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        ts = datetime(2024, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        await collector.add_event(label="test start", timestamp=ts, color="#888888", source="auto")
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = list(conn.execute("SELECT label, source, color FROM events"))
        assert len(rows) == 1
        assert rows[0][0] == "test start"
        assert rows[0][1] == "auto"

    @pytest.mark.asyncio
    async def test_span_event_written_with_end_ts(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        start = datetime(2024, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 6, 1, 13, 5, 0, tzinfo=timezone.utc)
        await collector.add_event(label="my span", timestamp=start, end_timestamp=end)
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = list(conn.execute("SELECT end_ts FROM events"))
        assert rows[0][0] == end.isoformat()

    @pytest.mark.asyncio
    async def test_event_updated_in_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        event = await collector.add_event(label="old label", color="#888888", dash="dash")
        await collector.update_event(
            event.id,
            label="new label",
            color="#ff0000",
            dash="solid",
            timestamp=event.timestamp,
        )
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT label, color, dash FROM events WHERE id = ?", (event.id,)
            ).fetchone()
        assert row[0] == "new label"
        assert row[1] == "#ff0000"
        assert row[2] == "solid"

    @pytest.mark.asyncio
    async def test_event_deleted_from_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        collector = await _empty_collector_with_db(db_path)
        event = await collector.add_event(label="to delete")
        await collector.delete_event(event.id)
        await collector.close_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 0


# ── Display host / URL resolution ────────────────────────────────────────────


class TestDisplayHost:
    def test_get_all_ips_returns_no_loopback(self):
        from otto.monitor.server import _get_all_ips

        ips = _get_all_ips()
        for ip in ips:
            assert not ip.startswith("127."), f"Loopback address {ip} should be excluded"

    def test_url_does_not_contain_0000(self):
        from otto.monitor.collector import MetricCollector
        from otto.monitor.server import MonitorServer

        server = MonitorServer(MetricCollector(hosts=[]), host="0.0.0.0", port=9999)
        assert "0.0.0.0" not in server.url

    def test_urls_returns_one_per_interface_when_bound_to_all(self):
        from otto.monitor.collector import MetricCollector
        from otto.monitor.server import MonitorServer, _get_all_ips

        server = MonitorServer(MetricCollector(hosts=[]), host="0.0.0.0", port=9999)
        ips = _get_all_ips()
        if ips:
            assert len(server.urls) == len(ips)
            for u in server.urls:
                assert "0.0.0.0" not in u

    def test_specific_bind_returns_single_url(self):
        from otto.monitor.collector import MetricCollector
        from otto.monitor.server import MonitorServer

        server = MonitorServer(MetricCollector(hosts=[]), host="10.0.0.1", port=9999)
        assert server.origin == "http://10.0.0.1:9999"
        assert server.urls == [f"http://10.0.0.1:9999/?key={server.key}"]
        assert server.url == f"http://10.0.0.1:9999/?key={server.key}"
