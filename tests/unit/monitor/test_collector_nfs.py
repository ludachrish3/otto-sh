import pytest

import otto.monitor.collector as collector_mod
from otto.monitor.collector import MetricCollector


async def _journal_mode(collector: MetricCollector) -> str:
    cur = await collector._db_conn.execute('PRAGMA journal_mode')
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]).lower()


@pytest.mark.asyncio
async def test_init_db_uses_delete_journal_on_network_fs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(collector_mod, 'network_fs_type', lambda p: 'nfs4')
    collector = MetricCollector(db_path=str(tmp_path / 'm.db'))
    with caplog.at_level('DEBUG', logger='otto'):
        await collector.init_db()
    try:
        assert await _journal_mode(collector) == 'delete'
        assert any('network filesystem' in r.message for r in caplog.records)
    finally:
        await collector.close_db()


@pytest.mark.asyncio
async def test_init_db_uses_wal_on_local_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(collector_mod, 'network_fs_type', lambda p: None)
    collector = MetricCollector(db_path=str(tmp_path / 'm.db'))
    await collector.init_db()
    try:
        assert await _journal_mode(collector) == 'wal'
    finally:
        await collector.close_db()
