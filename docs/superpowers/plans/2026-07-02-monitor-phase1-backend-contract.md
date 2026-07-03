# Monitor Phase 1: Backend Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the monitor backend into focused modules behind a stable `MetricCollector` facade, replace ad-hoc meta dicts with pydantic `TabSpec`/`ChartSpec` models, introduce parser API v2 (`ParseContext`), project-level parser registration, per-parser collection intervals, and fix the historical-mode zero-charts bug — all while the existing vanilla-JS frontend and the Phase 0 pin suites stay green.

**Architecture:** `collector.py` currently holds five jobs; each moves to its own module (`broadcast.py`, `db.py`, `store.py`, `history.py`) with `MetricCollector` keeping its exact public surface and delegating. The dashboard contract (`/api/meta`) becomes typed pydantic models serialized to the same JSON shape. Phase 0's wire-contract pins (`tests/e2e/monitor/dashboard/test_harness.py`) and 12 browser pins are the regression net: they must stay green except where this plan *deliberately* evolves them (called out per task).

**Tech Stack:** Python 3.10+, pydantic v2 (`OttoModel`), aiosqlite, FastAPI/sse-starlette (untouched), existing pytest tiers + Phase 0 Playwright suite.

**Spec:** `docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md` §1–2, Phase 1. Phase 0 deferred follow-ups absorbed here: `MonitorServer.force_stop()`, SSE event-lifecycle wire pins, export value-level round-trip, harness drain exception logging, historical `KNOWN-GAP` fix.

## Global Constraints

- **PRECONDITION:** the post-merge fixes currently staged on `main` (Makefile/noxfile coverage-append, `test_options_plugin.py` playwright fix, Vagrantfile) must be COMMITTED before creating the execution worktree — the worktree branches from HEAD and loses staged work.
- Execute in an isolated worktree (superpowers:using-git-worktrees). Fresh worktree: `uv sync` first. Chromium is already installed VM-wide.
- **Public API stability:** `MetricCollector`'s constructor and public methods must not change signature — `src/otto/suite/suite.py:447-462`, `src/otto/suite/plugin.py:314`, `src/otto/monitor/factory.py`, and `src/otto/cli/monitor.py` construct/use it directly.
- **The Phase 0 net stays green** except these two deliberate contract evolutions: Task 6 adds `"interval"` to `META_METRIC_KEYS`; Task 7 flips the historical `KNOWN-GAP` browser pin to assert charts render. Any OTHER pin failure is a regression in your change, not a test to edit.
- Behavior quirks are contract: preserve event-id assignment exactly (`rowid or next_id`, increment even when rowid used), timestamps `datetime.now(tz=timezone.utc)`, JSON export field spellings.
- Ban `from __future__ import annotations` (breaks the Sphinx nitpicky docs gate). Real 3.10+ annotations, module-top imports.
- Strict gates: `uv run ruff check . && uv run ruff format --check .` after every task (re-run `ruff check` after `ruff format`); `ty` runs only at `nox -s typecheck` — Task 13 budgets that round, but overridden methods need `@override` (typing_extensions) as you write them (`missing-override-decorator` is an error).
- Sphinx is nitpicky (`-W`): every new module gets an API page (Task 12); keep docstring cross-references resolvable.
- Test load: `-n auto` single passes only; never loop the full suite on the dev VM.
- Commit per task, message prefixes as given. If the commit hook errors about `/dev/tty`, do NOT use `--no-verify`; leave staged and report it.

## File Structure

```text
src/otto/monitor/broadcast.py    # NEW — Broadcaster: SSE subscriber queues (subscribe/unsubscribe/publish)
src/otto/monitor/db.py           # NEW — MetricDB: aiosqlite persistence, schema, WAL/DELETE, flock guard
src/otto/monitor/store.py        # NEW — MetricStore: in-memory series/chart-map/events + snapshots
src/otto/monitor/history.py      # NEW — JSON/SQLite import + JSON export (pure functions over MetricStore)
src/otto/monitor/collector.py    # SLIMS — targets, tick loops, orchestration; delegates to the four above
src/otto/monitor/parsers.py      # ParseContext; parse(output, *, ctx); interval attr; register_parsers()
src/otto/monitor/server.py       # + force_stop()
src/otto/models/monitor.py       # + ChartSpec, TabSpec, MonitorMeta
src/otto/models/jsonschema.py    # + monitor-meta schema entry
tests/unit/monitor/test_broadcast.py    # NEW
tests/unit/monitor/test_store.py        # NEW
tests/unit/monitor/test_meta_models.py  # NEW
tests/e2e/monitor/dashboard/test_harness.py           # + SSE lifecycle pins, export value round-trip
tests/e2e/monitor/dashboard/test_dashboard_historical.py  # KNOWN-GAP flip (Task 7)
tests/_fixtures/_fake_collector.py      # shim deletion (Task 7)
tests/_fixtures/_dashboard_harness.py   # force_stop adoption + drain logging (Task 11)
docs/api/monitor/{broadcast,db,store,history}.rst     # NEW stubs + index toctree
docs/guide/monitor.md                    # ctx/interval/register_parsers sections
```

---

### Task 1: Strengthen the net first — SSE event-lifecycle pins + export value-level round-trip

Written against the CURRENT backend, before anything moves. These pins protect every later task.

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_harness.py`

**Interfaces:**
- Consumes: `live_dash` fixture, `DashboardHarness.run()`, collector `add_event/update_event/delete_event`.
- Produces: `SSE_EVENT_KEYS`, `SSE_EVENT_DELETED_KEYS` constants later tasks must keep green.

- [ ] **Step 1: Add the pins**

Append to `test_harness.py` (reuse the existing `EVENT_KEYS` constant and the http.client streaming pattern from `test_sse_stream_delivers_metric_messages` — same de-chunking rule: `resp.readline()`, never `resp.fp`):

```python
SSE_EVENT_KEYS = {"type", *EVENT_KEYS}
SSE_EVENT_DELETED_KEYS = {"type", "id"}


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

        ev = live_dash.run(
            live_dash.collector.add_event(label="pin", color="#112233", dash="dot")
        )
        created = _next_sse_payload(resp)
        assert set(created) == SSE_EVENT_KEYS
        assert created["type"] == "event"
        assert created["id"] == ev.id

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
```

The existing export pin (`test_dashboard_historical.py::test_export_json_reimports_losslessly`) is browser-marked and key-set-only. Leave it as-is; add this browser-free, value-level twin here so the stronger pin runs in the hostless gate:

```python
def test_export_import_round_trip_preserves_values(
    live_dash: DashboardHarness[FakeCollector], tmp_path: Path
) -> None:
    """Losslessness at the value level, not just key sets (hostless twin of the browser pin)."""
    live_dash.run(live_dash.collector.add_event(label="evt", color="#112233", dash="dot"))
    exported = live_dash.run_export()  # see Step 2

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
```

Add imports as needed (`Path` from pathlib, `MetricCollector` from otto.monitor.collector — both may already be present).

- [ ] **Step 2: Add the `run_export` helper**

In `tests/_fixtures/_dashboard_harness.py`, add to `DashboardHarness` (collector `to_json()` is sync but must run where the collector lives; a tiny coroutine keeps the one-loop rule):

```python
    def run_export(self) -> str:
        """Serialize the collector to its --file JSON on the server loop."""

        async def _export() -> str:
            return self.collector.to_json()

        return self.run(_export())
