# Monitor Plan 5c — Event Marking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users mark, edit and delete events (instant marks and spans) from the dashboard in live mode and while reviewing a `.db` archive, with the chart gesture rework (drag-zoom, Ctrl-drag pan, wheel freed) folded in.

**Architecture:** Session-aware event routes replace the legacy live-only `/api/event*` routes; live requests delegate to the collector (whose SSE publish already exists), review-`.db` requests do a per-mutation flocked read-write open of the archive and patch the served document. The frontend adds one mutation module whose every 2xx response is applied through the existing `applyFragment` upsert (the SSE echo becomes a harmless duplicate), plus marking UI (AppBar split control, EventsPanel compose row, slide-over editor) and an ECharts `BrushComponent` gesture layer.

**Tech Stack:** FastAPI + pydantic v2 + aiosqlite/sqlite3 (backend); React + zustand + react-aria + vendored Untitled UI + ECharts (frontend); pytest + vitest + Playwright (tests).

**Spec:** `docs/superpowers/specs/2026-07-18-monitor-event-marking-design.md` (approved 2026-07-18). Read it before starting any task.

## Global Constraints

- Worktree: `/home/vagrant/otto-sh/.claude/worktrees/monitor-event-marking`, branch `worktree-monitor-event-marking`. Fresh worktree: run `uv sync` once before any Python gate, and `npm ci` in `web/` before any web gate.
- **Every task's gate includes `nox -s lint` (ruff over everything, including test files) and, if the task touched anything under `web/`, `make check-ts`.** These two have each let a red slip to CI before; they are per-task, not end-of-branch.
- The browser lane is **`nox -s dashboard`** (chromium+firefox+webkit). A bare `pytest tests/e2e/monitor/dashboard` runs chromium only and must never be called "the browser lane passing".
- Never hand-edit anything under `web/src/components/` (vendored Untitled UI; byte-exact for the drift gate).
- Schema/TS contract: any change to `otto/models/monitor.py` models on the wire requires `bash scripts/gen_web_types.sh` and committing the regenerated `web/src/api/*.gen.ts` (the `make web` zero-diff gate enforces this).
- Timestamps cross the HTTP boundary as ISO-8601 strings; pydantic `datetime` fields parse/serialize them. Nothing hand-parses dates.
- **Mutation-check every load-bearing guard** (the 5b rule): before calling a test done, name the production change that should turn it red, make that change temporarily, and watch it fail. Steps below call this out where it matters most.
- Commits: conventional prefix, end the message with `Assisted-by: Claude Fable 5`.
- No new npm dependencies (Brush ships inside the existing `echarts` package). No new Python dependencies.

## File Structure

Backend:
- `src/otto/models/monitor.py` — gains `VALID_DASH_STYLES` (moved), `EventCreateBody`, `EventUpdateBody`
- `src/otto/models/jsonschema.py` — folds the two bodies into the export schema's `$defs` (the `MonitorSessionFragment` mechanism)
- `src/otto/monitor/events.py` — re-imports `VALID_DASH_STYLES` (single definition in models)
- `src/otto/monitor/db.py` — `update_event` SQL gains `ts`
- `src/otto/monitor/collector.py` — `update_event` gains `timestamp`
- `src/otto/monitor/archive_edit.py` — NEW: per-mutation flocked archive event writes
- `src/otto/monitor/server.py` — session-aware routes replace legacy; `archive_path`; mutable document body; `editable` in `/api/mode`
- `src/otto/cli/monitor.py` — `_serve_review` passes `archive_path` for `.db` sources

Frontend:
- `web/src/data/bootstrap.ts` — `ModePayload.editable`
- `web/src/data/reviewStore.ts` — `editable` state + `setEditable`, `addWarning`
- `web/src/data/eventApi.ts` — NEW: create/end/update/delete + synthetic-fragment application
- `web/src/ui/shortcuts.ts` — `MARK_NOW_BINDING`
- `web/src/ui/uiStore.ts` — `eventEditor`, `sweepArmed`, `openSpan`, `markPopover` + actions
- `web/src/ui/commands.ts` — marking commands
- `web/src/ui/useGlobalShortcuts.ts` — Esc disarms sweep
- `web/src/ui/calendarTime.ts` — NEW: ms ↔ CalendarDateTime helpers (extracted from RangePicker)
- `web/src/shell/marking.ts` — NEW: shared imperative marking helpers
- `web/src/shell/MarkControl.tsx` — NEW: AppBar split control + label popover
- `web/src/shell/EventEditor.tsx` — NEW: slide-over editor
- `web/src/shell/EventsPanel.tsx` — compose row, edit/End-now affordances, refused-jump notice
- `web/src/shell/AppBar.tsx` — mounts MarkControl
- `web/src/App.tsx` — mounts EventEditor
- `web/src/charts/echarts.ts` — registers `BrushComponent`
- `web/src/charts/options.ts` — dataZoom rework, `zoomAbout`, brush config, markArea label theme fix
- `web/src/charts/ChartPanel.tsx` — brush arm/re-arm, `brushEnd`, `onSweep`, marker-count stamp
- `web/src/pages/SubjectPage.tsx` — sweep wiring, sweep chip, `+`/`-` buttons

Tests: `tests/unit/models/`, `tests/unit/monitor/`, `web/src/__tests__/`, `tests/e2e/monitor/dashboard/test_marking.py` (new) + existing files named per task.

---

### Task 1: Event request-body models + TS contract

**Files:**
- Modify: `src/otto/models/monitor.py`
- Modify: `src/otto/models/jsonschema.py` (`_monitor_export_schema`, imports at top)
- Modify: `src/otto/monitor/events.py`
- Create: `tests/unit/models/test_monitor_event_bodies.py`
- Regenerate: `web/src/api/export.gen.ts` (via `scripts/gen_web_types.sh`)

**Interfaces:**
- Produces: `otto.models.monitor.VALID_DASH_STYLES: frozenset[str]` (moved here; still re-exported from `otto.monitor.events`), `EventCreateBody(label, timestamp=None, end_timestamp=None, color="#888888", dash="dash")`, `EventUpdateBody(label=None, timestamp=None, end_timestamp=None, color=None, dash=None)` — both `OttoModel` (extra=forbid). `EventUpdateBody` distinguishes "absent" from "explicit null" via `model_fields_set`: an explicit `"end_timestamp": null` in the JSON body means **clear the end (span → point)**; the key absent means unchanged. TS: `EventCreateBody`/`EventUpdateBody` interfaces appear in `web/src/api/export.gen.ts`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/models/test_monitor_event_bodies.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_monitor_event_bodies.py -q`
Expected: FAIL — `ImportError: cannot import name 'EventCreateBody'`

- [ ] **Step 3: Implement the models**

In `src/otto/models/monitor.py`: add `import re` and extend the pydantic import to `from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator`. After `EventRecord`, add:

```python
VALID_DASH_STYLES = frozenset({"solid", "dot", "dash", "longdash", "dashdot", "longdashdot"})
"""Legal event dash styles. Lives in this leaf module (not otto.monitor.events,
which re-imports it) so the HTTP body models below can validate against it
without this module growing an otto.monitor edge — the same leaf-isolation
rule that keeps MIN_INTERVAL_SECONDS here (see module docstring)."""

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _checked_label(value: str) -> str:
    if not value.strip():
        raise ValueError("label must not be empty")
    return value


def _checked_color(value: str) -> str:
    if not _HEX_COLOR_RE.match(value):
        raise ValueError(f"color must be #rrggbb, got {value!r}")
    return value


def _checked_dash(value: str) -> str:
    if value not in VALID_DASH_STYLES:
        raise ValueError(f"dash must be one of {sorted(VALID_DASH_STYLES)}, got {value!r}")
    return value


class EventCreateBody(OttoModel):
    """``POST /api/session/{sid}/event`` request body (spec 2026-07-18).

    ``timestamp=None`` means "server-now" (the Mark-now flow). When both
    timestamps are present the span must be forward; the server re-checks the
    pair after resolving a ``None`` timestamp to now.
    """

    label: str
    timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    color: str = "#888888"
    dash: str = "dash"

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _checked_label(value)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _checked_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str) -> str:
        return _checked_dash(value)

    @model_validator(mode="after")
    def _span_forward(self) -> "EventCreateBody":
        if (
            self.timestamp is not None
            and self.end_timestamp is not None
            and self.end_timestamp <= self.timestamp
        ):
            raise ValueError("end_timestamp must be after timestamp")
        return self


class EventUpdateBody(OttoModel):
    """``PATCH /api/session/{sid}/event/{id}`` request body (spec 2026-07-18).

    Every field optional; ``model_fields_set`` distinguishes "absent"
    (unchanged) from an explicit JSON ``null``. Only ``end_timestamp`` uses
    that distinction — an explicit null CLEARS the end (span → point); for the
    other fields null means unchanged, same as absent. The merged
    start/end ordering check happens in the route, where the existing event's
    values are known.
    """

    label: str | None = None
    timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    color: str | None = None
    dash: str | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _checked_label(value)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return None if value is None else _checked_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str | None) -> str | None:
        return None if value is None else _checked_dash(value)
```

In `src/otto/monitor/events.py`: delete its own `VALID_DASH_STYLES = frozenset(...)` line and add near the top:

```python
from ..models.monitor import VALID_DASH_STYLES

__all__ = ["AUTO_EVENT_COLORS", "VALID_DASH_STYLES", "MonitorEvent"]
```

(`server.py` and any other `from .events import VALID_DASH_STYLES` importer keeps working; run `grep -rn "VALID_DASH_STYLES" src/ tests/` and confirm no other definition site exists.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_monitor_event_bodies.py tests/unit/monitor -q`
Expected: PASS (new file green, no monitor regressions)

- [ ] **Step 5: Fold the bodies into the export schema and regenerate TS**

In `src/otto/models/jsonschema.py`: extend the monitor import to `from .monitor import EventCreateBody, EventUpdateBody, MonitorExport, MonitorMeta, MonitorSessionFragment`. In `_monitor_export_schema()`, after `defs["MonitorSessionFragment"] = frag`, add (same no-clobber shape as the fragment fold-in above it):

```python
    for body_model in (EventCreateBody, EventUpdateBody):
        body = body_model.model_json_schema()
        for key, value in body.pop("$defs", {}).items():
            defs.setdefault(key, value)
        defs[body_model.__name__] = body
```

Run: `bash scripts/gen_web_types.sh`
Then: `git diff --stat web/src/api/` — expect `export.gen.ts` gained `EventCreateBody`/`EventUpdateBody` interfaces (and nothing else changed shape). Run `git diff web/src/api/export.gen.ts | head -60` and eyeball the two new interfaces.

- [ ] **Step 6: Gates**

Run: `uv run nox -s lint typecheck && uv run pytest tests/unit/models tests/unit/monitor -q`
Expected: all green. (`make check-ts` comes with the first web task; the gen'd file alone doesn't need it, but running `cd web && npx tsc --noEmit` is a cheap sanity check.)

- [ ] **Step 7: Commit**

```bash
git add src/otto/models/monitor.py src/otto/models/jsonschema.py src/otto/monitor/events.py web/src/api/export.gen.ts tests/unit/models/test_monitor_event_bodies.py
git commit -m "feat(monitor): event create/update body models on the format:1 contract"
```

---

### Task 2: Timestamp editing reaches the store and the archive

