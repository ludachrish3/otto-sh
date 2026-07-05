# Monitor redesign — Untitled UI + ECharts (design spec)

**Date:** 2026-07-05
**Status:** DESIGN DECIDED (brainstorming complete). NOT yet reviewed by Chris; NOT yet turned into an implementation plan. Companion tunnel died mid-session; remainder decided in terminal.
**Next step on resume:** Chris reviews this spec → then invoke `writing-plans` to produce the implementation plan.

---

## Context & goals

Move the **monitor** web UI entirely to the **Untitled UI** React component framework and swap the chart engine. Driven by a desire for a beautiful, well-supported, easier-to-maintain UI vs. today's hand-rolled components + 624-line `dashboard.css`.

The monitor is already a React 19 SPA (`web/src/`): zustand store, SSE live stream, ~7 components, Plotly `scattergl` (WebGL) charts with an `extendTraces` live-append fast path, retirement/legend-capping for unbounded PID series. The **coverage report** is a *separate, non-React* surface (static Jinja HTML) and is **explicitly out of scope** here (its own future spec).

Redesign goals Chris selected: **fleet overview at a glance**, **better metric navigation**, **richer chart interactions**, **cleaner event-annotation UX**, plus an ambitious **lab-topology visualization**.

This reopens a prior decision: `docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md` deliberately **kept Plotly** ("high regression risk for little gain") and listed chart-lib switching as a non-goal. That calculus flips when redesigning the whole UI anyway — the regression cost is paid regardless.

---

## Scope & phasing

