"""Per-mutation read-write event edits against a v2 session archive (Plan 5c).

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
from collections.abc import Iterator
from datetime import datetime

from .db import EVENT_INSERT_SQL, event_insert_params


class ArchiveLockedError(RuntimeError):
    """The archive's ``.lock`` is held — a live otto monitor is writing it."""


@contextlib.contextmanager
def _locked_connection(path: str) -> Iterator[sqlite3.Connection]:
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
    """Insert one event row for *session_id*; returns the new rowid."""
    with _locked_connection(path) as conn:
        _require_session(conn, path, session_id)
        cursor = conn.execute(
            EVENT_INSERT_SQL,
            event_insert_params(
                session_id,
                timestamp=timestamp,
                end_timestamp=end_timestamp,
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
    """Overwrite an event's editable fields. False if (session, id) matches nothing."""
    with _locked_connection(path) as conn:
        cursor = conn.execute(
            "UPDATE events SET label = ?, color = ?, dash = ?, ts = ?, end_ts = ? "
            "WHERE id = ? AND session_id = ?",
            (
                label,
                color,
                dash,
                timestamp.isoformat(),
                end_timestamp.isoformat() if end_timestamp else None,
                event_id,
                session_id,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_event(path: str, session_id: str, event_id: int) -> bool:
    """Delete one event row. False if (session, id) matches nothing."""
    with _locked_connection(path) as conn:
        cursor = conn.execute(
            "DELETE FROM events WHERE id = ? AND session_id = ?", (event_id, session_id)
        )
        conn.commit()
        return cursor.rowcount > 0