```

- [ ] **Step 3: Run to verify the new pins pass against current code**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -v`
Expected: all pass (now 9 tests). These are pins of existing behavior — if one fails, the pin is wrong, not the code; fix the pin.

- [ ] **Step 4: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add tests/e2e/monitor/dashboard/test_harness.py tests/_fixtures/_dashboard_harness.py
git commit -m "test: pin SSE event-lifecycle wire shapes + value-level export round-trip"
```

---

### Task 2: Extract Broadcaster

**Files:**
- Create: `src/otto/monitor/broadcast.py`
- Modify: `src/otto/monitor/collector.py` (delete `_subscribers` + the three pub/sub methods' bodies; delegate)
- Test: `tests/unit/monitor/test_broadcast.py`

**Interfaces:**
- Produces: `Broadcaster` with `subscribe() -> asyncio.Queue[dict[str, Any]]`, `unsubscribe(q) -> None`, `publish(payload: dict[str, Any]) -> None`. Collector keeps `subscribe()/unsubscribe()/_publish()` as thin delegates (server.py and FakeCollector's push path call them).

- [ ] **Step 1: Write the failing test**

`tests/unit/monitor/test_broadcast.py`:

```python
"""Broadcaster — SSE fan-out isolated from the collector."""

from otto.monitor.broadcast import Broadcaster


def test_publish_reaches_all_subscribers() -> None:
    b = Broadcaster()
    q1, q2 = b.subscribe(), b.subscribe()
    b.publish({"type": "metric", "value": 1.0})
    assert q1.get_nowait() == {"type": "metric", "value": 1.0}
    assert q2.get_nowait() == {"type": "metric", "value": 1.0}


def test_unsubscribed_queue_receives_nothing() -> None:
    b = Broadcaster()
    q1, q2 = b.subscribe(), b.subscribe()
    b.unsubscribe(q1)
    b.publish({"type": "event"})
    assert q1.empty()
    assert q2.get_nowait() == {"type": "event"}


def test_unsubscribe_unknown_queue_is_noop() -> None:
    import asyncio

    b = Broadcaster()
    b.unsubscribe(asyncio.Queue())  # never subscribed — must not raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/monitor/test_broadcast.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.monitor.broadcast'`

- [ ] **Step 3: Implement**

`src/otto/monitor/broadcast.py`:

```python
"""Broadcaster — fan-out of monitor payloads to SSE subscriber queues.

One ``asyncio.Queue`` per connected dashboard tab. ``publish()`` uses
``put_nowait()`` — safe because collection and the SSE route handlers all run
on the same event loop.
"""

import asyncio
from typing import Any


class Broadcaster:
    """Holds SSE subscriber queues and pushes JSON-safe payloads to all of them."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove ``q`` so it receives no further pushes (unknown queues are a no-op)."""
        self._subscribers = [sq for sq in self._subscribers if sq is not q]

    def publish(self, payload: dict[str, Any]) -> None:
        """Push a JSON-safe dict to every subscriber queue."""
        for q in list(self._subscribers):
            q.put_nowait(payload)
```

In `collector.py`: replace the `_subscribers` init line with `self._broadcast = Broadcaster()` (import at module top: `from .broadcast import Broadcaster`), and the three methods become:

```python
    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        """Register a new SSE subscriber and return its queue."""
        return self._broadcast.subscribe()

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        """Remove ``q`` from the SSE subscriber list so it receives no further pushes."""
        self._broadcast.unsubscribe(q)

    def _publish(self, payload: dict[str, Any]) -> None:
        self._broadcast.publish(payload)
```

Grep `tests/` for `_subscribers` first; if any test pokes it, update that test to go through `subscribe()` instead (behavior-preserving).

