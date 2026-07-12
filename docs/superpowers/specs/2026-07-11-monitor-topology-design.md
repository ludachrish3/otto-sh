# Monitor Topology (Plan 4) — Design

**Date:** 2026-07-11 · **Status:** approved (brainstorm with Chris, this date)
**Parents:** UX spec `2026-07-05-monitor-untitled-ui-redesign-design.md` §10 (topology), §6/§7 (chrome + toggle), §13 (states); contract spec `2026-07-10-monitor-export-format-and-dummy-data-phase-design.md` (`lab.links`, LinkSnapshot); `todo/topology_plan.md` (superseded UX, retained data-model lineage).

## Goal

Complete the review-mode surface: render the lab's topology from an imported
`format: 1` document — elements and hosts as nodes rooted at the local
"you are here" node via real hop chains, links as first-class selectable
objects — with a GitHub-Actions-pipeline feel (pan/zoom canvas, left-to-right
layered flow). Review-mode only; no backend changes.

**In scope (full §10):** inter-element map, intra-element view, link
inspector, Sources overlay, and the full reachability cascade
(down-vs-unreachable derivation). **Out of scope:** NetEm query/edit (backend
missing — inspector ships the reserved "Configure — coming soon" section),
grid unreachable-dimming (live hookup), event marking, live data.

## Decisions (from brainstorm)

1. **Canvas = React Flow** (`@xyflow/react`, exact-pinned). Chosen over
   hand-rolled SVG (pan/zoom polish for free) and Cytoscape/echarts-graph
   (both render to canvas, killing the data-testid Playwright contract).
   Nodes are our own React components — Tailwind tokens, testids, and
   arbitrary interiors (glyphs, rollups, slot badges) all work. Edges are
   styleable SVG paths. Air-gap: bundled npm dep, local stylesheet, no
   external assets — verified by the existing `make web` grep gate.
2. **Layout is ours and deterministic.** Only the hop skeleton places nodes;
   `hop` is a single-parent pointer so the layering input is a tree/forest
   by construction. Data-plane links (declared/dynamic, multi-route, cyclic)
   are cross-edges between already-placed nodes and never affect placement.
   No force layout, no dagre: depth = hop-chain length, stable ordering
   (slot-then-id within elements, id otherwise) → positions never jitter,
   screenshots and Playwright assertions stay stable.
3. **Full cascade now.** A dead gateway must render everything behind it
   *unreachable* (dimmed), distinct from *down* (red) — §10's stated reason
   the map roots through real hops.
4. **Cycle/scale posture (Chris):** topology may grow deep and wide, and
   connectivity may contain cycles via multiple routes. Keep the
   implementation simple but never wrong: hop-cycle guard (below), parallel-
   edge fan-out now, pan/zoom + minimap for size, and React Flow's
   `onlyRenderVisibleElements` documented as the scale escape hatch.

## Data layer — `web/src/data/topology.ts` (pure, no React)

```ts
type EffectiveStatus = HealthStatus | "unreachable";  // display-level; health.ts untouched

interface TopoNode {
  id: string;                     // host id, element id, or "local"
  kind: "local" | "element" | "host";
  depth: number;                  // hop-chain length back to local
  element?: DerivedElement;       // kind === "element"
  host?: HostSnapshot;            // kind === "host" (free host or intra view)
  effective: EffectiveStatus | EffectiveStatus[];  // rollup array for elements
}

interface TopoEdge {
  id: string;
  source: string; target: string;
  provenance: "hop" | "declared" | "dynamic" | "reports-for";
  link?: LinkSnapshot;            // absent for synthesized reports-for edges
  parallelIndex: number;          // 0..n-1 among edges sharing an endpoint pair
}

deriveReachability(session, healths): Map<string, EffectiveStatus>
buildTopoGraph(session, effective, opts: { expand?: string; sources: boolean }): TopoGraph
```

(`effective` is `deriveReachability`'s output — the graph builder never sees
raw health, so every rendered status has the cascade already applied.)

- **Inter-element graph** (`expand` unset): one node per element
  (members collapsed), free hosts as host nodes, rooted at `local`.
  An element's depth = min member depth; its rollup = members' effective
  statuses in slot-then-id order (reuses `elementRollup` ordering).
- **Intra-element graph** (`expand = elementId`): that element's members as
  host nodes (slot badges when the element is physical), plus the hop path
  from local so the view stays rooted. Singletons never reach this view
  (navigation sends them straight to the host page).
- **Reachability:** walk each host's hop chain (`host.hop` transitively);
  if any ancestor's raw status is `down`, the host's effective status is
  `unreachable`; otherwise its raw status. The walk carries a visited-set:
  a hop cycle (misconfig) clamps depth, yields `unknown` effective status
  for the cycle's members, and appends a session warning — fail-loud, never
  an infinite loop. Only hop chains matter (they are the collection path);
  data-plane routes never affect reachability.
