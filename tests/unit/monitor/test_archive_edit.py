"""Per-mutation read-write archive event edits (Plan 5c review-mode backend)."""

import fcntl
import os
from datetime import datetime, timedelta, timezone

import pytest
from _archive import _add_session, _make_archive

from otto.models.monitor import EventRecord
from otto.monitor import archive_edit
from otto.monitor.db import event_row_record, read_sessions
from otto.monitor.event_ops import EventChanged

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def test_insert_round_trips_through_read_sessions(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path,
        sid,
        timestamp=T0,
        end_timestamp=None,
        label="manual mark",
        source="manual",
        color="#888888",
        dash="dash",
    )
    [row] = read_sessions(path)
    [(event_id, _ts, end_ts, label, _source, _color, _dash)] = row.events
    assert event_id == rowid
    assert label == "manual mark"
    assert end_ts is None


def test_update_and_delete_by_session_and_id(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path,
        sid,
        timestamp=T0,
        end_timestamp=None,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )
    assert archive_edit.update_event(
        path,
        sid,
        rowid,
        timestamp=T0,
        end_timestamp=T0 + timedelta(minutes=2),
        label="renamed",
        color="#2ca02c",
        dash="solid",
    )
    [row] = read_sessions(path)
    [(_, _, end_ts, label, *_rest)] = row.events
    assert label == "renamed"
    assert end_ts is not None
    assert archive_edit.delete_event(path, sid, rowid)
    assert read_sessions(path)[0].events == []


def test_wrong_session_is_refused(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    with pytest.raises(LookupError):
        archive_edit.insert_event(
            path,
            "not-a-session",
            timestamp=T0,
            end_timestamp=None,
            label="x",
            source="manual",
            color="#888888",
            dash="dash",
        )
    rowid = archive_edit.insert_event(
        path,
        sid,
        timestamp=T0,
        end_timestamp=None,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )
    assert not archive_edit.update_event(
        path,
        "not-a-session",
        rowid,
        timestamp=T0,
        end_timestamp=None,
        label="y",
        color="#888888",
        dash="dash",
    )
    assert not archive_edit.delete_event(path, "not-a-session", rowid)


def test_held_lock_raises_archive_locked(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(archive_edit.ArchiveLockedError):
            archive_edit.insert_event(
                path,
                sid,
                timestamp=T0,
                end_timestamp=None,
                label="x",
                source="manual",
                color="#888888",
                dash="dash",
            )
    finally:
        os.close(fd)


def _mark(path, sid, *, color="#888888", label="x") -> int:
    return archive_edit.insert_event(
        path,
        sid,
        timestamp=T0,
        end_timestamp=None,
        label=label,
        source="manual",
        color=color,
        dash="dash",
    )


def _record(path, sid, rowid) -> EventRecord:
    [row] = read_sessions(path)
    [found] = [event_row_record(e) for e in row.events if e[0] == rowid]
    return found


def _edit_behind_the_servers_back(path, label) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("UPDATE events SET label = ?", (label,))
    conn.commit()
    conn.close()


def test_revert_update_compares_the_actual_row(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    old = _record(path, sid, rowid)
    _edit_behind_the_servers_back(path, "elsewhere")
    with pytest.raises(EventChanged) as caught:
        archive_edit.revert_event(path, sid, rowid, old, old.model_copy(update={"label": "w"}))
    assert caught.value.current is not None
    assert caught.value.current.label == "elsewhere"
    assert _record(path, sid, rowid).label == "elsewhere"


def test_revert_restores_a_legacy_colour(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid, color="red")
    legacy = _record(path, sid, rowid)
    assert archive_edit.update_event(
        path,
        sid,
        rowid,
        timestamp=T0,
        end_timestamp=None,
        label="x",
        color="#ff0000",
        dash="dash",
    )
    edited = _record(path, sid, rowid)
    result = archive_edit.revert_event(path, sid, rowid, edited, legacy)
    assert result is not None
    assert result.color == "red"
    assert _record(path, sid, rowid).color == "red"


def test_delete_returns_the_removed_row(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    before = _record(path, sid, rowid)
    assert archive_edit.delete_event(path, sid, rowid) == before
    assert archive_edit.delete_event(path, sid, rowid) is None


def test_revert_delete_leg_with_a_stale_expect_refuses(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    old = _record(path, sid, rowid)
    _edit_behind_the_servers_back(path, "elsewhere")
    with pytest.raises(EventChanged):
        archive_edit.revert_event(path, sid, rowid, old, None)
    assert _record(path, sid, rowid).label == "elsewhere"


def test_revert_delete_then_restore_round_trips(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    row = _record(path, sid, rowid)
    assert archive_edit.revert_event(path, sid, rowid, row, None) == row
    assert read_sessions(path)[0].events == []
    assert archive_edit.revert_event(path, sid, rowid, None, row) == row
    assert _record(path, sid, rowid) == row


def test_restore_refuses_a_present_id(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    row = _record(path, sid, rowid)
    with pytest.raises(EventChanged) as caught:
        archive_edit.restore_event(path, sid, rowid, row)
    assert caught.value.current == row


def test_restore_refuses_above_high_water(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    row = _record(path, sid, rowid)
    with pytest.raises(EventChanged) as caught:
        archive_edit.restore_event(path, sid, rowid + 5, row)
    assert caught.value.current is None
    assert len(read_sessions(path)[0].events) == 1


def test_restore_refuses_an_id_another_session_holds(tmp_path) -> None:
    """Ids are unique across the archive: an id session A holds is never
    restored into session B (it was an IntegrityError, a 500)."""
    path, sid_a = _make_archive(tmp_path)
    sid_b = _add_session(path, T0 + timedelta(days=1))
    rowid = _mark(path, sid_a)
    before = {s.id: s.events for s in read_sessions(path)}
    [row_a] = [event_row_record(e) for e in before[sid_a]]
    with pytest.raises(EventChanged) as caught:
        archive_edit.restore_event(path, sid_b, rowid, row_a.model_copy(update={"label": "b"}))
    assert caught.value.current is None
    assert {s.id: s.events for s in read_sessions(path)} == before


def test_revert_of_a_missing_event_raises_changed_none(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    row = _record(path, sid, rowid)
    archive_edit.delete_event(path, sid, rowid)
    with pytest.raises(EventChanged) as caught:
        archive_edit.revert_event(path, sid, rowid, row, row)
    assert caught.value.current is None


def test_locked_archive_raises_archive_locked(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = _mark(path, sid)
    row = _record(path, sid, rowid)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(archive_edit.ArchiveLockedError):
            archive_edit.revert_event(path, sid, rowid, row, None)
    finally:
        os.close(fd)
