"""Unit tests for the monitor boundary models (MetricPoint + import/export rows)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from otto.models import EventRecord, MetricPoint, MetricRecord
from otto.models.monitor import RowModel

_UTC = timezone.utc


class TestMetricPoint:
    def test_fields_round_trip(self):
        pt = MetricPoint(ts=datetime(2024, 3, 1, 10, tzinfo=_UTC), value=42.0, meta={"a": 1})
        assert pt.ts == datetime(2024, 3, 1, 10, tzinfo=_UTC)
        assert pt.value == 42.0
        assert pt.meta == {"a": 1}

    def test_meta_defaults_none(self):
        assert MetricPoint(ts=datetime(2024, 3, 1, 10, tzinfo=_UTC), value=1.0).meta is None

    def test_construct_skips_validation_and_dumps(self):
        # The hot live-append path: model_construct does no coercion/validation.
        pt = MetricPoint.model_construct(
            ts=datetime(2024, 3, 1, 10, tzinfo=_UTC), value=7.5, meta=None
        )
        assert pt.model_dump(mode="json", exclude_none=True) == {
            "ts": "2024-03-01T10:00:00Z",
            "value": 7.5,
        }

    def test_extra_forbidden(self):
        # MetricPoint is OttoModel — a stray key is an error, not silently dropped.
        with pytest.raises(ValidationError):
            MetricPoint(ts=datetime(2024, 3, 1, 10, tzinfo=_UTC), value=1.0, junk=2)


class TestMetricRecord:
    def test_accepts_json_spelling(self):
        rec = MetricRecord.model_validate(
            {
                "timestamp": "2024-03-01T10:00:00+00:00",
                "host": "r1",
                "label": "CPU %",
                "value": "33.3",
            }
        )
        assert rec.timestamp == datetime(2024, 3, 1, 10, tzinfo=_UTC)
        assert rec.host == "r1"
        assert rec.value == pytest.approx(33.3)  # string coerced to float

    def test_accepts_db_column_spelling(self):
        rec = MetricRecord.model_validate(
            {"ts": "2024-03-01T10:00:00+00:00", "label": "CPU %", "value": 33.3}
        )
        assert rec.timestamp == datetime(2024, 3, 1, 10, tzinfo=_UTC)
        assert rec.host == ""  # default for the pre-host-column schema

    def test_export_emits_json_spelling_and_omits_none_meta(self):
        rec = MetricRecord(
            timestamp=datetime(2024, 3, 1, 10, tzinfo=_UTC),
            host="",
            label="CPU %",
            value=9.8,
            meta=None,
        )
        dumped = rec.model_dump(mode="json", exclude_none=True)
        assert dumped == {
            "timestamp": "2024-03-01T10:00:00Z",
            "host": "",
            "label": "CPU %",
            "value": 9.8,
        }
        assert "meta" not in dumped  # exclude_none drops the None meta

    def test_unknown_keys_ignored(self):
        # Tolerant read-back: a stray key does not reject the row.
        rec = MetricRecord.model_validate(
            {"timestamp": "2024-03-01T10:00:00", "label": "X", "value": 1.0, "future_col": "v"}
        )
        assert rec.label == "X"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            MetricRecord.model_validate({"timestamp": "2024-03-01T10:00:00", "value": 1.0})


class TestEventRecord:
    def test_accepts_json_spelling_with_defaults(self):
        rec = EventRecord.model_validate(
            {"timestamp": "2024-03-01T10:00:00+00:00", "label": "start"}
        )
        assert rec.timestamp == datetime(2024, 3, 1, 10, tzinfo=_UTC)
        assert rec.label == "start"
        assert rec.source == "manual"
        assert rec.color == "#888888"
        assert rec.dash == "dash"
        assert rec.id is None
        assert rec.end_timestamp is None

    def test_accepts_db_column_spelling(self):
        rec = EventRecord.model_validate(
            {
                "id": 5,
                "ts": "2024-03-01T10:00:00+00:00",
                "end_ts": "2024-03-01T10:05:00+00:00",
                "label": "span",
                "source": "auto",
                "color": "#2ca02c",
                "dash": "solid",
            }
        )
        assert rec.id == 5
        assert rec.end_timestamp == datetime(2024, 3, 1, 10, 5, tzinfo=_UTC)

    def test_missing_timestamp_raises(self):
        with pytest.raises(ValidationError):
            EventRecord.model_validate({"label": "no ts"})


def test_rowmodel_base_is_lenient():
    assert RowModel.model_config["extra"] == "ignore"