- **Edges:** implicit hop edges + `lab.links` (declared/dynamic, with
  `impair` middlebox data passed through), plus dashed `reports-for` edges
  synthesized from metric `source` attribution when the Sources overlay is
  on. Edges between the same endpoint pair get increasing `parallelIndex`
  for renderer fan-out (kitchen-sink's declared tcp+udp may already share a
  pair).

**Fixture:** the Plan-1 generator grows a `cascade` scenario emitted as
`web/fixtures/cascade.json` (committed, drift-guarded like its siblings):
a gateway whose samples stop mid-session and two hosts behind it that go
silent at the same moment — raw health says down×3; the cascade must say
down×1 + unreachable×2. Kitchen-sink is not modified (Plan 2/3 test
arithmetic stays untouched).

## UI — `web/src/topo/`

- `layout.ts` — pure deterministic layered layout: depth → column,
  stable row order; O(n); returns React Flow node positions.
- `LocalNode.tsx` / `ElementNode.tsx` / `HostNode.tsx` — custom React Flow
  nodes (plain Tailwind divs). ElementNode: type glyph (▦ physical / ▤
  logical) + name + segmented health rollup fed *effective* statuses +
  host count + `⤢ enter`. HostNode: status dot (unreachable = dimmed
  treatment, distinct from the red down dot) + name + slot badge (physical
  elements only). Icon+label carry type; the colored dot means health only
  (§10).
- `LinkEdge.tsx` — custom edge: provenance styles (solid hop, distinct
  declared, dashed dynamic, dashed-light reports-for), mid-edge impair
  marker when the link carries a middlebox, curvature offset by
  `parallelIndex`, selectable.
- `TopologyPage.tsx` — `/topology` (inter-element) and
  `/topology/:elementId` (intra) as one page; `<Controls>` (fit/zoom)
  (minimap: see Amendments); `[Sources]` toggle in the context row (default
  off); breadcrumb `Topology / element / host` (§7 chrome pattern).
- `LinkInspector.tsx` — a non-modal fixed aside (see Amendments): type/protocol ·
  provenance · status/endpoints (+ latency when present), then the reserved
  **NetEm** section rendered disabled with "Configure — coming soon" (§10).
- **Navigation:** OverviewPage's context row gains the Grid ⇄ Topology
  toggle (existing `ToggleGroup` primitive; route-backed so deep links and
  back/forward work). Element `⤢ enter` → `/topology/:elementId`
  (singleton → `/host/:id` directly). Host node click → `/host/:id`.
- **Theme:** all node/edge colors from existing tokens (status family for
  health, gray scale for chrome, brand only as UI accent); React Flow's
  base stylesheet imported locally; dark mode via the existing `dark` class
  (canvas-free, so no `useIsDark` bridge needed).

## Testing

- **Vitest (pure, heavy):** layout (depths, stable order, cycle guard on a
  synthetic hop-loop doc — clamped + warned, no hang; parallel-index
  assignment), `deriveReachability` truth table against `cascade.json`
  (gateway down ⇒ descendants unreachable; recovered window ⇒ all ok) and
  the cycle case, graph mapping (links → provenance edges, impair
  passthrough, Sources synthesis).
- **RTL:** node components (rollup reflects effective statuses; slot badge
  physical-only), inspector fields + disabled NetEm stub. React Flow wrapped
  with fixed dimensions + established jsdom stubs (ResizeObserver et al.).
- **Playwright (data-testid contract):** Grid ⇄ Topology toggle;
  `topo-node-*` / `topo-link-*` render; enter → intra → host → subject
  page; link click → inspector content; Sources toggle adds/removes mgmt
  nodes + edges; cascade fixture → unreachable styled distinct from down;
  fit-view control changes the viewport transform (pan/zoom smoke).
- **Gates:** Plan-3 Task-9 sweep verbatim — `make coverage-hostless`,
  nox lint+ty, `make web` + air-gap (now also proving @xyflow/react),
  full vitest + ratchet recalibration, `make dashboard`, import-budget.

## Follow-ups (seeded now, not this plan)

- Grid unreachable-dimming (live hookup, §13).
- NetEm configure in the inspector (needs backend).
- Topology screenshot in the docs guide (rides the rewritten capture).
- Scale hardening if labs outgrow tens of nodes: `onlyRenderVisibleElements`,
  label decluttering.

## Amendments (post-implementation, 2026-07-11)

- **LinkInspector is a non-modal fixed aside** (Escape-to-close), not the modal
  SlideOver primitive: the modal backdrop blocked the review bar, defeating the
  inspector's own survives-range semantics and §10's side-panel intent. The
  events slide-over stays modal. Known gap: at ≤1280 px width the full-height
  aside occludes the review bar's Apply control — first follow-up.
- **Sources toggle gates only the reports-for edges**; mgmt hosts are lab hosts
  and stay visible as nodes (hiding them would misrepresent the lab).
- **MiniMap deferred to follow-ups** (with `onlyRenderVisibleElements`): at
  tens of nodes, fitView + Controls + the deterministic layout cover the
  stated scale posture; revisit when labs outgrow that.
