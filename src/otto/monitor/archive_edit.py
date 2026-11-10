"""Per-mutation read-write event edits against a v2 session archive.

Review mode holds no standing write connection — archives are cold files.
Each mutation opens the archive read-write under the same ``.lock`` flock a
live :class:`~otto.monitor.db.MetricDB` holds for its whole run, applies one
statement stamped with the target session id, commits, and closes. A held
lock means a live collector is writing this very file: refuse loud
(:class:`ArchiveLockedError` -> the server's 409), never queue behind it.

Synchronous by design (the sqlite3/`read_sessions` precedent); the server
calls these off the event loop via ``asyncio.to_thread``.
"""

import contextlib
import fcntl
import os
import sqlite3
from collections.abc import Generator
from datetime import datetime
from typing import Any

from ..errors import OttoError
from ..models.monitor import EventRecord
from ..utils import as_utc
from . import event_ops
from .db import (
    EVENT_ID_TAKEN_SQL,
    EVENT_INSERT_SQL,
    EVENT_RESTORE_SQL,
    EVENT_SELECT_COLUMNS,
    event_insert_params,
    event_restore_params,
    event_row_record,
)
from .event_ops import EventChanged, same_event


class ArchiveLockedError(OttoError, RuntimeError):
    """The archive's ``.lock`` is held — a live otto monitor is writing it."""


@contextlib.contextmanager
def _locked_connection(path: str) -> Generator[sqlite3.Connection, None, None]:
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            raise ArchiveLockedError(
                f"'{path}' is being written by a live otto monitor session; "
                "stop it (or wait for the run to finish) before editing the archive."
            ) from err
        conn = sqlite3.connect(path)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        # Closing the fd releases the flock if it was acquired.
        os.close(fd)


def _require_session(conn: sqlite3.Connection, path: str, session_id: str) -> None:
    if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
        raise LookupError(f"'{path}' holds no session '{session_id}'")


def insert_event(
    path: str,
    session_id: str,
    *,
    timestamp: datetime,
    end_timestamp: datetime | None,
    label: str,
    source: str,
    color: str,
    dash: str,
) -> int:
    """Insert one event row for *session_id*; returns the new rowid.

    A blank *label* raises :class:`~otto.monitor.event_ops.EventValidationError`
    (a ``ValueError``) before the archive is opened.
    """
    event_ops.check_label_write(None, label)
    with _locked_connection(path) as conn:
        _require_session(conn, path, session_id)
        cursor = conn.execute(
            EVENT_INSERT_SQL,
            event_insert_params(
                session_id,
                timestamp=as_utc(timestamp),
                end_timestamp=as_utc(end_timestamp),
                label=label,
                source=source,
                color=color,
                dash=dash,
            ),
        )
        conn.commit()
        rowid = cursor.lastrowid
        assert rowid is not None  # noqa: S101 — SQLite always sets lastrowid after INSERT
        return rowid


def _current(conn: sqlite3.Connection, session_id: str, event_id: int) -> EventRecord | None:
    """Read the event ``(session_id, event_id)`` as it stands, or ``None`` when absent."""
    row = conn.execute(
        f"SELECT {EVENT_SELECT_COLUMNS} FROM events WHERE id = ? AND session_id = ?",  # noqa: S608 — module constant
        (event_id, session_id),
    ).fetchone()
    return event_row_record(row) if row is not None else None


def _require_expected(
    conn: sqlite3.Connection, session_id: str, event_id: int, expect: EventRecord | None
) -> EventRecord | None:
    """Return the event's current state, raising :class:`EventChanged` unless it is ``expect``."""
    current = _current(conn, session_id, event_id)
    if not same_event(current, expect):
        raise EventChanged(current)
    return current


