# Monitor Plan 5c — event marking

**Status:** design approved 2026-07-18, awaiting spec review
**Predecessors:** Plan 5a (sessionized producer — sessions, format:1, the v2 archive),
Plan 5b (live streaming — one payload, SSE fragments), Untitled UI adoption + command
layer (the component base, palette and keybinding registry this plan builds on)
**Successor:** none named; collection scoping is the nearest queued idea

## Goal

Users can mark events from the dashboard: instant marks ("Mark now"), spans (start /
stop while you wait, or sweep a range on a chart), and full edit/delete — in live mode
**and** while reviewing a `.db` archive. The chart gesture model is reworked in the same
pass (drag to zoom, Ctrl/Cmd-drag to pan, wheel freed for page scroll) because span
marking and zoom selection compete for the same drag and must be designed together.

Everything downstream of creation already exists: the v2 `events` table, format:1
`EventRecord`, SSE fragments that upsert events by id and delete by
`deleted_event_ids`, markLine/markArea rendering with lane-stacked labels, and the
read-only EventsPanel. 5c supplies the missing writes and the UI in front of them.

## What exists today (and stays)

| Piece | State |
| --- | --- |
| `MonitorEvent` dataclass, `VALID_DASH_STYLES`, `AUTO_EVENT_COLORS` | unchanged |
| v2 `events` table (`ts`, `end_ts`, `label`, `source`, `color`, `dash`) | unchanged |
| SSE fragment vocabulary (`events: [...]`, `deleted_event_ids: [...]`) | unchanged |
| `applyFragment` upsert-by-id / delete semantics in the web store | unchanged |
| markLine (point) / markArea (span) rendering, greedy label lanes | unchanged |
| EventsPanel slide-over, jump-to-event | extended, not replaced |
| Suite events (`_otto_monitor_events` fixture, `add_monitor_event`) | unchanged |

A span is still expressed as `end_timestamp != null`; an *open* span (started, not yet
stopped) is indistinguishable from a point in the data model, and that stays true — the
client tracks "the span I started" ephemerally (see UI state below).

## Backend: session-aware event API

**BREAKING: the legacy event routes are deleted** (`POST /api/event`,
`POST /api/event/{id}/end`, `PATCH /api/event/{id}`, `DELETE /api/event/{id}`). They
predate sessions, mutate only the live collector, emit bare dataclass dicts, and have
no callers outside backend tests. The 5a spec explicitly deferred "session-aware event
endpoints" to 5c; this is that.

Replacement routes, all session-addressed:

| Route | Semantics |
| --- | --- |
| `POST /api/session/{sid}/event` | create; omitted `timestamp` ⇒ server-now (Mark now) → 201 `EventRecord` |
| `POST /api/session/{sid}/event/{id}/end` | stamp `end_timestamp` = server-now (span stop) → 200 `EventRecord` |
| `PATCH /api/session/{sid}/event/{id}` | partial edit: `label`, `color`, `dash`, **`timestamp`, `end_timestamp`** → 200 `EventRecord` |
| `DELETE /api/session/{sid}/event/{id}` | → 204 |

Request bodies are new pydantic models (`EventCreateBody`, `EventUpdateBody`) in
`otto.models.monitor`; responses are the existing format:1 `EventRecord`. Both ride
`scripts/gen_web_types.sh`, so the TS contract gains the request shapes under the
zero-diff gate. The dataclass-dict era ends at the HTTP boundary.

Boundary validation (422): dash ∈ `VALID_DASH_STYLES`; color is `#rrggbb`; label
non-empty; whenever both timestamps are present (create, or the merged result of a
PATCH), `end_timestamp > timestamp`. Timestamps parse through pydantic `datetime` —
nothing unvalidated reaches the store or the wire (the NaN lesson).

`MetricDB.update_event` today writes only `label, color, dash, end_ts`; it gains `ts`
so PATCH can move a start time.

### Routing by mode

- **Live server:** `{sid}` must equal the live frame's id (404 otherwise). The handler
  delegates to the existing collector path — DB write, store update, SSE publish —
  which is untouched.
- **Review server, `.db` source:** `{sid}` must exist in the served document (404
  otherwise). The mutation does a **per-mutation read-write open** of the archive:
  take the same `.lock` flock live writers hold (a concurrently-live archive refuses
  loud → 409), apply the INSERT/UPDATE/DELETE stamped with `{sid}`, close. Then patch
  the retained in-memory `MonitorExport` document and re-serialize the cached body.
  Archives stay cold files; review mode holds no standing write connection.
- **Review server, `.json` source:** 403. There is no persistence target; the UI never
  offers marking (below).

`GET /api/mode` gains `editable: bool` (live and `.db` review true, `.json` review
false) so the frontend can gate every marking affordance on one flag instead of
inferring source kinds.

The access-key middleware and TLS wrap the new routes automatically — same app, no new
auth surface.

## Frontend: one mutation flow, no optimistic state

