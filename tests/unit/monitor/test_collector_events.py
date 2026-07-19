"""PATCH-with-timestamp support: MetricCollector.update_event can move an event's start."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_update_event_moves_start_timestamp(tmp_path) -> None:
    """PATCH-with-timestamp support: the store AND the archive take the new ts."""
    from datetime import datetime, timedelta

    from otto.models import LabSnapshot
    from otto.monitor.collector import MetricCollector
    from otto.monitor.db import read_sessions
    from otto.monitor.export import build_session_metric_db
    from otto.monitor.session import new_frame

    frame = new_frame(label=None, note=None)
    db = build_session_metric_db(
        str(tmp_path / "a.db"), frame, LabSnapshot(), MetricCollector(hosts=[]), interval=5.0
    )
    collector = MetricCollector(hosts=[], db=db)
    collector.session_id = frame.id
    await collector.init_db()
    try:
        ev = await collector.add_event(label="pin")
        moved = ev.timestamp - timedelta(minutes=3)
        updated = await collector.update_event(
            ev.id, label="pin", color=ev.color, dash=ev.dash, timestamp=moved
        )
        assert updated is not None
        assert updated.timestamp == moved
    finally:
        await collector.close_db()

    [row] = read_sessions(str(tmp_path / "a.db"))
    [(_event_id, ts, _end_ts, _label, _source, _color, _dash)] = row.events
    assert datetime.fromisoformat(ts) == moved  # the archive took the move, not just the store
