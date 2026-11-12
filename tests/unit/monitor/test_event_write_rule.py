"""#964, #994: every library and archive event write goes through the one write rule.

``event_ops.check_event_write`` checks the label (#964) and the span order (#994).
"""

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from _archive import _make_archive
from _events import BLANKS
from _requests import asgi
from test_event_mutation_races import _live, _review

from otto.models.monitor import SPAN_ORDER_ERROR, EventRecord
from otto.monitor import archive_edit
from otto.monitor.collector import MetricCollector
from otto.monitor.db import event_row_record, read_sessions
from otto.monitor.event_ops import EventValidationError, editable_fields, same_event
from otto.monitor.events import MonitorEvent
from otto.utils import utc_instant

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
REFUSED = "label must not be empty"


def _seeded_collector(label: str) -> MetricCollector:
    """A collector holding one event (id 1), seeded straight into the store.

    Going around ``add_event`` is how a legacy blank-label event gets in.
    """
    collector = MetricCollector(hosts=[])
    collector._store.note_imported_event(MonitorEvent(timestamp=T0, label=label, id=1))
    return collector


def _published(collector: MetricCollector) -> list[object]:
    seen: list[object] = []
    collector.publish = seen.append  # type: ignore[method-assign]  # capture
    return seen


def _record(label: str) -> EventRecord:
    return EventRecord(timestamp=T0, label=label)


def _archive_with_event(tmp_path, label: str = "x") -> tuple[str, str, int]:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path,
        sid,
        timestamp=T0,
        end_timestamp=None,
        label=label,
        source="manual",
        color="#888888",
        dash="dash",
    )
    return path, sid, rowid


def _labels(path: str) -> list[str]:
    [row] = read_sessions(path)
    return [event_row_record(e).label for e in row.events]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", BLANKS)
class TestCollectorRefusesBlankLabels:
    async def test_add_event(self, blank: str) -> None:
        collector = MetricCollector(hosts=[])
        seen = _published(collector)
        with pytest.raises(ValueError, match=REFUSED):
            await collector.add_event(label=blank)
        assert collector.get_events() == []
        assert seen == []

    async def test_update_event(self, blank: str) -> None:
        collector = _seeded_collector("kept")
        seen = _published(collector)
        with pytest.raises(ValueError, match=REFUSED):
            await collector.update_event(
                event_id=1, label=blank, color="#888888", dash="dash", timestamp=T0
            )
        assert [e.label for e in collector.get_events()] == ["kept"]
        assert seen == []

    async def test_mutate_event(self, blank: str) -> None:
        collector = _seeded_collector("kept")
        seen = _published(collector)
        with pytest.raises(ValueError, match=REFUSED):
            await collector.mutate_event(
                event_id=1, resolve=lambda current: replace(current, label=blank)
            )
        assert [e.label for e in collector.get_events()] == ["kept"]
        assert seen == []

    async def test_restore_event(self, blank: str) -> None:
        collector = _seeded_collector("x")
        await collector.delete_event(event_id=1)
        seen = _published(collector)
        with pytest.raises(EventValidationError, match=REFUSED):
            await collector.restore_event(event_id=1, write=_record(blank))
        assert collector.get_events() == []
        assert seen == []

    async def test_revert_restore_leg(self, blank: str) -> None:
        collector = _seeded_collector("x")
        await collector.delete_event(event_id=1)
        with pytest.raises(EventValidationError, match=REFUSED):
            await collector.revert_event(event_id=1, expect=None, write=_record(blank))
        assert collector.get_events() == []

    async def test_revert_update_leg(self, blank: str) -> None:
        collector = _seeded_collector("kept")
        with pytest.raises(EventValidationError, match=REFUSED):
            await collector.revert_event(event_id=1, expect=_record("kept"), write=_record(blank))
        assert [e.label for e in collector.get_events()] == ["kept"]