**Files:**
- Modify: `src/otto/monitor/db.py` (`MetricDB.update_event`)
- Modify: `src/otto/monitor/collector.py` (`update_event`)
- Modify: `src/otto/monitor/server.py` (the two legacy handlers' `update_event` calls — kept compiling until Task 3 deletes them)
- Test: `tests/unit/monitor/test_collector_events.py` (create if no existing event-update test file fits; first run `grep -rln "update_event" tests/unit/monitor/` and extend the file that already covers it if one exists)

**Interfaces:**
- Produces: `MetricCollector.update_event(event_id, *, label, color, dash, timestamp, end_timestamp=None) -> MonitorEvent | None` — now keyword-only and **timestamp is required** (the route resolves unchanged fields; a required argument is what the 5a follow-ups asked for on `session_meta(interval=...)`, same trap). `MetricDB.update_event` now also rewrites `ts`.

- [ ] **Step 1: Write the failing test**

```python
# in the chosen test file
import pytest

pytestmark = pytest.mark.asyncio


async def test_update_event_moves_start_timestamp(tmp_path) -> None:
    """PATCH-with-timestamp support: the store AND the archive take the new ts."""
    from datetime import datetime, timedelta, timezone

    from otto.models import LabSnapshot
    from otto.monitor.collector import MetricCollector
    from otto.monitor.db import read_sessions
    from otto.monitor.export import build_session_metric_db
    from otto.monitor.session import new_frame

    frame = new_frame(label=None, note=None)
    db = build_session_metric_db(
        str(tmp_path / "a.db"), frame, LabSnapshot(), MetricCollector(hosts=[]), interval=5.0
    )
    collector = MetricCollector(hosts=[], db=db)
    collector.session_id = frame.id
    await collector.init_db()
    try:
        ev = await collector.add_event(label="pin")
        moved = ev.timestamp - timedelta(minutes=3)
        updated = await collector.update_event(
            ev.id, label="pin", color=ev.color, dash=ev.dash, timestamp=moved
        )
        assert updated is not None and updated.timestamp == moved
    finally:
        await collector.close_db()

    [row] = read_sessions(str(tmp_path / "a.db"))
    [(event_id, ts, end_ts, label, source, color, dash)] = row.events
    assert datetime.fromisoformat(ts) == moved  # the archive took the move, not just the store
```

(Adapt the open/close calls to the file's existing idiom — the monitor unit tests already drive `MetricCollector` + `MetricDB` against `tmp_path`; `grep -rn "init_db\|close_db" tests/unit/monitor/ | head` shows the current teardown spelling. Keep the final `read_sessions` assertion — that is the call-site artifact guard, per the 5a lesson.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/monitor/test_collector_events.py -q` (or the extended file)
Expected: FAIL — `TypeError` (unexpected keyword `timestamp`)

- [ ] **Step 3: Implement**

`src/otto/monitor/collector.py` — replace `update_event`'s signature and body:

```python
    async def update_event(
        self,
        event_id: int,
        *,
        label: str,
        color: str,
        dash: str,
        timestamp: datetime,
        end_timestamp: datetime | None = None,
    ) -> "MonitorEvent | None":
        """Overwrite an event's editable fields. Returns the updated event or None.

        Full-set semantics: the caller (the HTTP route) resolves unchanged
        fields from the existing event first — this method never guesses.
        ``timestamp`` is REQUIRED so a call site cannot silently keep passing
        the old start while believing it edited it (the 5a `interval=None`
        trap, made structural).
        """
        ev = self._store.find_event(event_id)
        if ev is None:
            return None
        ev.label = label
        ev.color = color
        ev.dash = dash
        ev.timestamp = timestamp
        ev.end_timestamp = end_timestamp
        if self._db:
            await self._db.update_event(ev)
        # No separate "updated" kind: the client upserts events by id, so an
        # edited event is just an event. One merge rule, not two.
        self._publish({"format": 1, "session": self.session_id, "events": [ev.to_dict()]})
        return ev
```

`src/otto/monitor/db.py` — `MetricDB.update_event`'s statement becomes:

```python
        await self._conn.execute(
            "UPDATE events SET label = ?, color = ?, dash = ?, ts = ?, end_ts = ? WHERE id = ?",
            (
                event.label,
                event.color,
                event.dash,
                event.timestamp.isoformat(),
                event.end_timestamp.isoformat() if event.end_timestamp else None,
                event.id,
            ),
        )
```

(and its docstring now says "label, color, dash, ts, and end_ts").

`src/otto/monitor/server.py` — the two legacy handlers (`end_event`, `update_event`) must pass the now-required keyword: add `timestamp=existing.timestamp,` to both `collector.update_event(...)` calls. Run `grep -rn "update_event(" src/ tests/` and fix any other caller the same way (tests included).

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest tests/unit/monitor -q && uv run nox -s lint typecheck`
Expected: PASS.

**Mutation check:** temporarily delete the `ev.timestamp = timestamp` line — the new test must fail on its store assertion; restore. Temporarily revert the SQL to the old column list — the `read_sessions` assertion must fail; restore.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/collector.py src/otto/monitor/db.py src/otto/monitor/server.py tests/unit/monitor/
git commit -m "feat(monitor): event updates can move the start timestamp (store + archive)"
```

---

### Task 3: Session-aware live routes replace the legacy event routes

**Files:**
- Create: `src/otto/monitor/event_ops.py` (the shared mutation-semantics seam — Chris's dedup directive 2026-07-18: one home for rules that must not fork between live, review-archive, library, and any future CLI surface)
- Create: `tests/unit/monitor/test_event_ops.py`
- Modify: `src/otto/monitor/server.py`
- Modify: `tests/unit/monitor/test_server.py`, `tests/unit/monitor/test_server_bodies.py` (`grep -rln "api/event" tests/` for the full caller list, including `tests/e2e/monitor/dashboard/test_harness.py`'s CRUD round-trip if it drives the HTTP routes)

**Interfaces:**
- Produces (consumed by Tasks 5 and 5b — review branches and the suite surface must call these, never re-implement them):
  - `event_ops.EventValidationError(ValueError)`
  - `event_ops.resolve_create(body: EventCreateBody) -> tuple[datetime, datetime | None]` — omitted timestamp ⇒ now (UTC); re-checks span ordering on the resolved pair
  - `event_ops.ResolvedEventFields` dataclass `(label, color, dash, timestamp, end_timestamp)`
  - `event_ops.merge_update(body: EventUpdateBody, *, existing_label, existing_color, existing_dash, existing_timestamp, existing_end) -> ResolvedEventFields` — `model_fields_set` semantics (absent = unchanged; explicit-null `end_timestamp` clears), span ordering checked on the merged pair
- Produces (BREAKING — legacy `POST/PATCH/DELETE /api/event*` deleted):
  - `POST /api/session/{session_id}/event` — body `EventCreateBody` → 201, format:1 `EventRecord` JSON
  - `POST /api/session/{session_id}/event/{event_id}/end` → 200 `EventRecord`; 409 if already ended
  - `PATCH /api/session/{session_id}/event/{event_id}` — body `EventUpdateBody` → 200 `EventRecord`
  - `DELETE /api/session/{session_id}/event/{event_id}` → 204
  - Wrong/unknown `session_id` → 404; review mode → 403 (until Task 5 makes `.db` review editable); merged inverted span → 422.
- Consumes: Task 1 bodies, Task 2 `update_event` signature.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/monitor/test_server.py` (mirror the file's existing server-boot idiom — `TestDeleteEvent` shows the harness: build a `MonitorServer`, serve on port 0, hit it with `urllib.request`/the file's helper; a live server needs `frame=new_frame(label=None, note=None)` and `lab=LabSnapshot()` so `collector.session_id` is stamped):

```python
class TestSessionEventRoutes:
    """Plan 5c: the session-aware event CRUD surface (live mode)."""

    async def test_create_returns_201_format1_record(self, live_server) -> None:
        sid = live_server.collector.session_id
        status, body = await post_json(live_server, f"/api/session/{sid}/event", {"label": "deploy"})
        assert status == 201
        assert body["label"] == "deploy" and body["source"] == "manual"
        assert isinstance(body["id"], int)
        assert "end_timestamp" not in body  # exclude_none: a point event omits it

    async def test_create_with_wrong_session_404s(self, live_server) -> None:
        status, _ = await post_json(live_server, "/api/session/nope/event", {"label": "x"})
        assert status == 404

    async def test_create_span_end_before_resolved_now_422s(self, live_server) -> None:
        sid = live_server.collector.session_id
        status, _ = await post_json(
            live_server, f"/api/session/{sid}/event",
            {"label": "x", "end_timestamp": "2020-01-01T00:00:00+00:00"},  # < server-now
        )
        assert status == 422

    async def test_end_stamps_now_and_second_end_409s(self, live_server) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(live_server, f"/api/session/{sid}/event", {"label": "soak"})
        status, ended = await post_json(
            live_server, f"/api/session/{sid}/event/{created['id']}/end", {}
        )
        assert status == 200 and ended["end_timestamp"] is not None
        status, _ = await post_json(live_server, f"/api/session/{sid}/event/{created['id']}/end", {})
        assert status == 409

    async def test_patch_moves_timestamp_and_clears_end(self, live_server) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(
            live_server, f"/api/session/{sid}/event",
            {"label": "x", "timestamp": "2026-07-18T12:00:00+00:00",
             "end_timestamp": "2026-07-18T12:05:00+00:00"},
        )
        status, patched = await patch_json(
            live_server, f"/api/session/{sid}/event/{created['id']}",
            {"timestamp": "2026-07-18T11:55:00+00:00", "end_timestamp": None},
        )
        assert status == 200
        assert patched["timestamp"].startswith("2026-07-18T11:55")
        assert "end_timestamp" not in patched  # explicit null cleared it (span -> point)

    async def test_patch_that_inverts_merged_span_422s(self, live_server) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(
            live_server, f"/api/session/{sid}/event",
            {"label": "x", "timestamp": "2026-07-18T12:00:00+00:00",
             "end_timestamp": "2026-07-18T12:05:00+00:00"},
        )
        status, _ = await patch_json(
            live_server, f"/api/session/{sid}/event/{created['id']}",
            {"timestamp": "2026-07-18T12:10:00+00:00"},  # start past the kept end
        )
        assert status == 422

    async def test_review_mode_mutations_403(self, review_server) -> None:
        sid = review_server.document.sessions[0].id
        status, _ = await post_json(review_server, f"/api/session/{sid}/event", {"label": "x"})
        assert status == 403
```

Write `post_json`/`patch_json`/`delete_` helpers (or reuse the file's existing request helper) that attach `?key={server.key}` — every route sits behind the access-key middleware. Also: update `TestDeleteEvent` to the new path shape, and extend `test_retired_endpoints_are_gone` with `POST /api/event`, `POST /api/event/1/end`, `PATCH /api/event/1`, `DELETE /api/event/1` all → 404/405.

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/unit/monitor/test_server.py -q`
Expected: new tests FAIL with 404s (routes absent); legacy-path tests still pass.

- [ ] **Step 3: Implement the routes**

In `src/otto/monitor/server.py`:
- Replace the `_EventBody`/`_EventUpdateBody` classes and the `VALID_DASH_STYLES` import with `from ..models.monitor import EventCreateBody, EventRecord, EventUpdateBody`.
- Inside `_build_app`, add helpers + routes (delete the four legacy handlers):

```python
    def _event_response(event_dict: dict[str, object], status_code: int = 200) -> JSONResponse:
        """Serialize a MonitorEvent.to_dict() as a format:1 EventRecord.

        One reshape, exclude_none like every other format:1 surface — a point
        event omits end_timestamp instead of carrying null (document_json's
        contract, so the SSE echo and the HTTP response are field-identical).
        """
        record = EventRecord.model_validate(event_dict)
        return JSONResponse(
            record.model_dump(mode="json", exclude_none=True), status_code=status_code
        )

    def _mutation_guard(session_id: str) -> JSONResponse | None:
        """404 for a session this server doesn't hold; 403 where editing is impossible."""
        if mode == "live":
            if frame is None or session_id != collector.session_id:
                return JSONResponse({"error": "unknown session"}, status_code=404)
            return None
        # Review mode: Task 5 adds .db-archive editing; until then (and for
        # .json sources permanently) review is read-only.
        return JSONResponse(
            {"error": "this monitor source is read-only"}, status_code=403
        )

    @app.post("/api/session/{session_id}/event")
    async def create_event(session_id: str, body: EventCreateBody) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        try:
            timestamp, end_timestamp = resolve_create(body)
        except EventValidationError as err:
            return JSONResponse({"error": str(err)}, status_code=422)
        event = await collector.add_event(
            label=body.label,
            timestamp=timestamp,
            color=body.color,
            dash=body.dash,
            source="manual",
            end_timestamp=end_timestamp,
        )
        return _event_response(event.to_dict(), status_code=201)

    async def _apply_live_update(
        event_id: int, existing: "MonitorEvent", body: EventUpdateBody
    ) -> JSONResponse:
        """One merge rule for PATCH and /end — event_ops resolves, collector writes."""
        try:
            fields = merge_update(
                body,
                existing_label=existing.label,
                existing_color=existing.color,
                existing_dash=existing.dash,
                existing_timestamp=existing.timestamp,
                existing_end=existing.end_timestamp,
            )
        except EventValidationError as err:
            return JSONResponse({"error": str(err)}, status_code=422)
        updated = await collector.update_event(
            event_id,
            label=fields.label,
            color=fields.color,
            dash=fields.dash,
            timestamp=fields.timestamp,
            end_timestamp=fields.end_timestamp,
        )
        if updated is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return _event_response(updated.to_dict())

    @app.post("/api/session/{session_id}/event/{event_id}/end")
    async def end_event(session_id: str, event_id: int) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Stamp a span's end with the server clock (the live Stop button)."""
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        if existing.end_timestamp is not None:
            return JSONResponse({"error": "Event already ended"}, status_code=409)
        # Ending IS a partial update (end_timestamp only) — same seam, not a
        # second resolution path.
        return await _apply_live_update(
            event_id, existing, EventUpdateBody(end_timestamp=datetime.now(tz=timezone.utc))
        )

    @app.patch("/api/session/{session_id}/event/{event_id}")
    async def update_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, body: EventUpdateBody
    ) -> JSONResponse:
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return await _apply_live_update(event_id, existing, body)

    @app.delete("/api/session/{session_id}/event/{event_id}")
    async def delete_event(session_id: str, event_id: int) -> Response:  # type: ignore[reportUnusedFunction]
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        if await collector.delete_event(event_id):
            return Response(status_code=204)
        return JSONResponse({"error": "Event not found"}, status_code=404)
```

Imports for the above: `from .event_ops import EventValidationError, merge_update, resolve_create` and `from .events import MonitorEvent` (annotation only) join server.py's imports.

**Before the server work, create the seam itself** — `src/otto/monitor/event_ops.py`:

```python
"""Shared event-mutation semantics for every otto surface (Plan 5c).

One home for the rules that must not fork between the live collector path,
the review-archive path (Task 5), the suite's programmatic marks (Task 5b),
and any future CLI command: how a create resolves an omitted timestamp, how
a partial update merges onto an existing event, and when a span's ordering
is invalid. Callers translate :class:`EventValidationError` into their own
surface's failure shape (HTTP 422; a raised ValueError in library code).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.monitor import EventCreateBody, EventUpdateBody


class EventValidationError(ValueError):
    """A semantically invalid event mutation (e.g. span end not after start)."""


@dataclass
class ResolvedEventFields:
    """The full field set an update resolves to — what a backend writes."""

    label: str
    color: str
    dash: str
    timestamp: datetime
    end_timestamp: datetime | None


def resolve_create(body: EventCreateBody) -> tuple[datetime, datetime | None]:
    """Resolve a create body to concrete ``(timestamp, end_timestamp)``.

    An omitted timestamp means server-now (the Mark-now flow). The span
    ordering is re-checked on the RESOLVED pair — the model can only validate
    what it holds, and a body with only ``end_timestamp`` set becomes a full
    pair here.
    """
    timestamp = body.timestamp or datetime.now(tz=timezone.utc)
    if body.end_timestamp is not None and body.end_timestamp <= timestamp:
        raise EventValidationError("end_timestamp must be after timestamp")
    return timestamp, body.end_timestamp


def merge_update(
    body: EventUpdateBody,
    *,
    existing_label: str,
    existing_color: str,
    existing_dash: str,
    existing_timestamp: datetime,
    existing_end: datetime | None,
) -> ResolvedEventFields:
    """Merge a partial update onto an existing event's fields.

    ``model_fields_set`` semantics: an absent field is unchanged; an explicit
    JSON null ``end_timestamp`` CLEARS the end (span -> point). The span
    ordering is checked on the MERGED pair — the only place it can be.
    Existing fields are passed explicitly (not as a model) so both backends —
    a live ``MonitorEvent`` and a review ``EventRecord`` — use this one rule
    without an adapter type.
    """
    provided = body.model_fields_set
    timestamp = (
        body.timestamp
        if "timestamp" in provided and body.timestamp is not None
        else existing_timestamp
    )
    end_timestamp = body.end_timestamp if "end_timestamp" in provided else existing_end
    if end_timestamp is not None and end_timestamp <= timestamp:
        raise EventValidationError("end_timestamp must be after timestamp")
    return ResolvedEventFields(
        label=body.label if body.label is not None else existing_label,
        color=body.color if body.color is not None else existing_color,
        dash=body.dash if body.dash is not None else existing_dash,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
    )
```

And its direct tests, `tests/unit/monitor/test_event_ops.py` (the seam outlives the routes — future surfaces call it without HTTP, so it gets non-HTTP coverage now):

```python
"""The shared event-mutation seam (Plan 5c) — one rule set, tested directly."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.models.monitor import EventCreateBody, EventUpdateBody
from otto.monitor.event_ops import EventValidationError, merge_update, resolve_create

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)

EXISTING = {
    "existing_label": "soak",
    "existing_color": "#888888",
    "existing_dash": "dash",
    "existing_timestamp": T0,
    "existing_end": T1,
}


def test_resolve_create_stamps_now_when_omitted() -> None:
    before = datetime.now(tz=timezone.utc)
    ts, end = resolve_create(EventCreateBody(label="x"))
    assert before <= ts <= datetime.now(tz=timezone.utc)
    assert end is None


def test_resolve_create_keeps_explicit_pair() -> None:
    assert resolve_create(EventCreateBody(label="x", timestamp=T0, end_timestamp=T1)) == (T0, T1)


def test_resolve_create_rejects_end_before_resolved_now() -> None:
    with pytest.raises(EventValidationError):
        resolve_create(EventCreateBody(label="x", end_timestamp=T0))  # T0 is in the past


def test_merge_absent_fields_unchanged() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"label": "renamed"}), **EXISTING)
    assert (fields.label, fields.timestamp, fields.end_timestamp) == ("renamed", T0, T1)


def test_merge_explicit_null_end_clears() -> None:
    fields = merge_update(EventUpdateBody.model_validate({"end_timestamp": None}), **EXISTING)
    assert fields.end_timestamp is None


def test_merge_rejects_start_moved_past_kept_end() -> None:
    moved = (T1 + timedelta(minutes=1)).isoformat()
    with pytest.raises(EventValidationError):
        merge_update(EventUpdateBody.model_validate({"timestamp": moved}), **EXISTING)
```

Update the module docstring's endpoint table (`POST /api/event` line → the four new routes). Fix every remaining `/api/event` caller found by `grep -rn '"/api/event' src/ tests/`.

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest tests/unit/monitor -q && uv run nox -s lint typecheck`
Expected: PASS.

**Mutation check:** temporarily make `_mutation_guard` return `None` unconditionally — `test_create_with_wrong_session_404s` and `test_review_mode_mutations_403` must fail; restore.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/server.py tests/
git commit -m "feat(monitor)!: session-aware event routes replace legacy /api/event*"
```

---

### Task 4: Archive event writes (`archive_edit.py`)

**Files:**
- Create: `src/otto/monitor/archive_edit.py`
- Create: `tests/unit/monitor/test_archive_edit.py`
- Modify: `src/otto/monitor/db.py` (extract `EVENT_INSERT_SQL` + `event_insert_params()`; `MetricDB.write_event` switches to them — Chris's dedup directive: the events column list must have ONE definition the live writer and the archive editor share. The two UPDATE statements deliberately stay separate: they differ in WHERE scoping — live trusts its bound frame, the archive editor scopes by `session_id`.)

```python
# added to src/otto/monitor/db.py at module level, near the schema statements
EVENT_INSERT_SQL = (
    "INSERT INTO events (session_id, ts, end_ts, label, source, color, dash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def event_insert_params(
    session_id: str,
    *,
    timestamp: datetime,
    end_timestamp: datetime | None,
    label: str,
    source: str,
    color: str,
    dash: str,
) -> tuple[str, str, str | None, str, str, str, str]:
    """Positional params matching EVENT_INSERT_SQL — shared by the live
    MetricDB and the review-mode archive editor (archive_edit.py) so the
    column list cannot drift between the two writers."""
    return (
        session_id,
        timestamp.isoformat(),
        end_timestamp.isoformat() if end_timestamp else None,
        label,
        source,
        color,
        dash,
    )
```

`MetricDB.write_event`'s execute becomes `await self._conn.execute(EVENT_INSERT_SQL, event_insert_params(self._frame.id, timestamp=event.timestamp, end_timestamp=event.end_timestamp, label=event.label, source=event.source, color=event.color, dash=event.dash))`; `archive_edit.insert_event`'s execute becomes `conn.execute(EVENT_INSERT_SQL, event_insert_params(session_id, timestamp=timestamp, end_timestamp=end_timestamp, label=label, source=source, color=color, dash=dash))` with `from .db import EVENT_INSERT_SQL, event_insert_params` — adjust the prescribed code below accordingly.

**Interfaces:**
- Produces:
  - `db.EVENT_INSERT_SQL` / `db.event_insert_params(...)` — the one events-INSERT definition
  - `class ArchiveLockedError(RuntimeError)` — the archive's `.lock` is held (a live collector is writing it)
  - `insert_event(path, session_id, *, timestamp, end_timestamp, label, source, color, dash) -> int` (returns the new rowid; raises `LookupError` if the session row is absent)
  - `update_event(path, session_id, event_id, *, timestamp, end_timestamp, label, color, dash) -> bool`
  - `delete_event(path, session_id, event_id) -> bool`
  - All synchronous (the server calls them via `asyncio.to_thread`); `timestamp`/`end_timestamp` are `datetime`/`datetime | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/monitor/test_archive_edit.py
"""Per-mutation read-write archive event edits (Plan 5c review-mode backend)."""

import asyncio
import fcntl
import os
from datetime import datetime, timedelta, timezone

import pytest

from otto.models import LabSnapshot
from otto.monitor import archive_edit
from otto.monitor.collector import MetricCollector
from otto.monitor.db import read_sessions
from otto.monitor.export import build_session_metric_db
from otto.monitor.session import new_frame

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _make_archive(tmp_path) -> tuple[str, str]:
    """A real finalized v2 archive with one session; returns (path, session_id)."""
    path = str(tmp_path / "a.db")
    frame = new_frame(label=None, note=None)
    db = build_session_metric_db(path, frame, LabSnapshot(), MetricCollector(hosts=[]), interval=5.0)

    async def _build() -> None:
        await db.open()
        await db.finalize(T0 + timedelta(minutes=10))
        await db.close()

    asyncio.run(_build())
    return path, frame.id


def test_insert_round_trips_through_read_sessions(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path, sid, timestamp=T0, end_timestamp=None,
        label="manual mark", source="manual", color="#888888", dash="dash",
    )
    [row] = read_sessions(path)
    [(event_id, ts, end_ts, label, source, color, dash)] = row.events
    assert event_id == rowid and label == "manual mark" and end_ts is None


def test_update_and_delete_by_session_and_id(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    rowid = archive_edit.insert_event(
        path, sid, timestamp=T0, end_timestamp=None,
        label="x", source="manual", color="#888888", dash="dash",
    )
    assert archive_edit.update_event(
        path, sid, rowid, timestamp=T0, end_timestamp=T0 + timedelta(minutes=2),
        label="renamed", color="#2ca02c", dash="solid",
    )
    [row] = read_sessions(path)
    [(_, _, end_ts, label, *_rest)] = row.events
    assert label == "renamed" and end_ts is not None
    assert archive_edit.delete_event(path, sid, rowid)
    assert read_sessions(path)[0].events == []


def test_wrong_session_is_refused(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    with pytest.raises(LookupError):
        archive_edit.insert_event(
            path, "not-a-session", timestamp=T0, end_timestamp=None,
            label="x", source="manual", color="#888888", dash="dash",
        )
    rowid = archive_edit.insert_event(
        path, sid, timestamp=T0, end_timestamp=None,
        label="x", source="manual", color="#888888", dash="dash",
    )
    assert not archive_edit.update_event(
        path, "not-a-session", rowid, timestamp=T0, end_timestamp=None,
        label="y", color="#888888", dash="dash",
    )
    assert not archive_edit.delete_event(path, "not-a-session", rowid)


def test_held_lock_raises_archive_locked(tmp_path) -> None:
    path, sid = _make_archive(tmp_path)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(archive_edit.ArchiveLockedError):
            archive_edit.insert_event(
                path, sid, timestamp=T0, end_timestamp=None,
                label="x", source="manual", color="#888888", dash="dash",
            )
    finally:
        os.close(fd)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_archive_edit.py -q`
Expected: FAIL — `ModuleNotFoundError: otto.monitor.archive_edit`

- [ ] **Step 3: Implement**

```python
# src/otto/monitor/archive_edit.py
"""Per-mutation read-write event edits against a v2 session archive (Plan 5c).

Review mode holds no standing write connection — archives are cold files.
Each mutation opens the archive read-write under the same ``.lock`` flock a
live :class:`~otto.monitor.db.MetricDB` holds for its whole run, applies one
statement stamped with the target session id, commits, and closes. A held
lock means a live collector is writing this very file: refuse loud
(:class:`ArchiveLockedError` -> the server's 409), never queue behind it.

Synchronous by design (the sqlite3/`read_sessions` precedent); the server
calls these off the event loop via ``asyncio.to_thread``.
"""

import contextlib
import fcntl
import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime


class ArchiveLockedError(RuntimeError):
    """The archive's ``.lock`` is held — a live otto monitor is writing it."""


@contextlib.contextmanager
def _locked_connection(path: str) -> Iterator[sqlite3.Connection]:
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            raise ArchiveLockedError(
                f"'{path}' is being written by a live otto monitor session; "
                "stop it (or wait for the run to finish) before editing the archive."
            ) from err
        conn = sqlite3.connect(path)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        # Closing the fd releases the flock if it was acquired.
        os.close(fd)


def _require_session(conn: sqlite3.Connection, path: str, session_id: str) -> None:
    if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
        raise LookupError(f"'{path}' holds no session '{session_id}'")


def insert_event(
    path: str,
    session_id: str,
    *,
    timestamp: datetime,
    end_timestamp: datetime | None,
    label: str,
    source: str,
    color: str,
    dash: str,
) -> int:
    """Insert one event row for *session_id*; returns the new rowid."""
    with _locked_connection(path) as conn:
        _require_session(conn, path, session_id)
        cursor = conn.execute(
            "INSERT INTO events (session_id, ts, end_ts, label, source, color, dash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                timestamp.isoformat(),
                end_timestamp.isoformat() if end_timestamp else None,
                label,
                source,
                color,
                dash,
            ),
        )
        conn.commit()
        rowid = cursor.lastrowid
        assert rowid is not None  # noqa: S101 — SQLite always sets lastrowid after INSERT
        return rowid


def update_event(
    path: str,
    session_id: str,
    event_id: int,
    *,
    timestamp: datetime,
    end_timestamp: datetime | None,
    label: str,
    color: str,
    dash: str,
) -> bool:
    """Overwrite an event's editable fields. False if (session, id) matches nothing."""
    with _locked_connection(path) as conn:
        cursor = conn.execute(
            "UPDATE events SET label = ?, color = ?, dash = ?, ts = ?, end_ts = ? "
            "WHERE id = ? AND session_id = ?",
            (
                label,
                color,
                dash,
                timestamp.isoformat(),
                end_timestamp.isoformat() if end_timestamp else None,
                event_id,
                session_id,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_event(path: str, session_id: str, event_id: int) -> bool:
    """Delete one event row. False if (session, id) matches nothing."""
    with _locked_connection(path) as conn:
        cursor = conn.execute(
            "DELETE FROM events WHERE id = ? AND session_id = ?", (event_id, session_id)
        )
        conn.commit()
        return cursor.rowcount > 0
```

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest tests/unit/monitor/test_archive_edit.py -q && uv run nox -s lint typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/archive_edit.py tests/unit/monitor/test_archive_edit.py
git commit -m "feat(monitor): flocked per-mutation event writes for v2 archives"
```

---

### Task 5: Review-mode `.db` editing + `editable` in `/api/mode`

**Files:**
- Modify: `src/otto/monitor/server.py` (`MonitorServer.__init__`, `_build_app`)
- Modify: `src/otto/cli/monitor.py` (`_serve_review` + its call site)
- Test: `tests/unit/monitor/test_server.py` (extend `TestSessionEventRoutes` + mode tests)

**Interfaces:**
- Produces: `MonitorServer(..., archive_path: Path | None = None)`; `GET /api/mode` → `{"mode", "source", "editable"}` where `editable = (mode == "live") or archive_path is not None`. Review-`.db` mutations persist to the archive AND update the served document body in place; `.json` review keeps the Task 3 403; a flock collision surfaces as 409.
- Consumes: Task 4's `archive_edit` functions and errors.

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/monitor/test_server.py` (build the archive with Task 4's `_make_archive` idiom — move that helper into the file or a shared `tests/unit/monitor/_archive.py` if both files need it):

```python
class TestReviewDbEditing:
    async def test_mode_advertises_editable(self, tmp_path) -> None:
        # live server -> editable true; .db review -> true; .json review -> false
        ...  # three servers, GET /api/mode, assert the editable field

    async def test_create_persists_and_updates_served_document(self, tmp_path) -> None:
        path, sid = _make_archive(tmp_path)
        server = _review_server(document=build_db_export(path), source=path, archive_path=Path(path))
        status, body = await post_json(
            server, f"/api/session/{sid}/event",
            {"label": "post-hoc note", "timestamp": "2026-07-18T12:01:00+00:00"},
        )
        assert status == 201
        # 1) the served document reflects it immediately (no restart)
        doc = json.loads(await get_text(server, "/api/monitor_sessions"))
        assert [e for e in doc["sessions"][0]["events"] if e["label"] == "post-hoc note"]
        # 2) it survives a fresh read of the archive (a restart)
        fresh = build_db_export(path)
        assert [e for e in fresh.sessions[0].events if e.label == "post-hoc note"]

    async def test_locked_archive_409s(self, tmp_path) -> None:
        path, sid = _make_archive(tmp_path)
        server = _review_server(document=build_db_export(path), source=path, archive_path=Path(path))
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status, _ = await post_json(server, f"/api/session/{sid}/event", {"label": "x"})
            assert status == 409
        finally:
            os.close(fd)

    async def test_delete_and_patch_round_trip(self, tmp_path) -> None:
        ...  # create -> PATCH label -> served doc shows it -> DELETE -> gone from doc AND build_db_export
```

Flesh the elided bodies out fully in the test file (the shapes above are complete for the interesting assertions; `_review_server` is a small local helper constructing `MonitorServer(collector=MetricCollector(targets=[]), mode="review", document=..., source_name=..., archive_path=...)` and serving it the same way the file's other tests do). Also update `test_review_mode_mutations_403` from Task 3 to construct its server with `archive_path=None` (a `.json` review) so it keeps pinning the 403.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_server.py -q`
Expected: new tests FAIL (`TypeError: unexpected keyword 'archive_path'`).

- [ ] **Step 3: Implement**

`src/otto/monitor/server.py`:
- `MonitorServer.__init__` gains `archive_path: Path | None = None`, stored and forwarded to `_build_app` (same keyword).
- `_build_app` signature gains `archive_path: Path | None = None`. Add `from . import archive_edit` and `from ..models.monitor import EventRecord` (already imported in Task 3).
- Replace the one-shot `_document_body` constant with a mutable holder, and add document-patch helpers:

```python
    # The served review body is cached (a --db archive's document can hold many
    # sessions; re-serializing per request is waste) but no longer immutable:
    # a review-mode event mutation (Plan 5c) patches `document` in place and
    # refreshes this cache. A dict holder rather than a bare nonlocal keeps the
    # closure reads/writes obvious.
    _document_state: dict[str, str | None] = {
        "body": document_json(document) if document is not None else None
    }

    def _require_document_body() -> str:
        body = _document_state["body"]
        if body is None:
            raise RuntimeError(
                "MonitorServer built with mode='review' but no document — this "
                "is a programming error: the CLI always supplies one for "
                "review mode."
            )
        return body

    def _document_session(session_id: str) -> "SessionRecord | None":
        if document is None:
            return None
        return next((s for s in document.sessions if s.id == session_id), None)

    def _patch_document_event(session_id: str, record: EventRecord) -> None:
        """Upsert *record* into the served document and refresh the cached body."""
        session = _document_session(session_id)
        assert session is not None  # noqa: S101 — guard ran before any write
        for i, existing in enumerate(session.events):
            if existing.id == record.id:
                session.events[i] = record
                break
        else:
            session.events.append(record)
        _document_state["body"] = document_json(document)

    def _drop_document_event(session_id: str, event_id: int) -> None:
        session = _document_session(session_id)
        assert session is not None  # noqa: S101 — guard ran before any write
        session.events = [e for e in session.events if e.id != event_id]
        _document_state["body"] = document_json(document)
```

(import `SessionRecord` under `from ..models.monitor import ...` for the annotation.)
- `_mutation_guard` becomes mode-aware:

```python
    def _mutation_guard(session_id: str) -> JSONResponse | None:
        """404 for a session this server doesn't hold; 403 where editing is impossible."""
        if mode == "live":
            if frame is None or session_id != collector.session_id:
                return JSONResponse({"error": "unknown session"}, status_code=404)
            return None
        if archive_path is None:
            # A .json source has no persistence target (spec: .json review is
            # read-only); the UI hides marking via /api/mode's editable flag.
            return JSONResponse(
                {"error": "this monitor source is read-only"}, status_code=403
            )
        if _document_session(session_id) is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return None
```

- `get_mode` becomes:

```python
        return JSONResponse(
            {
                "mode": mode,
                "source": source_name,
                "editable": mode == "live" or archive_path is not None,
            }
        )
```

- Each mutation route grows a review branch after the guard. **The review branches call the SAME `event_ops` helpers Task 3 created — `resolve_create` runs once before the mode branch, and the review PATCH//end branches call `merge_update` on the document `EventRecord`'s fields (`existing_timestamp=record.timestamp`, `existing_end=record.end_timestamp`, …). Re-implementing the merge/validation in the review branch is a defect (Chris's dedup directive), not a style choice.** Pattern for create (the other three follow it; `timestamp`/`end_timestamp` below are `resolve_create`'s outputs):

```python
        if mode == "review":
            assert archive_path is not None  # noqa: S101 — guard enforced this
            try:
                rowid = await asyncio.to_thread(
                    archive_edit.insert_event,
                    str(archive_path),
                    session_id,
                    timestamp=timestamp,
                    end_timestamp=end_timestamp,
                    label=body.label,
                    source="manual",
                    color=body.color,
                    dash=body.dash,
                )
            except archive_edit.ArchiveLockedError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            record = EventRecord(
                id=rowid,
                timestamp=timestamp,
                end_timestamp=end_timestamp,
                label=body.label,
                source="manual",
                color=body.color,
                dash=body.dash,
            )
            _patch_document_event(session_id, record)
            return JSONResponse(
                record.model_dump(mode="json", exclude_none=True), status_code=201
            )
```

For end/PATCH the "existing event" lookup in review mode reads the **document session's** `events` (not `collector.get_events()`): factor a small `_find_event(session_id, event_id) -> EventRecord | MonitorEvent | None` that branches on mode, or inline the branch per route — keep the merged-span 422 check identical in both branches. `update_event`/`delete_event` review branches call `archive_edit.update_event`/`delete_event` (falsy → 404), then `_patch_document_event`/`_drop_document_event`. `end` in review stamps `datetime.now(tz=timezone.utc)` exactly like live and 409s when the document record already has an `end_timestamp`.

`src/otto/cli/monitor.py` — `_serve_review` gains `archive_path: Path | None = None`, forwards it to `MonitorServer`; the call site passes `archive_path=path if path.suffix.lower() == ".db" else None` (grep `_serve_review(` for the one call).

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest tests/unit/monitor -q && uv run nox -s lint typecheck`
Expected: PASS.

**Mutation check:** temporarily skip the `_patch_document_event` call in create — `test_create_persists_and_updates_served_document`'s served-document assertion must fail while its `build_db_export` assertion still passes (proving the two assertions pin different mechanisms); restore.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/server.py src/otto/cli/monitor.py tests/unit/monitor/test_server.py
git commit -m "feat(monitor): review-mode .db archives are event-editable; /api/mode advertises editability"
```

---

### Task 5b: Library marks ride the same validation seam

(Chris's dedup directive, 2026-07-18: generalize event handling across otto surfaces — library marks must obey the same rules as web marks.)

**Files:**
- Modify: `src/otto/suite/suite.py` (`add_monitor_event`, around line 572)
- Test: extend the file `grep -rln "add_monitor_event" tests/unit/` names (follow its existing suite-fixture idiom)

**Interfaces:**
- Produces: `OttoSuite.add_monitor_event(label, color, dash)` now validates through `EventCreateBody` before touching the collector — a blank label, non-`#rrggbb` color, or unknown dash raises `pydantic.ValidationError` (a `ValueError` subclass) at the call site instead of silently persisting a style the charts cannot render. Valid calls are behaviorally unchanged. The auto lifecycle fixture (`_otto_monitor_events`) is untouched — it passes fixed known-good constants.
- Consumes: Task 1's `EventCreateBody`.

- [ ] **Step 1: Write the failing test**

In the grep-named test file, following its existing arrangement for a monitor-active suite:

```python
def test_add_monitor_event_rejects_invalid_dash_loud() -> None:
    """Library marks obey the same validation as web marks (one seam)."""
    with pytest.raises(ValueError, match="dash"):
        suite.add_monitor_event("checkpoint", dash="wavy")


def test_add_monitor_event_rejects_non_hex_color() -> None:
    with pytest.raises(ValueError, match="color"):
        suite.add_monitor_event("checkpoint", color="red")
```

plus an assertion in (or beside) the existing happy-path test that a valid call still records exactly as before.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest <that file> -q` — expected: FAIL (no exception raised today; the bad dash is accepted silently).

- [ ] **Step 3: Implement**

At the top of `add_monitor_event`'s body (import `EventCreateBody` from `otto.models.monitor` — a leaf import, no import-budget cost):

```python
        # Same rules as every other marking surface (Plan 5c, one seam):
        # constructing the body IS the validation — pydantic.ValidationError
        # (a ValueError) surfaces bad input at the call site instead of
        # persisting a style the dashboard cannot render.
        EventCreateBody(label=label, color=color, dash=dash)
```

Check `grep -n "add_monitor_event" src/otto/suite/plugin.py` — if the plugin forwards to this method it is covered; if it has its own collector call, give it the same guard. Update `add_monitor_event`'s docstring to name the raise.

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest <that file> tests/unit/monitor -q && uv run nox -s lint typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/suite/suite.py tests/
git commit -m "feat(suite): monitor marks validate through the shared event seam"
```

---

### Task 6: Frontend mutation client (`eventApi.ts`) + `editable` in the store

**Files:**
- Modify: `web/src/data/bootstrap.ts`, `web/src/data/reviewStore.ts`
- Create: `web/src/data/eventApi.ts`
- Test: `web/src/__tests__/eventapi.test.ts` (create), extend `web/src/__tests__/bootstrap.resync.test.ts`'s mode-payload fixtures

**Interfaces:**
- Produces:
  - `reviewStore`: `editable: boolean` (default `false`), actions `setEditable(editable: boolean)` and `addWarning(message: string)` (appends to the existing `warnings` channel — which `DataWarningsBanner` renders, so palette-initiated failures have a pixel).
  - `eventApi.ts`: `class EventApiError extends Error`; `createEvent(sessionId, input: EventCreateInput): Promise<EventRecord>`; `endEvent(sessionId, eventId): Promise<EventRecord>`; `updateEvent(sessionId, eventId, input: EventUpdateInput): Promise<EventRecord>`; `deleteEvent(sessionId, eventId): Promise<void>`; input types per Task 1's TS contract (`EventUpdateInput.end_timestamp?: string | null` — explicit null clears).
  - Every 2xx applies a synthetic fragment via `appendFragment` before resolving.

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/__tests__/eventapi.test.ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createEvent, deleteEvent, EventApiError, updateEvent } from "../data/eventApi";
import { useReviewStore } from "../data/reviewStore";
import minimal from "../../fixtures/minimal.json";

// minimal.json's first session id — see the fixture; hydrate once per test so
// the synthetic fragments have a session to land on.
function hydrate(): string {
  useReviewStore
    .getState()
    .actions.importMonitorSessions(JSON.stringify(minimal), "test");
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

const record = (id: number, label: string) => ({
  id,
  timestamp: "2026-07-18T12:01:00+00:00",
  label,
  source: "manual",
  color: "#888888",
  dash: "dash",
});

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("eventApi", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("createEvent POSTs and upserts the response into the active session", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(7, "deploy"), 201));
    const before = useReviewStore.getState().sessions[0].events.length;
    await createEvent(sid, { label: "deploy" });
    const events = useReviewStore.getState().sessions[0].events;
    expect(events).toHaveLength(before + 1);
    expect(events.at(-1)).toMatchObject({ id: 7, label: "deploy" });
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("a duplicate SSE echo of the same record is a no-op (upsert by id)", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    const after = useReviewStore.getState().sessions[0].events.length;
    // the echo the live stream would deliver:
    useReviewStore.getState().actions.appendFragment({
      format: 1, session: sid, events: [record(7, "deploy")],
    });
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(after);
  });

  it("updateEvent replaces the row in place", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "renamed")));
    await updateEvent(sid, 7, { label: "renamed" });
    const events = useReviewStore.getState().sessions[0].events;
    expect(events.filter((e) => e.id === 7)).toHaveLength(1);
    expect(events.find((e) => e.id === 7)?.label).toBe("renamed");
  });

  it("deleteEvent removes the row", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteEvent(sid, 7);
    expect(useReviewStore.getState().sessions[0].events.find((e) => e.id === 7)).toBeUndefined();
  });

  it("a non-2xx surfaces the server's error and applies nothing", async () => {
    const sid = hydrate();
    const before = useReviewStore.getState().sessions[0].events.length;
    vi.mocked(fetch).mockResolvedValue(okJson({ error: "archive is locked" }, 409));
    await expect(createEvent(sid, { label: "x" })).rejects.toThrow(EventApiError);
    await expect(
      createEvent(sid, { label: "x" }).catch((e) => Promise.reject(e.message)),
    ).rejects.toMatch("archive is locked");
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(before);
  });
});
```

(Check `web/fixtures/minimal.json`'s actual session id/shape and adjust `hydrate()` accordingly; if `minimal.json` carries no session, use `kitchen-sink.json`.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd web && npx vitest run src/__tests__/eventapi.test.ts`
Expected: FAIL — module `../data/eventApi` not found.

