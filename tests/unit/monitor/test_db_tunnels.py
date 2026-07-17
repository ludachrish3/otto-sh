"""tunnels_json persistence: in-place v2 column, chart_map_json precedent."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.monitor.db import MetricDB, UnsupportedDBError, read_sessions
from otto.monitor.session import SessionFrame

START = datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)


def _frame() -> SessionFrame:
    return SessionFrame(id="s1", label=None, note=None, start=START, end=None)


def _open_write_close(path: str, tunnels_json: str) -> None:
    async def go() -> None:
        db = MetricDB(path, _frame(), lab_json="{}", meta_json="{}")
        await db.open()
        await db.write_tunnels(tunnels_json)
        await db.close()

    asyncio.run(go())


def test_write_tunnels_round_trips(tmp_path: Path) -> None:
    path = str(tmp_path / "m.db")
    _open_write_close(path, '[{"id": "tun-a-1"}]')
    rows = read_sessions(path)
    assert rows[0].tunnels_json == '[{"id": "tun-a-1"}]'


def test_fresh_session_defaults_to_empty_list(tmp_path: Path) -> None:
    path = str(tmp_path / "m.db")

    async def go() -> None:
        db = MetricDB(path, _frame(), lab_json="{}", meta_json="{}")
        await db.open()
        await db.close()

    asyncio.run(go())
    assert read_sessions(path)[0].tunnels_json == "[]"


def test_pre_column_v2_database_is_refused_loud(tmp_path: Path) -> None:
    """The chart_map_json precedent: same version, old shape, loud refusal
    naming the missing column."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, label TEXT, note TEXT, start TEXT NOT NULL,
            end TEXT, lab_json TEXT NOT NULL DEFAULT '{}',
            meta_json TEXT NOT NULL DEFAULT '{}',
            chart_map_json TEXT NOT NULL DEFAULT '{}'
        );
        PRAGMA user_version = 2;
        """
    )
    conn.close()
    with pytest.raises(UnsupportedDBError, match="tunnels_json"):
        read_sessions(path)
