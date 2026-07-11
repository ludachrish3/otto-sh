# Monitor historical export format + dummy-data UI phase — design

**Date:** 2026-07-10
**Status:** approved design, pending implementation plan
**Relationship to prior specs:** `2026-07-05-monitor-untitled-ui-redesign-design.md`
remains the UX source of truth (views, chrome, interactions). This spec defines the
**data contract** that phase runs on, the **fixture strategy**, and **amends that
spec's phasing** (§2): topology moves into the first build phase, and the build is
review-mode-first against committed fixtures — no live backend required.

## 1. Goals

1. Build the redesigned monitor UI against **dummy historical data** so the full UX
   (including an arbitrarily complex lab) is nailed down before backend hookup.
2. **Contract-first:** the dummy data conforms to the *real* new export format —
   pydantic-modeled, schema-generated TS types. Fixtures are permanent test assets;
   the backend later just emits the same format. No throwaway shapes, no rework.
3. **Lab config rides in the export, per session.** Lab configuration drifts over
   long time ranges; a historical run must render under the lab config *as it was
   at run time*.

## 2. Decisions (locked)

- **Contract-first** over throwaway JSON or TS-only types.
- **Per-session lab snapshot.** JSON denormalizes (full config per session);
  the future SQLite schema normalizes configs into a deduped table (§7).
- **Topology is in scope** for this phase (folds the UX spec's Phase 2 view in —
  the link foundation, tunnels, and impairment libraries have all landed on main).
- **Elements are derived from hosts by default**, with an **optional explicit
  `elements` section** for element-level truth (incl. zero-host elements — an
  empty chassis). Singleton is always derived from membership count, never stored.
- **Dynamic (tunnel) links are excluded from real exports.** The snapshot is a
  *static-config* document; tunnels are runtime state with a lifetime inside a
  session, so any one-shot snapshot of them misleads. If runtime link history ever
  matters, the truthful design is timestamped link *events* on the timeline
  (future, out of scope). The `provenance` enum keeps all three values (mirrors
  `otto.link`), and one fixture link is `dynamic` purely to exercise the styling.
- **`impair` stays in the snapshot** — it is the *declared* in-path middlebox from
  lab.json (static config). Applied netem *parameters* are runtime state and are
  excluded (same reasoning as tunnels).
- **Clean break, no compatibility.** There are no otto users; the legacy
  unversioned JSON/db formats and their tolerant loaders are deleted. Old files
  fail loud with a brief "re-export from a current run" error. No converter.
- **Historical health is derived, not stored** (§6): presence/absence of samples
  within the selected range. "Last known status" means *last known within the
  currently selected range* — narrowing the range re-evaluates it.
- **Per-session presentation meta** (chart/tab specs + intervals) is embedded in
  the export (§4): client-side Import has no parser catalog to rebuild specs from,
  health derivation needs cadences, and chart definitions drift over months
  exactly like lab configs.
- **Air-gap delivery is a hard constraint** (carried forward, restated by Chris
  2026-07-10): the delivered app must be fully usable air-gapped, with every
  asset inside the otto-sh wheel (§9).

## 3. Export format (JSON)

Top-level `format: 1` — the first *versioned* format; the legacy format's marker
is the field's absence.

```jsonc
{
  "format": 1,
  "sessions": [
    {
      "id": "2026-07-09T22-10-03-nightly",     // unique within the file
      "label": "nightly stress",               // optional human name
      "note": null,                            // optional free text
      "start": "2026-07-09T22:10:03Z",         // ISO-8601, as the record models emit
      "end": "2026-07-10T04:10:00Z",           // null = still open
      "lab": {
        "elements": [ /* ElementRecord, optional section */ ],
        "hosts":    [ /* HostSnapshot */ ],
        "links":    [ /* LinkSnapshot */ ]
      },
      "meta": {                                // presentation meta, frozen at run time
        "interval": 5.0,                       // global collection interval
        "charts": [ /* ChartSpec: label, y_title, unit, command, chart, interval */ ],
        "tabs":   [ /* TabSpec: id, label, metrics, kind, columns */ ]
      },
      "metrics":    [ /* MetricRecord */ ],
      "events":     [ /* EventRecord — unchanged from today */ ],
      "log_events": [ /* LogEventRecord — unchanged from today */ ],
      "chart_map":  { /* series label → chart group — unchanged */ }
    }
  ]
}
```

`ChartSpec`/`TabSpec`/`EventRecord`/`LogEventRecord` are today's pydantic models
reused verbatim (`src/otto/models/monitor.py`).

### 3.1 New records

**`HostSnapshot`** — the view-relevant subset of `HostSpec`. **Never credentials.**

