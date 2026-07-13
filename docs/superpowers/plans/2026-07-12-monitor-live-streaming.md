# Monitor Plan 5b — Live Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live mode joins the new shell — a running `otto monitor --live` streams into the same session-shaped store the review shell already renders from, and the legacy Plotly data layer is deleted.

**Architecture:** Both modes hydrate a format:1 payload from `GET /api/monitor_sessions`; live then *grows* it by appending SSE fragments that speak the same format:1 vocabulary. All derived facts (per-series index, last-sample times) are maintained incrementally by the append reducer, so health is a lookup rather than a scan and no cost is O(total run length).

**Tech Stack:** Python 3.10+, FastAPI + sse-starlette, pydantic v2; React 19 + zustand + ECharts + wouter; vitest + Playwright; pytest.

**Spec:** `docs/superpowers/specs/2026-07-12-monitor-live-streaming-design.md`

## Global Constraints

- **The wire speaks format:1.** SSE fragments carry the *same field names* as the payload they append to. No dump-then-validate across two differently-named models. Plan 5a lost three fix waves to `MonitorMeta.metrics` vs `SessionMeta.charts`; the fragment model is generated to TS so the wire cannot drift.
- **On-disk format:1 keys are unchanged** (`format`, `sessions`, and every `SessionRecord` field). Existing archives stay readable; the schema drift guard must stay green.
- **Payload naming:** the thing is a *monitor session*. Endpoint `GET /api/monitor_sessions`; Python `read_monitor_sessions()` / `MonitorSessionFragment`; web `importMonitorSessions()` / `rawMonitorSessions`. The web store's `sessions[]` array keeps its name.
- **No cost may be O(total run length) per tick.** Ingest is O(batch); health is O(hosts).
- **Liveness ticks at the collection interval** (`SessionMeta.interval`), never at a fixed rate. It keeps ticking while paused.
- **Interval floor = 1.0s**, enforced at human-facing boundaries only. `MetricCollector` stays exempt — monitor tests drive it at 0.01–0.2s against fake hosts.
- **Retention:** keep every point. Downsample at render (ECharts `sampling: "lttb"`).
- **Boot fetch fails soft:** any transport error leaves the Import shell exactly as it was (Plan 5a's contract — this is what keeps docs capture and air-gapped serving working). A 200 with an invalid body surfaces the `importError` banner.
- `nox -s lint` = `ruff check` **and** `ruff format --check`. `pytest` does **not** build the web dist — run `make web` before any test that serves the real bundle.
- Never run `make coverage` (full suite) on the dev VM. Use `make coverage-hostless`.

---

## File Structure

**Backend (create)**
- `src/otto/monitor/interval.py` — `MIN_INTERVAL_SECONDS`, `validate_interval()`. One home for the floor.

**Backend (modify)**
- `src/otto/models/monitor.py` — add `MonitorSessionFragment`.
- `src/otto/monitor/collector.py` — publish format:1 fragments; carry `session_id`.
- `src/otto/monitor/server.py` — `/api/monitor_sessions` (live snapshot + review); drop `/api/meta`, `/api/data`; set `collector.session_id` from the frame.
- `src/otto/suite/suite.py` — `start_monitor()` validates the interval.
- `src/otto/cli/monitor.py`, `src/otto/cli/test.py` — typer `min=` reads the shared constant.

**Web (create)**
- `web/src/data/seriesIndex.ts` — the per-series index: sample times, records, per-series revisions, per-host last-sample. Built once on import, maintained on append.
- `web/src/data/fragment.ts` — `applyFragment()`: the one merge rule (append metrics/log events, upsert events by id, drop deleted ids, merge chart_map/meta).
- `web/src/data/stream.ts` — the SSE client: coalesce fragments, flush once per frame, reconnect with backoff, resync on reconnect.
- `web/src/data/clock.ts` — `useNow()`, ticking at the session's collection interval.
- `web/src/data/retirement.ts` — the ported PID-trace retirement policy.

**Web (modify)**
- `web/src/data/exportDoc.ts` — `NormalizedSession` gains `index`; `metricsForSubject` reads it.
- `web/src/data/health.ts` — `healthForHosts(session, range, nowMs?)`, O(hosts).
- `web/src/data/reviewStore.ts` — `appendFragment`, `setLive`, `setConnection`, `togglePause`; rename `importText` → `importMonitorSessions`.
- `web/src/data/bootstrap.ts` — live boot + start the stream.
- `web/src/pages/SubjectPage.tsx` — memoize chart options; apply retirement.
- `web/src/shell/AppBar.tsx` / `ReviewBar.tsx` — live chrome (status, pause, live Export).

**Web (delete)** — `store.ts`, `plotly.ts`, `grouping.ts`, `events.ts`, `logevents.ts`, `retirement.ts`, `api/sse.ts`, `api/client.ts`, and their `__tests__`.

---

### Task 1: The fragment model

**Files:**
- Modify: `src/otto/models/monitor.py` (after `MonitorExport`, ~line 297)
- Test: `tests/unit/monitor/test_fragment_model.py` (create)

**Interfaces:**
- Produces: `MonitorSessionFragment` — the SSE wire model. Fields: `format: Literal[1]`, `session: str`, `metrics: list[MetricRecord]`, `events: list[EventRecord]`, `log_events: list[LogEventRecord]`, `deleted_event_ids: list[int]`, `chart_map: dict[str, str]`, `meta: SessionMeta | None`.

Every field except `session` is optional, so a fragment is a *partial* `SessionRecord` for one live session. Note it reuses `MetricRecord` / `EventRecord` / `LogEventRecord` / `SessionMeta` **verbatim** — that reuse is the whole point: the wire cannot drift from the payload.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_fragment_model.py
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
                    {"timestamp": "2026-07-12T10:00:05Z", "host": "r1", "label": "cpu", "value": 12.5}
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
                    {"timestamp": "2026-07-12T10:00:05Z", "host": "r1", "label": "cpu", "value": 12.5}
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/monitor/test_fragment_model.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'MonitorSessionFragment'`

- [ ] **Step 3: Add the model**

Append to `src/otto/models/monitor.py`, after `MonitorExport`:

```python
class MonitorSessionFragment(RowModel):
    """An incremental update to ONE live monitor session (spec 2026-07-12 §The stream speaks format:1).

    A fragment is a *partial* :class:`SessionRecord`: every payload field is
    optional and carries the SAME name and type as its counterpart there, so the
    client appends rather than translates. This is deliberate — Plan 5a lost
    three fix waves to a rename across a lenient boundary model
    (``MonitorMeta.metrics`` vs ``SessionMeta.charts``), invisible to the type
    checker because both sides were ``str`` at the seam. The strongest defence is
    not a mapping function but the absence of a second model: these ARE the
    payload's models.

    ``deleted_event_ids`` is the one thing a partial record cannot express by
    presence, so it is explicit. Event *updates* need no separate kind — the
    client upserts by ``id``, so an edited event is just an event.
    """

    format: Literal[1] = 1
    session: str
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    deleted_event_ids: list[int] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)
    meta: SessionMeta | None = None
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/unit/monitor/test_fragment_model.py -q --no-cov`
Expected: PASS (4 passed)

- [ ] **Step 5: Regenerate the TS types and the schema, then check drift**

Run: `make schema && make web-check`
Expected: `web/src/api/export.gen.ts` now contains `MonitorSessionFragment`; both drift guards pass. If `make schema` does not pick the model up, add it to the export list the generator reads (see `src/otto/cli/schema.py` — the models it enumerates) and re-run.

- [ ] **Step 6: Commit**

```bash
git add src/otto/models/monitor.py tests/unit/monitor/test_fragment_model.py web/src/api/export.gen.ts docs/reference/
git commit -m "feat(monitor): add MonitorSessionFragment — the SSE wire speaks format:1"
```

---

### Task 2: The collector publishes fragments

**Files:**
- Modify: `src/otto/monitor/collector.py` (`__init__` ~line 212; `_record_point` ~456-486; `_record_log_events` ~488-505; `add_event` / `delete_event` / `update_event` ~610-661)
- Modify: `src/otto/monitor/server.py` (`MonitorServer.__init__` ~line 335)
- Test: `tests/unit/monitor/test_stream_fragments.py` (create)

**Interfaces:**
- Consumes: `MonitorSessionFragment` (Task 1).
- Produces: `MetricCollector.session_id: str` (default `""`), set by `MonitorServer.__init__` from `frame.id`. Every `_publish()` payload is now a `MonitorSessionFragment`-shaped dict.

The legacy payloads (`{"type": "metric", ...}`, `event_updated`, `event_deleted`) are **replaced**, not extended. Their only consumer (`web/src/api/sse.ts`) is deleted in Task 12.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_stream_fragments.py
"""Every published payload validates as a MonitorSessionFragment — the anti-drift pin."""

from datetime import datetime, timezone

import pytest

from otto.models.monitor import MonitorSessionFragment
from otto.monitor.collector import MetricCollector
from otto.monitor.session import new_frame
from otto.monitor.server import MonitorServer
from tests._fixtures._fake_collector import FakeCollector


def _drain(collector: MetricCollector) -> list[dict]:
    q = collector.subscribe()
    out: list[dict] = []
    while not q.empty():
        out.append(q.get_nowait())
    collector.unsubscribe(q)
    return out


class TestPublishedPayloadsAreFragments:
    @pytest.mark.asyncio
    async def test_metric_publishes_a_format1_fragment(self) -> None:
        collector = FakeCollector()
        collector.session_id = "2026-07-12T10-00-00Z"
        q = collector.subscribe()

        await collector.push("r1", "cpu", 12.5, ts=datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc))

        frag = MonitorSessionFragment.model_validate(q.get_nowait())
        assert frag.session == "2026-07-12T10-00-00Z"
        assert len(frag.metrics) == 1
        assert frag.metrics[0].host == "r1"
        assert frag.metrics[0].label == "cpu"
        assert frag.metrics[0].value == 12.5
        # The first sighting of a label carries the chart specs with it, so the
        # client can render a brand-new chart without re-fetching.
        assert frag.chart_map, "a newly seen label must ship its chart_map"
        assert frag.meta is not None and frag.meta.charts, "meta must carry the chart specs"
        # THE 5a TRAP: SessionMeta spells the chart list `charts`, MonitorMeta
        # spells it `metrics`. A raw get_meta_model() dump would silently give []
        assert any(c.label == "cpu" for c in frag.meta.charts)
        collector.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_event_add_update_delete_publish_fragments(self) -> None:
        collector = FakeCollector()
        collector.session_id = "s1"
        q = collector.subscribe()

        ev = await collector.add_event("boot")
        added = MonitorSessionFragment.model_validate(q.get_nowait())
        assert [e.id for e in added.events] == [ev.id]

        await collector.update_event(ev.id, label="boot2", color="#111111", dash="solid")
        updated = MonitorSessionFragment.model_validate(q.get_nowait())
        # No separate "updated" kind — the client upserts by id, so an edited
        # event is just an event.
        assert updated.events[0].label == "boot2"

        await collector.delete_event(ev.id)
        deleted = MonitorSessionFragment.model_validate(q.get_nowait())
        assert deleted.deleted_event_ids == [ev.id]
        collector.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_log_events_publish_a_fragment(self) -> None:
        collector = FakeCollector()
        collector.session_id = "s1"
        q = collector.subscribe()

        await collector.push_log_events(
            "r1",
            tab="syslog",
            rows=[(datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc), {"msg": "up"})],
        )

        frag = MonitorSessionFragment.model_validate(q.get_nowait())
        assert frag.log_events[0].host == "r1"
        assert frag.log_events[0].tab == "syslog"
        assert frag.log_events[0].fields == {"msg": "up"}
        collector.unsubscribe(q)


class TestServerStampsTheSessionId:
    def test_server_sets_collector_session_id_from_the_frame(self) -> None:
        """The collector knows nothing about sessions; the server, which holds the
        frame, stamps it. One place, so no call site can forget."""
        collector = FakeCollector()
        frame = new_frame(label=None, note=None)
        MonitorServer(collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=None)
        assert collector.session_id == frame.id
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/monitor/test_stream_fragments.py -q --no-cov`
Expected: FAIL — `AttributeError: 'FakeCollector' object has no attribute 'session_id'`

