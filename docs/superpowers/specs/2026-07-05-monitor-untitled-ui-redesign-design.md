# Monitor redesign — Untitled UI + ECharts (design spec)

**Date:** 2026-07-05

**Status:** DESIGN COMPLETE (brainstorming finished across 3 sessions via the visual companion + terminal). Awaiting Chris's review of this consolidated spec → then `writing-plans`.

**Next step:** Chris reviews this doc; on approval, invoke `writing-plans` to produce the implementation plan. Build in a worktree.

---

## 1. Context & goals

Move the **monitor** web UI to the **Untitled UI** React framework and swap the chart engine, as a **full redesign** (not a port). The monitor is already a React 19 SPA (`web/src/`): zustand store, SSE live stream, ~7 components + a hand-rolled 624-line `dashboard.css`, Plotly `scattergl` (WebGL) charts.

Redesign goals: fleet overview at a glance, better metric navigation, richer chart interactions, cleaner event UX, and a **lab-topology visualization** — extended during design into a full **element / external-data-source model**.

This reopens `docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md`, which deliberately kept Plotly ("high regression risk for little gain"). That calculus flips when redesigning the whole UI — the regression cost is paid regardless.

The **coverage report** is a separate, non-React surface (static Jinja) and is **out of scope** here (own future spec).

---

## 2. Scope & phasing

- **In scope:** the monitor only.
- **Out of scope:** coverage-report migration (separate future spec); NetEm editing UI (deferred until the NetEm backend exists); intra-chassis network sub-view (deferred).
- **Phase 1 — foundation & core UX:** Tailwind v4 + Untitled UI + ECharts; app shell + routing; final chrome; fleet grid (element-grouped, degrades gracefully without element data); per-subject view + provenance; events; system states; live + historical/review modes + import. Backend: `/api/meta` extension (lab/board/slot/hop/health), element grouping, launch-mode flag.
- **Phase 2 — topology visualization:** inter-element network map, intra-element graph, first-class links + inspector, Sources overlay — plus the richer element/link/source backend contract. The **link model + static topology layer is now foundation-supplied** by `2026-07-06-link-foundation-design.md` (`otto.link`: unified `Link` type, implicit-from-`hop` + declared derivation); the **dynamic-link overlay** is gated on link sub-project #2 and the **Sources overlay** on #5.
- **Future:** NetEm query + edit; intra-chassis network sub-view.

The **element model** is introduced in Phase 1 (grid grouping + provenance); the **topology visualization** of it is Phase 2.

---

## 3. Tech stack

- **Untitled UI React, free / open-source tier** (open-code — component source copied into the repo; fits own-the-code + air-gap; no license cost). Brings **Tailwind CSS v4 + React Aria Components** (+ `motion` for transitions). React 19 already present.
- **Apache ECharts** for charts (see §5). Vite build (existing) gains Tailwind v4; dist still served by `src/otto/monitor/server.py`. Air-gapped, no CDN (existing constraint).
- **URL routing** (lightweight router) for deep links: `/`, `/host/:name` (and element routes), `/topology`. Shareable links, back/forward, refresh-stable.

---

## 4. Data model — subjects & sources

Two independent axes for any metric series:

- **Subject** — *who the stat is about*: a **host** (an otto host) or an **element**.
- **Source** — *where the data came from*: the subject itself (**self**), or an external **management host**.

**Elements:**

- An **element** is a subject that is a **collection of hosts** (and possibly sub-elements); it is **not necessarily an otto host**. Types: **physical** (chassis with numbered slots), **logical** (a cluster, no slots), **singleton** (exactly one host).
- Physical and logical elements are **identical except that physical hosts carry a slot number**.
- Elements are **drillable subjects** with their own view. A **singleton** renders as a lone node that expands straight to its one host's stats.

**External sources (backend later):** an element-management host parses arbitrary CSVs/files (the table-driven parsing recently built for host stats) on its own cadence, producing series **assigned to hosts or elements**. The frontend needs only the subject × source model now.

---

## 5. Chart engine — Apache ECharts

