"""Harness self-tests + the wire-contract pins Phase 1/Plan 5b must keep green.

No browser: these run everywhere the hostless gate runs. The *_KEYS sets pin
the exact JSON shapes of /api/monitor_sessions and the SSE fragment messages —
the contract the Phase 1 backend refactor, Phase 2 React port, and Plan 5b
live streaming build against. ``/api/meta``/``/api/data`` were retired in
Plan 5b Task 3 (both modes now hydrate through /api/monitor_sessions).
"""

import contextlib
import http.client
import json
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from otto.models import (
    HostSnapshot,
    LabSnapshot,
    MetricRecord,
    MonitorExport,
    SessionMeta,
    SessionRecord,
)
from otto.monitor import server as server_module
from otto.monitor.collector import MetricCollector
from otto.monitor.session import new_frame
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


MODE_KEYS = {"mode", "source"}
LOG_EVENT_ROW_KEYS = {"timestamp", "host", "tab", "fields"}
EVENT_KEYS = {"id", "timestamp", "label", "source", "color", "dash", "end_timestamp"}
# The SSE stream speaks format:1 (plan 5b, Task 2): every message is a
# MonitorSessionFragment-shaped dict, so its top-level keys are always a
# subset of {"format", "session", "metrics", "events", "log_events",
# "deleted_event_ids", "chart_map", "meta"} — never a bare "type" tag.
SSE_LOG_EVENT_KEYS = {"format", "session", "log_events"}
SSE_METRIC_KEYS = {"format", "session", "metrics"}
SSE_EVENT_KEYS = {"format", "session", "events"}
SSE_EVENT_DELETED_KEYS = {"format", "session", "deleted_event_ids"}


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:  # local test server
        return json.load(resp)


_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 2, tzinfo=timezone.utc)


def _two_session_document() -> MonitorExport:
    """A two-session ``format:1`` document, built inline via the models.

    Stands in for a ``--db`` archive read back through ``build_db_export``
    without a real SQLite file — the review-mode server only needs a valid
    :class:`MonitorExport`, so constructing one directly is the more direct
    pin for the wire contract this module owns.
    """
    return MonitorExport(
        format=1,
        sessions=[
            SessionRecord(
                id="2026-07-01T00-00-00Z",
                label="run-1",
                start=_T0,
                end=_T0 + timedelta(hours=1),
                lab=LabSnapshot(hosts=[HostSnapshot(id="h1", element="h1")]),
                meta=SessionMeta(interval=5.0),
                metrics=[MetricRecord(timestamp=_T0, host="h1", label="CPU %", value=42.0)],
                chart_map={"CPU %": "CPU"},
            ),
            SessionRecord(
                id="2026-07-02T00-00-00Z",
                label="run-2",
                note="second run",
                start=_T1,
            ),
        ],
    )


@pytest.fixture
def live_export_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    """A live-mode harness with ``frame``/``lab`` supplied, for the routes that need them.

    ``live_dash`` (conftest.py) deliberately omits ``frame``/``lab`` — every
    pin that fixture backs hits ``/api/stream`` or ``/api/mode``, neither of
    which need them. ``/api/monitor_sessions`` and ``/api/export/json`` in
    live mode both build a snapshot document from ``frame``/``collector``/
    ``lab`` (see ``MonitorServer``'s ``monitor_sessions``/``export_json``
    routes) and 500 (``RuntimeError``) without them, so those pins get their
    own minimal harness rather than widening ``live_dash`` for every
    consumer.
    """
    harness = DashboardHarness(
        FakeCollector(), frame=new_frame(label=None, note=None), lab=LabSnapshot()
    ).start()
    yield harness
    harness.stop()


@pytest.fixture
def review_dash() -> Iterator[DashboardHarness[MetricCollector]]:
    """A review-mode harness serving :func:`_two_session_document` verbatim."""
    harness = DashboardHarness(
        MetricCollector(hosts=[], parsers=[]),
        mode="review",
        document=_two_session_document(),
        source_name="x.db",
    ).start()
    yield harness
    harness.stop()