- [ ] **Step 4: Run the affected suites**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: all pass (FakeCollector's push → `_record_point` → `_publish` path proves delegation).

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/broadcast.py src/otto/monitor/collector.py tests/unit/monitor/test_broadcast.py
git commit -m "refactor(monitor): extract Broadcaster from MetricCollector"
```

---

### Task 3: Extract MetricDB

**Files:**
- Create: `src/otto/monitor/db.py`
- Modify: `src/otto/monitor/collector.py` (move `_SCHEMA`, `init_db`, `close_db`, `_db_*` bodies)

**Interfaces:**
- Produces: `MetricDB(path: str)` with `async open() -> None` (lock + journal-mode + schema + end_ts migration), `async close() -> None`, `async write_point(ts, host, label, value) -> None`, `async write_event(event: MonitorEvent) -> int` (rowid), `async update_event(event) -> None`, `async delete_event(event_id: int) -> None`. Collector keeps `init_db()/close_db()` public (tests call them) and `self._db: MetricDB | None`.

- [ ] **Step 1: Survey existing DB tests**

Run: `grep -rn "_db_conn\|_lock_fd\|init_db\|close_db" tests/unit/monitor/ | head -20`
These tests define the behavior contract. Any that touch `collector._db_conn`/`_lock_fd` directly must be updated to reach through `collector._db` (e.g. `collector._db._conn`) — mechanical rename, no behavior change. Note each in the report.

- [ ] **Step 2: Implement MetricDB by moving code**

`src/otto/monitor/db.py` — move verbatim from `collector.py` (docstrings included), reshaped as a class. The `_SCHEMA` constant, the flock acquire/release, `network_fs_type` journal selection, `PRAGMA busy_timeout`, the `end_ts` migration, and every `_db_*` method body move unchanged; only `self._db_conn` → `self._conn`, `self._db_path` → `self._path`:

```python
"""MetricDB — SQLite persistence for monitor metrics and events.

Owns the aiosqlite connection, the schema, the WAL-vs-DELETE journal choice
(network filesystems can't WAL), and the flock guard that stops two live
collectors writing one database. Extracted from MetricCollector; behavior is
identical.
"""

import fcntl
import logging
import os

import aiosqlite

from ..filesystem import network_fs_type
from .events import MonitorEvent

logger = logging.getLogger("otto")

_SCHEMA = """ ...moved verbatim... """


class MetricDB:
    """Persistent async SQLite store for one monitor database file."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock_fd: int | None = None

    async def open(self) -> None:
        """Acquire the flock guard, open the connection, apply schema + migration."""
        # body of the old init_db(), minus the `if not self._db_path: return`
        ...

    async def close(self) -> None:
        # body of old close_db()
        ...

    async def write_point(self, ts: "datetime", host: str, label: str, value: float) -> None:
        # body of old _db_write_point (keep the `if not self._conn: return` guard)
        ...
```

(Ellipses above mark verbatim moves, not new code to invent — copy the exact bodies from `collector.py`, including the RuntimeError message for the lock conflict and the network-FS debug logs.)

Collector side:

```python
    async def init_db(self) -> None:
        """Open the persistent DB (no-op without a --db path). See MetricDB."""
        if self._db is not None or not self._db_path:
            return
        db = MetricDB(self._db_path)
        await db.open()
        self._db = db

    async def close_db(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
```

and `_record_point`/event methods call `if self._db: await self._db.write_point(...)` etc. (`_db_write_event` returned 0 with no DB — preserve: `rowid = await self._db.write_event(event) if self._db else 0`).

- [ ] **Step 3: Run the DB-focused suites**

Run: `uv run pytest tests/unit/monitor/test_collector_db.py tests/unit/monitor/test_collector_nfs.py tests/unit/monitor/test_monitor_import_export.py -v`
Expected: all pass (after any Step-1 mechanical renames).

- [ ] **Step 4: Full monitor unit + harness pins**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: green.

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/db.py src/otto/monitor/collector.py tests/unit/monitor/
git commit -m "refactor(monitor): extract MetricDB from MetricCollector"
```

---

### Task 4: Extract MetricStore

**Files:**
- Create: `src/otto/monitor/store.py`
- Modify: `src/otto/monitor/collector.py`
- Test: `tests/unit/monitor/test_store.py`

**Interfaces:**
- Produces: `MetricStore` with:
  - `append_point(key: str, point: MetricPoint, *, label: str, chart: str) -> None` (creates the deque lazily, records `chart_map[label] = chart`)
  - `snapshot_series() -> dict[str, list[MetricPoint]]`, `snapshot_chart_map() -> dict[str, str]`
  - `events() -> list[MonitorEvent]` (copy), `find_event(event_id) -> MonitorEvent | None`, `add_event(event: MonitorEvent, rowid: int) -> MonitorEvent` (id-assignment quirk preserved: `event.id = rowid or self._next_event_id; self._next_event_id += 1`), `remove_event(event_id) -> bool`
  - `hosts_from_series() -> list[str]` (sorted unique `key.split("/")[0]` for keys containing `/`)
  - `note_imported_event(event: MonitorEvent) -> None` (append + `self._next_event_id = max(self._next_event_id, event.id) + 1` — the from_json bookkeeping)
  - raw attributes `series: dict[str, deque[MetricPoint]]`, `chart_map: dict[str, str]` stay accessible for the history module (Task 5).
- Collector keeps `get_series()/get_chart_map()/get_events()` delegating and `self._store: MetricStore`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/monitor/test_store.py`:

```python
"""MetricStore — in-memory series/chart-map/event bookkeeping."""

from datetime import datetime, timezone

from otto.models import MetricPoint
from otto.monitor.events import MonitorEvent
from otto.monitor.store import MetricStore

TS = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _point(value: float) -> MetricPoint:
    return MetricPoint.model_construct(ts=TS, value=value, meta=None)


def test_append_point_creates_series_and_chart_map() -> None:
    store = MetricStore()
    store.append_point("host1/Overall CPU", _point(1.0), label="Overall CPU", chart="CPU")
    store.append_point("host1/Overall CPU", _point(2.0), label="Overall CPU", chart="CPU")
    assert [p.value for p in store.snapshot_series()["host1/Overall CPU"]] == [1.0, 2.0]
    assert store.snapshot_chart_map() == {"Overall CPU": "CPU"}


def test_snapshot_is_a_copy() -> None:
    store = MetricStore()
    store.append_point("h/x", _point(1.0), label="x", chart="X")
    snap = store.snapshot_series()
    snap["h/x"].append(_point(9.9))
    assert len(store.snapshot_series()["h/x"]) == 1


def test_event_id_quirk_preserved() -> None:
    """rowid wins when nonzero, but next_id advances regardless (legacy behavior)."""
    store = MetricStore()
    e1 = store.add_event(MonitorEvent(timestamp=TS, label="a"), rowid=0)
    assert e1.id == 1
    e2 = store.add_event(MonitorEvent(timestamp=TS, label="b"), rowid=7)
    assert e2.id == 7
    e3 = store.add_event(MonitorEvent(timestamp=TS, label="c"), rowid=0)
    assert e3.id == 3  # next_id advanced past e2 despite the rowid


def test_hosts_from_series_ignores_bare_labels() -> None:
    store = MetricStore()
    store.append_point("host2/CPU", _point(1.0), label="CPU", chart="CPU")
    store.append_point("host1/CPU", _point(1.0), label="CPU", chart="CPU")
    store.append_point("bare-label", _point(1.0), label="bare-label", chart="X")
    assert store.hosts_from_series() == ["host1", "host2"]


def test_remove_and_find_event() -> None:
    store = MetricStore()
    ev = store.add_event(MonitorEvent(timestamp=TS, label="a"), rowid=0)
    assert store.find_event(ev.id) is ev
    assert store.remove_event(ev.id) is True
    assert store.remove_event(ev.id) is False
    assert store.find_event(ev.id) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_store.py -v`
Expected: FAIL — no module `otto.monitor.store`.

- [ ] **Step 3: Implement MetricStore and delegate**

`src/otto/monitor/store.py`:

```python
"""MetricStore — the monitor's in-memory time-series and event state.

Series are keyed ``"hostname/label"`` (bare ``label`` for historical imports
without a host column). Extracted from MetricCollector; the id-assignment
quirk in add_event is deliberately preserved (see test_store.py).
"""

from collections import deque

from ..models import MetricPoint
from .events import MonitorEvent


class MetricStore:
    """In-memory series, chart map, and events for one collector."""

    def __init__(self) -> None:
        self.series: dict[str, deque[MetricPoint]] = {}
        self.chart_map: dict[str, str] = {}
        self._events: list[MonitorEvent] = []
        self._next_event_id: int = 1

    def append_point(self, key: str, point: MetricPoint, *, label: str, chart: str) -> None:
        """Store one point, creating the series lazily, and record its chart group."""
        if key not in self.series:
            self.series[key] = deque()
        self.series[key].append(point)
        self.chart_map[label] = chart

    def snapshot_series(self) -> dict[str, list[MetricPoint]]:
        return {key: list(pts) for key, pts in self.series.items()}

    def snapshot_chart_map(self) -> dict[str, str]:
        return dict(self.chart_map)

    def events(self) -> list[MonitorEvent]:
        return list(self._events)

    def find_event(self, event_id: int) -> MonitorEvent | None:
        return next((e for e in self._events if e.id == event_id), None)

    def add_event(self, event: MonitorEvent, rowid: int) -> MonitorEvent:
        """Assign the id (DB rowid wins; the counter advances regardless) and store."""
        event.id = rowid or self._next_event_id
        self._next_event_id += 1
        self._events.append(event)
        return event

    def note_imported_event(self, event: MonitorEvent) -> None:
        """Track an event loaded from a file, keeping the id counter ahead of it."""
        self._next_event_id = max(self._next_event_id, event.id) + 1
        self._events.append(event)

    def remove_event(self, event_id: int) -> bool:
        for i, ev in enumerate(self._events):
            if ev.id == event_id:
                self._events.pop(i)
                return True
        return False

    def hosts_from_series(self) -> list[str]:
        return sorted({key.split("/")[0] for key in self.series if "/" in key})
```

Collector: replace `self._series`/`self._chart_map`/`self._events`/`self._next_event_id` with `self._store = MetricStore()`; every internal use routes through it (`_record_point` calls `self._store.append_point(key, point, label=label, chart=view.chart)`; `add_event` becomes `event = self._store.add_event(event, rowid)`; `delete_event`/`update_event` use `remove_event`/`find_event`; `get_series/get_chart_map/get_events` delegate to the snapshots; `get_meta`'s host derivation uses `hosts_from_series()`). `from_json`/`from_sqlite` still write `collector._store.series[...]` directly for now — Task 5 moves them.

Grep `tests/` and `src/otto/suite/` for `_series`/`_chart_map`/`_events` pokes and update mechanically (report each).

- [ ] **Step 4: Run the affected suites**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: green.

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/store.py src/otto/monitor/collector.py tests/unit/monitor/
git commit -m "refactor(monitor): extract MetricStore from MetricCollector"
```

---

### Task 5: Extract history (JSON/SQLite import + JSON export)

**Files:**
- Create: `src/otto/monitor/history.py`
- Modify: `src/otto/monitor/collector.py` (`from_json`, `from_sqlite`, `to_json`, `export_json` become delegates)

**Interfaces:**
- Consumes: `MetricStore` raw attrs (`series`, `chart_map`), `note_imported_event`.
- Produces: `load_json_into(store: MetricStore, path: str) -> None`, `async load_sqlite_into(store: MetricStore, path: str) -> None`, `to_json(store: MetricStore) -> str`. Collector classmethods keep their exact signatures and behavior:

```python
    @classmethod
    def from_json(cls, path: str, parsers: "list[MetricParser] | None" = None) -> "MetricCollector":
        collector = cls(hosts=[], parsers=parsers)
        load_json_into(collector._store, path)
        return collector
```

- [ ] **Step 1: Move the code**

`src/otto/monitor/history.py` gets the bodies of `from_json` (loop over `data["metrics"]` / `chart_map` / `events` — validation via `MetricRecord`/`EventRecord` moves verbatim, `collector._series[key]` becomes `store.series[key]`, the event id bookkeeping becomes `store.note_imported_event(event)` with the pre-existing `id = rec.id if rec.id is not None else <next>` handling folded in), `from_sqlite` (both PRAGMA table_info compatibility branches verbatim), and `to_json` (the `MetricRecord(...).model_dump(mode="json", exclude_none=True)` export loop verbatim). Module docstring:

```python
"""Historical import/export for monitor data.

Pure functions over MetricStore: the JSON ``--file`` format and the SQLite
database written by live collection. Lenient on read (RowModel), exact on
write — the export is byte-compatible with what ``--file`` accepts.
"""
```

`note_imported_event` id handling: the JSON path assigns `rec.id if rec.id is not None else` the store's running counter — implement as:

```python
        event = MonitorEvent(..., id=rec.id if rec.id is not None else 0, ...)
        if event.id == 0:
            store.add_event(event, rowid=0)
        else:
            store.note_imported_event(event)
```

(Same for the SQLite branch. This preserves the legacy max-tracking exactly; `test_export_import_round_trip_preserves_values` from Task 1 and `tests/unit/monitor/test_monitor_import_export.py` are the guards.)

- [ ] **Step 2: Run the import/export suites**

Run: `uv run pytest tests/unit/monitor/test_monitor_import_export.py tests/e2e/monitor/dashboard/test_harness.py -v`
Expected: green — the Task 1 round-trip pin is the main referee here.

- [ ] **Step 3: Full monitor sweep**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard -q`
Expected: green (browser pins included — export button flows unchanged).

- [ ] **Step 4: Lint and commit**

```bash
git add src/otto/monitor/history.py src/otto/monitor/collector.py
git commit -m "refactor(monitor): extract history import/export from MetricCollector"
```

---

### Task 6: Typed meta contract — ChartSpec / TabSpec / MonitorMeta

**Files:**
- Modify: `src/otto/models/monitor.py` (add the three models), `src/otto/monitor/collector.py` (`get_meta` builds via models), `src/otto/monitor/parsers.py` (declare `interval` attr), `src/otto/models/jsonschema.py` (emit `monitor-meta`)
- Modify: `tests/e2e/monitor/dashboard/test_harness.py` (**deliberate pin evolution**: `META_METRIC_KEYS` gains `"interval"`)
- Test: `tests/unit/monitor/test_meta_models.py`

**Interfaces:**
- Produces (Phase 2's TS generation consumes the schema; Task 7/9 consume the models):

```python
class ChartSpec(OttoModel):
    label: str
    y_title: str
    unit: str
    command: str
    chart: str
    interval: float | None = None

class TabSpec(OttoModel):
    id: str
    label: str
    metrics: list[str]

class MonitorMeta(OttoModel):
    hosts: list[str]
    live: bool
    metrics: list[ChartSpec]
    tabs: list[TabSpec]
```

- `MetricCollector.get_meta() -> dict[str, Any]` (unchanged signature) now returns `self.get_meta_model().model_dump(mode="json")`; new public `get_meta_model() -> MonitorMeta`.
- `MetricParser.interval: float | None = None` class attribute (declared + documented now; the scheduler honors it in Task 9).

- [ ] **Step 1: Write the failing tests**

`tests/unit/monitor/test_meta_models.py`:

```python
"""MonitorMeta — the typed /api/meta contract."""

import pytest

from otto.models.monitor import ChartSpec, MonitorMeta, TabSpec
from tests._fixtures._fake_collector import FakeCollector


@pytest.mark.asyncio
async def test_get_meta_model_matches_dict_form() -> None:
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    model = fake.get_meta_model()
    assert isinstance(model, MonitorMeta)
    assert model.model_dump(mode="json") == fake.get_meta()


def test_chart_spec_wire_shape() -> None:
    spec = ChartSpec(label="CPU", y_title="Usage %", unit="%", command="top", chart="CPU")
    assert set(spec.model_dump(mode="json")) == {
        "label", "y_title", "unit", "command", "chart", "interval",
    }
    assert spec.interval is None


def test_tab_spec_wire_shape() -> None:
    tab = TabSpec(id="cpu", label="CPU", metrics=["CPU", "Load"])
    assert set(tab.model_dump(mode="json")) == {"id", "label", "metrics"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_meta_models.py -v`
Expected: FAIL — ImportError (no ChartSpec).

- [ ] **Step 3: Implement**

Models exactly as in Interfaces (docstrings: "One dashboard chart/tab descriptor served by /api/meta — the declarative contract the frontend renders from; TS types are generated from this schema in Phase 2"). If ruff flags `id` (builtin shadow, A003): it should not fire on pydantic model fields under the current config (grep `.ruff.toml`/pyproject for A003 first); if it genuinely does, keep the wire name via `model_config` population and rename discussion goes to the controller — do NOT silently rename the wire key.

`MetricParser` gains, next to the other class attributes:

```python
    interval: float | None = None
    """Collection interval override in seconds for this parser's command.
    ``None`` means the collector's global ``--interval`` (the default).
    Honored by :meth:`MetricCollector.run`'s per-interval scheduling."""
```

`collector.get_meta()` refactors to:

```python
    def get_meta_model(self) -> MonitorMeta:
        """The typed /api/meta payload (see get_meta for the dict form)."""
        hosts = self._store.hosts_from_series()
        if not hosts and self._hosts:
            hosts = [h.name for h in self._hosts]

        tabs: dict[str, TabSpec] = {}
        for v in self._views:
            tab_id = getattr(v, "tab", "metrics")
            tab_label = getattr(v, "tab_label", "Metrics")
            if tab_id not in tabs:
                tabs[tab_id] = TabSpec(id=tab_id, label=tab_label, metrics=[])
            tabs[tab_id].metrics.append(v.chart)

        metrics = [
            ChartSpec(
                label=v.chart,
                y_title=v.y_title,
                unit=v.unit,
                command=getattr(v, "command", None) or getattr(v, "oid", ""),
                chart=v.chart,
                interval=getattr(v, "interval", None),
            )
            for v in self._views
        ]
        return MonitorMeta(hosts=hosts, live=bool(self._hosts), metrics=metrics, tabs=tabs_list)
```

(`tabs_list = list(tabs.values())`; `TabSpec.metrics` mutation requires the model non-frozen — OttoModel default; if OttoModel is frozen, build plain dicts first and construct TabSpec at the end.) `get_meta()` returns `self.get_meta_model().model_dump(mode="json")`.

`models/jsonschema.py`: read `build_schemas` and append one entry mirroring the `settings`/`reservations` pattern:

```python
    docs["monitor-meta"] = _decorate(
        MonitorMeta.model_json_schema(),
        "monitor-meta",
        "Monitor dashboard /api/meta payload",
    )
```

(adjust the dict/return name to the function's actual shape; import `MonitorMeta` from `.monitor` at module top).

- [ ] **Step 4: Evolve the wire pin deliberately**

In `test_harness.py`: `META_METRIC_KEYS = {"label", "y_title", "unit", "command", "chart", "interval"}` with a comment: `# "interval" added in Phase 1 (per-parser collection intervals) — deliberate contract evolution.`

- [ ] **Step 5: Run the referee suites**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard -q && uv run pytest tests/unit/models -q 2>/dev/null || uv run pytest tests/unit -k jsonschema -q`
Expected: green — including all 12 browser pins (the old frontend ignores the extra `interval` key).

Run: `make schema && ls schemas/ | grep monitor`
Expected: `monitor-meta.schema.json` emitted.

- [ ] **Step 6: Lint and commit**

```bash
git add src/otto/models/monitor.py src/otto/models/jsonschema.py src/otto/monitor/collector.py src/otto/monitor/parsers.py tests/
git commit -m "feat(monitor): typed /api/meta contract (ChartSpec/TabSpec/MonitorMeta) + schema export"
```

---

### Task 7: Historical catalog fix (the KNOWN-GAP) + FakeCollector shim deletion

**Files:**
- Modify: `src/otto/monitor/collector.py` (`__init__`)
- Modify: `tests/_fixtures/_fake_collector.py` (delete the manual `_parsers`/`_views` shim)
- Modify: `tests/e2e/monitor/dashboard/test_dashboard_historical.py` (**deliberate pin flip**), `tests/e2e/monitor/dashboard/test_harness.py` (historical tabs pin)

**Interfaces:**
- Consumes: the `KNOWN-GAP:` comment marks the pinned bug (grep it).
- Produces: collectors with no live targets still declare the parser catalog — `/api/meta` serves the same `tabs`/`metrics` a live collector would, so historical `--file` data renders.

- [ ] **Step 1: Flip the tests first (they become the failing spec)**

In `test_dashboard_historical.py::test_historical_mode_chrome`, delete the `KNOWN-GAP:` comment block and the zero-count assertions; replace with the rendering assertions (this is the plan's Phase 0 original intent, now achievable):

```python
def test_historical_mode_chrome(
    page: Page, historical_dash: DashboardHarness[MetricCollector]
) -> None:
    page.goto(historical_dash.url)
    expect(page.locator("#status-label")).to_have_text("Historical")
    expect(page.locator("body")).to_have_class(re.compile(r"\bhistorical\b"))
    # Historical collectors declare the DEFAULT_PARSERS catalog, so tabs and
    # charts render immediately without host selection (fixed Phase 1 —
    # previously /api/meta had no tabs and nothing rendered).
    expect(page.locator(".tab-btn")).to_have_text(["CPU", "Memory", "Disk"])
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()
    expect(page.locator("#host-select option")).to_have_text(["historical"])
    expect(page.locator("#pause-btn")).to_be_disabled()
    # Fixture series render onto their charts; both fixture events annotate.
    overall_len = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )
    assert overall_len == 3
    labels = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return ((gd?.layout || {}).annotations || []).map(a => a.text);"
        "}"
    )
    assert sorted(labels) == ["Maintenance", "Reboot"]
