"""Harness self-tests + the wire-contract pins Phase 1 must keep green.

No browser: these run everywhere the hostless gate runs. The *_KEYS sets pin
the exact JSON shapes of /api/meta, /api/data, and SSE metric messages — the
contract the Phase 1 backend refactor and Phase 2 React port build against.
"""

import http.client
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from otto.monitor import server as server_module
from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import LogEvent
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [pytest.mark.hostless, pytest.mark.xdist_group("dashboard")]


@pytest.fixture(autouse=True)
def _tolerate_missing_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let these wire-contract pins run on a checkout that hasn't run ``make web``.

    Unlike its siblings in this directory, this module is *not*
    ``browser``-marked, so it isn't deselected by the hostless lanes'
    ``-m "not browser"`` filter — by design (see the module docstring: "these
    run everywhere the hostless gate runs"). Every test here hits ``/api/*``
    only, never ``/`` or the built JS/CSS, so a real React build buys
    nothing. When ``dist/index.html`` already exists (the real ``dashboard``
    e2e lane, which runs ``make web`` first), leave ``_STATIC_DIR`` alone so
    that lane still exercises the real build end to end. When it's missing
    (every hostless lane on a checkout that skipped ``make web``), stand in
    a throwaway marker page instead of letting ``MonitorServer.__init__``
    refuse to construct at all.
    """
    if (server_module._STATIC_DIR / "dist" / "index.html").exists():
        return
    static_dir = tmp_path / "_hermetic_static"
    dist_dir = static_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>HERMETIC_TEST_DIST_MARKER</html>")
    monkeypatch.setattr(server_module, "_STATIC_DIR", static_dir)


META_KEYS = {"hosts", "live", "metrics", "tabs", "interval"}
# "interval" (global cadence) added in Phase 2 — deliberate contract evolution.
META_METRIC_KEYS = {"label", "y_title", "unit", "command", "chart", "interval"}
# "interval" added in Phase 1 (per-parser collection intervals) — deliberate contract evolution.
META_TAB_KEYS = {"id", "label", "metrics"}
DATA_KEYS = {"series", "events", "chart_map", "log_events"}
# "log_events" added in Phase 3 Plan B (log-sourced data) — deliberate contract evolution.
LOG_EVENT_ROW_KEYS = {"timestamp", "host", "tab", "fields"}
SSE_LOG_EVENT_KEYS = {"type", "host", "tab", "rows"}
EVENT_KEYS = {"id", "timestamp", "label", "source", "color", "dash", "end_timestamp"}
SSE_METRIC_KEYS = {"type", "host", "label", "chart", "y_title", "unit", "key", "ts", "value"}
SSE_EVENT_KEYS = {"type", *EVENT_KEYS}
SSE_EVENT_DELETED_KEYS = {"type", "id"}


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:  # local test server
        return json.load(resp)


def test_serves_meta_and_data(live_dash: DashboardHarness[FakeCollector]) -> None:
    meta = _get_json(live_dash.url + "/api/meta")
    assert meta["live"] is True
    assert meta["hosts"] == ["host1", "host2"]
    data = _get_json(live_dash.url + "/api/data")
    assert len(data["series"]["host1/Overall CPU"]) == 3  # the preloaded ticks


def test_meta_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    meta = _get_json(live_dash.url + "/api/meta")
    assert set(meta) == META_KEYS
    assert all(set(m) == META_METRIC_KEYS for m in meta["metrics"])
    assert all(set(t) == META_TAB_KEYS for t in meta["tabs"])
    assert [t["id"] for t in meta["tabs"]] == ["cpu", "memory", "disk", "network"]


def test_data_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    live_dash.run(live_dash.collector.add_event(label="pinned", color="#112233", dash="dot"))
    data = _get_json(live_dash.url + "/api/data")
    assert set(data) == DATA_KEYS
    # Points carry ts/value always; meta only when present (exclude_none).
    point_keys = {k for pts in data["series"].values() for p in pts for k in p}
    assert {"ts", "value"} <= point_keys <= {"ts", "value", "meta"}
    # Pin the wire format, not just the key set: ts must stay ISO-8601.
    first_point = next(p for pts in data["series"].values() for p in pts)
    datetime.fromisoformat(first_point["ts"].replace("Z", "+00:00"))
    assert all(set(e) == EVENT_KEYS for e in data["events"])


def test_data_log_events_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    live_dash.run(
        live_dash.collector._record_log_events(
            "host1", "syslog", [LogEvent(ts=ts, fields={"message": "pinned"})]
        )
    )
    data = _get_json(live_dash.url + "/api/data")
    assert all(set(row) == LOG_EVENT_ROW_KEYS for row in data["log_events"])
    row = data["log_events"][0]
    assert row == {
        "timestamp": ts.isoformat(),
        "host": "host1",
        "tab": "syslog",
        "fields": {"message": "pinned"},
    }


def test_sse_stream_delivers_batched_log_events(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        live_dash.run(
            live_dash.collector._record_log_events(
                "host1",
                "syslog",
                [
                    LogEvent(ts=ts, fields={"message": "a"}),
                    LogEvent(ts=ts, fields={"message": "b"}),
                ],
            )
        )
        payload: dict[str, Any] | None = None
        while payload is None:
            line = resp.readline().decode()
            assert line, "SSE stream closed before a log_event message arrived"
            if line.startswith("data:"):
                candidate = json.loads(line[len("data:") :])
                if candidate["type"] == "log_event":
                    payload = candidate
    finally:
        conn.close()
    assert set(payload) == SSE_LOG_EVENT_KEYS
    assert payload["host"] == "host1"
    assert payload["tab"] == "syslog"
    assert [r["fields"]["message"] for r in payload["rows"]] == ["a", "b"]
    assert all(set(r) == {"ts", "fields"} for r in payload["rows"])


def test_sse_stream_delivers_metric_messages(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()  # subscribe() has run once headers arrive
        live_dash.run(live_dash.collector.push("host1", "Overall CPU", 42.0))
        payload: dict[str, Any] | None = None
        while payload is None:
            # HTTPResponse.readline() de-chunks; never read resp.fp (raw
            # socket file) or you'll see chunked-transfer framing lines.
            line = resp.readline().decode()
            assert line, "SSE stream closed before a metric message arrived"
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :])
    finally:
        conn.close()
    assert set(payload) == SSE_METRIC_KEYS
    assert payload["type"] == "metric"
    assert payload["key"] == "host1/Overall CPU"
    # Pin the wire format, not just the key set: ts must stay ISO-8601.
    datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))


def test_sse_stream_delivers_metric_messages_with_meta(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    """Pins the optional-meta variant: same shape plus a ``meta`` key when present."""
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()  # subscribe() has run once headers arrive
        live_dash.run(live_dash.collector.push("host1", "Overall CPU", 42.0, meta={"Used": "1 G"}))
        payload: dict[str, Any] | None = None
        while payload is None:
            line = resp.readline().decode()
            assert line, "SSE stream closed before a metric message arrived"
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :])
    finally:
        conn.close()
    assert set(payload) == SSE_METRIC_KEYS | {"meta"}
    assert payload["meta"] == {"Used": "1 G"}


def test_historical_fixture_loads(historical_dash: DashboardHarness[MetricCollector]) -> None:
    meta = _get_json(historical_dash.url + "/api/meta")
    assert meta["live"] is False
    assert meta["hosts"] == []  # bare labels → no host derived → historical UI
    assert [t["id"] for t in meta["tabs"]] == ["cpu", "memory", "disk", "network"]
    data = _get_json(historical_dash.url + "/api/data")
    assert set(data["series"]) == {"Overall CPU", "Load (1m)", "Memory Usage"}
    assert len(data["events"]) == 2


def test_stop_joins_server_thread(live_dash: DashboardHarness[FakeCollector]) -> None:
    live_dash.stop()  # idempotent with the fixture finalizer
    assert not live_dash.thread_alive


def _next_sse_payload(resp: http.client.HTTPResponse) -> dict[str, Any]:
    """Read lines until the next `data:` frame and parse its JSON payload."""
    while True:
        line = resp.readline().decode()
        assert line, "SSE stream closed before an expected message arrived"
        if line.startswith("data:"):
            return json.loads(line[len("data:") :])


def test_sse_event_lifecycle_wire_contract(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    """Pin the event/event_updated/event_deleted SSE shapes (metric shape is pinned above)."""
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()

        ev = live_dash.run(live_dash.collector.add_event(label="pin", color="#112233", dash="dot"))
        created = _next_sse_payload(resp)
        assert set(created) == SSE_EVENT_KEYS
        assert created["type"] == "event"
        assert created["id"] == ev.id
        # Pin the wire format, not just the key set: ts must stay ISO-8601.
        datetime.fromisoformat(created["timestamp"])

        live_dash.run(
            live_dash.collector.update_event(ev.id, label="pin2", color="#445566", dash="dash")
        )
        updated = _next_sse_payload(resp)
        assert set(updated) == SSE_EVENT_KEYS
        assert updated["type"] == "event_updated"
        assert updated["label"] == "pin2"

        live_dash.run(live_dash.collector.delete_event(ev.id))
        deleted = _next_sse_payload(resp)
        assert set(deleted) == SSE_EVENT_DELETED_KEYS
        assert deleted == {"type": "event_deleted", "id": ev.id}
    finally:
        conn.close()


def test_export_import_round_trip_preserves_values(
    live_dash: DashboardHarness[FakeCollector], tmp_path: Path
) -> None:
    """Losslessness at the value level, not just key sets (hostless twin of the browser pin)."""
    live_dash.run(live_dash.collector.add_event(label="evt", color="#112233", dash="dot"))
    exported = live_dash.run_export()

    out = tmp_path / "exported.json"
    out.write_text(exported)
    reloaded = MetricCollector.from_json(str(out))

    original = live_dash.collector.get_series()
    round_tripped = reloaded.get_series()
    assert round_tripped.keys() == original.keys()
    for key, pts in original.items():
        assert [(p.ts, p.value, p.meta) for p in round_tripped[key]] == [
            (p.ts, p.value, p.meta) for p in pts
        ]
    assert [e.to_dict() for e in reloaded.get_events()] == [
        e.to_dict() for e in live_dash.collector.get_events()
    ]
    assert reloaded.get_chart_map() == live_dash.collector.get_chart_map()
