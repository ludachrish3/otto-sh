"""EventCreateBody/EventUpdateBody — the 5c HTTP boundary validation table."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from otto.models.monitor import EventCreateBody, EventUpdateBody

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 18, 12, 5, tzinfo=timezone.utc)


class TestEventCreateBody:
    def test_minimal_body_defaults(self) -> None:
        body = EventCreateBody(label="deploy")
        assert body.timestamp is None  # server stamps now
        assert body.end_timestamp is None
        assert body.color == "#888888"
        assert body.dash == "dash"

    def test_span_body_round_trips(self) -> None:
        body = EventCreateBody(label="soak", timestamp=T0, end_timestamp=T1)
        assert body.end_timestamp == T1

    @pytest.mark.parametrize("label", ["", "   "])
    def test_blank_label_rejected(self, label: str) -> None:
        with pytest.raises(ValidationError, match="label"):
            EventCreateBody(label=label)

    @pytest.mark.parametrize("color", ["red", "#12345", "#12345g", "rgb(1,2,3)"])
    def test_non_hex_color_rejected(self, color: str) -> None:
        with pytest.raises(ValidationError, match="color"):
            EventCreateBody(label="x", color=color)

    def test_unknown_dash_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dash"):
            EventCreateBody(label="x", dash="wavy")

    def test_inverted_span_rejected(self) -> None:
        with pytest.raises(ValidationError, match="end_timestamp"):
            EventCreateBody(label="x", timestamp=T1, end_timestamp=T0)

    def test_equal_span_rejected(self) -> None:
        with pytest.raises(ValidationError, match="end_timestamp"):
            EventCreateBody(label="x", timestamp=T0, end_timestamp=T0)

    def test_end_without_start_is_allowed_here(self) -> None:
        # The server resolves timestamp=now first, then re-checks the pair —
        # the model can only validate what it holds.
        assert EventCreateBody(label="x", end_timestamp=T1).end_timestamp == T1

    def test_unknown_field_rejected(self) -> None:
        # extra='forbid' (inherited from OttoModel) — a typo'd 'color' must
        # not silently vanish into an ignored field.
        with pytest.raises(ValidationError):
            EventCreateBody.model_validate({"label": "deploy", "colour": "#ffffff"})


class TestEventUpdateBody:
    def test_explicit_null_end_is_distinguishable_from_absent(self) -> None:
        cleared = EventUpdateBody.model_validate({"end_timestamp": None})
        untouched = EventUpdateBody.model_validate({})
        assert "end_timestamp" in cleared.model_fields_set
        assert "end_timestamp" not in untouched.model_fields_set

    def test_provided_values_are_validated(self) -> None:
        with pytest.raises(ValidationError, match="dash"):
            EventUpdateBody(dash="wavy")
        with pytest.raises(ValidationError, match="color"):
            EventUpdateBody(color="blue")
        with pytest.raises(ValidationError, match="label"):
            EventUpdateBody(label="   ")

    def test_none_values_pass_field_validators(self) -> None:
        body = EventUpdateBody(label=None, color=None, dash=None)
        assert body.label is None

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventUpdateBody.model_validate({"dashh": "dot"})