```

(`import re` if not present.) In `test_harness.py::test_historical_fixture_loads`, add:

```python
    assert [t["id"] for t in meta["tabs"]] == ["cpu", "memory", "disk"]
```

- [ ] **Step 2: Run to verify the flipped pins fail for the right reason**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_dashboard_historical.py::test_historical_mode_chrome tests/e2e/monitor/dashboard/test_harness.py::test_historical_fixture_loads -v`
Expected: FAIL — empty tab list (the bug).

- [ ] **Step 3: Fix `__init__`**

In `collector.py.__init__`, after the unified/snmp_views loop, add:

```python
        # A collector with no live targets (historical --file/--db replay, or a
        # scripted test collector) still declares its parser CATALOG: /api/meta
        # must describe tabs/charts so the dashboard can lay out loaded data.
        # Without this, from_json/from_sqlite served `tabs: []` and historical
        # mode rendered no charts at all.
        if not self._targets and targets is None:
            unified = list(parser_dict.values())
            self._parsers = dict(parser_dict)
```

Placement detail: `parser_dict` exists only on the `targets is None` branch — restructure minimally so it is in scope (e.g. initialize `parser_dict: dict[str, MetricParser] = {}` before the branch), and make sure `self._views` is built AFTER this block so it picks up `unified` (move the `self._views` assembly below it, unchanged).