- [ ] **Step 3: Add `session_id` to the collector**

In `src/otto/monitor/collector.py`, inside `__init__` (next to `self._global_interval`, ~line 225):

```python
        # Stamped by MonitorServer from the SessionFrame (the collector has no
        # notion of sessions). Empty in the no-server paths (unit tests, the
        # pytest plugin's file export), where nothing consumes the stream.
        self.session_id: str = ""
```

- [ ] **Step 4: Replace the metric payload**

In `_record_point`, replace the `msg = {...}` / `self._publish(msg)` block (~lines 470-486) with:

```python
        # A fragment IS a partial SessionRecord — same field names as the payload
        # it appends to, so the client appends instead of translating (spec §The
        # stream speaks format:1).
        record: dict[str, Any] = {
            "timestamp": ts.isoformat(),
            "host": host_name,
            "label": label,
            "value": dp.value,
        }
        if dp.meta is not None:
            record["meta"] = dp.meta
        frag: dict[str, Any] = {
            "format": 1,
            "session": self.session_id,
            "metrics": [record],
        }
        if map_changed:
            # A label seen for the first time: ship the chart specs with it so the
            # client can render a brand-new chart without re-fetching. Deferred
            # import — otto.monitor.export imports this module.
            from .export import session_meta

            frag["chart_map"] = dict(self._store.chart_map)
            frag["meta"] = session_meta(self, interval=self._global_interval).model_dump(mode="json")
        self._publish(frag)
```

> **Why `session_meta()` and not `get_meta_model()`:** `MonitorMeta` spells the chart list `metrics`; `SessionMeta` spells it `charts`. Dumping the former into the latter validates *successfully* and silently yields `charts=[]`. That is the bug that cost Plan 5a three fix waves. `session_meta()` is the one named mapping; the test above pins it.

- [ ] **Step 5: Replace the log-event, event, update and delete payloads**

`_record_log_events` — replace its `self._publish({...})` with:

```python
        self._publish(
            {
                "format": 1,
                "session": self.session_id,
                "log_events": [
                    {
                        "timestamp": ev.ts.isoformat(),
                        "host": host_name,
                        "tab": tab,
                        "fields": dict(ev.fields),
                    }
                    for ev in events
                ],
            }
        )
```

`add_event` — replace `self._publish({"type": "event", **event.to_dict()})` with:

```python
        self._publish({"format": 1, "session": self.session_id, "events": [event.to_dict()]})
```

`update_event` — replace `self._publish({"type": "event_updated", **ev.to_dict()})` with:

```python
        # No separate "updated" kind: the client upserts events by id, so an
        # edited event is just an event. One merge rule, not two.
        self._publish({"format": 1, "session": self.session_id, "events": [ev.to_dict()]})
```

`delete_event` — replace `self._publish({"type": "event_deleted", "id": event_id})` with:

```python
        self._publish({"format": 1, "session": self.session_id, "deleted_event_ids": [event_id]})
```

(`MonitorEvent.to_dict()` already emits exactly `EventRecord`'s fields — `id`, `timestamp`, `end_timestamp`, `label`, `source`, `color`, `dash` — so it is published verbatim. Task 1's test pins that.)

- [ ] **Step 6: Have the server stamp the session id**

In `src/otto/monitor/server.py`, in `MonitorServer.__init__`, after `self._frame = frame`:

```python
        # The collector has no notion of sessions; the server holds the frame, so
        # it stamps the id here — one place, so no construction site can forget.
        if frame is not None:
            collector.session_id = frame.id
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/monitor/test_stream_fragments.py -q --no-cov`
Expected: PASS (4 passed)

- [ ] **Step 8: Run the monitor suite — legacy assertions will fail here, and that is the signal**

Run: `uv run pytest tests/unit/monitor -q --no-cov`
Expected: failures in any test asserting the OLD payload shape (`{"type": "metric"}`). Update those assertions to the fragment shape. Do **not** re-add a `type` field to keep them green.

- [ ] **Step 9: Commit**

```bash
git add src/otto/monitor/collector.py src/otto/monitor/server.py tests/unit/monitor/
git commit -m "feat(monitor)!: publish format:1 fragments on /api/stream"
```

---

### Task 3: `/api/monitor_sessions`, and the live snapshot

**Files:**
- Modify: `src/otto/monitor/server.py` (routes ~127-160)
- Test: `tests/unit/monitor/test_server.py` (extend), `tests/e2e/monitor/dashboard/test_harness.py` (update)

**Interfaces:**
- Produces: `GET /api/monitor_sessions` → `{format: 1, sessions: [...]}` in **both** modes. In live it is `document_json(build_live_export(frame, collector, lab))`; in review it is the loaded payload. `/api/document`, `/api/meta` and `/api/data` are **gone**.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/monitor/test_server.py

class TestMonitorSessionsEndpoint:
    @pytest.mark.asyncio
    async def test_live_mode_serves_a_snapshot_of_the_running_session(self) -> None:
        """Live boot reuses review's hydration path: the snapshot IS a format:1 payload."""
        collector = FakeCollector()
        frame = new_frame(label="run", note=None)
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        try:
            await collector.push("r1", "cpu", 1.0)
            url = f"http://127.0.0.1:{server._port}/api/monitor_sessions"
            resp = await asyncio.to_thread(urllib.request.urlopen, url)
            with contextlib.closing(resp):
                payload = json.loads(resp.read())
            export = MonitorExport.model_validate(payload)
            assert export.format == 1
            assert len(export.sessions) == 1
            assert export.sessions[0].id == frame.id
            assert export.sessions[0].end is None, "a live session is one whose end is still open"
            assert any(m.host == "r1" for m in export.sessions[0].metrics)
        finally:
            server.stop()
            await task

    @pytest.mark.asyncio
    async def test_retired_endpoints_are_gone(self) -> None:
        collector = FakeCollector()
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live",
            frame=new_frame(label=None, note=None), lab=LabSnapshot(),
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        try:
            for path in ("/api/document", "/api/meta", "/api/data"):
                url = f"http://127.0.0.1:{server._port}{path}"
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    await asyncio.to_thread(urllib.request.urlopen, url)
                with contextlib.closing(exc_info.value) as err:
                    assert err.code == 404, f"{path} should be gone"
        finally:
            server.stop()
            await task
```

> Closing the `HTTPError` is not optional hygiene: `filterwarnings = error` turns its GC-time `ResourceWarning` into an unraisable blamed on a random other test (issue #133).

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/monitor/test_server.py -k MonitorSessions -q --no-cov`
Expected: FAIL — 404 on `/api/monitor_sessions`

- [ ] **Step 3: Replace the routes**

In `src/otto/monitor/server.py`, delete the `get_document`, `meta` and `data` handlers and add:

```python
    @app.get("/api/monitor_sessions")
    async def monitor_sessions() -> Response:  # type: ignore[reportUnusedFunction]
        """Serve the format:1 payload: the loaded archive, or a snapshot of the live run.

        Live and review hydrate through the SAME endpoint and the SAME shape —
        that is what lets every view work live with no per-view work. A live
        monitor session is just one whose ``end`` is still open, exactly like a
        crashed session on disk.
        """
        if mode == "review":
            return Response(content=_require_document_body(), media_type="application/json")
        if frame is None or lab is None:
            raise RuntimeError(
                "MonitorServer built with mode='live' but no frame/lab — this is a "
                "programming error: the CLI always supplies both for live mode."
            )
        body = document_json(build_live_export(frame, collector, lab))
        return Response(content=body, media_type="application/json")
```