- [ ] **Step 3: Implement**

```ts
// web/src/data/eventApi.ts
// The dashboard's ONLY mutation surface (Plan 5c). One rule for both modes:
// every 2xx response is applied locally as a synthetic fragment through
// applyFragment's existing upsert — in live mode the SSE echo then delivers
// the same record again and the upsert-by-id makes it a no-op, so ordering
// between response and echo cannot matter and no optimistic/rollback state
// exists anywhere. A failed request applies nothing; callers surface the
// thrown EventApiError inline at the control that issued it.
import type { EventCreateBody, EventRecord, EventUpdateBody } from "../api/export.gen";
import { useReviewStore } from "./reviewStore";

export class EventApiError extends Error {}

export type EventCreateInput = Omit<EventCreateBody, "timestamp" | "end_timestamp"> & {
  timestamp?: string;
  end_timestamp?: string;
};
/** `end_timestamp: null` (explicit) clears the end — span becomes a point. */
export type EventUpdateInput = EventUpdateBody;

async function request(path: string, init: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new EventApiError(`Network error: ${String(err)}`);
  }
  if (!res.ok) throw new EventApiError(await errorMessage(res));
  return res;
}

async function errorMessage(res: Response): Promise<string> {
  const fallback = `Request failed (${res.status})`;
  try {
    const body = (await res.json()) as { error?: unknown; detail?: unknown };
    if (typeof body.error === "string") return body.error;
    // FastAPI body-validation failures arrive as {"detail": [{msg, ...}]}.
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const msg = (body.detail[0] as { msg?: unknown } | undefined)?.msg;
      if (typeof msg === "string") return msg;
    }
  } catch {
    // fall through to the status-based message
  }
  return fallback;
}

function applyRecord(sessionId: string, record: EventRecord): void {
  useReviewStore
    .getState()
    .actions.appendFragment({ format: 1, session: sessionId, events: [record] });
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const base = (sessionId: string) => `/api/session/${encodeURIComponent(sessionId)}/event`;

export async function createEvent(
  sessionId: string,
  input: EventCreateInput,
): Promise<EventRecord> {
  const res = await request(base(sessionId), jsonInit("POST", input));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

export async function endEvent(sessionId: string, eventId: number): Promise<EventRecord> {
  const res = await request(`${base(sessionId)}/${eventId}/end`, jsonInit("POST", {}));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

export async function updateEvent(
  sessionId: string,
  eventId: number,
  input: EventUpdateInput,
): Promise<EventRecord> {
  const res = await request(`${base(sessionId)}/${eventId}`, jsonInit("PATCH", input));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

export async function deleteEvent(sessionId: string, eventId: number): Promise<void> {
  await request(`${base(sessionId)}/${eventId}`, { method: "DELETE" });
  useReviewStore
    .getState()
    .actions.appendFragment({ format: 1, session: sessionId, deleted_event_ids: [eventId] });
}
```