- Replace Plotly with **ECharts** (canvas; optional WebGL via echarts-gl if needed). Chosen over Recharts (SVG — weakest for live streaming), uPlot (fastest but most manual), and keep-Plotly (valid low-risk default, declined for cohesion + features).
- **Integration:** a thin typed wrapper (`web/src/echarts.ts`) mirroring today's `plotly.ts` pure-builder architecture — `buildLayout`/`buildMetricTraces` become ECharts `setOption` builders. Preserve a **live-append fast path** (`appendData` / incremental `setOption`). *Proposed:* direct instance management via refs (like current `plotly.ts`), not `echarts-for-react` — **confirm at plan time.**
- **Native wins:** `axisPointer` linked cursor → the synced crosshair across stacked charts; `dataZoom` → brush-any-chart-to-zoom-all (in historical mode this drives the Range control, §11).
- Tree-shake `echarts/core` imports to keep the bundle lean.

---

## 6. Navigation & app shell

- **Overview-home, drill-in:** the fleet overview is the landing page; a **Grid ⇄ Topology** toggle flips it; clicking/drilling a subject pushes into a focused view with a back breadcrumb. Events open in a slide-over.
- **Two-tier chrome:** a persistent **global app bar** (§7) + a **per-view context row** (breadcrumb + subject pills; the Grid/Topology toggle on the overview; the review bar in historical mode).
- Breadcrumb deepens through the hierarchy: `Fleet / element / host`, `Topology / element / (intra) / host`.

---

## 7. Global chrome (final)

- **Brand "⬡ otto monitor" is always fixed upper-left.**
- **Pause** shows optionally to the brand's right — **live mode only** (freezes the live view for inspection; gone in historical).
- **Far-right, always visible & fixed:** status **text · status dot · ⋯ overflow menu** (text left of the dot; the tiny ⋯ rightmost). Only the two frequently-referenced things — pause (left) and status (right) — stay on the bar.
- **⋯ overflow menu** holds the infrequent actions: **Import, Export, theme toggle**. Present in both live and historical modes.
- **Theme:** seed the initial theme from `prefers-color-scheme`; the toggle is a simple **two-state light↔dark** that shows the **opposite** (what a click switches to) and persists the choice. No standing "System" mode.
- **Status** readouts: `Live ●` (green) / `HISTORICAL ●` (blue) / disconnected (amber); status light never moves.

---

## 8. Fleet overview — Grid

