"""#965: the library and archive write routes normalise a naive event timestamp to UTC."""

import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from _archive import _make_archive

from otto.models.monitor import EventRecord
from otto.monitor import archive_edit
from otto.monitor.collector import MetricCollector
from otto.monitor.db import event_row_record
from otto.monitor.event_ops import same_event
from otto.monitor.events import MonitorEvent

NAIVE = datetime(2026, 7, 18, 12, 0)  # noqa: DTZ001 — the naive input under test
AWARE = NAIVE.replace(tzinfo=timezone.utc)
PLUS2 = timezone(timedelta(hours=2))


def _persisted(path: str) -> list[tuple[str, str | None]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT ts, end_ts FROM events ORDER BY id").fetchall()


class TestMonitorEvent:
    def test_naive_timestamps_mean_utc(self) -> None:
        event = MonitorEvent(timestamp=NAIVE, label="x", end_timestamp=NAIVE + timedelta(hours=1))
        assert event.timestamp == AWARE
        assert event.end_timestamp == AWARE + timedelta(hours=1)
        assert event.timestamp.tzinfo is not None
        assert event.end_timestamp is not None
        assert event.end_timestamp.tzinfo is not None

    def test_an_existing_offset_is_preserved(self) -> None:
        stamp = NAIVE.replace(tzinfo=PLUS2)
        event = MonitorEvent(timestamp=stamp, label="x", end_timestamp=stamp)
        assert event.timestamp.utcoffset() == timedelta(hours=2)
        assert event.end_timestamp is not None
        assert event.end_timestamp.utcoffset() == timedelta(hours=2)

    def test_replace_renormalises(self) -> None:
        event = replace(MonitorEvent(timestamp=AWARE, label="x"), end_timestamp=NAIVE)
        assert event.end_timestamp == AWARE


@pytest.mark.asyncio
async def test_add_event_stores_an_aware_timestamp() -> None:
    collector = MetricCollector(hosts=[])
    event = await collector.add_event(
        label="x", timestamp=NAIVE, end_timestamp=NAIVE + timedelta(hours=1)
    )
    assert event.timestamp == AWARE
    assert event.to_dict()["timestamp"] == "2026-07-18T12:00:00+00:00"


def _insert(path: str, sid: str, timestamp: datetime, end_timestamp: datetime | None) -> int:
    return archive_edit.insert_event(
        path,
        sid,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )


class TestArchiveWritesPersistAwareText:
    def test_insert_event_persists_the_utc_offset(self, tmp_path) -> None:
        path, sid = _make_archive(tmp_path)
        _insert(path, sid, NAIVE, NAIVE + timedelta(hours=1))
        assert _persisted(path) == [("2026-07-18T12:00:00+00:00", "2026-07-18T13:00:00+00:00")]

    def test_insert_event_keeps_an_existing_offset(self, tmp_path) -> None:
        path, sid = _make_archive(tmp_path)
        _insert(path, sid, NAIVE.replace(tzinfo=PLUS2), None)
        assert _persisted(path) == [("2026-07-18T12:00:00+02:00", None)]

    def test_update_event_persists_the_utc_offset(self, tmp_path) -> None:
        path, sid = _make_archive(tmp_path)
        rowid = _insert(path, sid, AWARE, None)
        assert archive_edit.update_event(
            path,
            sid,
            rowid,
            timestamp=NAIVE,
            end_timestamp=NAIVE + timedelta(hours=1),
            label="x",
            color="#888888",
            dash="dash",
        )
        assert _persisted(path) == [("2026-07-18T12:00:00+00:00", "2026-07-18T13:00:00+00:00")]


def test_a_legacy_naive_stored_row_reads_as_utc_and_still_matches_a_revert_expect() -> None:
    row = (7, "2026-07-18T12:00:00", "2026-07-18T13:00:00", "old", "manual", "#888888", "dash")
    record = event_row_record(row)
    assert record.timestamp == AWARE
    assert record.end_timestamp == AWARE + timedelta(hours=1)
    expect = EventRecord(
        id=7,
        timestamp=NAIVE,
        end_timestamp=NAIVE + timedelta(hours=1),
        label="old",
        source="manual",
        color="#888888",
        dash="dash",
    )
    assert same_event(expect, record)


def _legacy_naive_archive(tmp_path) -> str:
    """A finalized archive rewritten as a pre-#965 one: every timestamp column naive."""
    path, sid = _make_archive(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE sessions SET start = ?, end = ? WHERE id = ?",
            ("2026-07-18T12:00:00", "2026-07-18T13:00:00", sid),
        )
        conn.execute(
            "INSERT INTO metrics (session_id, ts, host, label, value) VALUES (?, ?, 'h', 'cpu', 1)",
            (sid, "2026-07-18T12:10:00"),
        )
        conn.execute(
            "INSERT INTO events (session_id, ts, end_ts, label) VALUES (?, ?, ?, 'old')",
            (sid, "2026-07-18T12:30:00", "2026-07-18T12:40:00"),
        )
        conn.execute(
            "INSERT INTO log_events (session_id, ts, host, tab) VALUES (?, ?, 'h', 'log')",
            (sid, "2026-07-18T12:20:00"),
        )
    return path


def _assert_every_timestamp_is_utc(document_text: str) -> None:
    """Every timestamp the served document carries is aware UTC (``Z``), none naive."""
    import json

    session = json.loads(document_text)["sessions"][0]
    stamps = [session["start"], session["end"]]
    stamps += [m["timestamp"] for m in session["metrics"]]
    stamps += [e["timestamp"] for e in session["events"]]
    stamps += [e["end_timestamp"] for e in session["events"] if "end_timestamp" in e]
    stamps += [e["timestamp"] for e in session["log_events"]]
    assert len(stamps) == 6
    assert all(stamp.endswith("Z") for stamp in stamps), stamps


@pytest.mark.parametrize("suffix", [".db", ".json"])
def test_a_legacy_naive_archive_serves_bounds_samples_and_events_as_utc(
    tmp_path, suffix: str
) -> None:
    """Finding 1 of the branch review: naive means UTC for EVERY timestamp a review
    serves, not only events, so a legacy event stays inside its capture window."""
    from pathlib import Path

    from otto.monitor.export import document_json
    from otto.monitor.review import load_review_document

    db_path = Path(_legacy_naive_archive(tmp_path))
    source = db_path
    if suffix == ".json":
        # A .json export written before #965 spelled every timestamp naive.
        source = tmp_path / "legacy.json"
        aware = document_json(load_review_document(path=db_path))
        source.write_text(re.sub(r"(T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z", r"\1", aware))
    _assert_every_timestamp_is_utc(document_json(load_review_document(path=source)))