def test_sse_stream_delivers_batched_log_events(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/stream?key={live_dash.server.key}",
            headers={"Accept": "text/event-stream"},
        )
        resp = conn.getresponse()
        ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        live_dash.run(
            live_dash.collector.push_log_events(
                "host1",
                tab="syslog",
                rows=[
                    (ts, {"message": "a"}),
                    (ts, {"message": "b"}),
                ],
            )
        )
        payload: dict[str, Any] | None = None
        while payload is None:
            line = resp.readline().decode()
            assert line, "SSE stream closed before a log_event message arrived"
            if line.startswith("data:"):
                candidate = json.loads(line[len("data:") :])
                if "log_events" in candidate:
                    payload = candidate
    finally:
        conn.close()
    assert set(payload) == SSE_LOG_EVENT_KEYS
    assert [le["host"] for le in payload["log_events"]] == ["host1", "host1"]
    assert [le["tab"] for le in payload["log_events"]] == ["syslog", "syslog"]
    assert [le["fields"]["message"] for le in payload["log_events"]] == ["a", "b"]
    assert all(set(le) == LOG_EVENT_ROW_KEYS for le in payload["log_events"])


def test_sse_stream_delivers_metric_messages(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/stream?key={live_dash.server.key}",
            headers={"Accept": "text/event-stream"},
        )
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
    assert payload["metrics"][0]["host"] == "host1"
    assert payload["metrics"][0]["label"] == "Overall CPU"
    assert payload["metrics"][0]["value"] == 42.0
    # Pin the wire format, not just the key set: timestamp must stay ISO-8601.
    datetime.fromisoformat(payload["metrics"][0]["timestamp"].replace("Z", "+00:00"))


def test_sse_stream_delivers_metric_messages_with_meta(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    """Pins the optional-meta variant: same shape plus a ``meta`` key when present."""
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/stream?key={live_dash.server.key}",
            headers={"Accept": "text/event-stream"},
        )
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
    # dp.meta rides inside the metric record itself (MetricRecord.meta), a
    # different field from the fragment's own top-level "meta" (the session
    # chart-catalog) — so the top-level key set is unchanged.
    assert set(payload) == SSE_METRIC_KEYS
    assert payload["metrics"][0]["meta"] == {"Used": "1 G"}


def test_mode_wire_contract_live(live_dash: DashboardHarness[FakeCollector]) -> None:
    payload = _get_json(live_dash.api_url("/api/mode"))
    assert set(payload) == MODE_KEYS
    assert payload["mode"] == "live"
    assert payload["source"] is None


def test_monitor_sessions_serves_a_live_snapshot(
    live_export_dash: DashboardHarness[FakeCollector],
) -> None:
    """Live boot reuses review's hydration path: the snapshot IS a format:1 payload.

    ``live_dash`` (no frame/lab) can't hit this route — /api/monitor_sessions
    in live mode needs both to build the snapshot, exactly like
    /api/export/json — so this uses ``live_export_dash`` instead, same as
    ``test_export_json_emits_format_1`` below.
    """
    payload = _get_json(live_export_dash.api_url("/api/monitor_sessions"))
    assert payload["format"] == 1
    assert len(payload["sessions"]) == 1
    session = payload["sessions"][0]
    assert "end" not in session, "a live session is one whose end is still open"


def test_export_json_emits_format_1(
    live_export_dash: DashboardHarness[FakeCollector],
) -> None:
    payload = _get_json(live_export_dash.api_url("/api/export/json"))
    assert payload["format"] == 1
    assert isinstance(payload["sessions"], list)
    assert len(payload["sessions"]) == 1
    session = payload["sessions"][0]
    assert {"id", "start", "lab", "meta", "metrics", "chart_map"} <= set(session)


def test_mode_wire_contract_review(review_dash: DashboardHarness[MetricCollector]) -> None:
    payload = _get_json(review_dash.api_url("/api/mode"))
    assert set(payload) == MODE_KEYS
    assert payload["mode"] == "review"
    assert payload["source"] == "x.db"


def test_monitor_sessions_round_trips_in_review_mode(
    review_dash: DashboardHarness[MetricCollector],
) -> None:
    """/api/monitor_sessions in review mode re-serves the loaded document verbatim.

    Same endpoint as the live snapshot above — review and live hydrate
    through the one route (see the module docstring).
    """
    payload = _get_json(review_dash.api_url("/api/monitor_sessions"))
    assert MonitorExport.model_validate(payload) == _two_session_document()


def test_stop_joins_server_thread(live_dash: DashboardHarness[FakeCollector]) -> None:
    live_dash.stop()  # idempotent with the fixture finalizer
    assert not live_dash.thread_alive