def _update_row(
    conn: sqlite3.Connection,
    session_id: str,
    event_id: int,
    fields: dict[str, Any],
    current: EventRecord,
) -> bool:
    """Run the one UPDATE of an event's editable fields (``fields`` is ``editable_fields``).

    ``current`` is the row as it stands: a label that differs from it must not
    be blank (:func:`~otto.monitor.event_ops.check_label_write`), checked
    before the UPDATE for every caller.
    """
    event_ops.check_label_write(current.label, fields["label"])
    cursor = conn.execute(
        "UPDATE events SET label = ?, color = ?, dash = ?, ts = ?, end_ts = ? "
        "WHERE id = ? AND session_id = ?",
        (
            fields["label"],
            fields["color"],
            fields["dash"],
            fields["timestamp"].isoformat(),
            fields["end_timestamp"].isoformat() if fields["end_timestamp"] else None,
            event_id,
            session_id,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def _delete_row(conn: sqlite3.Connection, session_id: str, event_id: int) -> bool:
    """Run the one DELETE of an event row."""
    cursor = conn.execute(
        "DELETE FROM events WHERE id = ? AND session_id = ?", (event_id, session_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def _restore_row(
    conn: sqlite3.Connection, session_id: str, event_id: int, write: EventRecord
) -> EventRecord:
    """Put ``write`` back under ``event_id``, if this session may reuse that id.

    Refused when the id is present, held by another session, or never handed out,
    or when ``write``'s label is blank (a restore introduces its label).
    """
    event_ops.check_label_write(None, write.label)
    _require_session(conn, "archive", session_id)
    present = _current(conn, session_id, event_id)
    if present is not None:
        raise EventChanged(present)
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
    if not event_ops.id_handed_out(event_id, high_water=seq[0] if seq is not None else 0):
        raise EventChanged(None)
    # Another session's event: not this session's to show, so `current` is None.
    if conn.execute(EVENT_ID_TAKEN_SQL, (event_id,)).fetchone() is not None:
        raise EventChanged(None)
    conn.execute(
        EVENT_RESTORE_SQL,
        event_restore_params(
            event_id,
            session_id,
            timestamp=write.timestamp,
            end_timestamp=write.end_timestamp,
            label=write.label,
            source=write.source,
            color=write.color,
            dash=write.dash,
        ),
    )
    conn.commit()
    restored = _current(conn, session_id, event_id)
    assert restored is not None  # noqa: S101 — just inserted under the same connection
    return restored


def update_event(
    path: str,
    session_id: str,
    event_id: int,
    *,
    timestamp: datetime,
    end_timestamp: datetime | None,
    label: str,
    color: str,
    dash: str,
) -> bool:
    """Overwrite an event's editable fields. False if (session, id) matches nothing.

    A *label* that differs from the stored one must not be blank
    (:class:`~otto.monitor.event_ops.EventValidationError`); one that keeps it
    passes, so a legacy blank-label row can still change colour or end.
    """
    fields = event_ops.ResolvedEventFields(
        label=label,
        color=color,
        dash=dash,
        timestamp=as_utc(timestamp),
        end_timestamp=as_utc(end_timestamp),
    )
    with _locked_connection(path) as conn:
        current = _current(conn, session_id, event_id)
        if current is None:
            return False
        return _update_row(conn, session_id, event_id, event_ops.editable_fields(fields), current)


def delete_event(path: str, session_id: str, event_id: int) -> EventRecord | None:
    """Delete one event row; return the removed row, or ``None`` if (session, id) matches none."""
    with _locked_connection(path) as conn:
        removed = _current(conn, session_id, event_id)
        if removed is not None:
            _delete_row(conn, session_id, event_id)
        return removed


def restore_event(path: str, session_id: str, event_id: int, write: EventRecord) -> EventRecord:
    """Put a deleted event back under its own id (the undo of a delete).

    Raises :class:`~otto.monitor.event_ops.EventChanged` when an event already
    has the id (carrying it) or the id was never handed out (the table's
    ``sqlite_sequence`` is the high-water mark), so a restore can neither
    overwrite an event nor claim an id a later create would collide with. A
    legacy colour or dash in ``write`` is written unchanged; a blank label is
    refused (:class:`~otto.monitor.event_ops.EventValidationError`).
    """
    with _locked_connection(path) as conn:
        return _restore_row(conn, session_id, event_id, write)


def revert_event(
    path: str,
    session_id: str,
    event_id: int,
    expect: EventRecord | None,
    write: EventRecord | None,
) -> EventRecord | None:
    """Move an event from ``expect`` to ``write``, only if it is still ``expect`` (#929).

    The review-mode twin of ``MetricCollector.revert_event``; the leg comes
    from :func:`~otto.monitor.event_ops.revert_leg`. Returns the stored row
    after an update or restore, the removed row after a delete. A missing
    event on the update or delete leg raises ``EventChanged(None)``, and a
    blank new label on the update or restore leg raises ``EventValidationError``.
    """
    leg = event_ops.revert_leg(expect, write)
    with _locked_connection(path) as conn:
        if leg == "restore":
            assert write is not None  # noqa: S101 — revert_leg's "restore" means write is set
            return _restore_row(conn, session_id, event_id, write)
        current = _require_expected(conn, session_id, event_id, expect)
        if current is None:
            raise EventChanged(None)
        if write is None:
            _delete_row(conn, session_id, event_id)
            return current
        _update_row(conn, session_id, event_id, event_ops.editable_fields(write), current)
        return _current(conn, session_id, event_id)
