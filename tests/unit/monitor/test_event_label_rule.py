"""#964: every library and archive event write goes through the one label rule."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from _archive import _make_archive
from _events import BLANKS

from otto.models.monitor import EventRecord
from otto.monitor import archive_edit
from otto.monitor.collector import MetricCollector
from otto.monitor.db import event_row_record, read_sessions
from otto.monitor.event_ops import EventValidationError
from otto.monitor.events import MonitorEvent

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
    collector._publish = seen.append  # type: ignore[method-assign]  # capture
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

    def test_revert_restore_leg(self, tmp_path, blank: str) -> None:
        path, sid, rowid = _archive_with_event(tmp_path)
        archive_edit.delete_event(path, sid, rowid)
        with pytest.raises(EventValidationError, match=REFUSED):
            archive_edit.revert_event(path, sid, rowid, None, _record(blank))
        assert _labels(path) == []

    def test_revert_update_leg(self, tmp_path, blank: str) -> None:
        path, sid, rowid = _archive_with_event(tmp_path)
        [current] = [event_row_record(e) for e in read_sessions(path)[0].events]
        with pytest.raises(EventValidationError, match=REFUSED):
            archive_edit.revert_event(
                path, sid, rowid, current, current.model_copy(update={"label": blank})
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
    conn = sqlite3.connect(path)
    conn.execute("UPDATE events SET label = ?", (blank,))
    conn.commit()
    conn.close()
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