`web/src/data/reviewStore.ts` — add to `ReviewState`: `editable: boolean;` (initial `false`); to `ReviewActions` and `actions`:

```ts
    /** From /api/mode (Plan 5c): whether this server accepts event mutations
     * (live, or a .db-sourced review). Gates every marking affordance. */
    setEditable: (editable: boolean) => set({ editable }),
    /** Append one message to the warnings channel (rendered by
     * DataWarningsBanner) — the surface for mutation failures issued from
     * chrome with no inline error slot of its own (palette commands). */
    addWarning: (message) => set({ warnings: [...get().warnings, message] }),
```

`web/src/data/bootstrap.ts` — `ModePayload` gains `editable: boolean`; `isModePayload` additionally requires `typeof rec.editable === "boolean"`; after each `setMode(...)` call add `useReviewStore.getState().actions.setEditable(modeBody.editable);` (in the live branch, only inside the `if (hydrated)` block, next to `setMode("live")`). Update the mode-payload fixtures in `web/src/__tests__/bootstrap.resync.test.ts` (grep `"/api/mode"`) to carry `editable`.

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

**Mutation check:** temporarily drop the `applyRecord` call in `createEvent` — the first test's length assertion must fail; restore.

- [ ] **Step 5: Commit**

```bash
git add web/src/data/eventApi.ts web/src/data/reviewStore.ts web/src/data/bootstrap.ts web/src/__tests__/
git commit -m "feat(monitor-web): event mutation client applying responses as synthetic fragments"
```

