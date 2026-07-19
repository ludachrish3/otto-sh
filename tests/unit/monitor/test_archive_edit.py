"""Per-mutation read-write archive event edits (Plan 5c review-mode backend)."""

import fcntl
import os
from datetime import datetime, timedelta, timezone

import pytest
from _archive import _make_archive

from otto.monitor import archive_edit
from otto.monitor.db import read_sessions

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
