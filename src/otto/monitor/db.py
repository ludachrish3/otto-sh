"""MetricDB — SQLite persistence for monitor metrics and events.

Owns the aiosqlite connection, the schema, the WAL-vs-DELETE journal choice
(network filesystems can't WAL), and the flock guard that stops two live
collectors writing one database. Extracted from MetricCollector; behavior is
identical.

Schema v2 is session-scoped: one physical database file can hold many
sessions (one per live run, appended over time), and every row is stamped
with the session it belongs to. There is no migration path from the
pre-session (v1) schema — opening or reading anything that is not v2 fails
loud (:class:`UnsupportedDBError`); see :func:`read_sessions`.
"""

import contextlib
import fcntl
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

from ..filesystem import network_fs_type
from .events import MonitorEvent
from .session import SessionFrame

logger = logging.getLogger(__name__)

_SCHEMA = """
-- chart_map_json was added to v2 IN PLACE, before v2 ever shipped — there is
-- deliberately no migration and no version bump to hunt for. It is not
-- derivable from anything else in a session (meta_json's charts are one entry
-- per CHART; metric rows carry per-SERIES labels, and the two rarely match),
-- and without it the producer emits chart_map={} and the frontend charts every
-- series separately (see otto/monitor/export.py, web/src/data/seriesTree.ts).
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT    PRIMARY KEY,
    label          TEXT,
    note           TEXT,
    start          TEXT    NOT NULL,
    end            TEXT,
    lab_json       TEXT    NOT NULL DEFAULT '{}',
    meta_json      TEXT    NOT NULL DEFAULT '{}',
    chart_map_json TEXT    NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    label      TEXT    NOT NULL,
    value      REAL    NOT NULL,
    source     TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    end_ts     TEXT,
    label      TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'manual',
    color      TEXT    NOT NULL DEFAULT '#888888',
    dash       TEXT    NOT NULL DEFAULT 'dash'
);
CREATE TABLE IF NOT EXISTS log_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    tab        TEXT    NOT NULL DEFAULT '',
    fields     TEXT    NOT NULL DEFAULT '{}'
);
"""

SCHEMA_VERSION = 2


class UnsupportedDBError(RuntimeError):
    """Raised for any monitor database that is not schema v2.

    Legacy pre-session databases are deliberately unsupported (spec
    2026-07-12: no migration path); the message must say so.
    """


def _check_version(version: int, tables: set[str], path: str) -> None:
    """Raise :class:`UnsupportedDBError` unless *version*/*tables* are v2.

    A brand-new (empty) file has no tables yet — that's fine, the caller is
    about to create the schema. Anything with tables but the wrong
    ``user_version`` is either a pre-session (v1) capture or a database from
    a future otto; both are refused loud, with no migration offered.
    """
    if tables and version != SCHEMA_VERSION:
        raise UnsupportedDBError(
            f"'{path}' uses a pre-session schema (or schema version "
            f"{version}); otto no longer reads pre-session monitor databases "
            "and provides no migration — use a fresh --db file (not supported: "
            "converting legacy captures)."
        )


def _check_session_columns(columns: set[str], path: str) -> None:
    """Refuse a ``sessions`` table that predates the ``chart_map_json`` column.

    ``chart_map_json`` was added to v2 in place, pre-release (see ``_SCHEMA``),
    so the version number cannot distinguish the two shapes — only the column
    can. Databases written by an intermediate development build of v2 exist
    (on this branch, at least), and ``CREATE TABLE IF NOT EXISTS`` will not
    add the column to them. Without this guard they die on the first SELECT
    with a raw ``sqlite3.OperationalError`` instead of otto's own fail-loud
    error.
    """
    if columns and "chart_map_json" not in columns:
        raise UnsupportedDBError(
            f"'{path}' uses an early development build of schema v2 (its sessions "
            "table has no chart_map_json column); otto provides no migration — "
            "use a fresh --db file (not supported: converting pre-column captures)."
        )


def _pragma_version(row: Any, path: str) -> int:
    """Extract the ``user_version`` int from a fetched PRAGMA row, or raise.

    A missing row would mean the connection itself is unusable — PRAGMA
    user_version always returns exactly one row on any live connection.
    """
    if row is None:
        raise UnsupportedDBError(
            f"'{path}' is not a monitor database (PRAGMA user_version returned no row)"
        )
    return row[0]