- [ ] **Step 4: Delete the FakeCollector shim**

In `tests/_fixtures/_fake_collector.py.__init__`, remove the `self._parsers = dict(DEFAULT_PARSERS)` / `self._views = list(DEFAULT_PARSERS.values())` lines and their comment — the base class now provides the catalog (that was always the intended semantics). Keep `force_live`. Update the class docstring accordingly. Remove the now-unused `DEFAULT_PARSERS` import if nothing else uses it.

- [ ] **Step 5: Run the referee suites**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard -q`
Expected: ALL green — flipped pins pass, FakeCollector drift guard passes via the base-class catalog, and the other 11 browser pins are untouched.

- [ ] **Step 6: Lint and commit**

```bash
git add src/otto/monitor/collector.py tests/_fixtures/_fake_collector.py tests/e2e/monitor/dashboard/
git commit -m "fix(monitor): historical collectors declare the parser catalog — --file mode renders again"
```

---

### Task 8: Parser API v2 — ParseContext

**Files:**
- Modify: `src/otto/monitor/parsers.py` (ParseContext; abstract + 4 built-in signatures; drop base `core_count`; module-docstring example), `src/otto/monitor/collector.py` (drop the mutation loop; pass ctx), `todo/parser-core-count-via-parse-kwarg.md` (Status → Done)
- Test: `tests/unit/monitor/test_parsers.py` (update call sites; add ctx test)

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ParseContext:
    """Tick-local input to MetricParser.parse — extensible without signature breaks."""
    core_count: int = 1

# abstract:
def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]: ...
```