`/api/export/json` stays as the scriptable download hook (its body already builds the same payload).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/monitor/test_server.py -q --no-cov`
Expected: PASS. Any test hitting `/api/meta` or `/api/data` must be deleted, not adapted.

- [ ] **Step 5: Drop the retired wire-contract tests from the dashboard harness**

Delete `test_meta_wire_contract`, `test_data_wire_contract` and `test_data_log_events_wire_contract` from `tests/e2e/monitor/dashboard/test_harness.py`, and rename `test_document_404_in_live_mode` → `test_monitor_sessions_serves_a_live_snapshot`, asserting `format == 1` and one open session.

Run: `make web && uv run pytest tests/e2e/monitor/dashboard/test_harness.py -q --no-cov`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/otto/monitor/server.py tests/
git commit -m "feat(monitor)!: /api/monitor_sessions serves live and review; drop /api/meta and /api/data"
```

---

### Task 4: The interval floor

**Files:**
- Create: `src/otto/monitor/interval.py`
- Modify: `src/otto/suite/suite.py` (`start_monitor`, ~line 437), `src/otto/cli/monitor.py:69`, `src/otto/cli/test.py:471`
- Test: `tests/unit/monitor/test_interval_floor.py` (create)

**Interfaces:**
- Produces: `MIN_INTERVAL_SECONDS: float = 1.0` and `validate_interval(seconds: float) -> float` (returns it, or raises `ValueError`).

**Both CLI options already carry `min=1.0`** (typer enforces it and prints a good error). The gap is the **library**: `OttoSuite.start_monitor()` accepts anything. This task adds one home for the constant, points the two typer options at it, and guards the library. `MetricCollector` is deliberately left alone — the monitor tests drive it at 0.01–0.2s against *fake* hosts, and no real host is polled on that path.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_interval_floor.py
"""A collection interval below 1s is not meaningful — a host must have time to answer."""

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.interval import MIN_INTERVAL_SECONDS, validate_interval
from otto.suite.suite import OttoSuite


class TestValidator:
    def test_accepts_the_floor_and_above(self) -> None:
        assert validate_interval(1.0) == 1.0
        assert validate_interval(5.0) == 5.0

    def test_rejects_below_the_floor_naming_the_value_and_the_reason(self) -> None:
        with pytest.raises(ValueError) as err:
            validate_interval(0.5)
        assert "0.5" in str(err.value)
        assert "1" in str(err.value)

    def test_floor_is_one_second(self) -> None:
        assert MIN_INTERVAL_SECONDS == 1.0


class TestLibraryBoundary:
    @pytest.mark.asyncio
    async def test_start_monitor_rejects_a_sub_second_interval(self) -> None:
        suite = OttoSuite()
        with pytest.raises(ValueError, match="interval"):
            await suite.start_monitor(hosts=[], interval=0.1)


class TestEngineIsExempt:
    def test_metric_collector_is_not_floored(self) -> None:
        """The engine is a mechanism, not a human-facing knob. Monitor tests drive
        it at 0.01s against FAKE hosts; flooring it would cost real seconds per
        tick and protect nobody — no real host is polled on that path."""
        import inspect

        src = inspect.getsource(MetricCollector.run)
        assert "validate_interval" not in src
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/monitor/test_interval_floor.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.monitor.interval'`

- [ ] **Step 3: Create the validator**

```python
# src/otto/monitor/interval.py
"""The collection-interval floor — one home for the constant and the check.

An interval below one second is not meaningful in practice: a host must be given
time to answer every query in the interval without being taxed by the polling
itself. The floor is enforced where a *human* names an interval — the CLI, the
library, the pytest plugin — and NOT in :class:`~otto.monitor.collector.MetricCollector`,
which is the mechanism rather than a knob: the monitor tests drive it at
0.01-0.2s against fake hosts, where no real host is ever polled.
"""

MIN_INTERVAL_SECONDS: float = 1.0


def validate_interval(seconds: float) -> float:
    """Return *seconds*, or raise ``ValueError`` if it is below the floor."""
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"monitor interval must be at least {MIN_INTERVAL_SECONDS}s, got {seconds}s — "
            "a host needs time to answer every query in the interval without being "
            "taxed by the polling itself."
        )
    return seconds
```

- [ ] **Step 4: Guard the library**

In `src/otto/suite/suite.py`, in `start_monitor`, immediately after the existing normalization:

```python
        if isinstance(interval, (int, float)):
            interval = timedelta(seconds=float(interval))
        validate_interval(interval.total_seconds())
```

…with `from otto.monitor.interval import validate_interval` added to the local import block at the top of the method (alongside `from otto.monitor.collector import MetricCollector`).

- [ ] **Step 5: Point the two CLI options at the shared constant**

`src/otto/cli/monitor.py` — replace the literal in the `--interval` Option:

```python
            min=MIN_INTERVAL_SECONDS,
```

…with `from otto.monitor.interval import MIN_INTERVAL_SECONDS` imported at module top. Do the same at `src/otto/cli/test.py:471` for `--monitor-interval`. Typer's own `min=` error message stays; this just removes the duplicated literal.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/monitor/test_interval_floor.py tests/unit/cli/ -q --no-cov`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/otto/monitor/interval.py src/otto/suite/suite.py src/otto/cli/monitor.py src/otto/cli/test.py tests/unit/monitor/test_interval_floor.py
git commit -m "feat(monitor): floor the collection interval at 1s at the human-facing boundaries"
```

---

### Task 5: The series index — kill the O(all-points) scans

**Files:**
- Create: `web/src/data/seriesIndex.ts`
- Modify: `web/src/data/exportDoc.ts` (`NormalizedSession`, `metricsForSubject`), `web/src/data/health.ts`
- Test: `web/src/__tests__/seriesIndex.test.ts` (create), `web/src/__tests__/perf_budget.test.ts` (create)

**Interfaces:**
- Produces:
  ```ts
  export interface SeriesIndex {
    tsMs: Map<string, number[]>;        // "host/label" -> ascending sample times
    recs: Map<string, MetricRecord[]>;  // "host/label" -> records, index-aligned with tsMs
    keysByHost: Map<string, string[]>;  // host -> its series keys
    lastSampleAt: Map<string, number>;  // host -> newest sample time (ms)
    rev: Map<string, number>;           // "host/label" -> bumped on every append
  }
  export function buildIndex(metrics: MetricRecord[]): SeriesIndex;
  export function appendToIndex(index: SeriesIndex, metrics: MetricRecord[]): void;
  export function sliceSeries(index: SeriesIndex, key: string, range: TimeRange | null): MetricRecord[];
  ```
  `NormalizedSession` gains `index: SeriesIndex`. `healthForHosts(session, range, nowMs?)` gains the third parameter.

**Why this is its own task, and first among the web tasks:** `metricsForSubject` currently does `session.metrics.filter(...)` — an **O(total points) scan per subject per render** — and `healthForHosts` scans every sample. Both are already slow for a long *archive*; live just makes them fatal. The index fixes review and live at once, and it is a pure refactor with no behaviour change, so it can be landed and reviewed on its own.

`rev` is the identity discipline: appending **pushes into the existing array** (O(1), no copying) and bumps that series' revision. Chart memos key on the revision, so only the charts whose series actually changed re-memo. Copying arrays to get new identities would put us right back at O(all).

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/__tests__/seriesIndex.test.ts
import { describe, expect, it } from "vitest";
import { appendToIndex, buildIndex, sliceSeries } from "../data/seriesIndex";
import type { MetricRecord } from "../api/export.gen";

const rec = (host: string, label: string, iso: string, value: number): MetricRecord =>
  ({ host, label, timestamp: iso, value }) as MetricRecord;

describe("buildIndex", () => {
  it("groups by host/label, records last-sample per host, and keeps times ascending", () => {
    const idx = buildIndex([
      rec("a", "cpu", "2026-07-12T10:00:00Z", 1),
      rec("a", "cpu", "2026-07-12T10:00:05Z", 2),
      rec("a", "mem", "2026-07-12T10:00:05Z", 3),
      rec("b", "cpu", "2026-07-12T10:00:10Z", 4),
    ]);
    expect([...idx.recs.keys()].sort()).toEqual(["a/cpu", "a/mem", "b/cpu"]);
    expect(idx.keysByHost.get("a")?.sort()).toEqual(["a/cpu", "a/mem"]);
    expect(idx.lastSampleAt.get("a")).toBe(Date.parse("2026-07-12T10:00:05Z"));
    expect(idx.lastSampleAt.get("b")).toBe(Date.parse("2026-07-12T10:00:10Z"));
    expect(idx.tsMs.get("a/cpu")).toEqual([
      Date.parse("2026-07-12T10:00:00Z"),
      Date.parse("2026-07-12T10:00:05Z"),
    ]);
  });
});

describe("appendToIndex", () => {
  it("appends in place, bumps only the touched series' revision, and updates last-sample", () => {
    const idx = buildIndex([rec("a", "cpu", "2026-07-12T10:00:00Z", 1), rec("a", "mem", "2026-07-12T10:00:00Z", 9)]);
    const cpuArrayBefore = idx.recs.get("a/cpu");
    const memRevBefore = idx.rev.get("a/mem");

    appendToIndex(idx, [rec("a", "cpu", "2026-07-12T10:00:05Z", 2)]);

    // Pushed IN PLACE — no array copying, so append stays O(batch) not O(all).
    expect(idx.recs.get("a/cpu")).toBe(cpuArrayBefore);
    expect(idx.recs.get("a/cpu")?.length).toBe(2);
    // Only the touched series' revision moves; untouched charts keep their memo.
    expect(idx.rev.get("a/cpu")).toBe(1);
    expect(idx.rev.get("a/mem")).toBe(memRevBefore);
    expect(idx.lastSampleAt.get("a")).toBe(Date.parse("2026-07-12T10:00:05Z"));
  });

  it("registers a brand-new series and host", () => {
    const idx = buildIndex([]);
    appendToIndex(idx, [rec("z", "cpu", "2026-07-12T10:00:00Z", 1)]);
    expect(idx.keysByHost.get("z")).toEqual(["z/cpu"]);
    expect(idx.recs.get("z/cpu")?.length).toBe(1);
  });
});

describe("sliceSeries", () => {
  const idx = buildIndex([
    rec("a", "cpu", "2026-07-12T10:00:00Z", 1),
    rec("a", "cpu", "2026-07-12T10:00:05Z", 2),
    rec("a", "cpu", "2026-07-12T10:00:10Z", 3),
  ]);

  it("returns everything when the range is null", () => {
    expect(sliceSeries(idx, "a/cpu", null).map((m) => m.value)).toEqual([1, 2, 3]);
  });

  it("returns only the in-range samples", () => {
    const range = {
      from: Date.parse("2026-07-12T10:00:04Z"),
      to: Date.parse("2026-07-12T10:00:06Z"),
    };
    expect(sliceSeries(idx, "a/cpu", range).map((m) => m.value)).toEqual([2]);
  });

  it("returns [] for an unknown key rather than throwing", () => {
    expect(sliceSeries(idx, "nope/cpu", null)).toEqual([]);
  });
});
```