- **In scope:** the monitor only.
- **Out of scope (separate future spec):** coverage-report migration. (When tackled, it forks: rebuild covreport as React/prerendered, OR restyle its Jinja templates with Tailwind + Untitled UI tokens — Untitled UI is a *React* library and can't drop into Jinja.)
- **One spec, two phases:**
  - **Phase 1 — foundation & core UX:** Tailwind v4 + Untitled UI + ECharts, app shell, fleet grid, per-host view, events, global chrome. Runs on existing data (+ minor meta additions).
  - **Phase 2 — lab topology (stretch):** the spatial rack/hop visualization + the backend contract that feeds it. Designed fully here, implemented last.

---

## Locked decisions

### Chart engine — Apache ECharts
- Replace Plotly with **ECharts** (canvas; optional WebGL via echarts-gl if ever needed). Chosen over Recharts (SVG, weakest for live high-frequency streaming — Chris's instinct confirmed), uPlot (fastest but most manual chrome), and keep-Plotly (valid low-risk default, declined in favor of cohesion + features).
- **Integration:** thin typed wrapper module (`web/src/echarts.ts`) mirroring today's `plotly.ts` pure-builder architecture — `buildLayout`/`buildMetricTraces` become ECharts `setOption` config builders. Preserve a **live-append fast path** (`appendData` / incremental `setOption`). *Proposed:* direct instance management via refs (like current `plotly.ts`), NOT `echarts-for-react`, to keep the fast-append path + fine control. (Confirm at plan time.)
- **Native wins used:** `axisPointer` linked cursor → the synced crosshair across stacked charts; `dataZoom` → brush-any-chart-to-zoom-all.
- Tree-shake `echarts/core` imports (line, grid, tooltip, dataZoom, axisPointer…) to keep bundle lean. Air-gapped/bundled, no CDN (existing constraint).

### App shell & navigation — "Overview-home, drill-in" (companion option C)
- Fleet overview **is** the landing page. Click a host → push into a focused host view with a `‹ Fleet / <host>` back breadcrumb. Events open as a **slide-over**.
- **Grid ⇄ Topology** segmented toggle on the overview.
- **Two-tier chrome:** a persistent **global app bar** + a **per-view context row** (the Grid/Topology toggle and the breadcrumb/host pills are view-specific, NOT in the global bar).
- **URL routing** (deep links): `/`, `/host/:name`, `/topology`. Shareable links, browser back/forward, refresh-stable. Lightweight router.

### Fleet grid — "Status tiles" (companion option B)
- Compact tiles **grouped by lab**: status light · host name · `board · slot` · big **CPU** headline number.
- **Down** tiles show outage duration next to the indicator (`down · 2m`); healthy tiles stay clean (no other time metric).
- Click a tile → host view.
- *Open detail:* headline metric should gracefully fall back when CPU isn't collected for a given board (embedded boards may not report CPU%).

### Per-host view — synced stack + left filter panel (companion option B + filter merge)
- **Left panel** (combined filter + series control), top→bottom: **search** (free-text substring) → **quick-filter chips** (toggle whole metric groups) → **series tree with per-series checkboxes** (narrows to active filter; noisy groups like `proc/ (12)` collapse to a count). This **replaces legend-capping/retirement-as-a-workaround**.
- **Right:** charts **stacked on a shared time axis**; **one synchronized cursor** shown via the **hover scrubber** (no redundant header clock); **brush any chart to zoom the whole stack**.
- Header: breadcrumb + host name + status dot + `board·slot`/`hop` pills.

### Lab topology — "Rack + hop lines," hybrid (companion option B) — PHASE 2 STRETCH
- Each lab drawn as its **chassis**: **numbered slots**, occupied slots show a **board-type-labeled tile** with a **health light**; **gap-collapse** for sparse racks (e.g. a 24-slot rack with only slots 1–2 & 19–20 filled → the empty run becomes one expandable "⋯ N empty ⋯" divider).
- **Hybrid:** slotted boards live in the chassis; **free non-slotted elements** (VMs, gateways, servers — `slot` is nullable) are drawn as free nodes.
- **Local root:** the `local` "you are here" host is the visible root (otto's real built-in `local`, normally excluded from the fleet). Everything connects back to local **transitively through the real hop chain** (`local → gw1 → chassis → board`) — no direct spider-web. This also honestly shows the **reachability cascade** (gateway down ⇒ everything behind it unreachable).
- **Encoding convention:** node **type** = icon + label (local / gateway / slotted board / host-VM); **colored dot = health ONLY**; hop lines solid (+ transport label). Do NOT overload border/line style for identity (the mock's dashed `buildsrv` border was ad-hoc, not a convention).
- Click any element → drills into its chart view. Down board = red light + outage duration.
- **Intra-chassis network view: DEFERRED** (note-and-defer). If added later: a **Chassis ⇄ Network sub-toggle** (two clean views), NOT an always-on overlay (avoids cramping).
- **Edge cases to handle at Phase-2 design/impl:** down-hop cascade (covered by transitive wiring), **multiple chassis per lab** (side-by-side racks), **multi-hop chains** (A→gw1→gw2→board), **missing metadata** (board but no slot; no hop → directly attached).

### Events
- **Global** markers (session/lab-wide), overlaid on **every** host's charts (matches today).
- **Create:** click-a-chart to place an instant marker, **drag** for a span, plus a **"mark now" button/hotkey** for live capture.
- **Review/edit:** the events **slide-over** lists all events (jump/edit).
- Instantaneous = vertical line; span = filled rect + edge lines (port `buildShapes`/`buildAnnotations` semantics to ECharts markLine/markArea).

### Global chrome — "Split bar" (companion option B)
- Brand left, then **Import · Export** (session data I/O grouped by the brand).
- Right cluster: **pause/play · theme toggle · status text · status light**.
- **Status light is hard-right and immovable**; status text sits to its **left** so the light never shifts when status changes (Chris's explicit rule).
- **Theme toggle:** **Light / Dark / System** (three-state; follows OS `prefers-color-scheme`; **dark stays the effective default** when no OS preference).
- Pause/play only meaningful in live mode (disabled/hidden in historical).

---

## Backend contract additions
- **Extend `/api/meta`** (or add `/api/topology`) with per-host: `lab`, `board`, `slot`, `hop`, and **health** (status + last-seen + outage duration). Source fields already exist on the host model (`src/otto/models/host.py`: `board: str|None`, `slot: int|None`, `hop: str|None`); the monitor just doesn't expose them yet. Feeds the fleet tiles AND the topology view.
- **Health/unresponsive tracking is NEW server-side work:** per-host last-seen + a threshold to declare "down," plus outage duration. (Algorithm/threshold TBD at impl.)
- Regenerate TS types from the schema (`scripts/gen_web_types.sh` / `MonitorMeta` in `src/otto/models/monitor.py`).
- **Import (new capability): CLIENT-SIDE.** "Import" reads a previously-exported JSON entirely in the browser and hydrates the store into historical mode (mirrors `/api/export/json`). No server endpoint, no persisted state, no upload/security surface. (Today only Export exists.)

---

## Data layer — keep / evolve / replace
- **KEEP (view-agnostic):** `store.ts` (zustand), `sse.ts`, `events.ts`, `logevents.ts`.
- **EVOLVE:** `grouping.ts` / `retirement.ts` → the **metric-tree model** behind the left panel; legend-capping-as-workaround goes away (tree checkboxes own visibility). Stale-series logic may still be useful.
- **REPLACE:** all `components/*.tsx` (rebuilt with Untitled UI), `dashboard.css` (→ **Tailwind v4**), `plotly.ts` (→ `echarts.ts`).

### Tech stack
- **Untitled UI React, FREE / open-source tier** (open-code — copy component source into the repo; fits own-the-code + air-gap; no license cost; upgrade to PRO only if we hit a wall). Brings **Tailwind CSS v4 + React Aria Components** (+ `motion` for transitions). React 19 already in place.
- Vite build (existing) gains Tailwind v4; dist still served by `src/otto/monitor/server.py`.

---

## Testing
- **Retire the DOM-parity Playwright harness** — it existed to prove the legacy→React port matched; a redesign moots it and it tests the wrong contract.
- Write fresh **behavior-based Playwright** E2E: drill-in navigation, metric filtering, event marking (click/drag/mark-now), topology render, live append, theme toggle, deep-link routes.
- Keep **vitest** unit tests for pure logic (store, grouping/tree, events, echarts builders, covreport sort).
- Update **air-gap gates** + **import-budget guards** for the new deps (Tailwind/React Aria/motion/echarts).

---

## Non-goals / deferred
- Coverage-report migration (separate spec).
- Intra-chassis network sub-view (deferred; Chassis⇄Network toggle when added).
- Server-side import; PRO Untitled UI tier (revisit only if needed).

---

## Effort & risk (honest)
Large — effectively a **view-layer rewrite** + a **new backend contract** + a **novel visualization** + a **test-suite rewrite**. Top risks:
1. **ECharts live-append parity** with Plotly's WebGL fast path (perf under high-frequency multi-series streaming).
2. **Test-suite rewrite** (parity harness retired).
3. **Topology layout algorithm** (chassis + free nodes + transitive hop wiring + gap-collapse + multi-chassis).
Mitigation: the pure data layer (`store`/`sse`/`events`) survives intact, which keeps the blast radius to the view layer + additive backend.

---

## Companion selections captured (for the record)
Before the tunnel dropped, the visual companion logged Chris's clicks, all consistent with the above: app-shell = **C (drill-in)**, fleet = **B (status tiles)**, topology = **B (rack + hop lines)**. Mockups persist under `.superpowers/brainstorm/434013-1783255364/content/` (gitignored).

## Resume checklist
1. Chris reviews this spec; adjust if needed.
2. Confirm the one proposed impl detail: ECharts direct-wrapper vs `echarts-for-react`.
3. Invoke `writing-plans` → implementation plan (Phase 1 tasks, Phase 2 stretch tasks).
4. Use a worktree for the build (isolation from main).