@pytest.mark.parametrize("blank", BLANKS)
class TestArchiveRefusesBlankLabels:
    def test_insert(self, tmp_path, blank: str) -> None:
        path, sid = _make_archive(tmp_path)
        with pytest.raises(ValueError, match=REFUSED):
            archive_edit.insert_event(
                path,
                sid,
                timestamp=T0,
                end_timestamp=None,
                label=blank,
                source="manual",
                color="#888888",
                dash="dash",
            )
        assert _labels(path) == []

    def test_update(self, tmp_path, blank: str) -> None:
        path, sid, rowid = _archive_with_event(tmp_path)
        with pytest.raises(ValueError, match=REFUSED):
            archive_edit.update_event(
                path,
                sid,
                rowid,
                timestamp=T0,
                end_timestamp=None,
                label=blank,
                color="#888888",
                dash="dash",
            )
        assert _labels(path) == ["x"]

    def test_restore(self, tmp_path, blank: str) -> None:
        path, sid, rowid = _archive_with_event(tmp_path)
        archive_edit.delete_event(path, sid, rowid)
        with pytest.raises(EventValidationError, match=REFUSED):
            archive_edit.restore_event(path, sid, rowid, _record(blank))
        assert _labels(path) == []

    def test_guarded_update(self, tmp_path, blank: str) -> None:
        """An update guarded by the row it read (a revert's update leg) shares the rule."""
        path, sid, rowid = _archive_with_event(tmp_path)
        [current] = [event_row_record(e) for e in read_sessions(path)[0].events]
        with pytest.raises(EventValidationError, match=REFUSED):
            archive_edit.update_event(
                path,
                sid,
                rowid,
                expect=current,
                **editable_fields(current.model_copy(update={"label": blank})),
            )
        assert _labels(path) == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_change_that_keeps_a_legacy_blank_label_succeeds_on_the_collector(
    blank: str,
) -> None:
    collector = _seeded_collector(blank)
    updated = await collector.mutate_event(
        event_id=1, resolve=lambda current: replace(current, color="#2ca02c")
    )
    assert updated is not None
    assert (updated.label, updated.color) == (blank, "#2ca02c")


