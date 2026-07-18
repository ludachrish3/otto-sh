"""Unit tests for the monitor boundary models (MetricPoint + import/export rows)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from otto.models import (
    ChartSpec,
    ChartSpecRecord,
    EventRecord,
    MetricPoint,
    MetricRecord,
    MonitorExport,
    SessionMeta,
    TabSpec,
    TabSpecRecord,
)
from otto.models.monitor import DEFAULT_MAX_SERIES_PER_CHART, RowModel

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


class TestMetricRecordSource:
    def test_source_defaults_none_and_is_omitted(self):
        rec = MetricRecord(
            timestamp=datetime(2026, 7, 1, 8, tzinfo=_UTC), host="r1", label="CPU %", value=1.0
        )
        assert rec.source is None
        assert "source" not in rec.model_dump(mode="json", exclude_none=True)

    def test_source_round_trips(self):
        rec = MetricRecord.model_validate(
            {
                "timestamp": "2026-07-01T08:00:00+00:00",
                "host": "chassis-a_lc1",
                "label": "PSU Temp °C",
                "value": 41.5,
                "source": "mgmt-01",
            }
        )
        assert rec.source == "mgmt-01"
        assert rec.model_dump(mode="json", exclude_none=True)["source"] == "mgmt-01"


class TestChartSpecMaxSeries:
    """``max_series=None`` (an uncapped chart) must survive ``exclude_none`` dumps.

    ``document_json`` (see :func:`otto.monitor.export.document_json`) dumps
    with ``exclude_none=True`` for frontend-fixture parity. Without
    ``ChartSpec``'s ``model_serializer(mode="wrap")``, that flag drops a
    meaningful ``max_series=None`` exactly like an absent field, and read-back
    refills the pydantic default (``DEFAULT_MAX_SERIES_PER_CHART``) — silently
    re-capping an uncapped chart (e.g. ``PerCoreCpuParser``'s "CPU" chart)
    every time a saved export round-trips. "Missing" must keep meaning
    "default-capped" (old exports written before this field existed) while an
    explicit ``None`` keeps meaning "uncapped".
    """

    def _spec(self, **overrides) -> ChartSpec:
        fields = {
            "label": "CPU",
            "y_title": "Usage %",
            "unit": "%",
            "command": "cat /proc/stat",
            "chart": "CPU",
            "max_series": None,
        }
        fields.update(overrides)
        return ChartSpec(**fields)

    def test_none_survives_exclude_none_round_trip(self):
        dumped = self._spec().model_dump(mode="json", exclude_none=True)
        assert dumped["max_series"] is None
        assert ChartSpec.model_validate(dumped).max_series is None

    def test_none_survives_when_nested_in_the_export_document(self):
        # The crux: document_json dumps a MonitorExport, not a bare ChartSpec —
        # the wrap serializer must fire from a nested model_dump too.
        record = ChartSpecRecord(**self._spec().model_dump())
        meta = SessionMeta(charts=[record], tabs=[])
        dumped = meta.model_dump(mode="json", exclude_none=True)
        assert dumped["charts"][0]["max_series"] is None
        assert SessionMeta.model_validate(dumped).charts[0].max_series is None

    def test_missing_key_still_defaults_to_capped(self):
        # A spec dict predating this field (no "max_series" key at all) must
        # keep validating to the default cap, not to uncapped.
        spec = ChartSpec.model_validate(self._spec().model_dump(exclude={"max_series"}))
        assert spec.max_series == DEFAULT_MAX_SERIES_PER_CHART


class TestExportDocument:
    def _doc(self) -> dict:
        return {
            "format": 1,
            "sessions": [
                {
                    "id": "s1",
                    "start": "2026-07-01T08:00:00+00:00",
                    "end": "2026-07-01T10:00:00+00:00",
                    "lab": {
                        "elements": [{"id": "spare-chassis", "type": "physical"}],
                        "hosts": [
                            {
                                "id": "chassis-a_lc1",
                                "element": "chassis-a",
                                "name": "chassis-a lc1",
                                "board": "lc1",
                                "slot": 1,
                                "hop": "edge-gw",
                                "os_type": "unix",
                                "os_name": "Linux",
                                "ip": "10.20.1.11",
                                "interfaces": {"eth0": "10.20.1.11"},
                                "labs": ["fixture"],
                                "is_virtual": True,
                            }
                        ],
                        "links": [
                            {
                                "id": "chassis-a_lc1--edge-gw",
                                "endpoints": [
                                    {"host": "edge-gw", "ip": "10.20.1.1"},
                                    {"host": "chassis-a_lc1", "interface": "eth0"},
                                ],
                                "protocol": "tcp",
                                "provenance": "implicit",
                            }
                        ],
                    },
                    "meta": {
                        "interval": 15.0,
                        "charts": [
                            {
                                "label": "CPU %",
                                "y_title": "CPU %",
                                "unit": "%",
                                "command": "fixture:cpu",
                                "chart": "cpu",
                                "interval": 15.0,
                            }
                        ],
                        "tabs": [{"id": "overview", "label": "Overview", "metrics": ["CPU %"]}],
                    },
                    "metrics": [
                        {
                            "timestamp": "2026-07-01T08:00:00+00:00",
                            "host": "chassis-a_lc1",
                            "label": "CPU %",
                            "value": 33.3,
                        }
                    ],
                    "events": [],
                    "log_events": [],
                    "chart_map": {"CPU %": "CPU %"},
                }
            ],
        }

    def test_round_trip(self):
        doc = MonitorExport.model_validate(self._doc())
        assert doc.format == 1
        s = doc.sessions[0]
        assert s.lab.hosts[0].slot == 1
        assert s.lab.links[0].provenance == "implicit"
        assert s.meta.charts[0].chart == "cpu"
        dumped = doc.model_dump(mode="json", exclude_none=True)
        assert MonitorExport.model_validate(dumped) == doc

    def test_format_field_is_required(self):
        # A legacy (unversioned) document must fail loud, not default to format 1.
        with pytest.raises(ValidationError):
            MonitorExport.model_validate({"sessions": []})

    def test_unknown_format_rejected(self):
        with pytest.raises(ValidationError):
            MonitorExport.model_validate({"format": 2, "sessions": []})

    def test_read_back_is_lenient(self):
        # Forward compat: an unknown key from a newer otto is ignored, not rejected.
        raw = self._doc()
        raw["sessions"][0]["lab"]["hosts"][0]["future_field"] = "x"
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].lab.hosts[0].id == "chassis-a_lc1"

    def test_link_provenance_validated(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["links"][0]["provenance"] = "tunnel"
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_link_needs_exactly_two_endpoints(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["links"][0]["endpoints"].append({"host": "x"})
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_open_session_and_optional_fields_omitted(self):
        raw = self._doc()
        del raw["sessions"][0]["end"]
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].end is None
        dumped = doc.model_dump(mode="json", exclude_none=True)
        assert "end" not in dumped["sessions"][0]
        assert "label" not in dumped["sessions"][0]

    def test_read_back_is_lenient_in_nested_meta(self):
        # The presentation-meta specs must be lenient too: chart definitions
        # drift over months exactly like lab configs (spec §2). Guards against
        # nesting the strict live-meta ChartSpec/TabSpec by accident.
        raw = self._doc()
        raw["sessions"][0]["meta"]["charts"][0]["future_style"] = "x"
        raw["sessions"][0]["meta"]["tabs"][0]["future_layout"] = "y"
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].meta.charts[0].chart == "cpu"
        assert doc.sessions[0].meta.tabs[0].id == "overview"

    def test_live_meta_specs_stay_strict(self):
        # Both halves of the seam split: the live, internal meta specs stay
        # strict, their export-record variants stay lenient.
        assert ChartSpec.model_config["extra"] == "forbid"
        assert TabSpec.model_config["extra"] == "forbid"
        assert ChartSpecRecord.model_config["extra"] == "ignore"
        assert TabSpecRecord.model_config["extra"] == "ignore"

    def test_link_needs_at_least_two_endpoints(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["links"][0]["endpoints"] = [{"host": "x"}]
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_element_type_validated(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["elements"][0]["type"] = "virtual"
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_host_element_required(self):
        raw = self._doc()
        del raw["sessions"][0]["lab"]["hosts"][0]["element"]
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)
