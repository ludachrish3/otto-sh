"""Both export producers carry the last known tunnel set (spec 2026-07-16 §3)."""

import json
from datetime import datetime, timezone

from otto.models.monitor import LabSnapshot, TunnelRecord
from otto.monitor.collector import MetricCollector
from otto.monitor.db import SessionRow
from otto.monitor.export import _session_record, build_live_export
from otto.monitor.session import SessionFrame

REC = TunnelRecord(
    id="tun-a-1",
    protocol="udp",
    service_port=15001,
    hops=["a", "b"],
    status="ok",
    carriers_present=4,
    carriers_expected=4,
)
START = datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)


def test_live_export_carries_collector_tunnels() -> None:
    collector = MetricCollector(hosts=[])
    collector._tunnels = [REC]
    frame = SessionFrame(id="s1", label=None, note=None, start=START, end=None)
    doc = build_live_export(frame, collector, LabSnapshot())
    assert doc.sessions[0].tunnels == [REC]


def test_db_export_parses_tunnels_json() -> None:
    row = SessionRow(
        id="s1",
        label=None,
        note=None,
        start=START.isoformat(),
        end=None,
        lab_json="{}",
        meta_json="{}",
        chart_map_json="{}",
        tunnels_json=json.dumps([REC.model_dump(mode="json")]),
        metrics=[],
        events=[],
        log_events=[],
    )
    assert _session_record(row).tunnels == [REC]
