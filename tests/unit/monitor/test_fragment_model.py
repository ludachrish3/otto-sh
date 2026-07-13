"""The SSE fragment speaks format:1 — the same field names as the payload it appends to."""

import pytest
from pydantic import ValidationError

from otto.models.monitor import EventRecord, MetricRecord, MonitorSessionFragment, SessionRecord


class TestFragmentSpeaksFormat1:
    def test_metric_fragment_fields_match_the_session_record(self) -> None:
        """A fragment's metrics validate as SessionRecord.metrics do — no rename."""
        frag = MonitorSessionFragment.model_validate(
            {
                "format": 1,
                "session": "2026-07-12T10-00-00Z",
                "metrics": [
                    {
                        "timestamp": "2026-07-12T10:00:05Z",
                        "host": "r1",
                        "label": "cpu",
                        "value": 12.5,
                    }
                ],
            }
        )
        assert isinstance(frag.metrics[0], MetricRecord)
        assert frag.metrics[0].host == "r1"
        assert frag.metrics[0].value == 12.5

        # The same dict must validate inside a SessionRecord. If these two ever
        # disagree, the wire has drifted from the payload it appends to.
        rec = SessionRecord.model_validate(
            {
                "id": "s",
                "start": "2026-07-12T10:00:00Z",
                "metrics": [
                    {
                        "timestamp": "2026-07-12T10:00:05Z",
                        "host": "r1",
                        "label": "cpu",
                        "value": 12.5,
                    }
                ],
            }
        )
        assert rec.metrics[0].model_dump() == frag.metrics[0].model_dump()

    def test_every_payload_field_is_optional_except_session(self) -> None:
        frag = MonitorSessionFragment.model_validate({"format": 1, "session": "s"})
        assert frag.metrics == []
        assert frag.events == []
        assert frag.log_events == []
        assert frag.deleted_event_ids == []
        assert frag.chart_map == {}
        assert frag.meta is None

    def test_event_fragment_accepts_a_monitor_event_to_dict(self) -> None:
        """MonitorEvent.to_dict() is published verbatim — it must validate as EventRecord."""
        frag = MonitorSessionFragment.model_validate(
            {
                "format": 1,
                "session": "s",
                "events": [
                    {
                        "id": 3,
                        "timestamp": "2026-07-12T10:00:05Z",
                        "label": "boot",
                        "source": "manual",
                        "color": "#888888",
                        "dash": "dash",
                        "end_timestamp": None,
                    }
                ],
            }
        )
        assert isinstance(frag.events[0], EventRecord)
        assert frag.events[0].id == 3

    def test_session_is_required(self) -> None:
        with pytest.raises(ValidationError):
            MonitorSessionFragment.model_validate({"format": 1})
