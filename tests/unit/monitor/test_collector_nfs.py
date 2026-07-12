import pytest

import otto.monitor.db as db_mod
from otto.monitor.collector import MetricCollector
from otto.monitor.session import new_frame


async def _journal_mode(collector: MetricCollector) -> str:
    cur = await collector._db._conn.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]).lower()


def _anon_db(path: str) -> db_mod.MetricDB:
    return db_mod.MetricDB(
        path,
        new_frame(label=None, note=None),
        lab_json="{}",
        meta_json="{}",
    )


@pytest.mark.asyncio
async def test_init_db_uses_delete_journal_on_network_fs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(db_mod, "network_fs_type", lambda p: "nfs4")
    collector = MetricCollector(db=_anon_db(str(tmp_path / "m.db")))
    with caplog.at_level("DEBUG", logger="otto"):
        await collector.init_db()
    try:
        assert await _journal_mode(collector) == "delete"
        assert any("network filesystem" in r.message for r in caplog.records)
    finally:
        await collector.close_db()


@pytest.mark.asyncio
async def test_init_db_uses_wal_on_local_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "network_fs_type", lambda p: None)
    collector = MetricCollector(db=_anon_db(str(tmp_path / "m.db")))
    await collector.init_db()
    try:
        assert await _journal_mode(collector) == "wal"
    finally:
        await collector.close_db()