---

### Task 7: Marking state, bindings, palette commands, shared helpers

**Files:**
- Modify: `web/src/ui/shortcuts.ts`, `web/src/ui/uiStore.ts`, `web/src/ui/commands.ts`, `web/src/ui/useGlobalShortcuts.ts`
- Create: `web/src/shell/marking.ts`
- Test: extend `web/src/__tests__/commands.test.tsx`, `web/src/__tests__/useglobalshortcuts.test.tsx`; create `web/src/__tests__/marking.test.ts`

**Interfaces:**
- Produces:
  - `shortcuts.ts`: `export const MARK_NOW_BINDING: Binding = { key: "e", mod: true };` (⌘E — clear of the reserved list; ⌘M is macOS-owned).
  - `uiStore.ts`: `export interface EventDraft { sessionId: string; timestampMs: number; endTimestampMs: number | null; label: string; color: string; dash: string }`; `export type EventEditorTarget = { kind: "edit"; sessionId: string; eventId: number } | { kind: "draft"; draft: EventDraft }`; state `eventEditor: EventEditorTarget | null`, `sweepArmed: boolean`, `openSpan: { sessionId: string; eventId: number } | null`, `markPopover: "mark" | "start" | null`; actions `openEventEditor(target)`, `closeEventEditor()`, `armSweep()`, `disarmSweep()`, `setOpenSpan(span | null)`, `openMarkPopover(kind)`, `closeMarkPopover()`.
  - `marking.ts`: `markNow(label): Promise<void>`, `startSpan(label): Promise<void>` (creates then `setOpenSpan`), `endOpenSpan(): Promise<void>` (ends then clears), `blankDraft(session: NormalizedSession): EventDraft` (label "", color `"#888888"`, dash `"dash"`, `timestampMs: session.endMs`, `endTimestampMs: null`). All throw `EventApiError` upward — callers decide the surface.
  - Palette: Actions rows `action-add-event` + `action-sweep-span` (enabled when `editable && session`), `action-mark-now` (binding ⌘E) + `action-start-span` + `action-end-span` (live-only; end-span `enabled` only while `openSpan` matches the active session). Command-initiated failures go to `addWarning` (the banner pixel).
  - `useGlobalShortcuts`: `Escape` disarms an armed sweep before any other handling.

- [ ] **Step 1: Write the failing tests**

In `web/src/__tests__/marking.test.ts` (fetch-mocked like eventapi.test.ts): `startSpan("soak")` sets `openSpan` to the created id; `endOpenSpan()` calls the `/end` route for that id and clears `openSpan`; `markNow` posts with no timestamp field. In `commands.test.tsx` (follow its existing render-hook idiom): with `editable: true` + live mode the five marking rows exist with the stated ids/labels/enabled states; with `editable: true` + review mode only `action-add-event`/`action-sweep-span` appear; with `editable: false` none do; `action-mark-now` carries `MARK_NOW_BINDING`. In `useglobalshortcuts.test.tsx`: with `sweepArmed: true`, dispatching `Escape` calls `disarmSweep` and does not run any command.

- [ ] **Step 2: Run to verify they fail**

Run: `cd web && npx vitest run src/__tests__/marking.test.ts src/__tests__/commands.test.tsx src/__tests__/useglobalshortcuts.test.tsx`
Expected: FAIL (missing exports).

- [ ] **Step 3: Implement**

`shortcuts.ts`: append `export const MARK_NOW_BINDING: Binding = { key: "e", mod: true };` (with a one-line comment noting it cleared the reserved-key rule).

`uiStore.ts`: extend the interface/store with the fields and trivial actions above (each action is a one-line `set`; `disarmSweep: () => set({ sweepArmed: false })`, etc.).

`marking.ts`:

