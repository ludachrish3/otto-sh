"""The shared event-mutation seam (Plan 5c) — one rule set, tested directly."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from otto.models.monitor import EventCreateBody, EventRecord, EventUpdateBody
from otto.monitor.event_ops import (
    EventValidationError,
    ResolvedEventFields,
    check_revert_write,
    editable_fields,
    merge_update,
    resolve_create,
    revert_leg,
    same_event,
)
from tests._fixtures.paths import CHECKOUT_ROOT

_SAME_EVENT_CASES = json.loads(
    (CHECKOUT_ROOT / "tests" / "_fixtures" / "same_event_cases.json").read_text()
)
"""The shared case table; the dashboard's TS mirror of same_event runs it too."""

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)

EXISTING = {
    "existing_label": "soak",
    "existing_color": "#888888",
    "existing_dash": "dash",
    "existing_timestamp": T0,
    "existing_end": T1,
}


def test_resolve_create_stamps_now_when_omitted() -> None:
    before = datetime.now(tz=timezone.utc)
    ts, end = resolve_create(EventCreateBody(label="x"))
    assert before <= ts <= datetime.now(tz=timezone.utc)
    assert end is None


def test_resolve_create_keeps_explicit_pair() -> None:
    assert resolve_create(EventCreateBody(label="x", timestamp=T0, end_timestamp=T1)) == (T0, T1)


def test_resolve_create_rejects_end_before_resolved_now() -> None:
    with pytest.raises(EventValidationError, match="end_timestamp must be after timestamp"):
        resolve_create(EventCreateBody(label="x", end_timestamp=T0))  # T0 is in the past


def test_merge_absent_fields_unchanged() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"label": "renamed"}), **EXISTING)
    assert (fields.label, fields.timestamp, fields.end_timestamp) == ("renamed", T0, T1)


def test_merge_explicit_null_end_clears() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"end_timestamp": None}), **EXISTING)
    assert fields.end_timestamp is None


def test_merge_rejects_start_moved_past_kept_end() -> None:
    moved = (T1 + timedelta(minutes=1)).isoformat()
    with pytest.raises(EventValidationError, match="end_timestamp must be after timestamp"):
        merge_update(EventUpdateBody.model_validate({"timestamp": moved}), **EXISTING)


def _record(data: "dict[str, object] | None") -> EventRecord | None:
    return None if data is None else EventRecord.model_validate(data)


@pytest.mark.parametrize("case", _SAME_EVENT_CASES, ids=[c["name"] for c in _SAME_EVENT_CASES])
def test_same_event_cases(case: "dict[str, object]") -> None:
    a = _record(case["a"])  # type: ignore[arg-type]
    b = _record(case["b"])  # type: ignore[arg-type]
    assert same_event(a, b) is case["equal"]
    assert same_event(b, a) is case["equal"]


def test_revert_leg_table() -> None:
    record = EventRecord(timestamp=T0, label="x")
    assert revert_leg(record, record) == "update"
    assert revert_leg(record, None) == "delete"
    assert revert_leg(None, record) == "restore"
    with pytest.raises(EventValidationError, match="expect, write, or both"):
        revert_leg(None, None)


@pytest.mark.parametrize(
    ("expect_label", "refused"),
    [(None, True), ("x", True), ("  ", False)],
    ids=["restore", "update-changes-label", "update-keeps-label"],
)
def test_revert_write_label_is_checked_only_where_a_create_or_update_would(
    expect_label: str | None, refused: bool
) -> None:
    """A blank label is refused when a revert introduces it (a restore, or an
    update that changes the label) and put back as it is when the event
    already holds it: an older archive may carry one."""
    expect = None if expect_label is None else EventRecord(timestamp=T0, label=expect_label)
    write = EventRecord(timestamp=T0, label="  ")
    if refused:
        with pytest.raises(EventValidationError, match="label must not be empty"):
            check_revert_write(expect, write)
    else:
        check_revert_write(expect, write)


def test_editable_fields_reads_the_one_field_list_from_every_shape() -> None:
    """One list of what an edit writes, read alike from a resolved merge, a
    wire record and a live event, so a replace() over it needs no restating."""
    from dataclasses import replace

    from otto.monitor.events import MonitorEvent

    expected = {
        "label": "soak",
        "color": "#aabbcc",
        "dash": "dot",
        "timestamp": T0,
        "end_timestamp": T1,
    }
    resolved = ResolvedEventFields(**expected)
    record = EventRecord(id=7, source="auto", **expected)
    event = MonitorEvent(id=7, source="auto", **expected)
    assert editable_fields(resolved) == expected
    assert editable_fields(record) == expected
    assert editable_fields(event) == expected
    base = MonitorEvent(timestamp=T1, label="old", id=3, source="user_code")
    moved = replace(base, **editable_fields(resolved))
    assert (moved.id, moved.source) == (3, "user_code")  # identity is never an edit
    assert editable_fields(moved) == expected