- Collector: the `for parser in target.parsers.values(): parser.core_count = ...` loop (currently near the top of `run()`) is DELETED; `_process_host_results` gains a `ctx: ParseContext` parameter and calls `parser.parse(cmd_result.value, ctx=ctx)`; both call sites construct `ParseContext(core_count=target.core_count)`.

- [ ] **Step 1: Update the tests first**

In `tests/unit/monitor/test_parsers.py`, mechanical migration: every `parser.parse(output)` → `parser.parse(output, ctx=ParseContext())`; every test that set `parser.core_count = N` → passes `ctx=ParseContext(core_count=N)`. Add:

```python
def test_top_cpu_normalizes_by_ctx_core_count() -> None:
    parser = TopCpuParser(top_n=5)
    # (reuse the module's existing canned `top -bn2` output constant)
    two = parser.parse(TOP_OUTPUT, ctx=ParseContext(core_count=2))
    one = parser.parse(TOP_OUTPUT, ctx=ParseContext(core_count=1))
    proc_key = next(k for k in one if k.startswith("proc/"))
    assert two[proc_key].value == pytest.approx(one[proc_key].value / 2)


def test_parse_context_is_frozen() -> None:
    ctx = ParseContext(core_count=4)
    with pytest.raises(FrozenInstanceError):
        ctx.core_count = 8  # type: ignore[misc]
```

