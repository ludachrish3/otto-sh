# Monitor topology — ship-and-note follow-ups

From the final whole-branch review of `worktree-monitor-topology`
(`2026-07-11-monitor-topology-design.md`, 2026-07-11). Branch verdict: ready
to merge; none of these block it.

## Mechanical follow-ups

1. ~~Retire the fixture-stem enumeration class: derive the two Python stem
   lists and the exportdoc fixture list from one source (`build_all()` keys /
   fixtures glob). Three near-misses this phase.~~ Shipped in Task 1
   (2026-07-12): `build_all()` in `scripts/gen_monitor_fixtures.py` is now the
   single source; the drift guard parametrizes from it and `exportdoc.test.ts`
   reads the fixtures directory.
2. ~~MiniMap~~ shipped as an opt-in toggle, default off (Task 4, 2026-07-12).
3. `onlyRenderVisibleElements` is deliberately **not** shipped alongside the
   minimap toggle: it culls off-screen elements from the DOM entirely, and
   the dashboard e2e counts edges on a canvas that already withholds them
   until both endpoint nodes are measured — the exact mechanism behind the
   #130 webkit flake. Turning on DOM culling on top of that needs its own
   justification first (measure whether React Flow is actually slow at the
   node/edge counts we actually hit — kitchen-sink is small) and its own test
   strategy for not reintroducing a #130-shaped race into the e2e suite.
4. ~~Cosmetics: dangling "unreachable · " separator in `HostNode` when no
   badge/board; export `pairKey` from `topology.ts` (`TopologyPage`
   duplicates the sort-join twice).~~ Shipped in Task 2 (2026-07-12):
   `HostNode`'s detail line now builds parts and joins them instead of
   concatenating a fixed separator, and `pairKey` is exported from
   `topology.ts`.

## Deferred from the legend + routing spec (2026-07-12)

5. ~~**Static-link layering ("D\*").** Push a hub's peers one column deeper so
   same-column links become forward links — Chris's idea, and it makes db-01's
   peers read as downstream. Layer on **declared + implicit links only**: if a
   *dynamic* link gets a vote, a tunnel coming up shoves edge-gw and chassis-a a
   whole column sideways and the map reflows when the network changes. Needs its
   own design: cycle-safe layer assignment, routing for the skip-column edges it
   creates (`local` is attached to every `hop == null` element, so pushing any of
   them past column 1 *forces* a local edge to skip a column), and a decision on
   redefining the x-axis away from "hops from local" — under D\*, `workers` is 1
   hop from local but sits 2 columns out. Measured on kitchen-sink: layering on
   ALL links takes same-column edges 2 → 0 but swallowed edges 0 → 3. Net worse.
   The static-only variant is 1 and 1.~~ **DEAD (2026-07-14).** Superseded by
   the topology layout redesign (`docs/superpowers/specs/2026-07-14-topology-
   layout-redesign-design.md`): the data plane is now layered by its own
   structure (peel+dock, root on the spine cluster, orient by subtree mass),
   not pushed a column deeper off a hub. Measured directly against the real
   layered layout, D\* is a net regression, not just unnecessary: it **doubles
   swallowed edges** and **destroys the x-axis's meaning** the new layout just
   established (border → core → aggregation → access reads left-to-right
   *because* column index is now a real hierarchy signal; D\* would reintroduce
   an arbitrary hub-relative shove on top of that, breaking the axis's honesty
   for the same reason the design doc rejected raw hop-distance in the first
   place). Not revisiting without a new, different problem to solve.

6. ~~**Obstacle-aware routing for skip-column cross-depth edges.** `routeCrossColumn`
   anchors face-to-face between its two endpoints with no awareness of columns it
   passes over; a link that skips a column can still be swallowed by a node in an
   overlapping row band in the column it crosses. Trigger: an element at depth >= 3
   reached via a 2-long hop chain, with a declared or dynamic link to an element >= 2
   columns away. This is **pre-existing**, not introduced by the legend + routing
   work — `routeSameColumn`'s occlusion fix never touched `routeCrossColumn`. It is
   also not caught by anything in CI today: `kitchen-sink` has no element deep enough
   to produce a skip-column edge, so a new fixture with a deeper hop chain is needed
   before this can even be demonstrated, let alone regression-tested. Design this
   together with item 5 (static-link layering) — D\* changes which nodes sit at which
   depth, and therefore which edges skip a column in the first place; routing the
   skip-column case before deciding on layering risks solving the wrong shape.~~
   **NO LONGER NEEDED (2026-07-14).** The topology layout redesign measures
   `dp_swallowed` — a data-plane edge passing under any non-endpoint node,
   skip-column or not — as a hard budget on real fixtures (`isp-core.json`,
   `sprawl.json`), asserted at exactly 0 on both
   (`tests/e2e/monitor/dashboard/test_topology_budget.py`). The new layout
   swallows nothing to begin with, so there is no obstacle-aware routing left
   to design; the premise (item 5's D\* creating skip-column edges that need
   special routing) is also gone now that item 5 is dead. Revisit only if a
   future layout change reintroduces a swallowed skip-column edge — the budget
   test will say so.

