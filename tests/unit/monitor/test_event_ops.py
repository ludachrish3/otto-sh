"""The shared event-mutation seam (Plan 5c) — one rule set, tested directly."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.models.monitor import EventCreateBody, EventUpdateBody
from otto.monitor.event_ops import EventValidationError, merge_update, resolve_create

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
    with pytest.raises(EventValidationError):
        resolve_create(EventCreateBody(label="x", end_timestamp=T0))  # T0 is in the past


def test_merge_absent_fields_unchanged() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"label": "renamed"}), **EXISTING)
    assert (fields.label, fields.timestamp, fields.end_timestamp) == ("renamed", T0, T1)


def test_merge_explicit_null_end_clears() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"end_timestamp": None}), **EXISTING)
    assert fields.end_timestamp is None


def test_merge_rejects_start_moved_past_kept_end() -> None:
    moved = (T1 + timedelta(minutes=1)).isoformat()
    with pytest.raises(EventValidationError):
        merge_update(EventUpdateBody.model_validate({"timestamp": moved}), **EXISTING)