@dataclass
class SessionRow:
    """One session read back from a v2 archive, with all of its rows.

    Row tuples deliberately mirror the SQL column order rather than being
    modeled — this is the review-path reader, consumed by the producer
    (Task 3), not a public API surface.
    """

    id: str
    label: str | None
    note: str | None
    start: str
    end: str | None
    lab_json: str
    meta_json: str
    chart_map_json: str
    metrics: list[tuple[str, str, str, float, str | None]]  # ts, host, label, value, source
    events: list[
        tuple[int, str, str | None, str, str, str, str]
    ]  # id, ts, end_ts, label, source, color, dash
    log_events: list[tuple[str, str, str, str]]  # ts, host, tab, fields_json


class MetricDB:
    """Persistent async SQLite store, bound to ONE live session frame.

    The frame is a :class:`~otto.monitor.session.SessionFrame`, and every
    write is stamped with its session id. A single database file accumulates
    one row in ``sessions`` per live run over time — reopen a fresh
    ``MetricDB`` (with a fresh frame) for the next run.
    """

    def __init__(self, path: str, frame: SessionFrame, lab_json: str, meta_json: str) -> None:
        self._path = path
        self._frame = frame
        self._lab_json = lab_json
        self._meta_json = meta_json
        self._conn: aiosqlite.Connection | None = None
        self._lock_fd: int | None = None

    async def open(self) -> None:
        """Acquire the flock guard, open the connection, create this session's row."""
        # Acquire an exclusive file lock so two live collectors can't
        # write to the same database simultaneously.
        lock_path = self._path + ".lock"
        fd: int | None = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except OSError as err:
            # The contended path (flock raises while the fd is already open)
            # must not leak the descriptor: self._lock_fd is only set on a
            # SUCCESSFUL flock, so close() would never reclaim this one and
            # every refused instance would burn an fd for the process's life.
            if fd is not None:
                os.close(fd)
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
            try:
                await conn.execute(f"PRAGMA journal_mode={journal_mode}")
                await conn.execute("PRAGMA busy_timeout=5000")

                version_row = await (await conn.execute("PRAGMA user_version")).fetchone()
                version = _pragma_version(version_row, self._path)
                tables = {
                    row[0]
                    async for row in await conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                _check_version(version, tables, self._path)
                if "sessions" in tables:
                    _check_session_columns(
                        {row[1] async for row in await conn.execute("PRAGMA table_info(sessions)")},
                        self._path,
                    )
                await conn.executescript(_SCHEMA)
                await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                # chart_map_json is deliberately NOT inserted here: it is empty
                # at open() by construction (the store's chart_map is populated
                # only as points arrive, and open() runs before the first tick),
                # so the column's DEFAULT '{}' IS the correct seed. The collector
                # fills it in via write_chart_map() as new labels appear.
                await conn.execute(
                    "INSERT INTO sessions (id, label, note, start, end, lab_json, meta_json)"
                    " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (
                        self._frame.id,
                        self._frame.label,
                        self._frame.note,
                        self._frame.start.isoformat(),
                        self._lab_json,
                        self._meta_json,
                    ),
                )
                await conn.commit()
            except sqlite3.DatabaseError as err:
                # aiosqlite.connect() is lazy — same gap as read_sessions'
                # sqlite3.connect() above: a non-SQLite (or corrupted) file
                # only fails once a PRAGMA actually reads the header, raising
                # a bare "file is not a database" sqlite3.DatabaseError.
                # OperationalError is ALSO a DatabaseError subclass, so this
                # one clause covers both without shadowing UnsupportedDBError
                # (a RuntimeError, not a sqlite3 error) raised by the version/
                # column guards above — those fall through to the plain
                # BaseException clause below unconverted.
                await conn.close()
                raise UnsupportedDBError(
                    f"'{self._path}' is not a monitor database ({err})"
                ) from err
            except BaseException:
                # A failure between connect() and the assignment below must
                # not leak the aiosqlite connection either — otherwise it is
                # garbage-collected mid-loop and warns (or worse) on __del__.
                await conn.close()
                raise
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

    async def finalize(self, end: datetime) -> None:
        """Stamp this session's ``end`` timestamp. No-op if not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "UPDATE sessions SET end = ? WHERE id = ?",
            (end.isoformat(), self._frame.id),
        )
        await self._conn.commit()

    async def write_chart_map(self, chart_map_json: str) -> None:
        """Overwrite this session's series-label → chart-key map. No-op if not open.

        Called by the collector whenever a tick introduces a label the map did
        not already carry, NOT at finalize: a crashed session (``end`` left
        NULL) must still carry its grouping, or every one of its series renders
        as its own ungrouped, unit-less chart on replay (see
        :mod:`otto.monitor.export`).
        """
        if not self._conn:
            return
        await self._conn.execute(
            "UPDATE sessions SET chart_map_json = ? WHERE id = ?",
            (chart_map_json, self._frame.id),
        )
        await self._conn.commit()

    async def write_point(self, ts: datetime, host: str, label: str, value: float) -> None:
        """Insert one metric point. No-op if the connection is not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO metrics (session_id, ts, host, label, value, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._frame.id, ts.isoformat(), host, label, value, None),
        )
        await self._conn.commit()

    async def write_log_event(
        self, ts: datetime, host: str, tab: str, fields: dict[str, str]
    ) -> None:
        """Insert one log-event row (fields JSON-encoded). No-op if not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO log_events (session_id, ts, host, tab, fields) VALUES (?, ?, ?, ?, ?)",
            (self._frame.id, ts.isoformat(), host, tab, json.dumps(fields)),
        )
        await self._conn.commit()

    async def write_event(self, event: MonitorEvent) -> int:
        """Insert event into the DB and return the rowid (0 if no DB configured)."""
        if not self._conn:
            return 0
        cursor = await self._conn.execute(
            "INSERT INTO events (session_id, ts, end_ts, label, source, color, dash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._frame.id,
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


def read_sessions(path: str) -> list[SessionRow]:
    """Read every session (ordered by start) from a v2 archive. Fail-loud otherwise.

    Synchronous sqlite3, opened read-only — review mode (the CLI's historical
    viewer) runs before any event loop matters, so there is no need to route
    this through aiosqlite.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError as err:
        raise UnsupportedDBError(
            f"'{path}' is not a monitor database (cannot open read-only)"
        ) from err

    try:
        with contextlib.closing(conn):
            version_row = conn.execute("PRAGMA user_version").fetchone()
            version = _pragma_version(version_row, path)
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            _check_version(version, tables, path)
            # _check_version is deliberately lenient about a table-less file so
            # that open() can initialize a fresh one. read_sessions has no such
            # case — an existing-but-uninitialized file is simply not readable,
            # and without this guard the SELECT below would escape as a raw
            # sqlite3.OperationalError instead of our own error type.
            if "sessions" not in tables:
                raise UnsupportedDBError(
                    f"'{path}' is not a monitor database (no sessions table — the file exists "
                    "but was never initialized by a live otto monitor run)."
                )
            _check_session_columns(
                {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}, path
            )

            sessions: list[SessionRow] = []
            for row in conn.execute(
                "SELECT id, label, note, start, end, lab_json, meta_json, chart_map_json "
                "FROM sessions ORDER BY start"
            ):
                session_id, label, note, start, end, lab_json, meta_json, chart_map_json = row
                metrics = conn.execute(
                    "SELECT ts, host, label, value, source FROM metrics "
                    "WHERE session_id = ? ORDER BY ts",
                    (session_id,),
                ).fetchall()
                events = conn.execute(
                    "SELECT id, ts, end_ts, label, source, color, dash FROM events "
                    "WHERE session_id = ? ORDER BY ts",
                    (session_id,),
                ).fetchall()
                log_events = conn.execute(
                    "SELECT ts, host, tab, fields FROM log_events WHERE session_id = ? ORDER BY ts",
                    (session_id,),
                ).fetchall()
                sessions.append(
                    SessionRow(
                        id=session_id,
                        label=label,
                        note=note,
                        start=start,
                        end=end,
                        lab_json=lab_json,
                        meta_json=meta_json,
                        chart_map_json=chart_map_json,
                        metrics=[tuple(r) for r in metrics],
                        events=[tuple(r) for r in events],
                        log_events=[tuple(r) for r in log_events],
                    )
                )
    except sqlite3.DatabaseError as err:
        # sqlite3.connect() is lazy — a non-SQLite (or corrupted) file only
        # fails once the first PRAGMA actually reads the header, raising a
        # bare "file is not a database" sqlite3.DatabaseError. OperationalError
        # (caught above, for the read-only-open failure mode) is ALSO a
        # sqlite3.DatabaseError subclass, so this one except clause covers
        # both without shadowing UnsupportedDBError (a RuntimeError, not a
        # sqlite3 error) raised from inside the block above.
        raise UnsupportedDBError(f"'{path}' is not a monitor database ({err})") from err
    return sessions