@pytest.mark.parametrize("blank", BLANKS)
def test_a_change_that_keeps_a_legacy_blank_label_succeeds_on_the_archive(
    tmp_path, blank: str
) -> None:
    path, sid, rowid = _archive_with_event(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE events SET label = ?", (blank,))
    assert archive_edit.update_event(
        path,
        sid,
        rowid,
        timestamp=T0,
        end_timestamp=None,
        label=blank,
        color="#2ca02c",
        dash="dash",
    )
    assert _labels(path) == [blank]


# ---------------------------------------------------------------------------
# #994: the span, on every write family
# ---------------------------------------------------------------------------

START = T0
END = T0 + timedelta(hours=1)
BACKWARD_END = T0 - timedelta(hours=1)
SPAN_EXACT = f"^{SPAN_ORDER_ERROR}$"


def _forward_collector() -> MetricCollector:
    collector = MetricCollector(hosts=[])
    collector._store.note_imported_event(
        MonitorEvent(timestamp=START, end_timestamp=END, label="x", id=1)
    )
    return collector


def _forward_record() -> EventRecord:
    return EventRecord(timestamp=START, end_timestamp=END, label="x")


def _backward_record() -> EventRecord:
    return EventRecord(timestamp=START, end_timestamp=BACKWARD_END, label="x")


async def _c_add(c: MetricCollector) -> object:
    return await c.add_event(label="x", timestamp=START, end_timestamp=BACKWARD_END)


async def _c_restore(c: MetricCollector) -> object:
    return await c.restore_event(event_id=1, write=_backward_record())


async def _c_update(c: MetricCollector) -> object:
    return await c.update_event(
        event_id=1,
        label="x",
        color="#888888",
        dash="dash",
        timestamp=START,
        end_timestamp=BACKWARD_END,
    )


async def _c_mutate(c: MetricCollector) -> object:
    return await c.mutate_event(
        event_id=1, resolve=lambda current: replace(current, end_timestamp=BACKWARD_END)
    )


async def _c_revert_update(c: MetricCollector) -> object:
    return await c.revert_event(event_id=1, expect=_forward_record(), write=_backward_record())


async def _c_revert_restore(c: MetricCollector) -> object:
    return await c.revert_event(event_id=1, expect=None, write=_backward_record())


COLLECTOR_FAMILIES: dict[str, tuple[bool, Callable[[MetricCollector], Awaitable[object]]]] = {
    # name: (the event is deleted first, the refused write)
    "add_event": (False, _c_add),
    "restore_event": (True, _c_restore),
    "update_event": (False, _c_update),
    "mutate_event": (False, _c_mutate),
    "revert_update_leg": (False, _c_revert_update),
    "revert_restore_leg": (True, _c_revert_restore),
}


def _collector_state(collector: MetricCollector) -> list[dict[str, object]]:
    return [e.to_dict() for e in collector.get_events()]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", COLLECTOR_FAMILIES)
async def test_backward_span_refused_by_every_collector_write(family: str) -> None:
    deleted, write = COLLECTOR_FAMILIES[family]
    collector = _forward_collector()
    if deleted:
        await collector.delete_event(event_id=1)
    seen = _published(collector)
    before = _collector_state(collector)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await write(collector)
    assert _collector_state(collector) == before
    assert seen == []


def _stored(path: str) -> EventRecord:
    [row] = read_sessions(path)
    [event] = row.events
    return event_row_record(event)


def _a_insert(path: str, sid: str, rowid: int) -> object:
    return archive_edit.insert_event(
        path,
        sid,
        timestamp=START,
        end_timestamp=BACKWARD_END,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )


def _a_restore(path: str, sid: str, rowid: int) -> object:
    return archive_edit.restore_event(path, sid, rowid, _backward_record())


def _a_update(path: str, sid: str, rowid: int) -> object:
    return archive_edit.update_event(
        path,
        sid,
        rowid,
        timestamp=START,
        end_timestamp=BACKWARD_END,
        label="x",
        color="#888888",
        dash="dash",
    )


def _a_guarded_update(path: str, sid: str, rowid: int) -> object:
    """An update guarded by the row it read, as a review revert's update leg writes."""
    return archive_edit.update_event(
        path, sid, rowid, expect=_stored(path), **editable_fields(_backward_record())
    )


ARCHIVE_FAMILIES: dict[str, tuple[bool, Callable[[str, str, int], object]]] = {
    "insert_event": (False, _a_insert),
    "restore_event": (True, _a_restore),
    "update_event": (False, _a_update),
    "guarded_update_event": (False, _a_guarded_update),
}


def _rows(path: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


def _forward_archive(tmp_path: Path) -> tuple[str, str, int]:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path,
        sid,
        timestamp=START,
        end_timestamp=END,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )
    return path, sid, rowid


@pytest.mark.parametrize("family", ARCHIVE_FAMILIES)
def test_backward_span_refused_by_every_archive_write(tmp_path: Path, family: str) -> None:
    deleted, write = ARCHIVE_FAMILIES[family]
    path, sid, rowid = _forward_archive(tmp_path)
    if deleted:
        archive_edit.delete_event(path, sid, rowid)
    before = _rows(path)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        write(path, sid, rowid)
    assert _rows(path) == before


@pytest.mark.asyncio
async def test_zero_length_span_refused_by_add_event() -> None:
    collector = MetricCollector(hosts=[])
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.add_event(label="x", timestamp=START, end_timestamp=START)
    assert collector.get_events() == []


def test_zero_length_span_refused_by_insert_event(tmp_path: Path) -> None:
    path, sid = _make_archive(tmp_path)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        archive_edit.insert_event(
            path,
            sid,
            timestamp=START,
            end_timestamp=START,
            label="x",
            source="manual",
            color="#888888",
            dash="dash",
        )
    assert _rows(path) == []


NAIVE_START = datetime(2026, 7, 18, 12, 0)  # noqa: DTZ001 - naive means UTC
AWARE_END_BEFORE = datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc)
AWARE_END_AFTER = datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_mixed_naive_aware_span_on_add_event() -> None:
    collector = MetricCollector(hosts=[])
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.add_event(label="x", timestamp=NAIVE_START, end_timestamp=AWARE_END_BEFORE)
    stored = await collector.add_event(
        label="x", timestamp=NAIVE_START, end_timestamp=AWARE_END_AFTER
    )
    assert stored.timestamp == T0
    assert stored.timestamp.tzinfo is not None
    assert stored.end_timestamp == AWARE_END_AFTER