```ts
// web/src/shell/marking.ts
// Imperative marking helpers shared by MarkControl, the palette commands and
// EventsPanel's compose row — one implementation per flow, three triggers.
// All throw EventApiError upward: the CALLER owns the error surface (inline
// text for controls, the warnings banner for palette-initiated runs).
import { createEvent, endEvent } from "../data/eventApi";
import type { NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { type EventDraft, useUiStore } from "../ui/uiStore";

function requireActiveSessionId(): string {
  const id = useReviewStore.getState().activeSessionId;
  if (id === null) throw new Error("no active monitor session");
  return id;
}

export async function markNow(label: string): Promise<void> {
  await createEvent(requireActiveSessionId(), { label });
}

export async function startSpan(label: string): Promise<void> {
  const sessionId = requireActiveSessionId();
  const record = await createEvent(sessionId, { label });
  if (record.id != null) {
    useUiStore.getState().actions.setOpenSpan({ sessionId, eventId: record.id });
  }
}

export async function endOpenSpan(): Promise<void> {
  const span = useUiStore.getState().openSpan;
  if (span === null) return;
  await endEvent(span.sessionId, span.eventId);
  useUiStore.getState().actions.setOpenSpan(null);
}

export function blankDraft(session: NormalizedSession): EventDraft {
  return {
    sessionId: session.id,
    timestampMs: session.endMs,
    endTimestampMs: null,
    label: "",
    color: "#888888",
    dash: "dash",
  };
}
```

`commands.ts`: read `editable`, `openSpan` and the ui actions; after the theme action, push (icons: pick from `@untitledui/icons` — `Flag01` for marking rows, `Scissors01`/`Crop01` for sweep; use what exists, `npx vitest run` will fail the import if not):

```ts
    if (editable && session) {
      commands.push(
        {
          id: "action-add-event",
          label: "Add event…",
          section: "Actions",
          icon: Flag01,
          enabled: true,
          run: () => openEventEditor({ kind: "draft", draft: blankDraft(session) }),
        },
        {
          id: "action-sweep-span",
          label: "Sweep span on chart",
          section: "Actions",
          icon: Flag01,
          enabled: true,
          run: armSweep,
        },
      );
      if (mode === "live") {
        commands.push(
          {
            id: "action-mark-now",
            label: "Mark now…",
            section: "Actions",
            icon: Flag01,
            binding: MARK_NOW_BINDING,
            enabled: true,
            run: () => openMarkPopover("mark"),
          },
          {
            id: "action-start-span",
            label: "Start span…",
            section: "Actions",
            icon: Flag01,
            enabled: true,
            run: () => openMarkPopover("start"),
          },
          {
            id: "action-end-span",
            label: "End span",
            section: "Actions",
            icon: Flag01,
            enabled: openSpan?.sessionId === session.id,
            run: () => {
              void endOpenSpan().catch((err) =>
                addWarning(`End span failed: ${err instanceof Error ? err.message : String(err)}`),
              );
            },
          },
        );
      }
    }
```

(add the new dependencies to the `useMemo` dep list — biome will flag any misses.)

`useGlobalShortcuts.ts`: read `sweepArmed`/`disarmSweep` from `useUiStore`; at the top of the keydown handler:

```ts
      if (e.key === "Escape" && sweepArmed) {
        e.preventDefault();
        disarmSweep();
        return;
      }
```

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/ web/src/shell/marking.ts web/src/__tests__/
git commit -m "feat(monitor-web): marking state, palette commands and shared marking helpers"
```

---

### Task 8: AppBar MarkControl (split button + label popover)

**Files:**
- Create: `web/src/shell/MarkControl.tsx`
- Modify: `web/src/shell/AppBar.tsx`
- Test: `web/src/__tests__/markcontrol.test.tsx` (create)

**Interfaces:**
- Produces: `<MarkControl />` — rendered by AppBar when `mode === "live" && hasData`. Split control: primary `Button` "Mark now…" (`data-testid="mark-button"`) opening the label popover; a chevron `ButtonUtility` (`data-testid="mark-menu"`) opening a `Dropdown` with *Start span…* (`menu-start-span`), *End span* (`menu-end-span`, disabled without a matching `openSpan`), *Sweep span on chart* (`menu-sweep-span`), *Add event…* (`menu-add-event`). The popover (`data-testid="mark-popover"`) holds an autofocused label field (`mark-label-input`), a submit `Button` (`mark-submit`, label "Mark" or "Start" per `markPopover` kind), Enter submits, inline error text (`mark-error`) on `EventApiError`, closes on success.
- Consumes: Task 7's `marking.ts`, `uiStore` (`markPopover`, `openMarkPopover`, `closeMarkPopover`, `openSpan`, `armSweep`, `openEventEditor` + `blankDraft`).

- [ ] **Step 1: Write the failing test**

In `markcontrol.test.tsx` (jsdom + `@testing-library/react` + `user-event` — **react-aria needs `user-event`'s pointer sequences, `fireEvent.click` does not drive it**; copy the setup idiom from `commandmenu.test.tsx`): seed the store live+editable with a session (fixture hydrate as in eventapi.test.ts + `setMode("live")`, `setEditable(true)`); mock fetch. Cases: clicking `mark-button` opens the popover; typing a label + Enter POSTs to the create route and closes; a rejected fetch shows `mark-error` with the server message and keeps the popover open; the menu's *Start span…* opens the popover in start mode and submit records `openSpan`; *End span* disabled until then, enabled after.

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/__tests__/markcontrol.test.tsx`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```tsx
// web/src/shell/MarkControl.tsx
// The live marking hub (spec 2026-07-18 §UI surfaces): a composed split
// control — no vendored split button exists, so primary Button + Dropdown,
// matching the AppBar's ButtonUtility row. The label popover serves both
// Mark-now and Start-span (uiStore.markPopover carries which); flows that
// need explicit times live in the EventEditor, reachable from the menu.
import { ChevronDown } from "@untitledui/icons";
import { useEffect, useRef, useState } from "react";
import { Dialog, DialogTrigger, Popover } from "react-aria-components";

import { Button } from "@/components/base/buttons/button";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useActiveSession } from "../data/reviewStore";
import { TextInput } from "../ui/TextInput";
import { useUiStore } from "../ui/uiStore";
import { blankDraft, endOpenSpan, markNow, startSpan } from "./marking";

export function MarkControl() {
  const session = useActiveSession();
  const markPopover = useUiStore((s) => s.markPopover);
  const openSpan = useUiStore((s) => s.openSpan);
  const { openMarkPopover, closeMarkPopover, armSweep, openEventEditor } = useUiStore(
    (s) => s.actions,
  );
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset the draft label/error each time the popover opens, then focus.
  useEffect(() => {
    if (markPopover !== null) {
      setLabel("");
      setError(null);
      inputRef.current?.focus();
    }
  }, [markPopover]);

  if (!session) return null;
  const spanOpen = openSpan?.sessionId === session.id;

  const submit = async () => {
    if (!label.trim()) return;
    try {
      await (markPopover === "start" ? startSpan(label) : markNow(label));
      closeMarkPopover();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="flex items-center gap-0.5">
      <DialogTrigger
        isOpen={markPopover !== null}
        onOpenChange={(open) => {
          if (open) openMarkPopover("mark");
          else closeMarkPopover();
        }}
      >
        <Button size="sm" color="secondary" data-testid="mark-button">
          Mark now…
        </Button>
        <Popover placement="bottom end" offset={8}>
          <Dialog
            data-testid="mark-popover"
            aria-label={markPopover === "start" ? "Start span" : "Mark now"}
            className="flex items-start gap-2 rounded-xl bg-primary p-3 shadow-lg ring
              ring-secondary_alt focus:outline-hidden"
          >
            <div className="flex flex-col gap-1">
              <TextInput
                ref={inputRef}
                data-testid="mark-label-input"
                aria-label="Event label"
                placeholder="Label"
                value={label}
                onChange={setLabel}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submit();
                }}
              />
              {error !== null && (
                <p data-testid="mark-error" className="max-w-56 text-xs text-error-primary">
                  {error}
                </p>
              )}
            </div>
            <Button size="sm" color="primary" data-testid="mark-submit" onPress={() => void submit()}>
              {markPopover === "start" ? "Start" : "Mark"}
            </Button>
          </Dialog>
        </Popover>
      </DialogTrigger>
      <Dropdown.Root>
        <ButtonUtility
          aria-label="More marking actions"
          data-testid="mark-menu"
          icon={ChevronDown}
          color="tertiary"
          size="sm"
        />
        <Dropdown.Popover>
          <Dropdown.Menu>
            <Dropdown.Section>
              <Dropdown.Item
                id="start-span"
                label="Start span…"
                onAction={() => openMarkPopover("start")}
                data-testid="menu-start-span"
              />
              <Dropdown.Item
                id="end-span"
                label="End span"
                isDisabled={!spanOpen}
                onAction={() => void endOpenSpan().catch(() => {})}
                data-testid="menu-end-span"
              />
              <Dropdown.Item
                id="sweep-span"
                label="Sweep span on chart"
                onAction={armSweep}
                data-testid="menu-sweep-span"
              />
              <Dropdown.Item
                id="add-event"
                label="Add event…"
                onAction={() => openEventEditor({ kind: "draft", draft: blankDraft(session) })}
                data-testid="menu-add-event"
              />
            </Dropdown.Section>
          </Dropdown.Menu>
        </Dropdown.Popover>
      </Dropdown.Root>
    </div>
  );
}
```

Adapt `TextInput`'s props to its actual interface (read `web/src/ui/TextInput.tsx` first — if it lacks `ref`/`onKeyDown` pass-through, extend it there, it is authored code). The bare `.catch(() => {})` on the menu's End-span is wrong — route it to `addWarning` exactly as the palette command does (import `useReviewStore` for the action); the code block above shows placement, use the Task 7 catch shape.

In `AppBar.tsx`: `import { MarkControl } from "./MarkControl";` and render `{mode === "live" && hasData && <MarkControl />}` as the FIRST child of the right-hand cluster (before the pause toggle).

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/MarkControl.tsx web/src/shell/AppBar.tsx web/src/ui/TextInput.tsx web/src/__tests__/markcontrol.test.tsx
git commit -m "feat(monitor-web): AppBar mark split control with label popover"
```

---

### Task 9: EventEditor slide-over

**Files:**
- Create: `web/src/ui/calendarTime.ts` (extract `msToCalendarDateTime`/`calendarDateTimeToMs` from `RangePicker.tsx`; RangePicker imports them — pure move, its tests stay green)
- Create: `web/src/shell/EventEditor.tsx`
- Modify: `web/src/App.tsx` (mount `<EventEditor />` next to `<CommandLayer />` in the hasData branch)
- Test: `web/src/__tests__/eventeditor.test.tsx` (create)

**Interfaces:**
- Produces: `<EventEditor />` — self-contained; renders nothing while `uiStore.eventEditor === null`. Slide-over (`data-testid="event-editor"`) composed exactly like EventsPanel (ModalOverlay/Modal/Dialog + SlideoutMenu.Header/Content/Footer). Fields: label (`editor-label`), start/end date-time fields at **second** granularity (`editor-start`, `editor-end`; empty end ⇒ point event), color swatch row (`editor-color-<hex>` per swatch, from `EVENT_COLOR_SWATCHES`), dash `Select` (`editor-dash`) over the six `VALID_DASH_STYLES` values (hardcode the list in a module constant with a comment naming `otto/models/monitor.py` as the source — there is no TS export of it; a mismatched entry just 422s loudly). Footer: Save (`editor-save`), Cancel (`editor-cancel`), Delete (`editor-delete`, edit-mode only, two-press confirm: first press relabels to "Really delete?"). Draft targets create; edit targets send a full-field PATCH (`end_timestamp: null` explicitly when the end field is empty). Inline error `editor-error`. Success closes the editor.
- Consumes: `eventApi.createEvent/updateEvent/deleteEvent`, `uiStore.eventEditor/closeEventEditor`, `calendarTime.ts`, `useActiveSession` (edit mode resolves the record by id from `session.events`).

- [ ] **Step 1: Write the failing test**

`eventeditor.test.tsx` (same jsdom/user-event idiom as Task 8): seed store with kitchen-sink fixture + `setEditable(true)`. Cases: `openEventEditor({kind:"draft", draft:{...}})` renders prefilled fields; Save POSTs create with ISO strings matching the draft ms and closes; editing an existing span (`openEventEditor({kind:"edit", ...})` with a real event id from the fixture) prefills from the record, clearing the end field PATCHes `end_timestamp: null`; Delete requires the second press and then DELETEs; a 422 keeps it open with `editor-error` text. Mutation check to run once implemented: temporarily make Save send `timestamp` unconditionally as `new Date().toISOString()` — the draft-ms assertion must fail; restore.

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/__tests__/eventeditor.test.tsx`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

`calendarTime.ts` is the verbatim pair from RangePicker plus their imports, exported; RangePicker's two local copies deleted in favor of the import.

