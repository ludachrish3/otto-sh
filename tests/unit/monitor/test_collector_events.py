"""PATCH-with-timestamp support: MetricCollector.update_event can move an event's start."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from otto.models.monitor import EventRecord
from otto.monitor import event_ops
from otto.monitor.events import MonitorEvent


@pytest.mark.asyncio
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
            event_id=ev.id, label="pin", color=ev.color, dash=ev.dash, timestamp=moved
        )
        assert updated is not None
        assert updated.timestamp == moved
    finally:
        await collector.close_db()

    [row] = read_sessions(str(tmp_path / "a.db"))
    [(_event_id, ts, _end_ts, _label, _source, _color, _dash)] = row.events
    assert datetime.fromisoformat(ts) == moved  # the archive took the move, not just the store


class _HeldDB:
    """Quacks MetricDB's event writes, each held until ``release`` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def write_event(self, event: object) -> int:
        await self.release.wait()
        return 0

    async def update_event(self, event: object) -> None:
        await self.release.wait()

    async def delete_event(self, event_id: int) -> None:
        await self.release.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["add", "update", "delete"])
async def test_a_mutation_and_its_stamp_are_one_step_against_a_snapshot(op: str) -> None:
    """The dashboard replays a mutation onto a resync snapshot iff the
    mutation's ``observed_at`` is later than the snapshot's (#868). That is
    exact only if no snapshot can see a mutation whose stamp is later, so a
    mutation applies its in-memory change in the same synchronous step that
    stamps it: a DB write it awaits comes FIRST. A snapshot captured while
    the write is held sees the old state and an earlier stamp."""
    from otto.models.monitor import MonitorSessionFragment
    from otto.monitor.collector import MetricCollector, observed_now

    collector = MetricCollector(hosts=[])
    collector.session_id = "s1"
    event = await collector.add_event(label="before")
    held = _HeldDB()
    collector._db = held  # type: ignore[assignment]
    q = collector.subscribe()

    def snapshot() -> tuple[str, list[tuple[int, str]]]:
        # The live snapshot's order: stamp and state read in one step.
        return observed_now(), [(e.id, e.label) for e in collector.get_events()]

    before = [(event.id, "before")]
    match op:
        case "add":
            task = asyncio.create_task(collector.add_event(label="added"))
        case "update":
            task = asyncio.create_task(
                collector.update_event(
                    event_id=event.id,
                    label="after",
                    color=event.color,
                    dash=event.dash,
                    timestamp=event.timestamp,
                )
            )
        case _:
            task = asyncio.create_task(collector.delete_event(event_id=event.id))
    await asyncio.sleep(0)  # the mutation now waits on its DB write
    snap_stamp, snap_state = snapshot()
    held.release.set()
    await task
    frag = MonitorSessionFragment.model_validate(q.get_nowait())
    collector.unsubscribe(q=q)

    assert snap_state == before, "a snapshot taken mid-write must not see the mutation"
    assert frag.observed_at is not None
    assert frag.observed_at > datetime.fromisoformat(snap_stamp)
    after_stamp, after_state = snapshot()
    assert after_state != before
    assert datetime.fromisoformat(after_stamp) > frag.observed_at


def test_utc_now_is_strictly_increasing_even_when_the_wall_clock_steps_back(monkeypatch) -> None:
    """Two stamps never tie and never run backwards, so ordering two server
    steps by their stamps is exact (#868)."""
    from otto.monitor import event_ops

    frozen = datetime(2030, 1, 1, tzinfo=timezone.utc)
    readings = iter([frozen, frozen, frozen - timedelta(seconds=5), frozen + timedelta(seconds=1)])
    monkeypatch.setattr(event_ops, "_wall_clock", lambda: next(readings))
    monkeypatch.setattr(event_ops, "_last_stamp", None)
    stamps = [event_ops.utc_now() for _ in range(4)]
    assert stamps == sorted(set(stamps)), stamps
    assert stamps[3] == frozen + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_add_event_rejects_a_named_color() -> None:
    """#857: the write boundary raises on anything that is not hex."""
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    with pytest.raises(ValueError, match=r"color must be #rrggbb, got 'red'"):
        await collector.add_event(label="x", color="red")
    assert collector.get_events() == []


@pytest.mark.asyncio
async def test_add_event_rejects_an_unknown_dash() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    with pytest.raises(ValueError, match="dash must be one of"):
        await collector.add_event(label="x", dash="wavy")
    assert collector.get_events() == []