def test_mixed_naive_aware_span_on_insert_event(tmp_path: Path) -> None:
    path, sid = _make_archive(tmp_path)

    def insert(end: datetime) -> int:
        return archive_edit.insert_event(
            path,
            sid,
            timestamp=NAIVE_START,
            end_timestamp=end,
            label="x",
            source="manual",
            color="#888888",
            dash="dash",
        )

    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        insert(AWARE_END_BEFORE)
    insert(AWARE_END_AFTER)
    stored = _stored(path)
    assert stored.timestamp == T0
    assert stored.timestamp.tzinfo is not None
    assert stored.end_timestamp == AWARE_END_AFTER


def _past_end_body() -> dict[str, object]:
    """The one create body the model validator lets through that the backend must refuse."""
    return {
        "label": "x",
        "end_timestamp": (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(),
    }


@pytest.mark.asyncio
async def test_http_create_backend_refusal_is_422_in_live_mode() -> None:
    live = await _live()
    before = _collector_state(live.collector)
    create = f"/api/session/{live.collector.session_id}/event"
    status, body = await asgi(live.app, "POST", create, _past_end_body())
    assert status == 422
    assert SPAN_ORDER_ERROR in str(body)
    assert _collector_state(live.collector) == before


@pytest.mark.asyncio
async def test_http_create_backend_refusal_is_422_in_review_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    before = _rows(review.path)
    status, body = await asgi(review.app, "POST", review.base, _past_end_body())
    assert status == 422
    assert SPAN_ORDER_ERROR in str(body)
    assert _rows(review.path) == before


# A legacy backward span (stored before the rule) keeps working while its pair is kept.


def _legacy_collector() -> MetricCollector:
    collector = MetricCollector(hosts=[])
    collector._store.note_imported_event(
        MonitorEvent(timestamp=START, end_timestamp=BACKWARD_END, label="x", id=1)
    )
    return collector


@pytest.mark.asyncio
async def test_legacy_backward_span_keeps_on_recolour_on_the_collector() -> None:
    collector = _legacy_collector()
    updated = await collector.update_event(
        event_id=1,
        label="x",
        color="#2ca02c",
        dash="dash",
        timestamp=START,
        end_timestamp=BACKWARD_END,
    )
    assert updated is not None
    assert updated.color == "#2ca02c"
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.update_event(
            event_id=1,
            label="x",
            color="#2ca02c",
            dash="dash",
            timestamp=START,
            end_timestamp=BACKWARD_END - timedelta(minutes=1),
        )
    [kept] = collector.get_events()
    assert (kept.color, kept.end_timestamp) == ("#2ca02c", BACKWARD_END)
    await collector.delete_event(event_id=1)
    seen = _published(collector)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.restore_event(event_id=1, write=_backward_record())
    assert collector.get_events() == []
    assert seen == []


def test_legacy_backward_span_keeps_on_recolour_on_the_archive(tmp_path: Path) -> None:
    path, sid, rowid = _forward_archive(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE events SET end_ts = ?", (BACKWARD_END.isoformat(),))

    def update(end: datetime) -> bool:
        return archive_edit.update_event(
            path,
            sid,
            rowid,
            timestamp=START,
            end_timestamp=end,
            label="x",
            color="#2ca02c",
            dash="dash",
        )

    assert update(BACKWARD_END)
    legacy = _stored(path)
    assert legacy.color == "#2ca02c"
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        update(BACKWARD_END - timedelta(minutes=1))
    assert _stored(path) == legacy
    archive_edit.delete_event(path, sid, rowid)
    before = _rows(path)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        archive_edit.restore_event(path, sid, rowid, legacy)
    assert _rows(path) == before == []


@pytest.mark.asyncio
async def test_http_patch_moving_the_end_backward_is_422_in_live_mode() -> None:
    live = await _live()
    before = _collector_state(live.collector)
    status, body = await asgi(
        live.app, "PATCH", live.base, {"end_timestamp": "2000-01-01T00:00:00Z"}
    )
    assert status == 422
    assert SPAN_ORDER_ERROR in str(body)
    assert _collector_state(live.collector) == before


@pytest.mark.asyncio
async def test_http_patch_moving_the_end_backward_is_422_in_review_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    before = _rows(review.path)
    one = f"{review.base}/{review.event_id}"
    status, body = await asgi(review.app, "PATCH", one, {"end_timestamp": "2000-01-01T00:00:00Z"})
    assert status == 422
    assert SPAN_ORDER_ERROR in str(body)
    assert _rows(review.path) == before


# #994: spans compare as UTC instants, so a DST fold cannot hide a backward span.
# America/Chicago falls back at 02:00 CDT on 2026-11-01: 01:00-02:00 happens twice,
# first as CDT (-05:00, fold=0), then as CST (-06:00, fold=1).
CHICAGO = ZoneInfo("America/Chicago")
FOLD_CDT_0145 = datetime(2026, 11, 1, 1, 45, tzinfo=CHICAGO)  # 06:45Z
FOLD_CST_0115 = datetime(2026, 11, 1, 1, 15, fold=1, tzinfo=CHICAGO)  # 07:15Z


def _instants(event: EventRecord | MonitorEvent) -> tuple[datetime, datetime | None]:
    # The store reads back a fixed offset, and a fixed offset never equals a
    # ZoneInfo value inside the fold (PEP 495): compare the instants.
    return utc_instant(event.timestamp), utc_instant(event.end_timestamp)


def _fold_insert(path: str, sid: str, start: datetime, end: datetime) -> int:
    return archive_edit.insert_event(
        path,
        sid,
        timestamp=start,
        end_timestamp=end,
        label="x",
        source="manual",
        color="#888888",
        dash="dash",
    )


@pytest.mark.asyncio
async def test_a_fold_crossing_span_is_ordered_by_instant_on_add_event() -> None:
    collector = MetricCollector(hosts=[])
    # 01:15 CST -> 01:45 CDT: the later wall time is the earlier instant.
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.add_event(label="x", timestamp=FOLD_CST_0115, end_timestamp=FOLD_CDT_0145)
    assert collector.get_events() == []
    stored = await collector.add_event(
        label="x", timestamp=FOLD_CDT_0145, end_timestamp=FOLD_CST_0115
    )
    assert _instants(stored) == (utc_instant(FOLD_CDT_0145), utc_instant(FOLD_CST_0115))


def test_a_fold_crossing_span_is_ordered_by_instant_on_insert_event(tmp_path: Path) -> None:
    path, sid = _make_archive(tmp_path)
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        _fold_insert(path, sid, FOLD_CST_0115, FOLD_CDT_0145)
    assert _rows(path) == []
    _fold_insert(path, sid, FOLD_CDT_0145, FOLD_CST_0115)
    stored = _stored(path)
    assert _instants(stored) == (utc_instant(FOLD_CDT_0145), utc_instant(FOLD_CST_0115))


@pytest.mark.asyncio
async def test_a_fold_only_change_of_a_kept_start_is_checked() -> None:
    start = datetime(2026, 11, 1, 1, 15, tzinfo=CHICAGO)  # 06:15Z
    end = datetime(2026, 11, 1, 1, 10, fold=1, tzinfo=CHICAGO)  # 07:10Z
    collector = MetricCollector(hosts=[])
    collector._store.note_imported_event(
        MonitorEvent(timestamp=start, end_timestamp=end, label="x", id=1)
    )
    # The same wall time one fold later is 07:15Z, after the end: a change, and backward.
    with pytest.raises(EventValidationError, match=SPAN_EXACT):
        await collector.update_event(
            event_id=1,
            label="x",
            color="#888888",
            dash="dash",
            timestamp=start.replace(fold=1),
            end_timestamp=end,
        )
    [kept] = collector.get_events()
    assert kept.timestamp.fold == 0


def test_same_event_compares_fold_different_wall_times_as_instants() -> None:
    first = EventRecord(timestamp=datetime(2026, 11, 1, 1, 15, tzinfo=CHICAGO), label="x")
    second = EventRecord(timestamp=first.timestamp.replace(fold=1), label="x")
    assert not same_event(first, second)
    assert same_event(first, EventRecord(timestamp=first.timestamp, label="x"))
