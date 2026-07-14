# Topology layout redesign — design

**Date:** 2026-07-14
**Status:** approved (design), ready for planning
**Evidence:** `.superpowers/sdd/layout-preview-report.md` — twelve layout variants
measured against two fixtures in the REAL renderer (real `buildTopoGraph`, real
`routeEdge`, real React Flow). Every number below is measured, not estimated.

## The problem

The topology map's x-axis is **hops from `local` along the management chain**
(`layout.ts`: `x = depth * COL_W`). In a real lab that carries almost no
information: management paths are short — direct, or through one or two jump
hosts — so nearly every element lands in column 1 or 2.

Measured on a representative ISP core (23 elements: border, core, mobile-core
services, aggregation, access; management via two EMS and a jump host):

> **23 elements collapse into three columns — 1, 11, 12 — with 75 crossings
> between element links.** The network hierarchy is completely invisible.

Meanwhile the structure a user wants to *see* — the element-to-element network —
lives in the `declared` links, which the layout ignores when deciding position.
And the management plane (`local`, jump hosts, EMS) is topologically a **star
that touches everything**; a star overlaid on any layout is a hairball generator.

**Management is currently the skeleton and the network is the overlay. That is
backwards.**

## The metric

Only **data-plane** edges count: `declared` links, element ⇄ element. Management
edges (`local:*`, `implicit` hop edges, `reports-for`) may freely pass behind
elements — that is honest, unobtrusive, and explicitly **not** clutter (Chris,
2026-07-13). Two numbers:

- `dp_crossings` — crossings where both edges are data-plane.
- `dp_swallowed` — data-plane edges whose path passes under a non-endpoint node.
  React Flow draws edges **beneath** nodes, so a swallowed edge is **invisible**.

Reporting management-inclusive totals (as earlier rounds of this investigation
did) measures the wrong thing and inverts the ranking. Do not do it.

## Measured results

| variant | isp-core | sprawl |
| --- | --- | --- |
| **shipped layout today** | 75 cr / 0 sw | 21 cr / **3 sw** |
| hand-written `tier` + row-sort (W5) | 4 cr / 0 sw | — |
| tier-free, management declared (W11) | 12 cr / 0 sw | **3 cr / 0 sw** |
| tier-free + mass-oriented (W12) | **7 cr / 0 sw** | 9 cr / 0 sw |

Every candidate reaches **zero swallowed edges on both fixtures** and crushes the
baseline. The residual spread is small.

## Design

### 1. `management` on an element — the one thing a human declares

`lab.elements` already exists as an **optional** list of `ElementRecord`
(`id`, `type`, `description`); elements not listed are derived from their member
hosts. Add one optional boolean:

```jsonc
"elements": [
  { "id": "ems-01",  "management": true },
  { "id": "jump-01", "management": true }
]
```

A maintainer lists **only** the management elements and touches nothing else.
This is the single piece of information no graph algorithm can recover, and Chris
has ruled it a fair ask.

**Default when absent (the adoption path).** An element with **zero data-plane
links** is treated as management. Jump hosts and EMS typically have no `declared`
links, so an untouched lab gets most of the benefit for free; the flag exists to
override the inference, not to enable the feature. `local` is always management.

### 2. `tier` — optional, never required

`ElementRecord` also gains an optional `tier: string`. When present it pins an
element's column, refining the automatic layering (isp-core: 7 → 4 crossings).
**Nothing is blocked on it.** Chris's goal was to eliminate the *need* for tiers,
and the measurements above meet it: the tier-free result already beats the
baseline by an order of magnitude.

### 3. The layout pipeline

What we have been hand-rolling is the **Sugiyama framework** for layered graph
drawing, and we implement roughly one and a half of its four phases. The gaps are
why problems keep being found by eye. The new pipeline, in order:

1. **Subtract management.** Management elements are removed from the data-plane
   graph and placed in their own leftmost band.
