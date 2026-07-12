# Monitor topology — edge legend, hover card, and same-column edge routing

**Date:** 2026-07-12

**Status:** DESIGN COMPLETE (brainstormed with the visual companion + terminal). Awaiting Chris's review → then `writing-plans`.

**Predecessor:** `2026-07-11-monitor-topology-design.md` (the topology view itself, merged `29565ff`).

---

## 1. Context

Two complaints against the merged topology view, both about **edges**:

1. **No key.** Five link provenances are drawn in four shades of grey and three dash
   patterns, and node health is drawn in five more colours. Nothing on the canvas says
   what any of it means.
2. **Edges anchor in absurd places.** In `kitchen-sink`, `db-01`'s links to its peers
   leave `db-01`'s *left* face and arrive at the peers' *right* faces, swinging right
   across the whole column and back.

Investigation turned up a third problem the report didn't name, and it is the one that
actually constrains the design:

**React Flow renders edges *beneath* nodes.** An edge that overlaps a node box does not
cross it — it **disappears behind it**. Three of `kitchen-sink`'s eight inter-element
edges are currently swallowed for most of their length.

## 2. Goals

- A reader can decode every line style and every status colour without interacting.
- A reader can identify *which* link a given line is without opening the inspector.
- No **same-column** edge is hidden behind a node.
- Same-column links anchor on the faces nearest their peer.
- Reuse the repo's Untitled UI (open-code / React Aria) primitives in `web/src/ui/`.

**Known remaining gap (pre-existing, not introduced by this design):** a *cross-column*
edge that **skips** a column — e.g. a depth-3 element with a declared link to a depth-1
element — can still be swallowed by a depth-2 node in an overlapping row band.
`routeCrossColumn` anchors face-to-face between the two endpoints with no awareness of
intervening columns, and `kitchen-sink` has no fixture deep enough to exercise it. See
follow-up 9 in `todo/monitor-topology-followups.md`.

## 3. Non-goals

- Changing the layout (see §8, follow-up D\*).
- Changing the export contract or the link/tunnel data model (see §8).
- NetEm editing; MiniMap; the intra-element view's own layout.

---

## 4. Findings that shaped the design

These are measured, not assumed. The geometry below is `kitchen-sink`'s real
inter-element graph.

**Layout facts.** `layoutTopo` places `x = depth * COL_W` (280) and `y = row * ROW_H`
(110). Element nodes are 208px wide, host nodes 176px. So the gutter between columns is
**72px** and the vertical gap between rows is **38px**.