`EventEditor.tsx` essentials (structure mirrors EventsPanel's composition; complete the JSX in the same style):

```tsx
// State: one local `form` mirroring EventDraft, seeded on open from the
// target (draft verbatim; edit -> the record resolved out of
// session.events by id, parseTs for ms). Editing state keyed on the
// TARGET's identity: useEffect([target]) reseeds, so reopening never shows
// a stale abandoned edit (the RangePicker reseed-on-open idiom).
const editor = useUiStore((s) => s.eventEditor);
const { closeEventEditor } = useUiStore((s) => s.actions);
// ...seed form, render null when editor === null...

const save = async () => {
  try {
    if (editor.kind === "draft") {
      await createEvent(form.sessionId, {
        label: form.label,
        timestamp: new Date(form.timestampMs).toISOString(),
        ...(form.endTimestampMs !== null
          ? { end_timestamp: new Date(form.endTimestampMs).toISOString() }
          : {}),
        color: form.color,
        dash: form.dash,
      });
    } else {
      await updateEvent(editor.sessionId, editor.eventId, {
        label: form.label,
        timestamp: new Date(form.timestampMs).toISOString(),
        end_timestamp:
          form.endTimestampMs !== null ? new Date(form.endTimestampMs).toISOString() : null,
        color: form.color,
        dash: form.dash,
      });
    }
    closeEventEditor();
  } catch (err) {
    setError(err instanceof Error ? err.message : String(err));
  }
};
```

Date fields use the vendored standalone wrapper `InputDate` (`web/src/components/base/input/input-date.tsx`, the `AriaDateField`-wrapping export — NOT `InputDateBase`, which needs a DateRangePicker slot context):

```tsx
<InputDate
  aria-label="Start"
  granularity="second"
  value={msToCalendarDateTime(form.timestampMs)}
  onChange={(v) => v && setForm({ ...form, timestampMs: calendarDateTimeToMs(v) })}
/>
```

End field mirrors it with `value={form.endTimestampMs === null ? null : msToCalendarDateTime(form.endTimestampMs)}` and a small "clear end" `ButtonUtility` setting it null (react-aria date fields have no native clear). Swatches:

```tsx
// Default manual grey + the app's event accent + the auto lifecycle colors —
// mirrors AUTO_EVENT_COLORS (otto/monitor/events.py) plus two chart hues.
export const EVENT_COLOR_SWATCHES = [
  "#888888", "#7c5cff", "#2ca02c", "#d62728", "#1f77b4", "#ff7f0e",
] as const;
```

rendered as small `<button>` circles with a selection ring, `data-testid={`editor-color-${hex}`}`. Dash uses the vendored `Select` (`web/src/components/base/select/select.tsx` — match its actual item API when writing; SeriesPanel/CommandMenu show in-repo usage patterns). Save disabled while `label.trim() === ""` or `endTimestampMs !== null && endTimestampMs <= timestampMs` (mirror the server's 422 so the common case never round-trips).

`App.tsx`: render `<EventEditor />` immediately after `<CommandLayer />`.

- [ ] **Step 4: Run to verify green (including the named mutation check)**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/calendarTime.ts web/src/ui/RangePicker.tsx web/src/shell/EventEditor.tsx web/src/App.tsx web/src/__tests__/eventeditor.test.tsx
git commit -m "feat(monitor-web): slide-over event editor (draft create + full edit/delete)"
```

---

### Task 10: EventsPanel — compose row, edit/End-now, refused-jump signal

**Files:**
- Modify: `web/src/shell/EventsPanel.tsx`
- Modify: `web/src/pages/SubjectPage.tsx` (events button shows for `editable` even with zero events, so the compose row is reachable: change the `session.events.length > 0` gate to `session.events.length > 0 || editable`)
- Test: extend `web/src/__tests__/events_panel.test.tsx`

**Interfaces:**
- Produces: compose row (`data-testid="events-compose"`) — live+editable: label `TextInput` (`events-compose-label`) + Mark (`events-compose-mark`) / Start (`events-compose-start`) / Stop (`events-compose-stop`, disabled unless `openSpan` matches this session); review+editable: an "Add event…" `Button` (`events-compose-add`) opening a `blankDraft` editor. Per-row (id'd events only): edit `ButtonUtility` (`event-edit-<id>`) opening `{kind:"edit"}`; live endless rows get "End now" (`event-endnow-<id>`). Row click still jumps, but a refused jump (clamped `from >= to`) keeps the panel open and shows `data-testid="jump-notice"` ("Outside the session's time range"), cleared on the next successful jump or reopen.
- Consumes: Tasks 6–9 (`eventApi`, `marking.ts`, `uiStore`, `EventEditor` targets), `reviewStore.editable`.

- [ ] **Step 1: Write the failing tests**

Extend `events_panel.test.tsx`: (a) refused jump — seed a session, click a row whose ±15 min pad falls wholly outside `sessionBounds` (add such an event to the local fixture data or append one via `appendFragment`), assert `setRange` was not committed (range unchanged), panel still present, notice text shown — **this test must be proven red against the pre-fix code** (run it before implementing: it should FAIL today because the panel closes; that failing run is the regression proof); (b) compose row renders per mode/editable matrix; (c) Mark posts and the new row appears (fetch mocked); (d) End-now visible only for live endless rows; (e) edit button opens the editor target.

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd web && npx vitest run src/__tests__/events_panel.test.tsx`
Expected: new cases FAIL (notice/compose row absent; panel closes on refused jump).

- [ ] **Step 3: Implement**

In `EventsPanel.tsx`, the jump becomes:

```tsx
  const [jumpNotice, setJumpNotice] = useState<string | null>(null);

  const jump = (fromMs: number, toMs: number | null) => {
    const clamped = clampRange(
      { from: fromMs - JUMP_PAD_MS, to: (toMs ?? fromMs) + JUMP_PAD_MS },
      sessionBounds(session),
    );
    if (clamped.from >= clamped.to) {
      // setRange would refuse this silently (its inverted-range guard) and
      // the panel would close on a no-op — the recorded follow-up. Name it
      // instead and stay open.
      setJumpNotice("Outside the session's time range");
      return;
    }
    setJumpNotice(null);
    setRange(clamped);
    onClose();
  };
```

Compose row + row affordances follow the interface block above; the Mark/Start/Stop handlers call `marking.ts` helpers with a local inline-error state (`events-compose-error`), the same catch shape as MarkControl. Reset `jumpNotice` when `isOpen` flips true.

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/EventsPanel.tsx web/src/pages/SubjectPage.tsx web/src/__tests__/events_panel.test.tsx
git commit -m "feat(monitor-web): EventsPanel compose row, edit affordances and refused-jump notice"
```

---

### Task 11: Gestures A — wheel freed, Ctrl-drag pan, `+`/`-` zoom, dark-mode label fix

**Files:**
- Modify: `web/src/charts/options.ts`, `web/src/charts/ChartPanel.tsx`, `web/src/pages/SubjectPage.tsx`
- Test: extend `web/src/__tests__/chartoptions.test.ts`, `web/src/__tests__/subjectpage.zoom.test.tsx`

**Interfaces:**
- Produces:
  - `options.ts` dataZoom: `{ type: "inside", filterMode: "none", zoomOnMouseWheel: false, moveOnMouseMove: "ctrl", moveOnMouseWheel: false }` — wheel scrolls the page again (the TODO complaint), Ctrl-drag pans (ECharts' modifier set has no meta key, so pan is Ctrl on every platform — documented in Task 14).
  - `export function zoomAbout(window: TimeRange, factor: number): TimeRange | null` — new span = span×factor about the center, `null` when the zoomed-in span would fall below 1000 ms (the existing `MIN_ZOOM_DELTA_MS` floor).
  - `eventOverlay(events, theme)` — `theme` widens to `Pick<ChartTheme, "muted" | "ink">`; the markArea label object gains `color: theme.ink, fontSize: 10` (the dark-mode fix: it previously inherited ECharts' default label color, illegible against dark surfaces). `windowPatch` and `ChartPanel`'s `theme` prop widen to the same Pick; `ChartPanel`'s fallback becomes `{ muted: "", ink: "" }`.
  - ChartPanel stamps `data-echarts-marker-count` (markers.length) inside the incremental-patch effect — an e2e hook stamped from the imperative path (the 5b remedy pattern), used by Task 13.
  - SubjectPage: each chart wraps in a `relative` container with a left-edge overlay column of two `ButtonUtility`s (`zoom-in-<chartKey>`, `zoom-out-<chartKey>`) calling `onZoom(clampRange(zoomAbout(window_, 0.5| 2), bounds))` (skip when `zoomAbout` returns null).

- [ ] **Step 1: Write the failing tests**

`chartoptions.test.ts`: `buildStackOption`'s dataZoom carries the three new flags; `zoomAbout({from:0,to:10_000}, 0.5)` → `{from:2500,to:7500}`; `zoomAbout({from:0,to:1500}, 0.5)` → `null`; `eventOverlay`'s markArea label carries `color` equal to the passed `theme.ink` (assert on the actual data structure — this is the dark-mode regression pin; **prove it red first**: it must fail against today's overlay, which sets no color). `subjectpage.zoom.test.tsx`: clicking `zoom-in-*` pins a range half the current window about its center.

- [ ] **Step 2: Run to verify they fail**

Run: `cd web && npx vitest run src/__tests__/chartoptions.test.ts src/__tests__/subjectpage.zoom.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`options.ts`:

```ts
export function zoomAbout(window: TimeRange, factor: number): TimeRange | null {
  const span = (window.to - window.from) * factor;
  if (span < 1000) return null; // MIN_ZOOM_DELTA_MS: below this a zoom is noise
  const center = (window.from + window.to) / 2;
  return { from: Math.round(center - span / 2), to: Math.round(center + span / 2) };
}
```

dataZoom line + eventOverlay label change + the `Pick` widenings per the interface block (three call sites: `eventOverlay` signature, `windowPatch` args type, ChartPanel prop + fallback; `buildStackOption` already passes a full `ChartTheme`). ChartPanel's markers effect additionally stamps `el.current.dataset.echartsMarkerCount = String(markers.length);` next to the window-to stamp. SubjectPage's ChartSection gets the overlay column (absolute, `left-1 top-1`, `flex-col gap-1`, icons `Plus`/`Minus` from `@untitledui/icons`).

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/charts/ web/src/pages/SubjectPage.tsx web/src/__tests__/
git commit -m "feat(monitor-web): wheel freed, ctrl-drag pan, +/- zoom buttons, themed span labels"
```

---

### Task 12: Gestures B — brush zoom-select + sweep-to-mark

**Files:**
- Modify: `web/src/charts/echarts.ts` (register `BrushComponent` from `echarts/components`)
- Modify: `web/src/charts/options.ts` (brush config in `buildStackOption`)
- Modify: `web/src/charts/ChartPanel.tsx` (arm/re-arm, `brushEnd`, `dispatchAction` in `EChartsLike`, `sweepArmed`/`onSweep` props)
- Modify: `web/src/pages/SubjectPage.tsx` (sweep wiring + chip)
- Test: extend `web/src/__tests__/chartpanel.test.tsx`, `web/src/__tests__/chartoptions.test.ts`

**Interfaces:**
- Produces:
  - `buildStackOption` output gains `brush: { xAxisIndex: 0, brushStyle: {...}, outOfBrush: { colorAlpha: 1 } }` (`outOfBrush` neutralized — a selection sweep must not dim the series it crosses).
  - `ChartPanel` props gain `sweepArmed?: boolean; onSweep?: (range: TimeRange) => void;`. `EChartsLike` gains `dispatchAction: (payload: Record<string, unknown>) => void;`. Behavior: a `lineX` brush is armed via `dispatchAction({type:"takeGlobalCursor", key:"brush", brushOption:{brushType:"lineX", brushMode:"single"}})` after init AND after every notMerge `setOption` (arming is instance-level; the whole-model rebuild would otherwise silently drop it — one of the spec's two named no-op risks). On `brushEnd`: read `areas[0].coordRange` → `{from, to}` (rounded), always clear the ghost (`dispatchAction({type:"brush", areas:[]})`), ignore sub-1000 ms sweeps, then `onSweep(range)` when `sweepArmed` else `onZoom(range)`.
  - SubjectPage: `sweepArmed` from `uiStore`; `onSweep` → `openEventEditor({kind:"draft", draft:{...span times...}})` + `disarmSweep()`; a chip (`data-testid="sweep-chip"`, "Marking span — drag across a chart · Esc cancels") in the title row while armed.
- Consumes: Task 7 uiStore, Task 9 editor, Task 11 `zoomAbout`/theme work.

- [ ] **Step 1: Write the failing tests**

`chartpanel.test.tsx` already fakes an ECharts instance (see its existing mock): extend the fake with `dispatchAction` recording calls and an `emit("brushEnd", {...})` hook. Cases: (a) after mount, a `takeGlobalCursor` arming call was dispatched; (b) after an `option` prop change (notMerge rebuild), a SECOND arming call was dispatched — **mutation-proof this one**: with the re-arm line removed it must fail (this is the guard for no-op risk #1); (c) `brushEnd` with `coordRange: [t0, t1]` and `sweepArmed: false` calls `onZoom({from:t0,to:t1})` and dispatched a brush-clear; (d) same with `sweepArmed: true` calls `onSweep` and NOT `onZoom`; (e) a 400 ms sweep calls neither but still clears. `chartoptions.test.ts`: `buildStackOption` output has the `brush` key with `outOfBrush.colorAlpha === 1`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd web && npx vitest run src/__tests__/chartpanel.test.tsx src/__tests__/chartoptions.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement**

`echarts.ts`: add `BrushComponent` to the import from `echarts/components` and the `use([...])` list.

`options.ts` (`buildStackOption`, after `dataZoom`):

```ts
    brush: {
      xAxisIndex: 0,
      // Ghost styling for the in-flight sweep; series stay fully painted
      // (outOfBrush colorAlpha 1 — brushing here SELECTS a range, it never
      // filters data).
      brushStyle: { borderWidth: 1, color: "rgba(124, 92, 255, 0.08)", borderColor: "#7c5cff" },
      outOfBrush: { colorAlpha: 1 },
    },
```

`ChartPanel.tsx`:

```ts
const BRUSH_ARM_ACTION = {
  type: "takeGlobalCursor",
  key: "brush",
  brushOption: { brushType: "lineX", brushMode: "single" },
} as const;
```

- `latest` ref carries `{ win, onZoom, sweepArmed, onSweep }`.
- init effect: after `chart.current = instance`, `instance.dispatchAction(BRUSH_ARM_ACTION);` and:

```ts
    instance.on("brushEnd", (e) => {
      const evt = e as { areas?: { coordRange?: [number, number] }[] };
      const coordRange = evt.areas?.[0]?.coordRange;
      // Always clear the ghost — a committed zoom re-renders the window, and
      // a sweep opens the editor; a lingering brush rect over either is noise.
      instance.dispatchAction({ type: "brush", areas: [] });
      if (!coordRange) return;
      const range = { from: Math.round(coordRange[0]), to: Math.round(coordRange[1]) };
      if (range.to - range.from < MIN_ZOOM_DELTA_MS) return;
      if (latest.current.sweepArmed) latest.current.onSweep?.(range);
      else latest.current.onZoom?.(range);
    });
```

- option effect: after the notMerge `setOption`, `chart.current?.dispatchAction(BRUSH_ARM_ACTION);` with a comment naming the silent-drop risk.

`SubjectPage.tsx`: wire the two props on every `ChartPanel` (via ChartSection), `onSweep`:

```ts
  const onSweep = (r: TimeRange) => {
    disarmSweep();
    openEventEditor({
      kind: "draft",
      draft: { sessionId: session.id, timestampMs: r.from, endTimestampMs: r.to,
               label: "", color: "#888888", dash: "dash" },
    });
  };
```

and the chip in the title row:

```tsx
  {sweepArmed && (
    <span data-testid="sweep-chip" className="rounded-full bg-brand-50 px-2 py-0.5 text-xs text-brand-700">
      Marking span — drag across a chart · Esc cancels
    </span>
  )}
```

(match token classes to what the codebase already uses — grep an existing pill/badge for the right utility names rather than inventing new ones.)

- [ ] **Step 4: Run to verify green**

Run: `cd web && npx vitest run && cd .. && make check-ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/charts/ web/src/pages/SubjectPage.tsx web/src/__tests__/
git commit -m "feat(monitor-web): brush-based drag zoom-select and sweep-to-mark"
```

---

### Task 13: Browser lane + suite real-time pin

**Files:**
- Create: `tests/e2e/monitor/dashboard/test_marking.py`
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (a `db_review_dash` fixture: build a tmp archive — the Task 4 `_make_archive` shape with a couple of metric points via `write_point` — then a `DashboardHarness`-style review server with `archive_path` set; follow `review_dash`'s existing pattern, and give it a distinct name per the 5a follow-up about shadowing)
- Modify: whichever backend test file covers stream fragments (`grep -rln "subscribe()" tests/unit/monitor/` → extend with the suite-event test) and the suite tests (`grep -rln "add_monitor_event" tests/unit/`)

**Interfaces:**
- Consumes: everything above. All browser specs assert through testids/`data-echarts-*` attributes defined in Tasks 8–12.

- [ ] **Step 1: Backend suite-event stream pin (TDD)**

Test first, in the suite test file found by grep:

```python
async def test_suite_monitor_event_reaches_stream_subscribers() -> None:
    """A suite-emitted event must arrive as a format:1 fragment in real time —
    the acceptance criterion behind 'events appear while otto test --monitor
    runs' (spec 2026-07-18 §Real-time suite events)."""
    # Arrange a suite with a monitor collector the way the file's other
    # monitor tests do; then:
    q = suite._monitor_collector.subscribe()
    suite.add_monitor_event("checkpoint", color="#112233", dash="dot")
    payload = q.get_nowait()
    frag = MonitorSessionFragment.model_validate(payload)
    assert frag.events and frag.events[0].label == "checkpoint"
    assert frag.session == suite._monitor_collector.session_id
```

(Adapt arrangement to the file's fixtures; `add_monitor_event` may be async or sync — match its real signature from `src/otto/suite/suite.py:572`.) Run it BEFORE any fix: if it passes, real-time suite events already work at the backend and Chris's observation predates Plan 5b's frontend (record that conclusion in the task's commit message); if it fails, the failure names the actual gap — fix it at the source and keep the test.

- [ ] **Step 2: Write the browser specs**

`test_marking.py`, following the harness idioms in `test_live_shell.py`/`test_review_shell.py` (fixtures `live_dash`, `review_dash`, new `db_review_dash`; every wait is an expectation poll, never a flat sleep):

1. `test_mark_now_appears_without_reload` (live): open a host subject, `mark-button` → type label → Enter; expect `events-count` == prior+1 and the subject chart's `data-echarts-marker-count` to increment — the full SSE-echo round trip, no reload.
2. `test_span_start_stop_flow` (live): Start via the menu, expect `menu-end-span` enabled, End, expect the events row to show a duration.
3. `test_drag_zoom_select` (live or review): `page.mouse` down-move-up horizontally across a chart canvas; expect `data-echarts-window-to` to shrink toward the sweep and (live) the pause state to derive (pause toggle shows Resume).
4. `test_ctrl_drag_pans`: hold `Control`, drag; expect the window bounds to shift, width unchanged. **This is the spec's no-op risk #2 probe.** If it fails because the armed brush swallows the drag, implement the documented mitigation in `ChartPanel.tsx`'s init effect and re-run:

```ts
    // An armed global brush cursor captures every plain drag — including,
    // on some engines, a Ctrl-drag meant for dataZoom's pan. Drop the brush
    // cursor while Ctrl is held so the pan gesture reaches dataZoom.
    const setBrushForModifier = (e: KeyboardEvent) => {
      if (e.key !== "Control") return;
      instance.dispatchAction(
        e.type === "keydown"
          ? { type: "takeGlobalCursor", key: "brush", brushOption: { brushType: false } }
          : BRUSH_ARM_ACTION,
      );
    };
    document.addEventListener("keydown", setBrushForModifier);
    document.addEventListener("keyup", setBrushForModifier);
    // (remove both in the cleanup)
```

5. `test_wheel_scrolls_page_not_chart`: wheel over a chart; expect `window.scrollY` to change and `data-echarts-window-to` unchanged.
6. `test_sweep_creates_span_via_editor`: palette → "Sweep span on chart" → chip visible → drag across a chart → editor opens with both time fields populated → label + Save → `data-echarts-marker-count` incremented and the events row lists the span.
7. `test_zoom_buttons`: click `zoom-in-*`; expect the echarts window width to halve.
8. `test_db_review_edit_persists_across_restart` (`db_review_dash`): add an event via `events-compose-add` → editor → Save; restart the harness server against the same archive (stop + start, the fixture returns a handle for this); reload the page; the event is still listed.
9. `test_json_review_has_no_marking_chrome` (`review_dash`): no `mark-button`, no `events-compose`, no row edit affordances.
10. `test_refused_jump_keeps_panel_open` (live): POST an event timestamped outside the session's padded bounds via the HTTP API (the harness exposes the server key/port), open the panel, click the row; expect `jump-notice` visible and the panel still open.

- [ ] **Step 3: Run chromium first, then the full matrix**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_marking.py -q` (fast chromium iteration; `make web` first so the dist is fresh — the conftest guard will remind you)
Then: `uv run nox -s dashboard`
Expected: all three engines green. Timing differences between engines are real bugs until proven otherwise (5b's soak lesson) — fix the product or the wait, never pad a sleep.

- [ ] **Step 4: Visual gate refresh**

Run the screenshot lane (`uv run pytest tests/e2e/monitor/dashboard -k zz_shot -q`) and eyeball the span-label surfaces in BOTH themes — the Task 11 label fix is only human-verifiable here; the dark-mode kitchen-sink drill-in previously showed illegible overlapping span labels and must now read.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(monitor): browser lane for marking + gestures; suite event stream pin"
```

---

### Task 14: Docs, guide media, final gates

**Files:**
- Modify: the monitor guide (`grep -rln "otto monitor" docs/ | grep -v superpowers` — the user guide page that documents the dashboard; likely `docs/guide/monitor.*`)
- Modify: `todo/TODO.md` (delete the scroll-to-zoom item — it ships here), `todo/untitled-ui-adoption-followups.md` (mark the dark-mode markArea item and the `EventsPanel.jump()` item resolved with one-line pointers to this plan)

**Interfaces:** none new — prose + gates.

- [ ] **Step 1: Write the guide sections**

Marking: the live flows (Mark now ⌘E, Start/Stop spans, sweep-on-chart), review-`.db` editing ("archives are editable; `.json` exports are read-only"), the slide-over's fields, and that suite events (`add_monitor_event`, per-test lifecycle marks) appear live while `otto test --monitor` runs. Gestures: replace any wheel-zoom prose with drag = zoom-select, **Ctrl**-drag = pan (all platforms — note the deliberate non-use of ⌘), `+`/`-` buttons, wheel scrolls the page. Match the guide's existing voice and heading depth; mention the access-key URL behavior only if the surrounding text already does.

- [ ] **Step 2: Clean docs build**

Run: `uv run nox -s docs` (or the repo's docs session — check `noxfile.py`) from a CLEAN build (delete the build dir first): incremental `-W` misses broken `:doc:` refs in docstrings, and Tasks 1–5 touched docstrings.
Expected: zero warnings.

- [ ] **Step 3: The full gate stack, in order**

```bash
uv run nox -s lint typecheck
make check-ts
make web          # includes the gen_web_types zero-diff + air-gap checks
uv run nox -s dashboard
make coverage
```

Expected: every one green on the branch tip. `make coverage` is the per-task gate of record for the repo (no `make test` exists); the import-budget and schema drift guards ride inside these.

- [ ] **Step 4: Commit**

```bash
git add docs/ todo/
git commit -m "docs(monitor): marking + gesture guide; retire shipped follow-ups"
```

---

## Self-review checklist (run after writing, before handoff)

- Spec §Backend → Tasks 1–5. §Frontend flow → Task 6. §UI surfaces → Tasks 7–10. §Gestures → Tasks 11–12 (+13.4 mitigation). §Real-time suite events → Task 13.1/13.2-case-1. §Error handling → Tasks 3/5 (409/422), 6 (EventApiError), 7 (addWarning pixel), 8–10 (inline errors). §Testing → per-task + Task 13. §Docs/fold-ins → Tasks 10 (jump), 11 (dark label), 14. §Non-goals honored (no CLI marking, no `.json` editing, no full theme pass).
- Both spec-named no-op risks have named guards: brush re-arm (Task 12 test b, mutation-proofed) and Ctrl-drag capture (Task 13 case 4 probe + documented mitigation).
- Type consistency spot-checks: `update_event` keyword signature (Tasks 2/3/5 agree); `EventUpdateInput.end_timestamp?: string | null` (Tasks 6/9 agree); `EventEditorTarget`/`EventDraft` (Tasks 7/8/9/12 agree); testids used in Task 13 are all defined in Tasks 8–12.
- Dedup directive (Chris, 2026-07-18) → Task 3 `event_ops` (one create/merge/validation seam), Task 4 shared `EVENT_INSERT_SQL`, Task 5 review-branch reuse (re-implementation = defect), Task 5b library validation through the same seam.