| field | notes |
| --- | --- |
| `id` | derived host id (e.g. `carrot_seed`) — the key metric/log rows reference |
| `element` | element name (grouping key) |
| `name` | display name |
| `board`, `slot`, `hop` | optional; drive badges + topology wiring |
| `os_type`, `os_name`, `os_version` | optional |
| `ip` | management ip |
| `interfaces` | netdev → ip |
| `labs` | lab membership |
| `is_virtual` | bool |

**`LinkSnapshot`** — mirrors the foundation `Link` (`src/otto/link/model.py`):
`id`, `endpoints` (exactly 2 × `{host, interface, ip, port}`), `protocol`,
`provenance` (`implicit` / `declared` / `dynamic`), `name`, `impair`.

**`ElementRecord`** (optional section): `id` (the element name — the same string
member hosts carry in `HostSnapshot.element`), `type` (`physical` | `logical`),
`description?`. Elements not listed are derived from hosts (any member has `slot`
→ physical presentation; one member → singleton behavior). An explicit entry with
zero member hosts renders as an empty element (e.g. an empty chassis).

**`MetricRecord`** — today's fields (`timestamp`, `host`, `label`, `value`,
`meta?`) plus optional **`source`**: the host id of the *reporting* host when a
series comes from a management host; absent = self. That one field is the whole
subject × source axis on the wire. The `host` field may also hold an **element
id** for element-targeted series (e.g. chassis ambient temperature reported by a
mgmt host); the UI resolves `host` against host ids first, then element ids.
*Reviewed alternative:* renaming the field to `subject` — kept as `host` to match
the store's `host/label` series keys end-to-end; revisit at review if truthful
naming is preferred.

### 3.2 Semantics

- A JSON export of one run has exactly one session; multi-session files are
  produced from databases (or fixtures).
- Each session is self-contained: its own lab, meta, data. Nothing is shared
  across sessions in JSON (dedup is a database concern, §7).
- Real exporters write only `implicit` + `declared` links (§2).

## 4. Models, schema, TS generation

- Pydantic models live with the existing monitor records in
  `src/otto/models/monitor.py`; `make schema` emits the JSON schema; TS types
  regenerate via `scripts/gen_web_types.sh` (the existing `types.gen.ts` path).
- This phase ships **models + schema + fixtures only** — no exporter, no server
  emission, no SQLite work. Zero overlap with the monitor runtime.

## 5. Fixtures

Three committed JSON files under `web/fixtures/`, built by a **deterministic,
seeded Python generator** (`scripts/gen_monitor_fixtures.py`, `make
monitor-fixtures`) that constructs everything **through the pydantic models** —
conformance-checked by construction, regenerable in one step when the schema
evolves. The generator imports only `otto.link` and `otto.models` (deliberately
not `otto.configmodule`, which the in-flight extraction branch renames).

**`kitchen-sink.json`** — 1 session, ~2 h at 5 s cadence, ~10 hosts. Exercises
everything at once:

| piece | exercises |
| --- | --- |
| `edge-gw` singleton gateway; chassis hosts hop through it | multi-hop topology, reachability cascade, singleton drill-through |
| `chassis-a` physical, boards in slots 1, 2, 5 | slot badges, sparse slots, rack glyph |
| `workers` logical ×3 (no slots) | logical grouping, segmented health rollup |
| `spare-chassis` explicit element, zero hosts | empty-chassis node |
| `mgmt-01` reports PSU temp / fan rpm for boards + ambient temp targeted at `chassis-a` | source badges, Source filter, element-targeted series |
| links: declared tcp + udp, implicit hop edges, one `dynamic` (fixture-only), one with `impair` middlebox | provenance styles, link inspector, impair placement |
| one worker silent for 20 min mid-session | outage gap → `down · 20m` tile, dimmed last-known drill-in |
| events: points + spans, one overlapping pair, one span over a CPU spike | overlay, slide-over, chart↔event correlation |
| kernel-msg log table on two hosts | table tabs |
| holes: a host with no `hop`, a board with no `slot`, missing `os_version` | missing-metadata edge cases |

Series shapes are realistic (diurnal-ish CPU, sawtooth memory leak, correlated
spike under an event span) — chart UX can't be judged against noise.

**`minimal.json`** — 1 session, 1 singleton host, 2 series, no links/events.
Degenerate rendering and empty states.

**`drift.json`** — 3 sessions across "months" over one evolving lab: baseline →
host added + link rewired → board slot swapped + host removed + impairment added.
Proves the session picker and per-session-accurate lab rendering — the core
config-drift use case.

## 6. Derived health (historical)

A pure function of `(samples, selected range, cadences)` — vitest territory; the
same function later drives live mode's "unreachable" dimming.

- **Cadence per host** = fastest `interval` among its series (from session meta;
  global `interval` as fallback).
- **Status at range end**: gap = `min(rangeEnd, sessionEnd)` − last in-range
  sample. Within K × cadence → healthy; beyond → down with the gap as outage
  duration (K ≈ 3, tuned against the kitchen-sink fixture).
