"""MetricDB schema v2 — session-bound writes, multi-session archives,
fail-loud on anything that is not v2 (spec 2026-07-12: legacy read DROPPED)."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from otto.monitor.db import MetricDB, UnsupportedDBError, read_sessions
from otto.monitor.events import MonitorEvent
from otto.monitor.session import new_frame

UTC = timezone.utc
T0 = datetime(2026, 7, 12, 8, 0, 0, tzinfo=UTC)


def frame_at(label, minutes=0):
    return new_frame(label=label, note=f"note for {label}", now=T0 + timedelta(minutes=minutes))


CHART_MAP_JSON = '{"Overall CPU": "CPU", "proc/1234": "CPU"}'
"""A realistic chart_map: parser labels that do NOT equal their chart's label.

That inequality is the whole point of persisting the map — see
test_chart_map_round_trips.
"""


async def write_session(path, label, minutes=0, finalize=True, chart_map_json=CHART_MAP_JSON):
    db = MetricDB(
        str(path),
        frame_at(label, minutes),
        lab_json="{}",
        meta_json="{}",
    )
    await db.open()
    # chart_map is NOT a constructor seed: it is empty at open() by
    # construction and gets UPDATEd as labels appear (the collector does this
    # per new label — see MetricDB.write_chart_map).
    await db.write_chart_map(chart_map_json)
    await db.write_point(T0 + timedelta(minutes=minutes), "h1", "CPU %", 42.0)
    await db.write_log_event(T0 + timedelta(minutes=minutes), "h1", "kernel", {"msg": "x"})
    rowid = await db.write_event(
        MonitorEvent(timestamp=T0 + timedelta(minutes=minutes), label="mark", source="manual")
    )
    assert rowid > 0
    if finalize:
        await db.finalize(T0 + timedelta(minutes=minutes + 5))
    await db.close()


@pytest.mark.asyncio
async def test_open_creates_session_row_and_writes_stamp_session_id(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "run-1")
    sessions = read_sessions(str(path))
    assert [s.label for s in sessions] == ["run-1"]
    s = sessions[0]
    assert s.note == "note for run-1"
    assert s.end is not None
    assert len(s.metrics) == 1
    assert len(s.events) == 1
    assert len(s.log_events) == 1


@pytest.mark.asyncio
async def test_chart_map_round_trips(tmp_path):
    """The series-label -> chart-group map must survive the archive.

    Nothing else in a session can reconstruct it: SessionMeta.charts is one
    entry per CHART, while metric rows carry per-SERIES labels, and the two
    are rarely equal ("Overall CPU"/"proc/1234" both live in chart "CPU").
    Without this column the producer emits chart_map={}, and the frontend's
    `chartMap[label] ?? label` fallback (web/src/data/seriesTree.ts) turns
    every series into its own ungrouped, unit-less chart.

    This is the DB-level plumbing check; the end-to-end proof that a real
    collector run actually FILLS it lives in test_export_producer.py's
    test_db_export_chart_map_survives_a_real_collector_run.
    """
    path = tmp_path / "lab.db"
    await write_session(path, "run-1")
    (session,) = read_sessions(str(path))
    assert json.loads(session.chart_map_json) == {"Overall CPU": "CPU", "proc/1234": "CPU"}


@pytest.mark.asyncio
async def test_open_seeds_an_empty_chart_map(tmp_path):
    """open() must seed '{}' — the map cannot be known before the first tick."""
    path = tmp_path / "lab.db"
    db = MetricDB(str(path), frame_at("fresh"), lab_json="{}", meta_json="{}")
    await db.open()
    await db.close()
    (session,) = read_sessions(str(path))
    assert json.loads(session.chart_map_json) == {}


@pytest.mark.asyncio
async def test_multi_session_append_preserves_prior_sessions(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "run-1", minutes=0)
    await write_session(path, "run-2", minutes=60)
    sessions = read_sessions(str(path))
    assert [s.label for s in sessions] == ["run-1", "run-2"]
    assert all(len(s.metrics) == 1 for s in sessions)  # rows partitioned, not shared


@pytest.mark.asyncio
async def test_crash_leaves_end_null(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "crashed", finalize=False)
    (session,) = read_sessions(str(path))
    assert session.end is None  # reader-side fallback is the PRODUCER's job (Task 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "abort_at",
    ["PRAGMA user_version =", "INSERT INTO sessions"],
    ids=["before-version-stamp", "before-session-row"],
)
async def test_cancelled_open_does_not_poison_the_file(tmp_path, abort_at):
    """A cancelled/interrupted ``open()`` must leave the file as it found it.

    Follow-up to the #136-wave race: open()'s writes used to run outside any
    transaction (executescript autocommits DDL), so a cancellation landing
    after the CREATE TABLEs but before the version stamp left ``(tables,
    user_version=0)`` durable on disk — exactly the state ``_check_version``
    refuses as "pre-session schema". One interrupted startup then poisoned
    the --db path for every later run. With open()'s schema + version stamp
    + session row in a single transaction, nothing of an aborted open
    survives, and the next open of the same path succeeds.
    """
    import aiosqlite

    path = tmp_path / "lab.db"
    real_execute = aiosqlite.Connection.execute

    def aborting_execute(self, sql, parameters=None):
        # Raising at the call site stands in for a task cancellation being
        # delivered at this await point inside open().
        if sql.startswith(abort_at):
            raise asyncio.CancelledError
        return real_execute(self, sql, parameters)

    db = MetricDB(str(path), frame_at("victim"), lab_json="{}", meta_json="{}")
    with (
        patch.object(aiosqlite.Connection, "execute", aborting_execute),
        pytest.raises(asyncio.CancelledError),
    ):
        await db.open()

    # The interrupted open must not have poisoned the path: a later run
    # pointed at the same --db file opens it and archives normally.
    await write_session(path, "survivor")
    (session,) = read_sessions(str(path))
    assert session.label == "survivor"
    assert session.end is not None


@pytest.mark.asyncio
async def test_legacy_flat_db_refused_loud(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE metrics (id INTEGER PRIMARY KEY, ts TEXT, host TEXT, label TEXT, value REAL)"
    )
    conn.commit()
    conn.close()
    db = MetricDB(str(path), frame_at("x"), lab_json="{}", meta_json="{}")
    with pytest.raises(UnsupportedDBError, match=r"pre-session schema.*not supported"):
        await db.open()
    with pytest.raises(UnsupportedDBError, match=r"pre-session schema.*not supported"):
        read_sessions(str(path))


@pytest.mark.asyncio
async def test_pre_column_v2_db_refused_loud(tmp_path):
    """A v2 db from before chart_map_json existed must fail loud, not raw-sqlite.

    chart_map_json was added to v2 in place (pre-release, no version bump), so
    user_version alone cannot tell the two shapes apart — and CREATE TABLE IF
    NOT EXISTS will not add the column to an existing table. Without the column
    guard these databases (which exist on this branch) died on the first SELECT
    with sqlite3.OperationalError instead of otto's own error.
    """
    path = tmp_path / "precolumn.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, label TEXT, note TEXT, start TEXT, "
        "end TEXT, lab_json TEXT, meta_json TEXT)"  # no chart_map_json
    )
    conn.commit()
    conn.close()

    with pytest.raises(UnsupportedDBError, match="no chart_map_json column"):
        read_sessions(str(path))
    db = MetricDB(str(path), frame_at("x"), lab_json="{}", meta_json="{}")
    with pytest.raises(UnsupportedDBError, match="no chart_map_json column"):
        await db.open()


@pytest.mark.asyncio
async def test_future_schema_version_refused_loud(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.execute("CREATE TABLE sessions (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(UnsupportedDBError, match="schema version 99"):
        read_sessions(str(path))


def test_read_sessions_missing_file_raises(tmp_path):
    with pytest.raises(UnsupportedDBError, match="not a monitor database"):
        read_sessions(str(tmp_path / "nope.db"))


def test_read_sessions_uninitialized_file_raises(tmp_path):
    """An existing-but-empty file has no sessions table — fail loud, never leak sqlite3.

    _check_version's tables-empty leniency is deliberate for open() (a fresh
    file must be allowed to initialize), but read_sessions has no legitimate
    "about to initialize" case: without this guard the SELECT below raised a
    bare sqlite3.OperationalError("no such table: sessions") at the caller.
    """
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(UnsupportedDBError, match="not a monitor database"):
        read_sessions(str(path))


def test_read_sessions_non_sqlite_bytes_raises_unsupported(tmp_path):
    """A truncated/garbage file (e.g. a botched ``scp`` copy) must fail loud.

    sqlite3.connect() is lazy — the file-header check only happens on the
    first PRAGMA, which raises sqlite3.DatabaseError ("file is not a
    database"). That is the PARENT class of OperationalError (the only one
    the old code caught), so it used to escape read_sessions entirely as a
    raw sqlite3 error and blow up the CLI with a traceback instead of otto's
    own fail-loud UnsupportedDBError.
    """
    path = tmp_path / "garbage.db"
    path.write_bytes(b"not a sqlite database at all, just garbage bytes")
    with pytest.raises(UnsupportedDBError, match="not a monitor database"):
        read_sessions(str(path))


@pytest.mark.asyncio
async def test_metric_db_open_non_sqlite_bytes_raises_unsupported(tmp_path):
    """``MetricDB.open()`` has the SAME lazy-connect gap against a corrupted
    existing file (the ``--live --db`` path) — must fail the same way, not
    with a raw sqlite3.DatabaseError.
    """
    path = tmp_path / "garbage.db"
    path.write_bytes(b"not a sqlite database at all, just garbage bytes")
    db = MetricDB(str(path), frame_at("x"), lab_json="{}", meta_json="{}")
    with pytest.raises(UnsupportedDBError, match="not a monitor database"):
        await db.open()