- Grid groups into **element sections**; host **status tiles** nested inside each. (Lab becomes a higher-level grouping/filter — TBD. If no element data, degrade to flat/lab grouping.)
- **Element section header:** type glyph (▦ rack / ▤ logical / gateway) · **mixed-health rollup** · host/board count · `⤢ enter`.
- **Mixed-health rollup = segmented bar:** one segment per host colored by health — shows the actual distribution, scales with fleet size, reads at small sizes. **The same rollup is the element's health indicator on the topology map.**
- **Host tile:** status dot · name · `board · slot` · a **labeled headline metric** (`34% cpu`, not a bare number — the label matters because the headline falls back to another metric when CPU isn't collected). **Down** tiles show outage duration (`down · 2m`); healthy tiles stay clean.
- Click a tile → that host's view; `⤢ enter` on an element → its topology/intra view (a **singleton** goes straight to the host).

---

## 9. Per-subject view (host or element)

- **Left panel** (combined filter + series control), top→bottom:
  - **Search** (free-text substring).
  - **Quick-filter chips** — toggle whole metric groups; includes a **Source** filter (self / each management host).
  - **Series tree with per-series checkboxes** — narrows to the active filter; noisy groups (e.g. `proc/ (12)`) collapse to a count. This replaces today's legend-capping workaround.
- **Provenance (badge + filter):** the tree stays metric-first; externally-sourced series carry a small **source badge** (e.g. `mgmt-01`); **self is the unlabeled default** (only exceptions marked). Source also appears in the chart tooltip/legend.
- **Right:** charts **stacked on a shared time axis**; **one synchronized cursor** shown via the hover scrubber (no redundant header clock); **brush any chart to zoom the whole stack**.
- Header: breadcrumb + subject name + status dot + `board·slot`/`hop` pills.

---

## 10. Topology (Phase 2)

- **Inter-element network map (top level):** elements as **collection-nodes** (type glyph + segmented health rollup + host count + `⤢ enter`) with **links between them**, rooted at the **local "you are here"** node. Everything connects back to local **transitively through the real hop chain** (`local → gateway → element → host`) — no direct spider-web — which also shows the **reachability cascade** (a dead gateway ⇒ everything behind it flagged unreachable, not just "down"). Node **type = icon + label; the colored dot means health only.**
- **Intra-element view (unified):** a **network graph of member hosts + their connections**, rooted via the path from local. **Physical** elements badge each host node with its **slot number**; **logical** elements are identical **minus** the badge. This **supersedes any literal vertical-rack visual** (empty slots simply aren't drawn; placement is a badge, not a frame). A **singleton** expands straight to host stats.
- **Links are first-class, selectable objects.** Links render by **provenance** (implicit hop-edge / declared data-plane / dynamic tunnel — distinct styles; dynamic comes from the TTL-cached discovery overlay, not the free static base). Selecting a link opens the **right side-panel inspector** (same slide-over pattern as events): connectivity now (type/protocol · provenance · status / latency / endpoints) + a **reserved NetEm section** (delay / loss / jitter / rate) marked **future** ("Configure — coming soon"). NetEm will live on hosts *and* links; query/edit via this GUI is **deferred** until the NetEm backend lands.
- **Management host = optional "Sources" overlay** (default off): the base map stays physical/network; a `[Sources]` toggle reveals management hosts + their dashed "reports-for" edges to the elements they feed. Otherwise the mgmt host is provenance-only (badges in the metric panel).
- **Edge cases to handle at implementation:** multiple elements per lab (multiple nodes), multi-hop chains (`A → gw1 → gw2 → board`), missing metadata (no slot / no hop → free/attached node).

---

## 11. Events

- **Global** markers (session/lab-wide), overlaid on **every** subject's charts. Instantaneous = vertical line; span = filled area + edge lines (port `buildShapes`/`buildAnnotations` to ECharts markLine/markArea).
- **Marking — always available on any chart, panel open or not:** **click = point, click-drag = span.** Historical / overlapping / nested spans come from chart-dragging.
- **"Mark now" = one stateful split button:** quick-click = point; caret = start span; while a span runs it becomes **■ End span · \<timer\>**. Starting a span creates a **live, open-ended span** whose right edge tracks *now* and grows on the charts; its list row shows "running… ● live \<timer\>" until ended. **One open span at a time** via Mark-now.
- **Review/edit** in the events **slide-over:** reverse-chronological list (color swatch · label · time / span duration · type · ✎ / ⌫; a row jumps the charts to its time). **Inline expand** to edit; newly-placed events open straight into edit.

---

## 12. Live vs Historical / review mode

Two entry paths; the **"is there a live session underneath?"** bit decides everything:

- **Path 1 — live + import:** `otto monitor live` running, user imports a file → historical is a *temporary overlay* on a still-running live session. An **Exit** control returns to live.
- **Path 2 — launched at a source:** `otto monitor <file|db>` → the monitor **is** a hostless, read-only **review tool**. There is **no Exit button** (nothing to return to); you load different data via the session picker / import.
- **CLI (backend/CLI decision to confirm):** require the explicit `otto monitor live` to start a live session (guards against accidentally starting live); `otto monitor <source>` starts review mode. The frontend is told which mode it's in.

**Review bar** (per-view context row, historical only):

- `HISTORICAL` tag (no hourglass) + source name.
- **Session picker** — only when the source has >1 session (e.g. a database); hidden for a single file.
- **Range picker** — always present: presets (Full range / Last 15m / Last 1h) + **custom from–to**, applied across all charts. The synced brush-to-zoom drives this same Range.
- **Reset** — restores the session + full range as of when the view opened.

**Import** is **client-side** (mirrors Export): reads a previously-exported JSON in-browser and hydrates the store → historical mode. Export lives in the ⋯ menu in both modes (exports the loaded set); Import loads another collection.

---

## 13. System states

- **Empty — live:** "No metrics yet — hosts appear as the first samples arrive."
- **Empty — review:** "No data loaded — import a collection to review" + an Import button.
- **Loading:** skeleton shimmer tiles.
- **Disconnected:** amber "Connection lost — reconnecting…"; charts freeze; status dot amber; auto-retry.
- **Drilled-in unreachable host:** **last-known data, frozen & dimmed, + a banner** ("Unreachable for 2m — showing last-known data"). Keeps the pre-failure context (usually the most diagnostic moment) rather than blanking it.
- **Historical:** the `HISTORICAL` banner/tag + review bar; pause hidden.

---

## 14. Backend contract (frontend-facing; backend built later)

- **Extend `/api/meta`** with per-host `lab` / `board` / `slot` / `hop` + **health** (status + last-seen + outage duration). Fields already exist on the host model (`src/otto/models/host.py`); the monitor just doesn't expose them. Feeds tiles + topology. Regenerate TS types (`scripts/gen_web_types.sh` / `MonitorMeta`).
- **Element / topology model:** elements (id, type physical/logical/singleton, membership, health rollup), **links** — the foundation `Link` type (see `2026-07-06-link-foundation-design.md`): **static, available now** via `Lab.static_links()` (endpoints host+interface+ip · protocol, default `"tcp"` · provenance implicit/declared/dynamic · deterministic id); **live** status/latency arrives with the dynamic-discovery layer (link #2); **future** NetEm (link #3) — and **sources** (management host → assigned series; link #5). Phase 2.
- **Health/unresponsive tracking** (new server work): per-host last-seen + a "down" threshold + outage duration.
- **Launch-mode flag:** live vs review + the source reference, surfaced to the frontend.
- **Import:** client-side — no endpoint. **NetEm query/edit:** deferred.

---

## 15. Data layer — keep / evolve / replace

- **Keep (view-agnostic):** `store.ts` (zustand), `sse.ts`, `events.ts`, `logevents.ts`.
- **Evolve:** `grouping.ts` / `retirement.ts` → the metric-tree model + provenance; add element / topology / link / source models.
- **Replace:** all `components/*.tsx` (rebuilt with Untitled UI), `dashboard.css` (→ Tailwind v4), `plotly.ts` (→ `echarts.ts`).

---

## 16. Testing

- **Retire the DOM-parity Playwright harness** — it proved the legacy→React port matched; a redesign moots it and it tests the wrong contract.
- Fresh **behavior-based Playwright** E2E: drill-in navigation, metric filtering + provenance, event marking (click/drag/mark-now), topology render + link inspector, live append, historical/import + review controls, theme toggle, deep-link routes.
- Keep **vitest** unit tests for pure logic (store, metric-tree, events, echarts builders, health rollup).
- Update **air-gap gates** + **import-budget guards** for the new deps (Tailwind / React Aria / motion / echarts).

---

## 17. Non-goals / deferred

- Coverage-report migration (separate spec).
- Intra-chassis network sub-view (Chassis⇄Network toggle) — deferred.
- NetEm editing UI — deferred until backend exists (the link inspector reserves its home).
- Multiple concurrent open spans (one at a time via Mark-now; overlaps via chart-drag).
- Server-side import; PRO Untitled UI tier.

---

## 18. Effort & risk (honest)

Large — a **view-layer rewrite** + a **substantial new backend contract** (element / source / link / NetEm model — flagged by Chris as later work) + a **novel visualization** + a **test-suite rewrite**. Top risks:

1. **ECharts live-append parity** with Plotly's WebGL fast path under high-frequency multi-series streaming.
2. **Element/source/topology backend contract** — the biggest net-new backend surface; Phase 2 depends on it.
3. **Topology/element layout** (network graph + slot badges + transitive hop wiring + links).
4. **Test-suite rewrite** (parity harness retired).

Mitigation: the pure data layer (`store`/`sse`/`events`) survives intact; Phase 1 runs on a modest meta extension, deferring the heavy element/topology backend to Phase 2.

---

## 19. Open items (resolve at plan/impl time)

- ECharts wrapper: direct instance mgmt vs `echarts-for-react`.
- Headline-metric fallback rules when CPU isn't collected.
- Health/down threshold algorithm.
- Lab as a grouping level vs a filter, now that elements are primary.
- CLI shape: `otto monitor live` vs `otto monitor <source>` (backend).

---

## 20. Companion selections captured

Across the sessions the visual companion logged Chris's picks, all consistent with the above: shell = **overview drill-in**; grid = **element-grouped status tiles**; per-host = **synced stack + filter tree**; provenance = **badge + filter (B)**; topology = **rack/network** → refined to **network-graph + slot badges**; mgmt host = **Sources overlay (C)**; health rollup = **segmented bar (B)**; events = **stateful Mark-now (A)**; unreachable = **last-known dimmed (A)**; chrome = **brand-left + overflow-right**. Mockups persist under `.superpowers/brainstorm/*/content/` (gitignored).

## 21. Resume checklist

1. Chris reviews this spec; adjust if needed.
2. Confirm the ECharts wrapper choice (§19).
3. Invoke `writing-plans` → implementation plan (Phase 1 tasks; Phase 2 topology tasks).
4. Build in a worktree (isolation from main).