(`from dataclasses import FrozenInstanceError`; adapt the canned-output constant name to the file's actual one.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_parsers.py -v`
Expected: FAIL — no `ParseContext`.

- [ ] **Step 3: Implement**

- `ParseContext` frozen dataclass in `parsers.py` (stdlib `dataclasses` — parsers stay pydantic-free).
- Abstract signature + all four built-ins updated (`TopCpuParser.parse` uses `ctx.core_count` where it used `self.core_count`; Mem/Disk/Load add the kwarg and ignore it). Delete the base-class `core_count` attribute + docstring.
- Module docstring example updated to the new signature:

```python
    class MyAppParser(MetricParser):
        ...
        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            ...
```

- Collector: delete the mutation loop; thread ctx through both `_process_host_results` call sites (initial collection + tick loop):

```python
                    await self._process_host_results(
                        target.host.name, ts, list(res), target.parsers,
                        ctx=ParseContext(core_count=target.core_count),
                    )
```

- `todo/parser-core-count-via-parse-kwarg.md`: replace the `Status` section body with: `**Done** — implemented in monitor Phase 1 as \`ParseContext\` (a frozen dataclass carrying \`core_count\`, extensible without further signature breaks) rather than a bare kwarg.`

**Breaking change note:** third-party parsers must add the kwarg. `MonitorTarget.parsers` docs and `register_host_parsers` docstring stay accurate (they don't show parse signatures); the guide example updates in Task 12.

- [ ] **Step 4: Run the referee suites**

Run: `uv run pytest tests/unit/monitor tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: green (FakeCollector pushes bypass parse; live-path unit tests exercise ctx).

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/parsers.py src/otto/monitor/collector.py tests/unit/monitor/test_parsers.py todo/parser-core-count-via-parse-kwarg.md
git commit -m "feat(monitor)!: parser API v2 — parse(output, *, ctx: ParseContext); no more parser mutation"
```

---

### Task 9: Per-parser collection intervals

**Files:**
- Modify: `src/otto/monitor/collector.py` (`run()`, `_collect_one`)
- Test: `tests/unit/monitor/test_collector_run.py` (append)

**Interfaces:**
- Consumes: `MetricParser.interval` (Task 6), `ParseContext` (Task 8).
- Produces: `run(interval, duration)` signature unchanged. Internally: commands are bucketed by effective interval (`parser.interval or global`), one collection loop per bucket. SNMP targets always ride the global bucket. `_collect_one(target, timeout, commands: list[str] | None = None)` — `None` keeps today's all-commands behavior; a list restricts the batch.

- [ ] **Step 1: Write the failing test**

Append to `test_collector_run.py` (mirror the file's existing fake-host pattern — it already fakes `host.run` and asserts on recorded calls; reuse its helpers/fixtures rather than inventing new ones):

```python
@pytest.mark.asyncio
async def test_per_parser_interval_buckets_commands(fake_host_factory) -> None:
    """A parser with a faster interval is collected more often than the global tick."""

    class FastParser(MetricParser):
        y_title = "Fast"
        unit = ""
        command = "echo fast"
        chart = "Fast"
        interval = 0.05

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {self.chart: MetricDataPoint(value=1.0)}

    class SlowParser(MetricParser):
        y_title = "Slow"
        unit = ""
        command = "echo slow"
        chart = "Slow"

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {self.chart: MetricDataPoint(value=1.0)}

    host = fake_host_factory()  # adapt to the file's actual fake-host helper
    collector = MetricCollector(hosts=[host], parsers=[FastParser(), SlowParser()])
    task = asyncio.create_task(collector.run(interval=timedelta(seconds=0.2)))
    await asyncio.sleep(0.55)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    batches = host.recorded_batches  # adapt: however the fake records run() calls
    fast_calls = sum(1 for b in batches if b == ["echo fast"])
    slow_calls = sum(1 for b in batches if "echo slow" in b)
    # ~11 fast ticks vs ~3 slow ticks in 0.55s; assert the ratio loosely (CI jitter)
    assert fast_calls >= 2 * slow_calls
    assert all(b == ["echo fast"] or "echo fast" not in b for b in batches), (
        "fast command must never ride the slow batch"
    )
```

The `fake_host_factory` / `recorded_batches` names are placeholders for THIS test only in the plan — the implementer must read `test_collector_run.py` and reuse its existing fake-host machinery (it exists: the file already asserts `interval passed as timeout to run`). Everything else is exact.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_collector_run.py -k interval_buckets -v`
Expected: FAIL — both commands always collected together (single loop).

- [ ] **Step 3: Implement bucketed scheduling**

Rework `run()` (keeping the core-count probe and `init_db()` preamble exactly as-is):

```python
        secs = interval.total_seconds()
        start = datetime.now(tz=timezone.utc)

        # Bucket each target's commands by effective interval. SNMP targets
        # ride the global bucket (their source has no per-command intervals).
        buckets: dict[float, list[tuple[MonitorTarget, list[str] | None]]] = {}
        for target in self._targets:
            if target.snmp is not None:
                buckets.setdefault(secs, []).append((target, None))
                continue
            by_interval: dict[float, list[str]] = {}
            for cmd, parser in target.parsers.items():
                by_interval.setdefault(parser.interval or secs, []).append(cmd)
            for bucket_secs, cmds in by_interval.items():
                buckets.setdefault(bucket_secs, []).append((target, cmds))

        async def _bucket_loop(bucket_secs: float, entries: "list[tuple[MonitorTarget, list[str] | None]]") -> None:
            # Initial collection: no sleep, publish as soon as commands return.
            await self._collect_bucket(entries, bucket_secs)
            while duration is None or datetime.now(tz=timezone.utc) - start < duration:
                await asyncio.sleep(bucket_secs)
                await self._collect_bucket(entries, bucket_secs)

        await asyncio.gather(*(_bucket_loop(s, e) for s, e in buckets.items()))
```

with the shared tick body extracted from today's loop (this is the existing gather/match logic, parameterized by the command subset):

```python
    async def _collect_bucket(
        self,
        entries: "list[tuple[MonitorTarget, list[str] | None]]",
        timeout: float,
    ) -> None:
        results = await asyncio.gather(
            *(self._collect_one(target, timeout, commands) for target, commands in entries),
            return_exceptions=True,
        )
        ts = datetime.now(tz=timezone.utc)
        for (target, commands), result in zip(entries, results, strict=True):
            match result:
                case Results() as res:
                    await self._process_host_results(
                        target.host.name, ts, list(res), target.parsers,
                        ctx=ParseContext(core_count=target.core_count),
                    )
                case list():
                    await self._process_snmp_results(target.host.name, ts, result)
                case BaseException():
                    logger.warning(
                        "Monitor: error collecting from %s: %s", target.host.name, result
                    )
                case _:
                    continue
```

`_collect_one` gains the subset parameter:

```python
    async def _collect_one(
        self,
        target: MonitorTarget,
        timeout: float,
        commands: "list[str] | None" = None,
    ) -> "Results | list[tuple[str, MetricDataPoint, SnmpMetric]] | None":
        if target.snmp is not None:
            ...unchanged...
        return await target.host.run(
            commands if commands is not None else list(target.parsers.keys()),
            timeout=timeout,
        )
```

Note the sleep-first `results[0]` trick from the old loop disappears (each bucket sleeps explicitly) — simpler and equivalent. The old behavior (all parsers, one loop) is exactly the single-bucket case; existing `test_collector_run.py` tests are the referee.

- [ ] **Step 4: Run the referee suites**

Run: `uv run pytest tests/unit/monitor -q && uv run pytest tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: green. Run the new bucket test 3x for timing-flake confidence: `uv run pytest tests/unit/monitor/test_collector_run.py -k interval_buckets -q --count 3` (pytest-repeat is a dev dep) — if flaky, loosen the ratio, never tighten sleeps.

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/collector.py tests/unit/monitor/test_collector_run.py
git commit -m "feat(monitor): per-parser collection intervals via per-bucket loops"
```

---

### Task 10: Project-level register_parsers()

**Files:**
- Modify: `src/otto/monitor/parsers.py`
- Test: `tests/unit/monitor/test_parsers.py` (append)

**Interfaces:**
- Produces:

```python
PROJECT_PARSERS: Registry[MetricParser]  # keyed by command string

def register_parsers(parsers: "Sequence[MetricParser]") -> None:
    """Project-level: extend/override DEFAULT_PARSERS for every host."""
```

- Precedence in `get_host_parsers(host_id)`: per-host registration (whole-dict, unchanged) > project-level > defaults. Deep-copied on return, as today.

- [ ] **Step 1: Write the failing tests**

Append to `test_parsers.py` (check how the file isolates `HOST_PARSERS` between tests — mirror the same fixture/cleanup pattern for `PROJECT_PARSERS`; if none exists, add an autouse fixture that snapshots-and-restores both registries):

```python
class _SocketParser(MetricParser):
    y_title = "Sockets"
    unit = ""
    command = "ss -s"
    chart = "Sockets"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}


def test_register_parsers_extends_defaults_for_all_hosts() -> None:
    register_parsers([_SocketParser()])
    merged = get_host_parsers("any-host-without-per-host-registration")
    assert "ss -s" in merged
    assert set(DEFAULT_PARSERS) <= set(merged)


def test_register_parsers_overrides_default_command() -> None:
    class MyMem(MemParser):
        chart = "My Memory"

    register_parsers([MyMem()])
    merged = get_host_parsers("some-host")
    assert merged["free -b"].chart == "My Memory"


def test_per_host_registration_beats_project_level() -> None:
    register_parsers([_SocketParser()])
    register_host_parsers("special", dict(DEFAULT_PARSERS))
    assert "ss -s" not in get_host_parsers("special")  # per-host dict is total


def test_duplicate_project_registration_is_loud() -> None:
    register_parsers([_SocketParser()])
    with pytest.raises(Exception, match="ss -s"):  # Registry's duplicate error
        register_parsers([_SocketParser()])
```

Tighten the `match=`/exception type to whatever `Registry.register` actually raises (read `src/otto/registry.py`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_parsers.py -k register_parsers -v`
Expected: FAIL — no `register_parsers`.

- [ ] **Step 3: Implement**

In `parsers.py`, below the `HOST_PARSERS` block:

```python
# Project-wide parser additions/overrides, keyed by command string. Unlike
# HOST_PARSERS (whole-dict per host), entries here merge over DEFAULT_PARSERS
# for every host that has no per-host registration. Re-registering the same
# command is a config bug and raises loudly (Registry dupe machinery).
PROJECT_PARSERS: Registry[MetricParser] = Registry(
    "project metric parser", register_hint="otto.monitor.parsers.register_parsers()"
)


def register_parsers(parsers: "Sequence[MetricParser]") -> None:
    """Register project-level parsers that apply to every monitored host.

    Call from an init module (listed in ``.otto/settings.toml``). Each parser's
    ``command`` becomes its key: a command matching a DEFAULT_PARSERS entry
    overrides that built-in; a new command extends the set. Per-host
    registrations (``register_host_parsers``) take total precedence for their
    host. Registering the same command twice raises.
    """
    for p in parsers:
        PROJECT_PARSERS.register(p.command, p, origin=caller_module())


def get_host_parsers(host_id: str) -> dict[str, "MetricParser"]:
    """Return the parser dict for *host_id*: per-host > project-level > defaults."""
    if host_id in HOST_PARSERS:
        return copy.deepcopy(HOST_PARSERS.get(host_id))
    merged = dict(DEFAULT_PARSERS)
    merged.update(PROJECT_PARSERS.as_dict())  # adjust to Registry's real iteration API
    return copy.deepcopy(merged)
```

(`Sequence` from `collections.abc` at module top. `as_dict()` is illustrative — read `src/otto/registry.py` for the actual iteration surface (`items()`, `names()`, or mapping protocol) and use that; if none exists, iterate registered names. Do not add new Registry API without checking — 12 backends already use it, one probably iterates.)

- [ ] **Step 4: Run the referee suites**

Run: `uv run pytest tests/unit/monitor/test_parsers.py -v && uv run pytest tests/unit/monitor -q`
Expected: green, including registry-isolation across tests.

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): project-level register_parsers() — extend/override defaults for all hosts"
```

---

### Task 11: MonitorServer.force_stop() + harness absorption

**Files:**
- Modify: `src/otto/monitor/server.py`, `tests/_fixtures/_dashboard_harness.py`
- Test: `tests/unit/monitor/test_server.py` (append)

**Interfaces:**
- Produces: `MonitorServer.force_stop() -> None` — thread-safe like `stop()`; sets uvicorn `force_exit`, aborts open connection transports on the server's loop, then signals exit. No-op before `serve()`. The harness's `stop()` uses it, dropping the `self.server._server` reach-in.

- [ ] **Step 1: Write the failing test**

Append to `test_server.py` (mirror its construction style):

```python
def test_force_stop_before_serve_is_noop() -> None:
    server = MonitorServer(MetricCollector(hosts=[]))
    server.force_stop()  # must not raise: nothing started yet
    assert server.started is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_server.py -k force_stop -v`
Expected: FAIL — no attribute `force_stop`.

- [ ] **Step 3: Implement**

In `MonitorServer.__init__` add `self._loop: asyncio.AbstractEventLoop | None = None`; in `serve()`, right before creating the task: `self._loop = asyncio.get_running_loop()`. Then:

```python
    def force_stop(self) -> None:
        """Shut down without waiting for open connections to drain (thread-safe).

        SSE dashboards hold /api/stream open indefinitely, so a graceful
        shutdown can wait forever. This sets uvicorn's ``force_exit`` (skip the
        drain) and aborts open connection transports on the server's own loop
        (h11 never closes a mid-stream transport, so clients would otherwise
        not see the connection die). Used by test harnesses and Ctrl+C paths;
        prefer ``stop()`` when clients should finish cleanly.
        """
        server, loop = self._server, self._loop
        if server is not None and loop is not None:
            server.force_exit = True
            state = server.server_state

            def _abort_connections() -> None:
                for conn in list(state.connections):
                    transport = getattr(conn, "transport", None)
                    if transport is not None:
                        transport.abort()

            loop.call_soon_threadsafe(_abort_connections)
        self.stop()
```

Harness `stop()` in `_dashboard_harness.py` simplifies to:

```python
        if self._thread is None:
            return
        self.server.force_stop()
        self._thread.join(timeout=10)
        ...
```

(delete the local force_exit/abort code and its `_server` reach-in; keep the docstring, pointing at `MonitorServer.force_stop`). Also upgrade `_serve()`'s drain to log non-cancellation exceptions instead of discarding them (stdlib `asyncio.runners._cancel_all_tasks` pattern):

```python
            for task, result in zip(pending, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self._loop.call_exception_handler(
                        {
                            "message": "dashboard harness: task failed during teardown",
                            "exception": result,
                            "task": task,
                        }
                    )
```

(adapt names to the drain's actual local variables).

- [ ] **Step 4: Run the referee suites**

Run: `uv run pytest tests/unit/monitor/test_server.py -q && uv run pytest tests/e2e/monitor/dashboard -q`
Expected: green — `test_server_shutdown_shows_disconnected` (browser) is the live proof that force_stop's abort still fires `EventSource.onerror`. Run the dashboard dir 3x for teardown-flake confidence.

- [ ] **Step 5: Lint and commit**

```bash
git add src/otto/monitor/server.py tests/_fixtures/_dashboard_harness.py tests/unit/monitor/test_server.py
git commit -m "feat(monitor): MonitorServer.force_stop(); harness sheds its uvicorn reach-in"
```

---

### Task 12: Documentation

**Files:**
- Create: `docs/api/monitor/broadcast.rst`, `docs/api/monitor/db.rst`, `docs/api/monitor/store.rst`, `docs/api/monitor/history.rst`
- Modify: `docs/api/monitor/index.rst` (toctree), `docs/guide/monitor.md`

**Interfaces:** none (docs only), but `make docs` is a `-W` gate — unresolved refs fail.

- [ ] **Step 1: API stubs**

Each new file mirrors `docs/api/monitor/parsers.rst` exactly, e.g. `docs/api/monitor/store.rst`:

```rst
monitor.store
=============

.. automodule:: otto.monitor.store
```

(same pattern for broadcast/db/history). Add all four to the `index.rst` toctree alphabetically among the existing entries.

- [ ] **Step 2: Guide updates**

In `docs/guide/monitor.md`, update the custom-parser section (it shows the old `parse(self, output)` signature — grep for `def parse`) to the `ctx: ParseContext` form, and add two short sections:

```markdown
### Project-level parsers

Register parsers that apply to every monitored host from an init module
(listed in `.otto/settings.toml`):

    from otto.monitor.parsers import register_parsers
    from my_repo.parsers import SocketParser

    register_parsers([SocketParser()])

A parser whose `command` matches a built-in overrides it; new commands extend
the set. Per-host registrations (`register_host_parsers`) still take total
precedence for their host. Registering the same command twice raises.

### Per-parser collection intervals

Set `interval` (seconds) on a parser class to poll its command on its own
cadence; parsers without one use the global `--interval`:

    class SocketParser(MetricParser):
        command = "ss -s"
        interval = 30.0   # poll sockets every 30s regardless of --interval
        ...
```

- [ ] **Step 3: Build the docs gate**

Run: `make docs; echo "docs exit: $?"`
Expected: `docs exit: 0` (never judge `make docs` through a pipe — it masks the exit code).

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs(monitor): API pages for the decomposed modules; ctx/interval/register_parsers guide"
```

---

### Task 13: Full gates

**Files:** none (verification only; fix-forward anything found).

- [ ] **Step 1: Full-tier coverage** — Run: `make coverage` (includes the dashboard browser lane via prerequisite + `--cov-append`). Expected: green, ≥ 94%.
- [ ] **Step 2: Typecheck (the budgeted ty round)** — Run: `uv run nox -s typecheck`. Expected: green; fix strict-typing findings in files this branch touched.
- [ ] **Step 3: Lint** — Run: `uv run nox -s lint`. Expected: green.
- [ ] **Step 4: Docs** — Run: `make docs; echo "docs exit: $?"` → 0.
- [ ] **Step 5: Import budget** — Run: `make profile` (new modules are imported lazily via `otto.monitor` only; `import otto` must not grow). If the snapshot guard fails, the new modules leaked into the base import graph — fix the import, don't regenerate the snapshot without understanding why.
- [ ] **Step 6: Commit anything from fix-forward** with `fix:`-prefixed messages, then report the gate table.

## Deliberate pin evolutions (complete list — anything else failing is a bug)

1. Task 6: `META_METRIC_KEYS` gains `"interval"`.
2. Task 7: `test_historical_mode_chrome` flips from KNOWN-GAP zero-chart pins to rendering pins; `test_historical_fixture_loads` gains the tabs assertion.

## Explicitly out of scope (Phase 2+)

TS type generation from the schema; React port; trace retirement; per-plot frequency UI; SNMP per-OID intervals; the bare-`pytest` browser/asyncio co-selection guard; FakeCollector deepcopy convention (moot once ParseContext removes parser state).