**Why `db-01` looks wrong.** Every node exposes exactly two handles — `target` on
`Position.Left`, `source` on `Position.Right` (`nodes.tsx` `Ports`). `db-01`, `edge-gw`,
`mgmt-01` and `workers` all have `depth == 1`, so they stack in **one column** (row order
is `rowOrder`'s id sort). A same-column link therefore leaves a right face and re-enters a
left face, crossing the column twice.

**Occlusion, today.** Sampling each edge's bezier against the node rects:

| variant | columns | same-column links | edges swallowed by a node |
|---|---|---|---|
| **today** (hop-depth columns, fixed L/R handles) | 3 | 2 | **3** |

**The bow budget.** A line leaving a bottom-*centre* anchor must travel 104px sideways to
clear its own box, but the vertical gap to the next row is only 38px. It cannot turn that
fast — which is why a naive "bottom→top" fix leaves the `workers ⇄ db-01` links buried
under the boxes between. **Leaning the anchor along the bottom edge toward the gutter**
fixes that: the distance left to clear becomes a fixed 20px, *independent of how wide the
box is*.

**Two things that only fell out of implementing it** (both were wrong in this document's
first draft, and both were caught by sampling the emitted curve rather than by looking at
it):

- **The bow must not scale with node width.** Since the lean is a fixed inset from the
  corner, the horizontal distance to clear is constant. What actually governs whether the
  curve escapes is *how steeply it leaves the anchor* — i.e. the control-point placement.
- **Control points at `dy/3` / `2·dy/3` make the escape angle depend on the row-span.** The
  curve's initial direction is `(bow, dy/3)`, so the further apart the endpoints, the more
  *downward* it leaves — while the room to escape stays a fixed 38px. Clearance therefore
  decays with span: +16px at 3 rows, +6.6px at 4, +1px at 5, and **negative from 6 rows
  on**, i.e. the original bug returns on any lab with six elements at one hop depth.
  Capping the control-point offset (`CTRL_Y_MAX`) makes the exit angle span-independent.

`kitchen-sink`'s depth-1 column is in fact **five** deep — `spare-chassis` is an element
with no hosts, which still lands at depth 1 — so `db-01 ⇄ workers` is a **4-row** span.

Final constants: `LEAN_INSET = 20`, `CTRL_Y_MAX = 28`, `BOW_BASE = 104` (absolute),
`FAN_STEP = 12`, clamped by the gutter (`maxBulge = LEAN_INSET + (COL_W − width) − 8`,
and the bulge is `0.75 × bow`). Verified by sampling: every row span from 2 to 20 clears
every intervening box, for both element (208px) and host (176px) widths, with the arc
staying inside the existing gutter. **No layout change.**

**Why not re-layer instead (Chris's proposal).** Pushing a hub's peers one column deeper
turns every same-column link into a clean forward link. It was tested:

| variant | columns | same-column links | edges swallowed |
|---|---|---|---|
| **D** — layer on *all* links | 4 | 0 | **3** |
| **D\*** — layer on *static* links only | 3 | 1 (adjacent) | **1** |

D fails because **`local` has an edge to every element with a `hop == null` host**. Push
any of them past column 1 and its `local` edge must skip a column, where it is swallowed by
whatever sits in the skipped column. The intra-column tangle becomes a skip-column tangle.
D also lets a *dynamic* link (`tun-demo`) shove `edge-gw` and `chassis-a` a whole column
sideways when a tunnel comes up — the map reflows when the network changes.

D\* (layer on declared + implicit only) is genuinely better and worth doing, but it still
needs this spec's routing fix underneath (it leaves one same-column link and one swallowed
skip-column link), and it redefines the x-axis: `workers` is **1 hop** from local yet would
sit **2 columns** out. It is deferred to its own design (§8).

**Tunnels.** `Tunnel` already carries its full ordered chain — `path: tuple[TunnelHop,
...]`, len ≥ 2 (`src/otto/tunnel/model.py`). The **monitor export discards it**: a tunnel
is flattened to a two-endpoint `LinkSnapshot` with `provenance: "dynamic"`. Drawing a
tunnel as an overlay on the links it rides therefore requires a `format:1` export change,
and would not even alter `kitchen-sink` (its `tun-demo` runs `edge-gw ⇄ db-01`, between
which no declared or implicit link exists — there is nothing to wrap). Deferred (§8). This
spec instead makes a tunnel *look* like an overlay (§5.5).

---

## 5. Design

### 5.1 One style table, two consumers

`edgeStyle()` in `LinkEdge.tsx` is currently the only thing that knows what a provenance
looks like. A legend that restates those styles by hand will silently drift from the
canvas — a wrong key is worse than no key. So the encoding moves into one table:

```ts
// web/src/topo/edgeStyles.ts
export interface EdgeStyleSpec {
  label: string;                 // legend row text
  hint: string;                  // one-line meaning (hover card + title)
  stroke: string;                // var(--topo-edge-*)
  strokeWidth: number;
  strokeDasharray?: string;
  casing?: { stroke: string; strokeWidth: number };   // tunnels only
}
export const EDGE_STYLES: Record<TopoEdge["provenance"], EdgeStyleSpec> = { ... };
```

Consumed by **`LinkEdge`** (renders the path) and **`TopoLegend`** (renders a 30×10 SVG
swatch from the *same* spec). A unit test asserts `EDGE_STYLES` has a row for every member
of `TopoEdge["provenance"]`, so a new provenance cannot ship without a legend entry.

The impair pill is extracted to `ImpairPill.tsx` and used by both the canvas and the
legend, for the same reason.

Rows:

| provenance | label | hint |
|---|---|---|
| `declared` | declared | data-plane route from `lab.json` |
| `implicit` | hop (implicit) | management path derived from `hop` |
| `dynamic` | tunnel | realized by an otto tunnel |
| `reports-for` | reports for | metrics sourced from a management host |
| `local` | from local | directly reachable from this machine |
| *(pill)* | middlebox | an in-path impairment host |

Status rows reuse the already-exported `STATUS_DOT` from `nodes.tsx`: ok, down,
unreachable, no data, unknown.

### 5.2 The legend panel

- React Flow `<Panel position="bottom-left">`, **inside** `<ReactFlow>`, stacked above
  `<Controls>`.
- **Bottom-left, not right:** `LinkInspector` is a `fixed inset-y-0 right-0 w-96` aside, so
  any right-anchored panel is covered the moment an edge is selected.
- Collapsible; **default expanded**. React Aria `Disclosure` supplies the button semantics
  and `aria-expanded`. New `web/src/ui/Disclosure.tsx` follows the open-code pattern of the
  existing `ui/` primitives (React Aria Components + Tailwind).
- 340×150px, two columns (Links | Status) so it stays short on a laptop viewport.
- **All rows show always**, not only the kinds present in the current graph: the panel is
  small, it stays stable across `Sources` toggles and Playwright runs, and an absent row is
  itself informative.
- Collapse state is **component state only** — no `localStorage`, no store. It resets when
  the topology view unmounts. Persisting it is not worth a new storage key; revisit only if
  someone asks.
- Test ids: `topo-legend`, `topo-legend-toggle`, `topo-legend-link-<provenance>`,
  `topo-legend-status-<status>`.

### 5.3 The hover card

`BaseEdge` already renders a wide invisible interaction path (`interactionWidth`), so hover
needs no new hit-testing.

- On hover: bump the stroke width (same delta as `selected`) and render a card through
  `EdgeLabelRenderer` — `pointer-events: none`, so it never steals the click.
- The card is anchored at the **curve apex** (the `labelX`/`labelY` `routeEdge` returns),
  not at the cursor. Deterministic position ⇒ assertable in Playwright, and it can't jitter
  under the pointer. When an edge also has an impair pill, the card replaces it while
  hovered rather than stacking on top of it.
- Card contents: name/id · kind label (from `EDGE_STYLES[...].label`) · protocol ·
  endpoints · impair.
- **Degenerate edges are handled here**, unlike the inspector: `reports-for` edges have no
  `link` at all, and implicit hop groups carry `links[]` rather than `link`. The card shows
  "hop path · 3 links" and "reports for · mgmt-01 → chassis-a" instead of a raw edge id.
  (This is follow-up #2 in `todo/monitor-topology-followups.md`, addressed for hover; the
  inspector's own degenerate case stays open.)
- Click → `LinkInspector`, unchanged. Hover is a preview, not a replacement.
- Hover is mouse-only by nature; the keyboard/AT path remains click → inspector, and the
  legend (which needs no pointer at all) is what makes the encoding accessible.

### 5.4 Edge routing

`LinkEdge` is shared by the inter-element and intra-element views, so the routing below
applies to **both**. Only the *layout* of the intra view is out of scope (§3) — a
same-column pair of host nodes gets the same treatment as a same-column pair of elements,
with the narrower 176px host width feeding the same formulas.

New pure module `web/src/topo/routing.ts` — no React, no React Flow, fully unit-testable:

```ts
export function routeEdge(
  source: Rect, target: Rect, parallelIndex: number, groupSize: number,
): { path: string; labelX: number; labelY: number };
```

`LinkEdge` obtains the rects from `useInternalNode(source)` / `useInternalNode(target)`
(`internals.positionAbsolute` + `measured`) rather than the handle-derived
`sourceX/sourceY`. If a node is not yet measured on the first paint, fall back to the
`sourceX/sourceY` props for that frame.

**Cross-depth edges (different `x`)** — unchanged behaviour, restated as nearest-face:
the left-hand node's **right** face → the right-hand node's **left** face, with the existing
centred perpendicular fan for parallel groups.

**Same-column edges (equal `x`)** — the new case:

- `upper` = the node with the smaller `y`. Anchor on `upper`'s **bottom** face and
  `lower`'s **top** face.
- `rowSpan = round((lower.y - upper.y) / ROW_H)`.
- `rowSpan <= 1` → **straight line between the face centres.** This is exactly the clean
  short vertical the bug report asked for.
- `rowSpan >= 2` → the straight line would pass under the boxes between, so:
  - The anchor slides along the bottom/top face to `LEAN_INSET = 20`px from the corner
    nearest the gutter (each rect uses its own width, since a column may mix element and
    host nodes). The distance still to clear is then a fixed 20px, whatever the box width.
  - `BOW = min(maxBulge / 0.75, BOW_BASE + parallelIndex * FAN_STEP)` with
    `BOW_BASE = 104` (**absolute — not scaled by node width**, because the lean already
    made the clearance width-independent) and `FAN_STEP = 12`. The fan is **outward only**:
    a centred fan pushes the inner sibling back under the very box the bow exists to clear.
    `maxBulge = LEAN_INSET + (COL_W − width) − 8` clamps the arc inside the gutter, since a
    cubic offset by `k` bulges `0.75k`.
  - Control points at `(anchorX + BOW, y0 + ctrlY)` and `(anchorX + BOW, y1 − ctrlY)`, where
    `ctrlY = min(dy/3, CTRL_Y_MAX)` and `CTRL_Y_MAX = 28`. **The cap is the load-bearing
    part.** Without it, control points at `dy/3` make the curve's initial direction
    `(bow, dy/3)`, so it leaves ever more *downward* as the endpoints separate while the
    room to escape stays a fixed 38px — clearance decays with span and goes negative at 6
    rows, resurrecting the bug. Capping `ctrlY` makes the exit angle span-independent.
- **Bow direction is always `+x`** (toward deeper columns). The `−x` side of a column is
  where `local`'s edges run, and the deepest column always has a free right gutter.
- **Known geometric ceiling:** the gutter clamps the bulge, so this strategy holds to a
  row-span of ~20 (verified) and cannot be pushed indefinitely — a column that deep would
  need the deferred re-layering (§8) or a wider pitch. Far beyond any current lab.

**Label position bug, fixed in passing:** `fannedBezierPath` currently places the label at
the *full* perpendicular offset, but a cubic with both interior control points offset by
`k` only reaches `0.75k`. The label therefore floats off its own curve today. `routeEdge`
returns the apex (`0.75 × BOW`).

### 5.5 Tunnel casing

A `dynamic` edge draws its path **twice**: a wide low-opacity sleeve
(`--topo-edge-tunnel-casing`, width 7) beneath the existing dark dashed core (width 2).

No other edge has a casing, so a tunnel reads as something *wrapped around* a path rather
than as a peer of the other links — the "overlay" semantics `Provenance.DYNAMIC`'s
docstring already claims ("reserved for topology overlays"), without touching the model.
The cue is **form, not colour**: the palette keeps colour reserved for health, the brand
accent stays on "you are here"/hover, and the distinction survives colour-blindness.

New CSS custom property in `app.css` with a `.dark` alternate, alongside the existing
`--topo-edge-*` tokens.

---

## 6. Files

**New**
- `web/src/topo/edgeStyles.ts` — `EDGE_STYLES` table.
- `web/src/topo/routing.ts` — pure `routeEdge` geometry.
- `web/src/topo/TopoLegend.tsx` — the panel.
- `web/src/topo/ImpairPill.tsx` — shared by canvas + legend.
- `web/src/ui/Disclosure.tsx` — React Aria disclosure primitive.

**Modified**
- `web/src/topo/LinkEdge.tsx` — anchors from `useInternalNode`, casing, hover card.
- `web/src/topo/TopologyPage.tsx` — mount `<TopoLegend>` as a `<Panel>`.
- `web/src/app.css` — `--topo-edge-tunnel-casing` (light + dark).

`nodes.tsx` keeps its `Handle`s (React Flow requires them for edge validity); their
positions simply stop determining where lines attach.

## 7. Testing

Gate is `make coverage` (per `reference_make_gate_targets`).

**Unit (vitest), the load-bearing ones:**
- **Occlusion invariant.** For `kitchen-sink`'s depth-1 column, sample 400 points along
  each `routeEdge` path and assert **no point lies inside a non-endpoint node rect**. This
  is the exact check that found the flaw in the first design and it is what stops the bug
  regressing.
- Adjacent-row (`rowSpan == 1`) same-column edges are straight and anchored at face centres.
- Parallel same-column edges fan **outward only** (bow is monotonically non-decreasing in
  `parallelIndex`).
- `EDGE_STYLES` exhaustively covers `TopoEdge["provenance"]`.
- Label point lies **on** the returned curve.

**Component (testing-library):** legend renders one row per provenance and per status;
the toggle flips `aria-expanded`.

**Browser (Playwright, dashboard lane):** legend visible by default; collapse works;
hovering `topo-link-app-db` shows a hover card naming `app-db`; the impair pill still
renders on `metrics-udp`.

## 8. Follow-ups (explicitly out of scope)

Appended to `todo/monitor-topology-followups.md`:

1. **D\* — static-link layering.** Push a hub's peers one column deeper using **declared +
   implicit links only** (never dynamic, so tunnels can't reflow the map). Needs its own
   design: cycle-safe layer assignment, skip-column edge routing (a `local` edge to a
   pushed node will skip a column), and a decision on redefining the x-axis away from
   "hops from local". Measured payoff on `kitchen-sink`: same-column links 2 → 1, and
   `db-01`'s peers read as downstream.
2. **Tunnels as overlays on their underlay links.** Export `Tunnel.path` (a `format:1`
   export-contract change: schema, `export.gen.ts`, generator, fixtures, drift guards),
   then map each consecutive hop-pair onto an existing link and render the tunnel riding
   it, drawing a bare segment only where no link exists. Note this changes nothing for
   `kitchen-sink`, whose only tunnel has no underlay to ride — the fixture would need a
   multi-hop tunnel to exercise it.
3. The existing follow-ups 1–6 in that file are untouched by this spec, except that
   hover now handles the degenerate-edge case (#2) for hovering, not for the inspector.

## 9. Decisions

| Decision | Why |
|---|---|
| Fix routing, don't re-layer | The routing fix is needed either way; layering alone converts intra-column occlusion into skip-column occlusion (measured: 3 swallowed edges). |
| Legend bottom-left | `LinkInspector` is a fixed full-height right aside; a right-anchored key gets covered on the first edge click. |
| One `EDGE_STYLES` table | A legend that restates styles by hand drifts from the canvas. A wrong key is worse than none. |
| Legend shows all rows always | Small panel; stable under `Sources` toggles and in tests; an absent row is informative. |
| Grey casing, not accent | Keeps colour reserved for health, keeps the brand accent on "you are here"/hover, and the cue survives colour-blindness. |
| Anchor lean + outward-only fan | Bottom-*centre* anchoring cannot clear a 208px box in a 38px row gap. A centred fan pushes the inner sibling back under the box the bow exists to clear. |
| Capped control-point offset, absolute bow | The escape angle — not the bow size — is what a growing row-span destroys. Scaling the bow by node width is meaningless once the anchor lean has fixed the clearance distance. Both errors were found by sampling the emitted curve; neither was visible by eye. |