@pytest.mark.asyncio
async def test_add_event_stores_lowercase_hex() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="x", color="#7C5CFF")
    assert ev.color == "#7c5cff"
    assert collector.get_events()[0].color == "#7c5cff"


@pytest.mark.asyncio
async def test_monitor_event_construction_rejects_a_named_color() -> None:
    from datetime import datetime, timezone

    from otto.monitor.events import MonitorEvent

    with pytest.raises(ValueError, match=r"color must be #rrggbb, got 'red'"):
        MonitorEvent(timestamp=datetime.now(tz=timezone.utc), label="x", color="red")
    with pytest.raises(ValueError, match="dash must be one of"):
        MonitorEvent(timestamp=datetime.now(tz=timezone.utc), label="x", dash="wavy")
    assert MonitorEvent(timestamp=datetime.now(tz=timezone.utc), label="x", color="#ABC").color == (
        "#aabbcc"
    )


@pytest.mark.asyncio
async def test_update_event_rejects_a_named_color_before_mutating() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="before")
    q = collector.subscribe()
    with pytest.raises(ValueError, match=r"color must be #rrggbb, got 'red'"):
        await collector.update_event(
            event_id=ev.id, label="x", color="red", dash=ev.dash, timestamp=ev.timestamp
        )
    collector.unsubscribe(q=q)
    [stored] = collector.get_events()
    assert stored.label == "before"
    assert stored.color == "#888888"
    assert q.empty()  # nothing published


@pytest.mark.asyncio
async def test_update_event_rejects_an_unknown_dash_before_mutating() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="before")
    q = collector.subscribe()
    with pytest.raises(ValueError, match=r"dash must be one of .*, got 'wavy'"):
        await collector.update_event(
            event_id=ev.id, label="x", color=ev.color, dash="wavy", timestamp=ev.timestamp
        )
    collector.unsubscribe(q=q)
    [stored] = collector.get_events()
    assert (stored.label, stored.dash) == ("before", ev.dash)
    assert q.empty()  # nothing published


@pytest.mark.asyncio
async def test_update_event_normalises_color() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="x")
    updated = await collector.update_event(
        event_id=ev.id, label="x", color="#ABC", dash="solid", timestamp=ev.timestamp
    )
    assert updated is not None
    assert updated.color == "#aabbcc"


def _record(event: MonitorEvent) -> EventRecord:
    """The event as an EventRecord, through the one conversion (server.py's _event_response)."""
    return EventRecord.model_validate(event.to_dict())


@pytest.mark.asyncio
async def test_delete_event_returns_the_removed_event() -> None:
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="gone", color="#ABC")
    removed = await collector.delete_event(event_id=ev.id)
    assert removed is not None
    assert (removed.id, removed.label, removed.color) == (ev.id, "gone", "#aabbcc")
    assert collector.get_events() == []
    assert await collector.delete_event(event_id=ev.id) is None


@pytest.mark.asyncio
async def test_revert_update_writes_back_only_when_expected() -> None:
    from otto.models.monitor import MonitorSessionFragment
    from otto.monitor.collector import MetricCollector
    from otto.monitor.event_ops import EventChanged, same_event

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="before", color="#111111", dash="dot")
    original = _record(ev)
    edited = await collector.update_event(
        event_id=ev.id,
        label="after",
        color="#222222",
        dash="dash",
        timestamp=ev.timestamp + timedelta(seconds=5),
        end_timestamp=ev.timestamp + timedelta(seconds=9),
    )
    assert edited is not None
    expect = _record(edited)

    q = collector.subscribe()
    reverted = await collector.revert_event(event_id=ev.id, expect=expect, write=original)
    assert reverted is not None
    assert same_event(_record(reverted), original)
    frag = MonitorSessionFragment.model_validate(q.get_nowait())
    assert [e.id for e in frag.events or []] == [ev.id]

    with pytest.raises(EventChanged) as caught:
        await collector.revert_event(
            event_id=ev.id, expect=expect, write=original
        )  # expect is stale now
    assert caught.value.current is not None
    assert same_event(caught.value.current, original)
    [stored] = collector.get_events()
    assert same_event(_record(stored), original)
    assert q.empty()  # the refused revert published nothing
    collector.unsubscribe(q=q)