7. ~~**Tunnels as overlays on their underlay links.**~~
   **SHIPPED (2026-07-16).** The full overlay landed with the dynamic-tunnels
   feature (spec: `docs/superpowers/specs/2026-07-16-dynamic-tunnels-topology-design.md`):
   `TunnelRecord` carries the ordered hop path on format:1 (`SessionRecord.tunnels`,
   replace-semantics fragments), the collector discovers live tunnels on the
   collection interval across the whole lab, and the web maps each consecutive
   hop-pair onto its underlay link (riding segments reproduce the underlay's
   `routeEdge` geometry exactly) or draws a bare segment where none exists —
   with ok/degraded/uncertain styling, whole-tunnel selection, and an inspector
   block. `"dynamic"` left `LinkSnapshot.provenance` entirely; sprawl's 3-hop
   fixture tunnel exercises the riding case, kitchen-sink's `tun-demo` pins the
   bare-segment fallback.

## New from the topology layout redesign (2026-07-14)

8. **`routeEdge`'s parallel-edge fan uses a FIXED pixel offset.** `CROSS_FAN`/
   `ADJACENT_FAN` (`web/src/topo/routing.ts`) spread parallel edges apart by a
   constant perpendicular offset, which assumed the uniform `row * ROW_H` grid
   the old layout used. Coordinate assignment (Task 6) made row spacing
   non-uniform — nodes are pulled toward the median y of their data-plane
   neighbours, not evenly spaced — and the fixed fan doesn't adapt to that:
   measured, it costs ~1 crossing on isp-core (Task 6 report, "Finding 1": 4 →
   5, a near-miss flipping because the sampled curve geometry shifted once y
   stopped being a rigid grid). Make the fan's offset a function of the actual
   row gap around each edge instead of a constant.
9. **Explicit `management` / `tier` fields on `ElementRecord`** (design doc
   §1/§2) remain unbuilt — phase 3, deliberately deferred. A `format:1`
   change: schema, `export.gen.ts`, the generator, the fixtures, and the
   drift guards all move together. **Not required** — the zero-declared-links
   + is-a-hop-or-source inference already delivers the win (management
   partitioning and the data-plane layering both work on an untouched lab) —
   but an explicit field would let a maintainer override the inference where
   it guesses wrong (e.g. an element with a declared link that is still,
   organisationally, management).
10. **Leaf-docking is geometrically right but occasionally semantically odd.**
    On sprawl it docks both ToR switches (`tor-sw-a`, `tor-sw-b`) into
    `app-01`'s column via a chained peel
    (`zephyr-01`→`tor-sw-b`→`tor-sw-a`→`app-01`) — fine to look at (0
    `dp_swallowed`), backwards to read (switches are infrastructure, not
    services hanging off an app server). A declared `tier` (item 9) would fix
    it; so would a smarter dock target that weights a leaf's own type/degree
    rather than pure peel order. Known and accepted at design time (design
    doc, "Known warts"); recorded here as a live follow-up, not new.
11. **Hostless CI gates skip `make web-check`, so a pure-web break can sit on
    main indefinitely with every job green.** This branch found it directly:
    `main` had ten Biome "sort these imports" errors in files this branch
    never touched (live-streaming code), invisible to every hostless gate
    because none of them build or lint the web bundle — fixed in
    `9f5f49a` (`style(web): sort imports in the live-streaming files to
    unbreak the Biome gate`). Same family as the stale-web-dist trap
    (#131, #132): a gate that isn't run is not a gate. No fix proposed here
    beyond naming it — worth a follow-up on wiring `make web-check` (or at
    least `npm run check`) into whichever gate actually runs on every PR.
12. **A pure chain lab renders as a vertical stack.** On a daisy-chain
    (r1–r2–r3–r4 in series), the ≥75%-of-max-degree root rule makes every
    interior node "spine", so the chain collapses into one column with its
    ends in the next. It is sane (the same-column bias orders it, bows clear
    the boxes, it terminates) but it reads as a stack rather than a
    left-to-right run, and real labs have this shape. Neither `isp-core.json`
    nor `sprawl.json` exercises it — needs its own fixture to demonstrate and
    design against.

## Residue notes (2026-07-12)

- ~~`h-[calc(100vh-6.5rem)]` on `TopologyPage`'s `<main>`~~ fixed in `e3116a0`
  (shell is now `flex min-h-screen flex-col`; the topology `<main>` is
  `min-h-0 flex-1`). Task 4 added a committed regression guard for it
  (`test_topology_page_does_not_scroll`, forces a 1100px-wide viewport since
  the wrap only reproduces below ~1150px — Playwright's default 1280x720
  never triggers it on either fixture).
- ~~`LinkInspector`'s `onClose` re-subscribes the Escape listener every
  render.~~ Fixed in Task 2 (2026-07-12): `onClose` is now wrapped in
  `useCallback` in `TopologyPage`, so the Escape keydown effect's identity is
  stable and stops tearing down/re-subscribing on every render.