New module `web/src/data/eventApi.ts`: `createEvent`, `endEvent`, `updateEvent`,
`deleteEvent` — thin fetches against the routes above.

**Every 2xx response is applied locally as a synthetic fragment** through the existing
`applyFragment` path: a returned record becomes `{session: sid, events: [record]}`; a
delete becomes `{session: sid, deleted_event_ids: [id]}`. In live mode the SSE echo
delivers the same record again and the upsert-by-id makes the duplicate a no-op — so
one code path serves both modes, ordering between response and echo cannot matter, and
the store gains **zero** new event state. A failed request applies nothing and surfaces
inline at the control that issued it. There is no optimistic state and no rollback
logic anywhere.

`uiStore` gains the ephemeral marking state (all client-local, none persisted):

- `sweepArmed` — the span-sweep mode flag (see Gestures)
- `eventEditor` — the slide-over target: an event id, or a prefilled draft (not yet
  created)
- `openSpanId` — the span the user started via Start/"Start span…", backing the Stop
  button. A reload loses this pointer, **not** the event: while live, any endless
  event offers "End now" in its EventsPanel row and in the editor, so a
  reloaded-mid-span user stamps the end from there. `openSpanId` is convenience, not
  truth.

## UI surfaces

All marking chrome is gated on `mode.editable`; creation flows that stamp server-now
(Mark now, Start/Stop) are additionally live-only — in review you author times
explicitly.

**AppBar split button (live only).** Primary action **"Mark now…"**: a small popover
with an autofocused label field; Enter commits (`createEvent` with no timestamp).
Menu: *Start span…* (label popover → create, remember `openSpanId`), *End span*
(enabled while `openSpanId` is set → `endEvent`), *Sweep span on chart* (arms the
sweep mode), *Add event…* (opens a blank editor draft). There is no vendored split
button; it is composed from `Button` + `Dropdown`, matching the AppBar's existing
`ButtonUtility` row.

**EventsPanel.** Gains a compose row at the top — live: label field + **Mark** /
**Start** / **Stop** buttons (the old frontend's field-and-button flow, which matches
how spans are actually used: mark a start, wait, mark the end); editable review: an
**"Add event…"** button opening the editor draft. Rows gain an edit affordance
(opens the editor) alongside the existing jump-on-click. A **refused jump no longer
closes the panel** — the row shows "outside session bounds" instead (folds in the
recorded follow-up). Live rows for endless events offer **End now**.

**Slide-over editor.** Composed from the same slideout primitives as EventsPanel
(Header/Content/Footer; Footer holds Save / Cancel / Delete). Fields: label
(`Input`); start and end via the `InputDateBase` composition RangePicker established,
but at **second granularity** (events are second-scale; RangePicker's minute
granularity is a range-picking choice, not a house rule); an empty end field means a
point event; color as a swatch row (`AUTO_EVENT_COLORS` + the manual default — no
vendored color picker, swatches suffice); dash as a `Select` over
`VALID_DASH_STYLES`. Drafts (from sweep, *Add event…*) are **committed only on
Save** — an accidental sweep creates nothing. Delete confirms in place (danger
button → confirm state, no extra dialog).

**Command palette.** New commands in Actions, following the gating rule above:
*Add event…* and *Sweep span on chart* enabled whenever `mode.editable` (they author
times explicitly, so they work in `.db` review — where the palette and EventsPanel are
the entry points, since the AppBar split button is live-only); *Mark now…*,
*Start span…*, *End span* additionally live-only. Bindings are assigned in the plan
against `shortcuts.ts`'s
reserved-key rules (bare keys tolerate `shiftKey`, per the intl lesson); the palette
rows, AppBar menu addons and `Kbd` captions all derive from the one `Binding` source.

## Chart gestures (folded-in TODO rework)

The wheel-zoom complaint and span marking are solved as one gesture model:

| Gesture | Action |
| --- | --- |
| plain drag | select a time range → zoom (existing `setRange(clampRange(...))` path) |
| Ctrl/Cmd-drag | pan (inside dataZoom's native `moveOnMouseMove: "ctrl"`) |
| wheel | **released to the page** (`zoomOnMouseWheel: false`) — scrolling works again |
| `+` / `-` buttons, left edge of each chart | zoom about the current range's center, same `onZoom` path |
| drag while sweep mode armed | sweep a span → prefilled draft editor opens, mode disarms |
| Esc (sweep armed) | disarm, nothing created |

Mechanics: register ECharts' `BrushComponent`; arm a chrome-less `lineX` brush via
`dispatchAction({type: "takeGlobalCursor", ...})` — the toolbox UI is never mounted.
`brushEnd` yields the coordinate range; zoom-select clears the brush area after
applying the range; sweep mode instead opens the draft and disarms. While armed, a
small chip indicates the mode ("drag across a chart · Esc cancels") and the cursor
changes. Sweep-created times come from chart coordinates, which are already
sample-timescale — browser clock skew cannot enter.

Two integration facts the plan must treat as first-class (both are the kind of silent
no-op 5b taught us to hunt):

1. **Brush arming is instance-level, `setOption(notMerge: true)` is not.** ChartPanel
   rebuilds options with `notMerge` on data-shape changes; the brush config must ride
   `buildStackOption` and the global cursor must be re-asserted after every such
   rebuild, pinned by a test that mutates the rebuild path.
2. **An armed brush may swallow Ctrl-drag before dataZoom sees it.** Chosen
   mitigation if so: a keydown/keyup listener drops the brush cursor while the
   modifier is held. Verified against a real browser (the browser lane, not jsdom),
   all three engines.

## Real-time suite events

`otto test --monitor` already runs a `MonitorServer`, and `collector.add_event`
already publishes an SSE fragment for every event — so suite marks (per-test
start/pass/fail, `add_monitor_event`) *should* stream live today. Chris reports they
don't appear in real time; this plan verifies the path end-to-end early, fixes
whatever gap is found, and pins it regardless:

- backend: a suite-emitted event reaches `/api/stream` as a format:1 fragment
- browser: an event created mid-session appears (panel row + markLine) with **no
  reload**

"Events appear in real time while a monitored suite runs" is an acceptance criterion
of this plan, not a hope.

## Error handling

- Mutation failure (4xx/5xx/network): nothing applied locally; inline error at the
  issuing control (editor footer, compose row, popover). No global toast system is
  introduced.
- Live-writer collision on a review-edited archive: flock → 409 → inline "archive is
  being written by a live session".
- Two live dashboards: both converge via the SSE echo (upsert-by-id). `.db` review
  has no push channel; concurrent review editors are out of scope and the
  single-viewer assumption is documented.
- Server-now stamping (Mark now, Start, End) keeps event times on the same clock as
  samples — the accepted browser/server skew risk is not widened.

## Testing

- **Backend units:** all four routes × live/review-`.db`/review-`.json` (403) modes;
  404s (wrong sid, missing id); 409 (locked archive); 422 validation table; PATCH
  moving `ts`; archive round-trip — mutate via review server, restart, re-read shows
  the edit.
- **Vitest:** `eventApi` (fetch mocked); synthetic-fragment application incl. the
  duplicate-echo no-op; editor draft/save/validation; compose row modes; brush
  coordRange→TimeRange math; `+`/`-` zoom math; `openSpanId` lifecycle.
- **Browser lane** (`nox -s dashboard`, all three engines — never the bare pytest
  lane): mark-now appears live; drag zoom-select narrows; Ctrl-drag pans; wheel
  scrolls the page; sweep → draft → save → markArea; edit and delete round-trip;
  review-`.db` edit survives a server restart; `.json` review shows no marking
  chrome; refused jump keeps the panel open; dark-mode span labels legible (visual
  gate refresh).
- **Guard discipline (the 5b headline):** every load-bearing guard gets a mutation
  check — name the production change that should turn it red, make the change, watch
  it fail.
- **Gates per task and at the end:** `make web-check` after any `web/` touch,
  `nox -s lint` in every task gate (the otto-init lesson), schema zero-diff after
  model changes, `make coverage`, docs as a clean `-W` rebuild.

## Docs

Monitor guide gains a marking section (live + review, the start/wait/stop flow, the
sweep); the gesture paragraph replaces the wheel-zoom prose; the suite-monitoring page
notes events stream live. Docs media regenerates at build (kitchen-sink surfaces pick
up any marking chrome). Clean rebuild — incremental `-W` misses docstring `:doc:`
refs.

## Folded-in follow-ups

- Dark-mode span-label illegibility in `options.ts` — the deferred item whose note
  explicitly waits for "a dedicated pass over options.ts"; scoped to event label
  styling, **not** a full chartTheme CSS-var conversion.
- `EventsPanel.jump()` refused-jump signal (panel stays open, states why).

## Non-goals

- `.json` review editing (no persistence target; re-export flow deferred)
- CLI marking (`otto monitor mark …`) — the suite API covers programmatic marks
- Collection scoping (metric filters, `[monitor]` defaults) — separate queued idea
- PID retirement in the series selector (deferred item, unchanged)
- Full chart theme CSS-var pass beyond the event-label fix

## Acceptance criteria

1. Live: Mark now / Start / Stop / sweep create events that appear on charts and in
   EventsPanel without reload, in every open dashboard.
2. Review of a `.db` archive: create/edit/delete persist to the archive and survive a
   server restart; a `.json` review shows no marking UI and the API refuses with 403.
3. A monitored test suite's events appear in real time.
4. Drag zooms, Ctrl/Cmd-drag pans, the wheel scrolls the page, `+`/`-` buttons work.
5. Legacy `/api/event*` routes are gone; the TS contract regenerates clean; all gates
   green (`make coverage`, `nox -s dashboard` ×3 engines, `make web-check`,
   `nox -s lint`, docs `-W`).
