"""MetricDB — SQLite persistence for monitor metrics and events.

Owns the aiosqlite connection, the schema, the WAL-vs-DELETE journal choice
(network filesystems can't WAL), and the flock guard that stops two live
collectors writing one database. Extracted from MetricCollector; behavior is
identical.
"""

import fcntl
import logging
import os
from datetime import datetime

import aiosqlite

from ..filesystem import network_fs_type
from .events import MonitorEvent

logger = logging.getLogger("otto")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    host      TEXT    NOT NULL DEFAULT '',
    label     TEXT    NOT NULL,
    value     REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    end_ts    TEXT,
    label     TEXT    NOT NULL,
    source    TEXT    NOT NULL DEFAULT 'manual',
    color     TEXT    NOT NULL DEFAULT '#888888',
    dash      TEXT    NOT NULL DEFAULT 'dash'
);
"""


class MetricDB:
    """Persistent async SQLite store for one monitor database file."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock_fd: int | None = None

    async def open(self) -> None:
        """Acquire the flock guard, open the connection, apply schema + migration."""
        # Acquire an exclusive file lock so two live collectors can't
        # write to the same database simultaneously.
        lock_path = self._path + ".lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except OSError as err:
            raise RuntimeError(
                f"Another otto monitor instance is already writing to '{self._path}'. "
                "Use a different --db path, or stop the other instance."
            ) from err

        try:
            net_fstype = network_fs_type(self._path)
            journal_mode = "DELETE" if net_fstype else "WAL"
            if net_fstype:
                logger.debug(
                    "Monitor DB '%s' is on a network filesystem (%s); using "
                    "journal_mode=DELETE instead of WAL (WAL is unsupported over "
                    "network filesystems).",
                    self._path,
                    net_fstype,
                )
                logger.debug(
                    "Monitor DB lock guard on '%s' is same-host only on network "
                    "filesystems; for multi-machine setups sharing one DB, place it "
                    "on local disk.",
                    self._path,
                )

            conn = await aiosqlite.connect(self._path)
            await conn.execute(f"PRAGMA journal_mode={journal_mode}")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.executescript(_SCHEMA)
            # Migrate: add end_ts column if the events table predates span support
            col_names = {row[1] async for row in await conn.execute("PRAGMA table_info(events)")}
            if "end_ts" not in col_names:
                await conn.execute("ALTER TABLE events ADD COLUMN end_ts TEXT")
            await conn.commit()
            self._conn = conn
        except BaseException:
            # A failure after the lock was acquired must not leak the fd —
            # the caller discards this half-open instance, so nothing else
            # can release it.
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
            raise

    async def close(self) -> None:
        """Close the persistent DB connection and release the file lock."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    async def write_point(self, ts: datetime, host: str, label: str, value: float) -> None:
        """Insert one metric point. No-op if the connection is not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO metrics (ts, host, label, value) VALUES (?, ?, ?, ?)",
            (ts.isoformat(), host, label, value),
        )
        await self._conn.commit()

    async def write_event(self, event: MonitorEvent) -> int:
        """Insert event into the DB and return the rowid (0 if no DB configured)."""
        if not self._conn:
            return 0
        cursor = await self._conn.execute(
            "INSERT INTO events (ts, end_ts, label, source, color, dash) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.timestamp.isoformat(),
                event.end_timestamp.isoformat() if event.end_timestamp else None,
                event.label,
                event.source,
                event.color,
                event.dash,
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None  # noqa: S101 — internal invariant: SQLite always sets lastrowid after a successful INSERT
        return cursor.lastrowid

    async def delete_event(self, event_id: int) -> None:
        """Delete the event with the given id. No-op if the connection is not open."""
        if not self._conn:
            return
        await self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await self._conn.commit()

    async def update_event(self, event: MonitorEvent) -> None:
        """Update an existing event's label, color, dash, and end_ts. No-op if not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "UPDATE events SET label = ?, color = ?, dash = ?, end_ts = ? WHERE id = ?",
            (
                event.label,
                event.color,
                event.dash,
                event.end_timestamp.isoformat() if event.end_timestamp else None,
                event.id,
            ),
        )
        await self._conn.commit()