2. **Peel leaf services and dock them.** Iteratively strip data-plane degree-1
   elements and dock each into its attachment's column — *not* a column of its
   own. Guard: only peel when the neighbour's own remaining degree is ≥ 2, so a
   mutual pendant pair stays put. This reproduces automatically the insight that
   `hss`/`mme`/`pgw`/`sgw` are services *hanging off* the core, not a tier
   anything passes through — which a hand-written tier previously had to supply.
   **Do not peel degree-2 ring peers** (e.g. access nodes with an uplink plus a
   sibling ring link): measured, it collapses a real tier into the hub's column
   and regresses toward the baseline.
3. **Root on the spine cluster, not a single node.** Take the highest-degree
   cluster of the post-peel backbone as a single root *layer*, so mutually-linked
   spine peers (`core-01` + `core-02`) share a column instead of landing one hop
   apart.

   Concretely, per connected component: the root set is every backbone node whose
   degree is **≥ 75% of that component's own maximum degree**. A true k-core
   decomposition was tried and rejected — it degenerates to "coreness 2 for
   everyone" on both fixtures, which is no signal at all.
4. **Orient by subtree mass — but only when decisive.** The hierarchy's direction
   is not in the hop count, it is in the mass: behind the aggregation side of a
   core sit four aggs and eight access nodes; behind the border side sits almost
   nothing. Measure each root-neighbour's reachable subtree; heavy branches go
   **downstream** (right), light ones **upstream** (left).

   **The confidence guard is load-bearing for isp-core; it turned out to be
   inert for sprawl.** At design time the only evidence available was the
   prototype's own W11 → W12 comparison, where turning on unconditional
   mass-orientation helped isp-core (12 → 7) and regressed sprawl (3 → 9) —
   that comparison is why a guard was built at all, gating orientation on
   whether the mass difference is decisive and falling back to symmetric
   layering on a tie.

   **Verified after implementation (Task 7 review) that this is only half
   true for the shipped code.** Forcing `DECISIVE_RATIO = 1` — which makes
   the "decisive" check pass unconditionally — produces BYTE-IDENTICAL
   sprawl output. Sprawl's hub has two branches of exactly equal size, and
   the shipped sign rule only flips a branch upstream when its size is
   **strictly less than** the branch median; an exact tie sits *at* the
   median, never below it, so a tied branch never orients no matter what the
   guard decides. Sprawl was never at risk from the guard firing — it is
   protected by that strict inequality, not by the guard declining to act.
   Forcing `DECISIVE_RATIO = 4`, by contrast, *does* break isp-core (the
   `pe-*`-upstream invariant fails), so the guard is real and necessary
   there — just not for the fixture it was originally built to protect. Keep
   the guard; stop crediting it with sprawl's stability. See
   `web/src/topo/layout.ts`'s `DECISIVE_RATIO` docstring for the
   verification.

   **Resolution:** implemented and shipped with the guard (`DECISIVE_RATIO =
   2`); both fixtures' results were delivered, so the unoriented-rule (W11)
   fallback below was not needed.

5. **Barycentric row-sort**, on **data-plane links only** — management links must
   not drag the ordering around. Today `rowOrder` sorts by slot then id (i.e.
   alphabetically), which is why linked peers sit far apart and their links bow.
6. **Coordinate assignment** — the phase we simply never wrote. Give each node a
   y from the median y of its data-plane neighbours in adjacent columns, resolve
   overlaps to a minimum gap, sweep a few times, and **centre each column
   vertically** instead of top-aligning it. Today every column starts at `y = 0`,
   which is why `local` sits in the top-left corner with its edges raking
   down-and-right instead of radiating from the middle.

### 4. Management is drawn, faint, in its own band

Not hidden, not toggled. A pale management line crossing behind an element is
honest and unobtrusive, and hiding the management plane costs the user real
information. **Dimming alone is not enough for occlusion** (a pale edge still
passes *under* a node) — but occlusion of management edges does not matter, which
is exactly why the metric excludes them.

