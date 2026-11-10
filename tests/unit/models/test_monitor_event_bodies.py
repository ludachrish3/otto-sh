"""EventCreateBody/EventUpdateBody — the 5c HTTP boundary validation table."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from otto.models.monitor import EventCreateBody, EventRecord, EventUpdateBody

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

    def test_named_color_rejected_with_the_hex_message(self) -> None:
        # The route maps this ValidationError to 422; the text is the registry's.
        with pytest.raises(ValidationError, match=r"color must be #rrggbb, got 'red'"):
            EventCreateBody(label="x", color="red")

    @pytest.mark.parametrize(("given", "stored"), [("#ABC", "#aabbcc"), ("#7C5CFF", "#7c5cff")])
    def test_short_and_upper_hex_normalised(self, given: str, stored: str) -> None:
        assert EventCreateBody(label="x", color=given).color == stored

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
        with pytest.raises(ValidationError, match=r"\ncolour\n\s*Extra inputs are not permitted"):
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

    def test_a_blank_label_is_left_to_the_update_rule(self) -> None:
        """The body can't know the current label; the route's rule decides (#964)."""
        assert EventUpdateBody(label="   ").label == "   "

    def test_short_hex_normalised(self) -> None:
        assert EventUpdateBody(color="#ABC").color == "#aabbcc"

    def test_none_values_pass_field_validators(self) -> None:
        body = EventUpdateBody(label=None, color=None, dash=None)
        assert body.label is None

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"\ndashh\n\s*Extra inputs are not permitted"):
            EventUpdateBody.model_validate({"dashh": "dot"})


class TestEventRecordImportBoundary:
    """legacy colour is preserved and drawn as the default (#857): the IMPORT
    boundary never fails on, or destroys, a non-hex colour an old file or DB
    holds; the dashboard's own boundary draws it as the default."""

    TS = "2026-01-01T00:00:00Z"

    def test_legacy_named_color_is_kept_unchanged(self) -> None:
        assert EventRecord.model_validate({"timestamp": self.TS, "color": "red"}).color == "red"

    def test_null_color_takes_the_default(self) -> None:
        assert EventRecord.model_validate({"timestamp": self.TS, "color": None}).color == "#888888"

    def test_short_hex_is_normalised(self) -> None:
        assert (
            EventRecord.model_validate({"timestamp": self.TS, "color": "#ABC"}).color == "#aabbcc"
        )

    def test_null_dash_takes_the_default(self) -> None:
        assert EventRecord.model_validate({"timestamp": self.TS, "dash": None}).dash == "dash"