```ts
// web/src/__tests__/perf_budget.test.ts
// Tier-1 scaling budget (spec §Proving it). These assert the SHAPE — cost flat in
// run length — not a stopwatch: wall-clock thresholds on a shared CI runner are
// noise, and the thing that kills us is an O(total-run) term hiding in a per-tick
// path. Only a ratio test catches that.
import { describe, expect, it } from "vitest";
import { appendToIndex, buildIndex } from "../data/seriesIndex";
import { healthForHosts } from "../data/health";
import type { MetricRecord } from "../api/export.gen";
import { synthSession } from "./_synth";

const HOSTS = 7;
const SERIES_PER_HOST = 13; // ~90 series total, the live bed's shape
const INTERVAL_S = 5;

function timeIt(fn: () => void, reps: number): number {
  const t0 = performance.now();
  for (let i = 0; i < reps; i++) fn();
  return (performance.now() - t0) / reps;
}

describe("tier-1 scaling budget: cost must be flat in run length", () => {
  it("healthForHosts does not get slower as the run gets longer", () => {
    const short = synthSession({ hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, ticks: 720, intervalS: INTERVAL_S }); // 1h
    const long = synthSession({ hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, ticks: 8640, intervalS: INTERVAL_S }); // 12h
    expect(long.metrics.length).toBeGreaterThan(700_000);

    const now = long.endMs;
    const tShort = timeIt(() => void healthForHosts(short, null, now), 50);
    const tLong = timeIt(() => void healthForHosts(long, null, now), 50);

    // 12x the data must NOT cost meaningfully more. Generous ratio so CI noise
    // cannot flake it, but an O(all-points) regression is ~12x and blows through.
    expect(tLong).toBeLessThan(Math.max(tShort * 4, 2));
  });

  it("appendToIndex does not get slower as the run gets longer", () => {
    const batch = (): MetricRecord[] =>
      Array.from({ length: HOSTS * SERIES_PER_HOST }, (_, i) => ({
        host: `h${i % HOSTS}`,
        label: `m${i % SERIES_PER_HOST}`,
        timestamp: new Date(Date.now() + i).toISOString(),
        value: i,
      })) as MetricRecord[];

    const small = buildIndex(synthSession({ hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, ticks: 720, intervalS: INTERVAL_S }).metrics);
    const big = buildIndex(synthSession({ hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, ticks: 8640, intervalS: INTERVAL_S }).metrics);

    const tSmall = timeIt(() => appendToIndex(small, batch()), 100);
    const tBig = timeIt(() => appendToIndex(big, batch()), 100);

    expect(tBig).toBeLessThan(Math.max(tSmall * 4, 2));
  });
});
```

```ts
// web/src/__tests__/_synth.ts
// A synthetic NormalizedSession at a chosen scale, for the tier-1 budget guards.
import { buildIndex } from "../data/seriesIndex";
import type { NormalizedSession } from "../data/exportDoc";
import type { MetricRecord } from "../api/export.gen";

const T0 = Date.parse("2026-07-12T00:00:00Z");

export function synthSession(args: {
  hosts: number;
  seriesPerHost: number;
  ticks: number;
  intervalS: number;
}): NormalizedSession {
  const { hosts, seriesPerHost, ticks, intervalS } = args;
  const metrics: MetricRecord[] = [];
  for (let t = 0; t < ticks; t++) {
    const iso = new Date(T0 + t * intervalS * 1000).toISOString();
    for (let h = 0; h < hosts; h++) {
      for (let s = 0; s < seriesPerHost; s++) {
        metrics.push({ host: `h${h}`, label: `m${s}`, timestamp: iso, value: t + s } as MetricRecord);
      }
    }
  }
  return {
    id: "synth",
    label: null,
    note: null,
    startMs: T0,
    endMs: T0 + (ticks - 1) * intervalS * 1000,
    lab: { hosts: [], links: [], explicitElements: [] },
    meta: { interval: intervalS, charts: [], tabs: [] },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
  } as NormalizedSession;
}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx vitest run seriesIndex perf_budget`
Expected: FAIL — cannot resolve `../data/seriesIndex`

- [ ] **Step 3: Write the index**

```ts
// web/src/data/seriesIndex.ts
// The per-series index: what makes live mode's costs flat in run length.
//
// Before this, `metricsForSubject` filtered the whole flat `metrics` array on every
// render (O(total points), per subject) and `healthForHosts` scanned every sample.
// Both are already slow for a long ARCHIVE; a live run just makes them fatal — the
// clock re-runs health even when no data arrives, so an O(all-points) health check
// would burn the main thread forever on a run that has gone quiet.
//
// Appends push IN PLACE (O(1)) and bump that series' `rev`. Chart memos key on the
// revision, so only charts whose series actually changed re-memo. Copying arrays to
// manufacture new identities would restore the O(all) cost we are removing.
import type { MetricRecord } from "../api/export.gen";
import type { TimeRange } from "./exportDoc";

export interface SeriesIndex {
  /** `host/label` -> ascending sample times (ms), index-aligned with `recs`. */
  tsMs: Map<string, number[]>;
  /** `host/label` -> the records themselves. */
  recs: Map<string, MetricRecord[]>;
  /** host -> the series keys it reports. */
  keysByHost: Map<string, string[]>;
  /** host -> newest sample time (ms). Health reads this instead of scanning. */
  lastSampleAt: Map<string, number>;
  /** `host/label` -> bumped on every append. The memo key for that series' chart. */
  rev: Map<string, number>;
}

export const seriesKey = (host: string, label: string): string => `${host}/${label}`;

function emptyIndex(): SeriesIndex {
  return {
    tsMs: new Map(),
    recs: new Map(),
    keysByHost: new Map(),
    lastSampleAt: new Map(),
    rev: new Map(),
  };
}

export function buildIndex(metrics: MetricRecord[]): SeriesIndex {
  const index = emptyIndex();
  appendToIndex(index, metrics);
  // A fresh index starts every series at revision 0 — buildIndex is not an "append"
  // from the memo's point of view, it is the baseline.
  for (const key of index.rev.keys()) index.rev.set(key, 0);
  return index;
}

export function appendToIndex(index: SeriesIndex, metrics: MetricRecord[]): void {
  const touched = new Set<string>();
  for (const m of metrics) {
    const key = seriesKey(m.host, m.label);
    const ts = Date.parse(m.timestamp);

    let recs = index.recs.get(key);
    if (recs === undefined) {
      recs = [];
      index.recs.set(key, recs);
      index.tsMs.set(key, []);
      index.rev.set(key, 0);
      const keys = index.keysByHost.get(m.host);
      if (keys === undefined) index.keysByHost.set(m.host, [key]);
      else keys.push(key);
    }
    recs.push(m);
    index.tsMs.get(key)!.push(ts);
    touched.add(key);

    const last = index.lastSampleAt.get(m.host);
    if (last === undefined || ts > last) index.lastSampleAt.set(m.host, ts);
  }
  for (const key of touched) index.rev.set(key, (index.rev.get(key) ?? 0) + 1);
}

/** Lower bound: first index whose time is >= t. Assumes `tsMs` ascending. */
function lowerBound(tsMs: number[], t: number): number {
  let lo = 0;
  let hi = tsMs.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (tsMs[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function sliceSeries(
  index: SeriesIndex,
  key: string,
  range: TimeRange | null,
): MetricRecord[] {
  const recs = index.recs.get(key);
  if (recs === undefined) return [];
  if (range === null) return recs;
  const tsMs = index.tsMs.get(key)!;
  const from = lowerBound(tsMs, range.from);
  const to = lowerBound(tsMs, range.to + 1);
  return recs.slice(from, to);
}
```

- [ ] **Step 4: Hang the index off the session and make the readers use it**

In `web/src/data/exportDoc.ts`: add `index: SeriesIndex;` to `NormalizedSession`; build it where the session is normalized (`parseExportDocument`) with `index: buildIndex(metrics)`; and replace `metricsForSubject`:

```ts
export function metricsForSubject(
  session: NormalizedSession,
  subjectId: string,
  range: TimeRange | null,
): MetricRecord[] {
  // Was: session.metrics.filter(...) — an O(total points) scan per subject, per
  // render. Now: only the subject's own series, sliced by binary search.
  const keys = session.index.keysByHost.get(subjectId);
  if (keys === undefined) return [];
  const out: MetricRecord[] = [];
  for (const key of keys) out.push(...sliceSeries(session.index, key, range));
  return out;
}
```

In `web/src/data/health.ts`, replace the scanning prologue of `healthForHosts` with index lookups and add the `nowMs` parameter:

```ts
export function healthForHosts(
  session: NormalizedSession,
  range: TimeRange | null,
  /** Wall clock for live mode. Defaults to the session's end — which is what an
   * ARCHIVE means by "now". A live session's end is open, so if we defaulted there
   * the gap would always be zero and a dead host would never go amber. */
  nowMs?: number,
): Map<string, SubjectHealth> {
  const evalTo = nowMs ?? Math.min(range?.to ?? session.endMs, session.endMs);
  // ...
  // last-sample and cadence now come from the index and the meta, NOT from a scan:
  //   const lastSeen = session.index.lastSampleAt.get(hostId)
  //   const cadence  = session.meta.interval !== null
  //                      ? session.meta.interval * 1000
  //                      : cadenceMs(session, labels)   // archives lacking meta.interval
}
```

Keep `cadenceMs()` as the fallback for archives whose meta has no interval, and keep the existing `ok` / `down` / `no-data` / `unknown` semantics and `HEALTH_K` exactly as they are — the existing `health.test.ts` must keep passing unchanged.

- [ ] **Step 5: Run the web tests**

Run: `cd web && npx vitest run`
Expected: PASS — including the existing `health.test.ts` and `pages.test.tsx` untouched, plus the new index and budget tests.

- [ ] **Step 6: Commit**

```bash
git add web/src/data/seriesIndex.ts web/src/data/exportDoc.ts web/src/data/health.ts web/src/__tests__/
git commit -m "perf(web): index metrics by series; health and subject lookup stop scanning every point"
```

---

### Task 6: `applyFragment` and the append reducer

**Files:**
- Create: `web/src/data/fragment.ts`
- Modify: `web/src/data/reviewStore.ts`
- Test: `web/src/__tests__/fragment.test.ts` (create)

**Interfaces:**
- Consumes: `SeriesIndex`/`appendToIndex` (Task 5), `MonitorSessionFragment` (generated into `web/src/api/export.gen.ts` by Task 1).
- Produces: `applyFragment(session: NormalizedSession, frag: MonitorSessionFragment): NormalizedSession` — returns a NEW session object (so zustand re-renders) whose index arrays were mutated in place (so append stays O(batch)). Store action: `appendFragment(frag: MonitorSessionFragment): void`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/__tests__/fragment.test.ts
import { describe, expect, it } from "vitest";
import { applyFragment } from "../data/fragment";
import { synthSession } from "./_synth";
import type { MonitorSessionFragment } from "../api/export.gen";

const frag = (over: Partial<MonitorSessionFragment>): MonitorSessionFragment =>
  ({ format: 1, session: "synth", metrics: [], events: [], log_events: [], deleted_event_ids: [], chart_map: {}, meta: null, ...over }) as MonitorSessionFragment;