### 5. No layout dependency

| library | gzipped cost | deterministic | air-gap | best result (isp) |
| --- | --- | --- | --- | --- |
| `elkjs` | **+443 KB** | yes | **FAILS** — 16 non-allowlisted URL strings | 10 cr / 0 sw |
| `dagre` | +14 KB | yes | passes | **22 cr / 7 sw** — worst of all |
| **our own** | 0 | yes | passes | **4–7 cr / 0 sw** |

Neither earns its place. ELK is genuinely good but costs 443KB gzipped *and*
fails otto's air-gap check (it embeds EMF/Ecore namespace URIs — inert, but the
gate is the gate). Dagre is cheap and produced the worst layout of the lot: it has
no tier-pinning primitive, so it derives its own ranks and ignores the network's
semantics. **Writing the two missing Sugiyama phases ourselves beats both, for
about 150 lines and no dependency.**

D\* (push a hub's peers a column deeper) is **dead** — measured, it doubles
swallowed edges and destroys the x-axis's meaning. Obstacle-aware skip-column
routing is **no longer needed**: the new layout swallows nothing.

## Testing — the layout becomes measured, not eyeballed

This is the core of the change and the reason the prototype exists.

- **Both fixtures ship** as `web/fixtures/`: `sprawl` (deep: jump → gateway →
  chassis → line cards) and `isp-core` (short management paths, richly meshed
  data plane, EMS star). Adding them is a one-line change to `build_all()` — the
  enumeration retirement (`59a4ac4`) exists precisely to make this free.
  Lab-rich, metrics-sparse: a few hundred metric rows, not kitchen-sink's 13,724.
- **A measurement harness in vitest** computes `dp_crossings` and `dp_swallowed`
  from the real node rects and the real `routeEdge` paths, and asserts a **budget**
  per fixture. A regression that makes the map worse fails the build.
- The budgets are set from the measured results, with headroom — not from a guess.
- **`dp_swallowed` must be 0 on both fixtures.** An invisible edge is a bug, and
  the whole point of this design is that it no longer happens.

## Delivery order

The schema change is **not** a prerequisite — the zero-data-plane-links inference
(§1) means the layout works on an untouched lab. So the phases are independently
shippable, in this order:

1. **The test bed.** Ship both fixtures and the measurement harness *against the
   current layout*, with budgets set to today's measured numbers. This locks in
   the baseline and makes every later phase provable rather than asserted. On its
   own it changes no behaviour — it just starts telling the truth about the map.
2. **The layout pipeline** (§3, steps 1–6), with management *inferred*. This is
   where 75 → ~7 lives. Tighten the budgets from step 1 to the new numbers; the
   `dp_swallowed` budget becomes 0 and stays there.
3. **The `management` and `tier` fields** (§1, §2) as explicit overrides. A
   `format:1` change — schema, `export.gen.ts`, the generator, the fixtures, and
   the drift guards all move together. Deferrable without blocking phases 1–2.

If phase 2's confidence guard (§3.4) fails to deliver both fixtures' results,
ship the unoriented rule instead and record it — that is still 75 → 12 on isp-core
and 21 → 3 on sprawl.

## Explicitly out of scope

- Rendering a tunnel as an overlay riding its underlay links (still a `format:1`
  change: `Tunnel.path` is discarded on export).
- `onlyRenderVisibleElements`.
- Any change to `routeEdge`'s same-column bow, parallel-edge fan, or impair-pill
  anchoring — the layout change removes the pressure on all three.

## Known warts, not papered over

- **Leaf-docking is geometrically right but occasionally semantically odd.** On
  sprawl it docks both ToR switches into an app server's column. Fine to look at,
  backwards to read. A declared `tier` fixes it; so would a smarter dock target.
- **The confidence guard shipped and is load-bearing — for isp-core only.**
  Sprawl's protection was never the guard; it is the sign rule's strict
  less-than-median comparison. See §3.4's "Verified after implementation" note.
