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

5. **Static-link layering ("D\*").** Push a hub's peers one column deeper so
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
   The static-only variant is 1 and 1.

6. **Tunnels as overlays on their underlay links.** `Tunnel` already carries its
   ordered chain (`path: tuple[TunnelHop, ...]`, `src/otto/tunnel/model.py`) but
   the monitor export **discards it** — a tunnel is flattened to a two-endpoint
   `LinkSnapshot` with `provenance: "dynamic"`. Rendering a tunnel riding the
   links it traverses needs that path exported (a `format:1` change: schema,
   `export.gen.ts`, generator, fixtures, drift guards), then a rule mapping each
   consecutive hop-pair onto an existing link and drawing a bare segment where
   none exists. Note it would not change kitchen-sink at all: `tun-demo` runs
   edge-gw ⇄ db-01, between which no declared or implicit link exists — there is
   nothing to wrap. Exercising it needs a multi-hop tunnel in the fixture. The
   grey casing (shipped) is the cheap stand-in.

7. **Obstacle-aware routing for skip-column cross-depth edges.** `routeCrossColumn`
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
   skip-column case before deciding on layering risks solving the wrong shape.

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