- Zero in-range samples → "no data" for the window.
- **Log-only hosts get no health claim** (unknown/neutral): log cadence is sparse
  and event-driven; absence proves nothing.

## 7. Backend catch-up (recorded here, deferred)

Not in this phase; recorded so the contract is designed end-to-end:

- **SQLite:** `sessions(id, label, note, start_ts, end_ts, config_id)`;
  `configs(id, content_hash UNIQUE, config_json)` holding the denormalized
  lab-snapshot + presentation-meta blob, deduped by hash (lab changes are
  infrequent — N sessions over an unchanged lab share one row); `metrics` /
  `events` / `log_events` gain `session_id` (+ `metrics.source`).
- **Exporter/emitter:** live collector writes sessions + snapshots (implicit +
  declared links only); `/api/export/json` emits `format: 1`.
- **Server surfaces:** `/api/meta` extension, SSE unchanged-or-extended, live
  health tracker, launch-mode flag (`otto monitor live` vs `<source>`) — per the
  UX spec §14.

## 8. Build order (this phase)

1. **Contract:** models → `make schema` → TS types → generator → committed
   fixtures.
2. **Scaffold:** Tailwind v4 + Untitled UI + ECharts wrapper + router + theme;
   client-side **Import** as the front door; review chrome (HISTORICAL tag ·
   session picker · range picker · Reset).
3. **Views:** fleet grid (element derivation + explicit merge) → per-subject view
   (synced ECharts stack, series tree, source badges) → events slide-over +
   marking → topology (inter-element map → intra drill → link inspector).
4. **Tests ride along:** vitest for pure logic (element derivation,
   health-in-range, metric tree, echarts builders); Playwright behavior E2E
   loading fixtures via Import — fully hostless. The DOM-parity harness retires
   on this branch (UX spec §16). Air-gap + import-budget guards updated for the
   new deps.
5. **Dev loop:** dev-only `?fixture=kitchen-sink` query param auto-imports a
   fixture so `npm run dev` needs no manual file-picking.

Work happens on a worktree off main, replace-in-place in `web/`; the old
dashboard keeps working on main until merge. **Live hookup (§7) is a separate
later phase**, sequenced after the library-extraction branch lands.

## 9. Air-gap & packaging (hard constraint)

The delivered app must be fully usable in an air-gapped environment; **all assets
ship inside the otto-sh wheel** (the existing hatchling force-include of
`src/otto/monitor/static/dist/`, built at CI/release time). Concretely for the
new stack:

- **No CDN or runtime network fetch of any kind** — scripts, styles, fonts,
  icons, source maps. Everything resolves from the Vite bundle.
- **Fonts self-hosted:** Untitled UI defaults to Inter, commonly loaded from
  Google Fonts — that path is forbidden; the font files are vendored into the
  bundle (or the system font stack is used). This is the most likely regression
  point of the new stack and gets an explicit check.
- Untitled UI is the **open-code tier** (component source lives in the repo) and
  Tailwind v4 / ECharts / React Aria / motion are build-time npm deps bundled by
  Vite — nothing external at runtime by construction.
- **Fixtures (`web/fixtures/`) are dev/test assets and stay out of the wheel** —
  the shipped app loads user data via Import; bundling multi-MB dummy data would
  be pure bloat.
- **Enforcement (existing gates, extended to the new stack):** the dist grep gate
  (no external URLs in built assets) and the Playwright offline-render test must
  pass against the redesigned bundle; both get updated for the new asset shapes
  (font files, ECharts chunks).

## 10. Parallel-work safety (library extraction branch)

Checked against `2026-07-10-library-extraction-and-renames-design.md`: the
monitor package is untouched there; its only `web/` exposure is the coverage
`context`→`run` label sweep in `web/src/covreport/` (a different subtree). Our
Python footprint is additive (`models/monitor.py`, generator script, regenerated
schemas). Generated-schema conflicts resolve by rerunning `make schema`. The
generator's import discipline (§5) removes the `configmodule`→`config` rename
from our surface. **No sequencing dependency; whichever merges second pays ~one
schema regeneration.**

## 11. Non-goals

- Live-mode hookup, exporter, SQLite schema, server health tracking (§7 — later
  phase).
- NetEm editing UI; link-event timeline (runtime link history); intra-chassis
  network sub-view.
- Coverage-report migration (separate effort).
- Compatibility with legacy exports/dbs (deleted, fail loud).

## 12. Open items (resolve at plan/impl time)

- ECharts wrapper: direct instance management vs `echarts-for-react` (carried
  from UX spec §19; direct proposed).
- Exact health threshold K + minimum-gap floor for very fast cadences.
- `MetricRecord.host` vs `subject` naming (§3.1 — `host` unless review says
  otherwise).
- Fixture data volume vs repo size (target ≤ ~2–3 MB per file; trim cadence or
  duration if needed).