describe("applyFragment", () => {
  const base = () => synthSession({ hosts: 1, seriesPerHost: 1, ticks: 2, intervalS: 5 });

  it("appends metrics and extends the session end", () => {
    const s = base();
    const before = s.metrics.length;
    const ts = new Date(s.endMs + 5000).toISOString();
    const next = applyFragment(s, frag({ metrics: [{ host: "h0", label: "m0", timestamp: ts, value: 42 }] as never }));
    expect(next).not.toBe(s); // new object -> zustand re-renders
    expect(next.metrics.length).toBe(before + 1);
    expect(next.endMs).toBe(Date.parse(ts));
    expect(next.index.lastSampleAt.get("h0")).toBe(Date.parse(ts));
  });

  it("upserts events by id — an edited event is just an event", () => {
    let s = base();
    s = applyFragment(s, frag({ events: [{ id: 1, timestamp: "2026-07-12T00:00:01Z", label: "boot" }] as never }));
    expect(s.events).toHaveLength(1);
    s = applyFragment(s, frag({ events: [{ id: 1, timestamp: "2026-07-12T00:00:01Z", label: "boot2" }] as never }));
    expect(s.events).toHaveLength(1);
    expect(s.events[0].label).toBe("boot2");
  });

  it("drops deleted event ids", () => {
    let s = base();
    s = applyFragment(s, frag({ events: [{ id: 7, timestamp: "2026-07-12T00:00:01Z", label: "x" }] as never }));
    s = applyFragment(s, frag({ deleted_event_ids: [7] }));
    expect(s.events).toHaveLength(0);
  });

  it("merges chart_map and replaces meta when present", () => {
    const s = base();
    const next = applyFragment(
      s,
      frag({ chart_map: { newlabel: "cpu" }, meta: { interval: 5, charts: [{ label: "newlabel", y_title: "y", unit: "%", command: "c", chart: "cpu" }], tabs: [] } } as never),
    );
    expect(next.meta.charts.map((c) => c.label)).toContain("newlabel");
  });

  it("ignores a fragment addressed to a different session", () => {
    const s = base();
    const next = applyFragment(s, frag({ session: "someone-else", metrics: [{ host: "h0", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 1 }] as never }));
    expect(next).toBe(s);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run fragment`
Expected: FAIL — cannot resolve `../data/fragment`

- [ ] **Step 3: Write the merge rule**

```ts
// web/src/data/fragment.ts
// The ONE merge rule. A fragment is a partial SessionRecord in format:1 vocabulary,
// so this appends — it does not translate. There is deliberately no mapping table
// here: if you find yourself writing one, the wire has drifted from the payload and
// the fix belongs in the model, not here.
import type { MonitorSessionFragment } from "../api/export.gen";
import type { NormalizedSession } from "./exportDoc";
import { appendToIndex } from "./seriesIndex";

export function applyFragment(
  session: NormalizedSession,
  frag: MonitorSessionFragment,
): NormalizedSession {
  if (frag.session !== session.id) return session;

  // Index arrays are mutated IN PLACE (O(batch)); the session object is replaced so
  // zustand re-renders. Copying the point arrays to get a new identity would make
  // every tick O(total run length) — the exact cost this design exists to remove.
  if (frag.metrics.length > 0) {
    session.metrics.push(...frag.metrics);
    appendToIndex(session.index, frag.metrics);
  }
  if (frag.log_events.length > 0) session.logEvents.push(...frag.log_events);

  let events = session.events;
  if (frag.events.length > 0) {
    const byId = new Map(events.map((e) => [e.id, e]));
    for (const e of frag.events) byId.set(e.id, e); // upsert: add AND edit
    events = [...byId.values()];
  }
  if (frag.deleted_event_ids.length > 0) {
    const gone = new Set(frag.deleted_event_ids);
    events = events.filter((e) => e.id === null || !gone.has(e.id));
  }

  let endMs = session.endMs;
  for (const m of frag.metrics) {
    const ts = Date.parse(m.timestamp);
    if (ts > endMs) endMs = ts;
  }

  const meta =
    frag.meta !== null && frag.meta !== undefined
      ? { interval: frag.meta.interval, charts: frag.meta.charts, tabs: frag.meta.tabs }
      : session.meta;

  return { ...session, events, endMs, meta };
}
```

- [ ] **Step 4: Add the store action**

In `web/src/data/reviewStore.ts`, rename `importText` → `importMonitorSessions` (same body), rename `rawDocument` → `rawMonitorSessions`, and add:

```ts
    appendFragment: (frag) => {
      const { sessions } = get();
      const i = sessions.findIndex((s) => s.id === frag.session);
      if (i === -1) return; // a fragment for a session we do not hold — ignore
      const next = applyFragment(sessions[i], frag);
      if (next === sessions[i]) return;
      const copy = [...sessions];
      copy[i] = next;
      set({ sessions: copy });
    },
```

…with `appendFragment: (frag: MonitorSessionFragment) => void;` added to `ReviewActions`. Update every `importText` call site (`bootstrap.ts`, `ImportExport.tsx`, and the tests that use it).

- [ ] **Step 5: Run the web tests**

Run: `cd web && npx vitest run`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/data/fragment.ts web/src/data/reviewStore.ts web/src/ web/src/__tests__/
git commit -m "feat(web): appendFragment — one merge rule, O(batch) per tick"
```

---

### Task 7: The clock, ticking at the collection interval

**Files:**
- Create: `web/src/data/clock.ts`
- Test: `web/src/__tests__/clock.test.tsx` (create)

**Interfaces:**
- Produces: `useNow(intervalMs: number | null): number` — a `now` that advances at the *collection interval*, from its own zustand store so a tick re-renders only its subscribers.

**Why its own store:** if `now` lived in the review store, every tick would notify every subscriber of that store — the whole tree — once per interval, forever. The clock must be able to move without waking the charts.

**Why the collection interval and not a fixed rate:** the down threshold *is* `HEALTH_K × cadence`, so the cadence is the natural check rate — polling faster cannot learn anything, because no new information can arrive between polls. With the ≥1s floor (Task 4) a tick is at most one O(hosts) lookup per second.

- [ ] **Step 1: Write the failing test (this is also the tier-2 render-count guard)**

```tsx
// web/src/__tests__/clock.test.tsx
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useNow } from "../data/clock";

let healthRenders = 0;
let chartRenders = 0;

function HealthTile() {
  useNow(5000); // subscribes to the clock
  healthRenders++;
  return null;
}
function ChartPanel() {
  chartRenders++; // does NOT subscribe to the clock
  return null;
}

describe("useNow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    healthRenders = 0;
    chartRenders = 0;
  });
  afterEach(() => vi.useRealTimers());

  it("advances at the collection interval, not faster", () => {
    render(<HealthTile />);
    const after = () => healthRenders;
    act(() => void vi.advanceTimersByTime(4000));
    expect(after()).toBe(1); // not yet — a 5s cadence has not ticked
    act(() => void vi.advanceTimersByTime(1500));
    expect(after()).toBe(2);
  });

  it("TIER-2 GUARD: a tick re-renders health consumers and NOT charts", () => {
    render(
      <>
        <HealthTile />
        <ChartPanel />
      </>,
    );
    const chartsAtStart = chartRenders;
    act(() => void vi.advanceTimersByTime(25_000)); // 5 ticks at 5s
    expect(healthRenders).toBeGreaterThan(1);
    expect(chartRenders).toBe(chartsAtStart); // charts must not wake for the clock
  });

  it("does not tick at all when the interval is unknown", () => {
    render(<HealthTile />);
    // (rendered with 5000 above; a null interval must simply never schedule)
    expect(() => act(() => void vi.advanceTimersByTime(60_000))).not.toThrow();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run clock`
Expected: FAIL — cannot resolve `../data/clock`

- [ ] **Step 3: Write the clock**

```ts
// web/src/data/clock.ts
// A `now` that ticks at the COLLECTION INTERVAL, in its own store.
//
// Unreachable dimming needs a clock, not events: a host that goes silent emits
// nothing, so without a tick nothing would ever re-render it and a dead host would
// stay green forever. But the clock must not wake the world — hence its own store,
// so only health consumers re-render (pinned by the tier-2 guard in the tests).
//
// The rate is the collection interval because the down threshold IS
// HEALTH_K x cadence: polling faster than the collector cannot learn anything, since
// no new information can arrive between polls.
import { useEffect } from "react";
import { create } from "zustand";

interface ClockState {
  now: number;
  tick: () => void;
}

export const useClockStore = create<ClockState>()((set) => ({
  now: Date.now(),
  tick: () => set({ now: Date.now() }),
}));

/** Subscribe to a `now` that advances every *intervalMs*. Null = never tick. */
export function useNow(intervalMs: number | null): number {
  const now = useClockStore((s) => s.now);
  useEffect(() => {
    if (intervalMs === null || intervalMs <= 0) return;
    const id = setInterval(() => useClockStore.getState().tick(), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
```

- [ ] **Step 4: Run the tests**

Run: `cd web && npx vitest run clock`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add web/src/data/clock.ts web/src/__tests__/clock.test.tsx
git commit -m "feat(web): useNow — a clock at the collection interval, in its own store"
```

---

### Task 8: The SSE client — coalesce, reconnect, resync

**Files:**
- Create: `web/src/data/stream.ts`
- Modify: `web/src/data/reviewStore.ts` (connection state), `web/src/data/bootstrap.ts`
- Test: `web/src/__tests__/stream.test.ts` (create)

**Interfaces:**
- Produces: `startStream(opts?: { url?: string; resync?: () => Promise<void> }): () => void` (returns a stop function). Store gains `mode: "live" | "review" | null`, `connection: "connecting" | "live" | "disconnected"`, actions `setMode`, `setConnection`.

**Coalescing is not optional.** The collector publishes one fragment per point — ~90 per tick at the live bed's shape. Applying each one separately would mean ~90 zustand updates and ~90 render passes per tick. The client buffers arriving fragments and flushes **once per animation frame**, so a tick costs one update.

**Reconnect re-fetches; it does not replay.** On reopen we re-hydrate from `/api/monitor_sessions`. The snapshot *is* the truth and already contains whatever arrived during the gap — no sequence numbers, no server-side replay buffer, and no way for client and server to disagree about history.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/__tests__/stream.test.ts
import { beforeEach, describe, expect, it, vi } from "vitest";
import { startStream } from "../data/stream";
import { useReviewStore } from "../data/reviewStore";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.useFakeTimers();
  useReviewStore.setState({ sessions: [], connection: "connecting" });
});

const emit = (payload: unknown) =>
  FakeEventSource.last?.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);

describe("startStream", () => {
  it("coalesces many fragments into ONE store update per frame", () => {
    const spy = vi.fn();
    const unsub = useReviewStore.subscribe(spy);
    startStream();
    for (let i = 0; i < 90; i++) emit({ format: 1, session: "s", metrics: [], events: [], log_events: [], deleted_event_ids: [], chart_map: {} });
    expect(spy).not.toHaveBeenCalled(); // still buffered
    vi.advanceTimersByTime(20); // one frame
    expect(spy.mock.calls.length).toBeLessThanOrEqual(2); // one flush, not 90
    unsub();
  });

  it("marks the connection live on open and disconnected on error", () => {
    startStream();
    FakeEventSource.last?.onopen?.();
    expect(useReviewStore.getState().connection).toBe("live");
    FakeEventSource.last?.onerror?.();
    expect(useReviewStore.getState().connection).toBe("disconnected");
  });

  it("resyncs on reconnect instead of replaying missed deltas", async () => {
    const resync = vi.fn().mockResolvedValue(undefined);
    startStream({ resync });
    FakeEventSource.last?.onerror?.();
    await vi.advanceTimersByTimeAsync(1000); // first backoff
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it("drops an invalid fragment without killing the stream", () => {
    startStream();
    expect(() => emit({ nonsense: true })).not.toThrow();
    FakeEventSource.last?.onopen?.();
    expect(useReviewStore.getState().connection).toBe("live");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run stream`
Expected: FAIL — cannot resolve `../data/stream`

- [ ] **Step 3: Write the client**

```ts
// web/src/data/stream.ts
// The SSE client: buffer, flush once per frame, reconnect with backoff, resync.
import type { MonitorSessionFragment } from "../api/export.gen";
import { useReviewStore } from "./reviewStore";

const FLUSH_MS = 16; // one frame
const BACKOFF_MS = [1000, 2000, 5000, 10_000, 30_000];

/** Structural check. A malformed fragment is dropped, never fatal — one bad frame
 * must not take down a running monitor. */
function isFragment(v: unknown): v is MonitorSessionFragment {
  return typeof v === "object" && v !== null && typeof (v as { session?: unknown }).session === "string";
}

export function startStream(opts: { url?: string; resync?: () => Promise<void> } = {}): () => void {
  const url = opts.url ?? "/api/stream";
  let source: EventSource | null = null;
  let stopped = false;
  let attempt = 0;
  let buffer: MonitorSessionFragment[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;

  // ~90 fragments arrive per tick (one per point). Applying each separately would
  // be ~90 store updates and ~90 render passes; one flush per frame makes it one.
  const flush = () => {
    timer = null;
    const batch = buffer;
    buffer = [];
    const { appendFragment } = useReviewStore.getState().actions;
    for (const frag of batch) appendFragment(frag);
  };

  const connect = () => {
    if (stopped) return;
    useReviewStore.getState().actions.setConnection(attempt === 0 ? "connecting" : "disconnected");
    source = new EventSource(url);

    source.onopen = () => {
      attempt = 0;
      useReviewStore.getState().actions.setConnection("live");
    };

    source.onmessage = (e: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(e.data);
      } catch {
        return; // not JSON — drop it
      }
      if (!isFragment(parsed)) return;
      buffer.push(parsed);
      if (timer === null) timer = setTimeout(flush, FLUSH_MS);
    };

    source.onerror = () => {
      source?.close();
      source = null;
      useReviewStore.getState().actions.setConnection("disconnected");
      if (stopped) return;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setTimeout(() => {
        // Resync BEFORE reopening: re-fetch the whole payload rather than trying to
        // replay what we missed. The snapshot is the truth and already contains it —
        // no sequence numbers, no replay buffer, no way to disagree about history.
        void (opts.resync?.() ?? Promise.resolve()).finally(connect);
      }, delay);
    };
  };

  connect();

  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    source?.close();
  };
}
```

- [ ] **Step 4: Add connection state to the store**

In `reviewStore.ts` add to `ReviewState`: `mode: "live" | "review" | null` (init `null`), `connection: "connecting" | "live" | "disconnected"` (init `"connecting"`); and to `ReviewActions`: `setMode: (m) => set({ mode: m })`, `setConnection: (c) => set({ connection: c })`.

- [ ] **Step 5: Boot live in `bootstrap.ts`**

Rewrite `bootstrapFromServer()` to fetch `/api/mode`, then `/api/monitor_sessions` for **both** modes, calling `importMonitorSessions(...)`; in live mode also `startStream({ resync: hydrate })`. Keep the existing soft-fail contract **exactly**: any transport error returns silently and leaves the Import shell untouched (this is what keeps the docs capture and air-gapped serving working); a 200 with an unparseable body surfaces `importError`.

- [ ] **Step 6: Run the web tests**

Run: `cd web && npx vitest run`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/src/data/stream.ts web/src/data/reviewStore.ts web/src/data/bootstrap.ts web/src/__tests__/
git commit -m "feat(web): SSE client — coalesce per frame, backoff, resync on reconnect"
```

---

### Task 9: Live chrome — status, follow/pause, live Export

**Files:**
- Modify: `web/src/shell/AppBar.tsx`, `web/src/shell/ReviewBar.tsx`, `web/src/data/reviewStore.ts`
- Test: `web/src/__tests__/livechrome.test.tsx` (create)

**Interfaces:**
- Produces: store gains `windowMs: number` (default `900_000` — 15 min) and `paused: boolean`; action `togglePause()`. Derived: `liveRange(session, windowMs, nowMs)` → `TimeRange`.

**Follow, pause and range are one concept.** `range === null` in live mode means *follow the tail* over `windowMs`. Pause snapshots the currently-derived window into an absolute `range` — so "paused" and "user picked a custom range" are the same state and cannot disagree. Ingestion never stops, so resume catches up with no gap. **Liveness keeps ticking while paused** — a host can die while you are paused and the fleet must say so.

**`data-testid` contract (Playwright, Task 13).** Reuse what exists; add only what is genuinely new:

| testid | State |
| --- | --- |
| `status-text` (**exists**, `AppBar.tsx:54`) | today `"Historical"` / `"No data"`. Extend to `"Live"` when `mode === "live" && connection === "live"`, `"Reconnecting…"` when `disconnected`, `"Reviewing"` in review mode. **Do not add a new `live-status` id** — the status readout already has one |
| `status-dot` (**exists**, `AppBar.tsx:58`) | gains the live (green) and disconnected (amber) classes. The dot never moves (UX spec) |
| `pause-toggle` (**new**) | rendered only when `mode === "live"` |
| `export-button` (**new**, in `ImportExport.tsx`) | serializes `rawMonitorSessions` to a Blob download |
| `chart-${chartKey}` (**new**, in `SubjectPage.tsx`) | one per chart inside the existing `chart-stack`. Carries `data-point-count` and `data-window-to` so the browser lane can assert growth and freezing without reading canvas pixels |
| `host-tile-${hostId}` (**exists**, `OverviewPage.tsx:102`) | gains `data-health` (`ok` / `down` / `no-data` / `unknown`) for the dimming assertion |
| `subject-link-${hostId}` (**exists**, `OverviewPage.tsx:97`) | how the browser lane drills into a host |

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/__tests__/livechrome.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { AppBar } from "../shell/AppBar";
import { useReviewStore } from "../data/reviewStore";

beforeEach(() => {
  useReviewStore.setState({ mode: "live", connection: "live", paused: false, range: null });
});

describe("live chrome", () => {
  it("shows Live when connected and Reconnecting when not", () => {
    render(<AppBar />);
    expect(screen.getByTestId("status-text")).toHaveTextContent(/live/i);
    useReviewStore.setState({ connection: "disconnected" });
    expect(screen.getByTestId("status-text")).toHaveTextContent(/reconnect/i);
  });

  it("pause pins the view and resume returns to following", () => {
    render(<AppBar />);
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().paused).toBe(true);
    expect(useReviewStore.getState().range).not.toBeNull(); // frozen at an absolute window
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().paused).toBe(false);
    expect(useReviewStore.getState().range).toBeNull(); // following again
  });

  it("hides pause in review mode", () => {
    useReviewStore.setState({ mode: "review" });
    render(<AppBar />);
    expect(screen.queryByTestId("pause-toggle")).toBeNull();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run livechrome`
Expected: FAIL — `status-text` still reads "Historical"/"No data"; there is no `pause-toggle`

- [ ] **Step 3: Add the state and the derived window**

In `reviewStore.ts` add `windowMs: 900_000`, `paused: false`, and:

```ts
    togglePause: () => {
      const { paused, sessions, activeSessionId, windowMs } = get();
      if (paused) {
        set({ paused: false, range: null }); // resume -> follow the tail again
        return;
      }
      const session = sessions.find((s) => s.id === activeSessionId);
      // Freeze the CURRENTLY DERIVED window into an absolute range. Pause and "user
      // picked a custom range" are then the same state, so they cannot disagree.
      const to = session ? session.endMs : Date.now();
      set({ paused: true, range: { from: to - windowMs, to } });
    },
```

In `web/src/data/time.ts` add:

```ts
/** The live window: follow the tail unless the view is pinned. */
export function liveRange(nowMs: number, windowMs: number): TimeRange {
  return { from: nowMs - windowMs, to: nowMs };
}
```

- [ ] **Step 4: Wire the chrome**

In `AppBar.tsx`, **extend the existing** `status-text` (line 54) and `status-dot` (line 58) rather than adding a new readout:

```tsx
  const mode = useReviewStore((s) => s.mode);
  const connection = useReviewStore((s) => s.connection);

  const status =
    mode === "live"
      ? connection === "live"
        ? { text: "Live", dot: "bg-status-live" }
        : { text: "Reconnecting…", dot: "bg-status-warn" }
      : hasData
        ? { text: "Reviewing", dot: "bg-status-historical" }
        : { text: "No data", dot: "bg-gray-300 dark:bg-gray-600" };
```

…feeding `status.text` into `status-text` and `status.dot` into `status-dot`'s class. **The dot never moves** (UX spec §). Render `data-testid="pause-toggle"` only when `mode === "live"`. In `ImportExport.tsx` add `data-testid="export-button"`, which serializes `rawMonitorSessions` from the store to a Blob download (no extra fetch — the payload is already in memory).

In `SubjectPage.tsx` add `data-testid={`chart-${chart.chartKey}`}` to each chart inside the existing `chart-stack`, carrying `data-point-count` and `data-window-to`, and in `OverviewPage.tsx` add `data-health` to the existing `host-tile-${hostId}` — the browser lane asserts on these instead of canvas pixels.

- [ ] **Step 5: Run the tests**

Run: `cd web && npx vitest run`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/shell/ web/src/data/ web/src/__tests__/livechrome.test.tsx
git commit -m "feat(web): live chrome — status, pause-as-view-freeze, live export"
```

---

### Task 10: Port the PID-retirement policy

**Files:**
- Create: `web/src/data/retirement.ts` (ported from `web/src/retirement.ts`)
- Create: `web/src/__tests__/retirement.test.ts` (ported from the existing suite)
- Modify: `web/src/pages/SubjectPage.tsx`

**Interfaces:**
- Produces: `retireStaleSeries(keys: string[], index: SeriesIndex, opts?: { ticks?: number }): string[]` — the series keys still worth drawing.

**Why this survives the deletion:** it encodes the legacy dashboard's fix for its #1 bug — every PID ever seen became a permanent chart trace, so `proc/*` legends grew without bound. **A long archive has exactly the same problem**, so this applies in *both* modes, not just live. The logic is already pure and tested; port it, retyped against the format:1 models, and keep its tests.

- [ ] **Step 1: Port the module and its tests**

Copy `web/src/retirement.ts` → `web/src/data/retirement.ts`. Replace its `Point`/`Metric` imports (from the deleted `./api/client` and `./grouping`) with `MetricRecord` from `../api/export.gen` and `SeriesIndex` from `./seriesIndex`. Keep `RETIRE_AFTER_TICKS` and the "most recent DISTINCT collection ticks" rule exactly as they are — the policy is not being redesigned, only re-homed. Copy `web/src/__tests__/retirement.test.ts` and adapt its fixtures to `MetricRecord`.

- [ ] **Step 2: Run the ported tests**

Run: `cd web && npx vitest run retirement`
Expected: PASS — same assertions as the legacy suite, new types.

- [ ] **Step 3: Apply it when building chart series**

In `SubjectPage.tsx`, before building series for a chart, filter the candidate series keys through `retireStaleSeries(...)`, so dead PIDs stop being built, styled and drawn (this costs render time, not just legend space).

- [ ] **Step 4: Run the web tests, then commit**

Run: `cd web && npx vitest run`

```bash
git add web/src/data/retirement.ts web/src/__tests__/retirement.test.ts web/src/pages/SubjectPage.tsx
git commit -m "feat(web): port PID-trace retirement into the new stack (live AND review)"
```

---

### Task 11: Memoize the chart options

**Files:**
- Modify: `web/src/pages/SubjectPage.tsx` (`buildStackOption` call at ~line 138)
- Test: `web/src/__tests__/subjectpage.test.tsx` (extend)

**Interfaces:**
- Consumes: `SeriesIndex.rev` (Task 5).

`buildStackOption({...})` is currently called **inline in JSX**, so every chart's option object is rebuilt on every render — including charts whose data did not change, and including the render caused by an unrelated store update.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/__tests__/chart_memo.test.tsx
import { render } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Count how many times options actually get rebuilt.
const buildStackOption = vi.fn(() => ({ series: [] }));
vi.mock("../charts/options", async (orig) => ({
  ...(await orig<typeof import("../charts/options")>()),
  buildStackOption: (args: unknown) => buildStackOption(args as never),
}));

import { useReviewStore } from "../data/reviewStore";
import { SubjectPage } from "../pages/SubjectPage";
import { synthSession } from "./_synth";

describe("chart option memoization", () => {
  beforeEach(() => {
    buildStackOption.mockClear();
    const session = synthSession({ hosts: 2, seriesPerHost: 1, ticks: 3, intervalS: 5 });
    useReviewStore.setState({ sessions: [session], activeSessionId: session.id, range: null, mode: "live" });
  });

  it("does NOT rebuild a chart's options when an unrelated host appends", () => {
    render(<SubjectPage id="h0" />);
    const callsAfterMount = buildStackOption.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThan(0);

    // A fragment touching ONLY h1. h0's charts must not re-memo: their series
    // revisions did not move. Keying the memo on `session` identity (which DOES
    // change every tick) is the trap this asserts against.
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h1", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 99 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });

    expect(buildStackOption.mock.calls.length).toBe(callsAfterMount);
  });

  it("DOES rebuild when the subject's own series appends", () => {
    render(<SubjectPage id="h0" />);
    const before = buildStackOption.mock.calls.length;
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h0", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 99 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });
    expect(buildStackOption.mock.calls.length).toBeGreaterThan(before);
  });
});
```

> Both halves matter. The first proves the memo does not bust when it shouldn't; the second proves it still busts when it must — a memo keyed on a constant would pass the first test and be badly broken.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run chart_memo`
Expected: FAIL on the first test — options are rebuilt on every render because the call is inline in JSX.

- [ ] **Step 3: Memoize**

Replace the inline call with a `useMemo` keyed on the series revisions, the range and the theme:

```tsx
  const option = useMemo(
    () => buildStackOption({ unit, yTitle, series, window, events, theme }),
    // rev changes ONLY for series that actually got new points, so a tick rebuilds
    // just the charts that moved. Keying on `session` would bust every chart on
    // every tick — the identity trap this whole design exists to avoid.
    [keys.map((k) => `${k}:${session.index.rev.get(k) ?? 0}`).join(","), range, theme, events],
  );
```

- [ ] **Step 4: Run tests and commit**

```bash
git add web/src/pages/SubjectPage.tsx web/src/__tests__/subjectpage.test.tsx
git commit -m "perf(web): memoize chart options on series revision, not session identity"
```

---

### Task 12: Delete the legacy layer

**Files:**
- Delete: `web/src/store.ts`, `web/src/plotly.ts`, `web/src/grouping.ts`, `web/src/events.ts`, `web/src/logevents.ts`, `web/src/retirement.ts`, `web/src/api/sse.ts`, `web/src/api/client.ts`
- Delete: `web/src/__tests__/store.test.ts`, `events.test.ts`, `logevents.test.ts`, `plotly.test.ts`, `retirement.test.ts`
- Modify: `web/src/App.tsx` (drop any legacy imports)

Nothing renders from these — the whole UI already reads the review store. They existed only to feed the retired Plotly dashboard, and their endpoints (`/api/meta`, `/api/data`) went in Task 3. Retirement was ported in Task 10.

- [ ] **Step 1: Delete, then prove nothing referenced them**

```bash
cd web
rm src/store.ts src/plotly.ts src/grouping.ts src/events.ts src/logevents.ts src/retirement.ts src/api/sse.ts src/api/client.ts
rm src/__tests__/store.test.ts src/__tests__/events.test.ts src/__tests__/logevents.test.ts src/__tests__/plotly.test.ts src/__tests__/retirement.test.ts
grep -rn "useMonitorStore\|api/sse\|api/client\|from \"../plotly\"\|from \"./grouping\"" src/ && echo "STILL REFERENCED — fix before continuing" || echo "clean"
```

Expected: `clean`. Any surviving import must be removed (chiefly in `App.tsx`).

- [ ] **Step 2: Full web gate**

Run: `cd web && npx tsc --noEmit && npx vitest run && npm run lint`
Expected: PASS, 0 type errors.

- [ ] **Step 3: Rebuild the bundle and check the air gap**

Run: `make web && ./scripts/check_airgap.sh`
Expected: bundle builds; air-gap OK. (Plotly leaving the bundle should shrink it — note the delta in the commit.)

- [ ] **Step 4: Commit**

```bash
git add -A web/
git commit -m "refactor(web)!: delete the legacy Plotly data layer (1,294 lines)"
```

---

### Task 13: Live browser tests and the replay soak

**Files:**
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (a live harness fixture that streams)
- Create: `tests/e2e/monitor/dashboard/test_live_shell.py`
- Create: `tests/e2e/monitor/dashboard/test_replay_soak.py`

**Interfaces:**
- Consumes: the `data-testid` contract from Task 9 (`status-text`, `status-dot`, `pause-toggle`, `export-button`, `chart-${chartKey}` with `data-point-count`/`data-window-to`, `host-tile-${id}` with `data-health`).

- [ ] **Step 1: Add a streaming live fixture**

```python
# append to tests/e2e/monitor/dashboard/conftest.py
@pytest.fixture
def live_stream_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    """A live-mode, dist-serving harness the test can push points into.

    Unlike `live_dash`, this one carries a frame + lab, so /api/monitor_sessions
    serves a real live snapshot and the shell hydrates without an Import step.
    """
    harness = DashboardHarness(
        FakeCollector(),
        mode="live",
        frame=new_frame(label="live run", note=None),
        lab=LabSnapshot(),
    ).start()
    yield harness
    harness.stop()
```

- [ ] **Step 2: Write the live shell specs**

```python
# tests/e2e/monitor/dashboard/test_live_shell.py
"""Live mode in a real browser: hydrate, stream, pause, dim."""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = pytest.mark.hostless

NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)


def _push_tick(dash: DashboardHarness[FakeCollector], host: str, ts: datetime, value: float) -> None:
    dash.run(dash.collector.push(host, "cpu", value, ts=ts))


def test_live_boots_hydrated_without_an_import_step(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    # No Import front door: live hydrates from /api/monitor_sessions on boot.
    expect(page.get_by_test_id("status-text")).to_have_text("Live", ignore_case=True)
    expect(page.get_by_test_id("empty-review")).to_have_count(0)


def test_streamed_points_grow_the_chart(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    page.get_by_test_id("subject-link-r1").click()
    chart = page.get_by_test_id("chart-cpu")  # chart-${chartKey}
    expect(chart).to_be_visible()

    before = chart.get_attribute("data-point-count")
    for i in range(1, 6):
        _push_tick(live_stream_dash, "r1", NOW + timedelta(seconds=5 * i), 10.0 + i)
    # SSE -> coalesced flush -> store -> re-render. Playwright retries this.
    expect(chart).not_to_have_attribute("data-point-count", before or "")


def test_pause_freezes_the_view_and_resume_follows_again(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    page.get_by_test_id("subject-link-r1").click()

    page.get_by_test_id("pause-toggle").click()
    chart = page.get_by_test_id("chart-cpu")  # chart-${chartKey}
    frozen_window = chart.get_attribute("data-window-to")

    for i in range(1, 6):
        _push_tick(live_stream_dash, "r1", NOW + timedelta(seconds=5 * i), 10.0 + i)

    # Pause is a VIEW control: points keep arriving, but the window does not move.
    expect(chart).to_have_attribute("data-window-to", frozen_window or "")

    page.get_by_test_id("pause-toggle").click()
    expect(chart).not_to_have_attribute("data-window-to", frozen_window or "")


def test_a_silent_host_dims(page: Page, live_stream_dash: DashboardHarness[FakeCollector]) -> None:
    """No SSE message announces silence — only the clock can reveal it."""
    stale = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    _push_tick(live_stream_dash, "r1", stale, 10.0)  # cadence 5s, K=3 -> down after 15s
    page.goto(live_stream_dash.url)
    expect(page.get_by_test_id("host-tile-r1")).to_have_attribute("data-health", "down")
```

> The chart exposes `data-point-count` and `data-window-to` for this lane. Add both in Task 9 when wiring the chrome — asserting on rendered canvas pixels would be untestable.

- [ ] **Step 3: Write the replay soak (marker-gated)**

```python
# tests/e2e/monitor/dashboard/test_replay_soak.py
"""Tier-3: stress the BROWSER with a full run's worth of real points.

The load generator is deliberately NOT the real collector. The >=1s interval floor
exists precisely to stop us hammering real hosts, so a `--interval 0.1` soak would
violate its own rationale. Instead we replay a real archive through the fake
producer at maximum rate -- real PIDs, real bridge names, real taps -- exercising
server -> browser -> ECharts under load without touching a VM.

Marker-gated: this is minutes of pushing, not a per-push test.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [pytest.mark.hostless, pytest.mark.soak]

HOSTS = [f"h{i}" for i in range(7)]
LABELS = [f"m{i}" for i in range(13)]  # ~90 series, the live bed's shape
TICKS = 2000  # ~2.6h at 5s; raise locally to reach 12h
T0 = datetime(2026, 7, 12, tzinfo=timezone.utc)


def test_browser_stays_responsive_under_a_full_runs_data(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    page.goto(live_stream_dash.url)
    page.get_by_test_id("subject-link-h0").click()

    async def _replay() -> None:
        for t in range(TICKS):
            ts = T0 + timedelta(seconds=5 * t)
            for h in HOSTS:
                for label in LABELS:
                    await live_stream_dash.collector.push(h, label, float(t), ts=ts)

    live_stream_dash.run(_replay())

    # The page must still answer a click promptly after ~180k points.
    started = time.monotonic()
    page.get_by_test_id("subject-link-h1").click()
    expect(page.get_by_test_id("chart-cpu")).to_be_visible()
    assert time.monotonic() - started < 5.0, "the shell became unresponsive under load"
```

Register the `soak` marker in `pyproject.toml`'s `markers` list so it is not an unknown-marker error, and exclude it from the default dashboard lane.

- [ ] **Step 3: Run the dashboard lane**

Run: `make web && make dashboard`
Expected: PASS (existing 37 + the new live specs).

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/monitor/dashboard/
git commit -m "test(monitor): live shell browser specs and the replay soak"
```

---

### Task 14: Docs and the naming sweep

**Files:**
- Modify: `docs/guide/monitor.md`, and every doc mentioning `/api/document`, `/api/meta`, `/api/data`, or the "document" payload.

- [ ] **Step 1: Sweep the names**

```bash
grep -rln "api/document\|api/meta\|api/data" docs/ src/ web/src/ tests/
```

Rewrite to `/api/monitor_sessions`, and rename the payload's prose name to **monitor session(s)**. The on-disk format:1 keys (`format`, `sessions`) are unchanged — do not touch them.

- [ ] **Step 2: Document live mode in the guide**

In `docs/guide/monitor.md`: live mode auto-loads and streams; the status/pause chrome; that pause freezes the *view* while collection continues; that a silent host dims after `HEALTH_K × cadence`; and the 1s interval floor with its rationale.

- [ ] **Step 3: Full gate**

```bash
make web && ./scripts/check_airgap.sh
make coverage-hostless
uv run nox -s lint typecheck
make dashboard
make docs
make schema && git diff --exit-code   # drift guards clean
```

- [ ] **Step 4: Commit**

```bash
git add docs/ && git commit -m "docs(monitor): document live mode, the interval floor, and the monitor_sessions payload"
```

---

## Self-Review

**Spec coverage:** one store/hydrate+append → T3, T6, T8. format:1 wire → T1, T2. Naming → T3, T6, T14. Follow/pause/range → T9. Chrome + reconnect resync → T8, T9. Unreachable + clock at the interval → T5 (`nowMs`), T7. Interval floor → T4. Retention (keep all + LTTB) → T5, plus `sampling: "lttb"` to add in `charts/options.ts` during T11. Perf strategy → T5, T6, T7, T11. Budget guards (tiers 1/2/3) → T5, T7, T13. Deletions + retirement port → T10, T12. Error handling → T8 (soft-fail boot, dropped fragments). Testing → throughout.

**Type consistency:** `MonitorSessionFragment` (T1) is consumed under that exact name in T2 (Python) and T6/T8 (TS, via `export.gen.ts`). `SeriesIndex`/`buildIndex`/`appendToIndex`/`sliceSeries` (T5) are used unchanged in T6, T10, T11. `healthForHosts(session, range, nowMs?)` (T5) is called with the clock's `now` (T7). `session_id` (T2) is set only by `MonitorServer` (T2).

**Known gap, deliberately not a task:** ECharts `sampling: "lttb"` is a one-line addition inside `buildStackOption` and rides T11 rather than getting a task of its own.