def _open_held_sse(port: int, key: str) -> socket.socket | None:
    """Open a raw /api/stream SSE and return the socket *without* closing it.

    Held open, it stands in for a live browser EventSource. Returns ``None`` if
    the connect is refused — i.e. the server has already stopped accepting,
    which is exactly the state force_stop should reach. ``key`` must be the
    harness's real access key: an unkeyed request now 403s and closes
    immediately (Task 2's per-run gate), which would make every "held" socket
    here a fast-closing rejection instead of a genuinely open SSE connection —
    quietly defeating the race this test hammers for.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    with contextlib.suppress(OSError):
        sock.sendall(
            f"GET /api/stream?key={key} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode()
        )
    return sock


def test_force_stop_survives_sse_opened_during_shutdown(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    """A fresh SSE connecting *while the server shuts down* must not wedge it.

    Regression for the WebKit dashboard flake (issue #106). The theme-toggle
    test reloads the page, so the browser drops its EventSource and immediately
    opens a new one. If that new /api/stream is accepted in the window after
    force_stop aborted its one-shot snapshot of connections but before uvicorn
    closes the listening socket, the un-aborted mid-stream h11 transport keeps
    the server task alive (``asyncio.Server.wait_closed()`` waits on it on
    3.12+) and the harness thread never exits — surfacing as the teardown
    ``RuntimeError: dashboard harness thread did not exit within 10s``.

    We reproduce it by hammering the port with held-open SSE connections from a
    background thread across the shutdown, so some land in that window. The fix
    closes the listening sockets and aborts in one loop callback, so nothing can
    slip in: post-abort connects are refused, and the thread exits promptly.
    """
    port = urlsplit(live_dash.url).port
    assert port is not None

    held: list[socket.socket] = []
    keep_opening = threading.Event()
    keep_opening.set()

    def _hammer() -> None:
        # Open (and hold) fresh SSE connections continuously. Running across the
        # stop() below, some land in the shutdown window; a refused connect
        # (None) just means the sockets have closed — the state we want.
        while keep_opening.is_set():
            sock = _open_held_sse(port, live_dash.server.key)
            if sock is not None:
                held.append(sock)
            time.sleep(0.004)

    hammer = threading.Thread(target=_hammer, name="sse-hammer", daemon=True)
    hammer.start()
    try:
        time.sleep(0.03)  # let a few EventSources establish before shutting down
        # stop() force-aborts and joins the server thread (raising if it doesn't
        # exit within 10s). The hammer keeps opening SSEs throughout, so the fix
        # is exercised against connections arriving mid-shutdown. Held open, an
        # un-aborted one would wedge the join the way a live browser reload does.
        try:
            live_dash.stop()
        except RuntimeError as exc:  # "thread did not exit within 10s"
            pytest.fail(f"force_stop failed to converge with SSEs opening mid-shutdown: {exc}")
        assert not live_dash.thread_alive
    finally:
        keep_opening.clear()
        hammer.join(timeout=2.0)
        for sock in held:
            sock.close()


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
    """Pin the add/update/delete event SSE fragment shapes (metric shape is pinned above)."""
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/stream?key={live_dash.server.key}",
            headers={"Accept": "text/event-stream"},
        )
        resp = conn.getresponse()

        ev = live_dash.run(live_dash.collector.add_event(label="pin", color="#112233", dash="dot"))
        created = _next_sse_payload(resp)
        assert set(created) == SSE_EVENT_KEYS
        assert set(created["events"][0]) == EVENT_KEYS
        assert created["events"][0]["id"] == ev.id
        # Pin the wire format, not just the key set: timestamp must stay ISO-8601.
        datetime.fromisoformat(created["events"][0]["timestamp"])

        live_dash.run(
            live_dash.collector.update_event(ev.id, label="pin2", color="#445566", dash="dash")
        )
        updated = _next_sse_payload(resp)
        assert set(updated) == SSE_EVENT_KEYS
        # No separate "updated" kind — the client upserts by id, so an edited
        # event is just an event.
        assert updated["events"][0]["label"] == "pin2"

        live_dash.run(live_dash.collector.delete_event(ev.id))
        deleted = _next_sse_payload(resp)
        assert set(deleted) == SSE_EVENT_DELETED_KEYS
        assert deleted == {"format": 1, "session": "", "deleted_event_ids": [ev.id]}
    finally:
        conn.close()