@pytest.mark.asyncio
async def test_revert_delete_leg_and_a_missing_event() -> None:
    from otto.monitor.collector import MetricCollector
    from otto.monitor.event_ops import EventChanged

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="x")
    record = _record(ev)
    removed = await collector.revert_event(event_id=ev.id, expect=record, write=None)
    assert removed is not None
    assert removed.id == ev.id
    assert collector.get_events() == []
    with pytest.raises(EventChanged) as caught:
        await collector.revert_event(event_id=ev.id, expect=record, write=None)
    assert caught.value.current is None


@pytest.mark.asyncio
async def test_revert_restore_keeps_id_and_source() -> None:
    from otto.models.monitor import MonitorSessionFragment
    from otto.monitor.collector import MetricCollector

    collector = MetricCollector(hosts=[])
    await collector.add_event(label="first")
    ev = await collector.add_event(label="lifecycle", source="auto", color="#333333")
    removed = await collector.delete_event(event_id=ev.id)
    assert removed is not None

    q = collector.subscribe()
    restored = await collector.revert_event(event_id=ev.id, expect=None, write=_record(removed))
    assert restored is not None
    assert (restored.id, restored.source, restored.label, restored.color) == (
        ev.id,
        "auto",
        "lifecycle",
        "#333333",
    )
    assert collector._store.find_event(ev.id) is not None
    frag = MonitorSessionFragment.model_validate(q.get_nowait())
    assert [(e.id, e.source) for e in frag.events or []] == [(ev.id, "auto")]
    collector.unsubscribe(q=q)


@pytest.mark.asyncio
async def test_restore_refuses_present_or_unallocated_id() -> None:
    from otto.monitor.collector import MetricCollector
    from otto.monitor.event_ops import EventChanged

    collector = MetricCollector(hosts=[])
    ev = await collector.add_event(label="here")
    q = collector.subscribe()
    with pytest.raises(EventChanged) as present:
        await collector.restore_event(event_id=ev.id, write=_record(ev))
    assert present.value.current is not None
    assert present.value.current.label == "here"
    with pytest.raises(EventChanged) as unallocated:
        await collector.restore_event(event_id=ev.id + 100, write=_record(ev))
    assert unallocated.value.current is None
    assert q.empty()
    assert [e.id for e in collector.get_events()] == [ev.id]
    collector.unsubscribe(q=q)


@pytest.mark.asyncio
async def test_restore_on_reused_db_keeps_rowid(tmp_path) -> None:
    """A reused --db's rowids run ahead of a fresh store's counter: the restore
    still finds the id allocated and puts the event back under it."""
    from otto.models import LabSnapshot
    from otto.monitor.collector import MetricCollector
    from otto.monitor.db import read_sessions
    from otto.monitor.export import build_session_metric_db
    from otto.monitor.session import new_frame

    path = str(tmp_path / "reused.db")

    async def open_collector(now: datetime) -> MetricCollector:
        frame = new_frame(label=None, note=None, now=now)
        db = build_session_metric_db(
            path, frame, LabSnapshot(), MetricCollector(hosts=[]), interval=5.0
        )
        collector = MetricCollector(hosts=[], db=db)
        collector.session_id = frame.id
        await collector.init_db()
        return collector

    t0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    earlier = await open_collector(t0)
    try:
        assert earlier._db is not None
        await earlier._db.restore_event_row(
            MonitorEvent(timestamp=datetime.now(tz=timezone.utc), label="old", id=56)
        )
    finally:
        await earlier.close_db()

    collector = await open_collector(t0 + timedelta(hours=1))
    try:
        ev = await collector.add_event(label="live")
        assert ev.id == 57
        removed = await collector.delete_event(event_id=ev.id)
        assert removed is not None
        restored = await collector.revert_event(event_id=ev.id, expect=None, write=_record(removed))
        assert restored is not None
        assert restored.id == 57
        # The earlier session's 56 lies inside this collector's handed-out
        # range (1..57) but belongs to another session: refused as changed,
        # never an IntegrityError.
        with pytest.raises(event_ops.EventChanged) as caught:
            await collector.revert_event(
                event_id=56, expect=None, write=EventRecord(id=56, timestamp=t0, label="mine")
            )
        assert caught.value.current is None
    finally:
        await collector.close_db()

    live = [s for s in read_sessions(path) if s.id == collector.session_id]
    assert [row[0] for row in live[0].events] == [57]  # the archive holds it under 57 too

    old = [s for s in read_sessions(path) if s.id == earlier.session_id]
    assert [row[0] for row in old[0].events] == [56]  # untouched by the refusal below
