# Monitor topology — ship-and-note follow-ups

From the final whole-branch review of `worktree-monitor-topology`
(`2026-07-11-monitor-topology-design.md`, 2026-07-11). Branch verdict: ready
to merge; none of these block it.

## Mechanical follow-ups

(Items 1, 2 and 4 shipped 2026-07-12; items 5–7 of the legend + routing
deferrals died or shipped by 2026-07-16 — pruned 2026-07-25. Original numbers
kept for the survivors.)

3. `onlyRenderVisibleElements` is deliberately **not** shipped alongside the
   minimap toggle: it culls off-screen elements from the DOM entirely, and
   the dashboard e2e counts edges on a canvas that already withholds them
   until both endpoint nodes are measured — the exact mechanism behind the
   #130 webkit flake. Turning on DOM culling on top of that needs its own
   justification first (measure whether React Flow is actually slow at the
   node/edge counts we actually hit — kitchen-sink is small) and its own test
   strategy for not reintroducing a #130-shaped race into the e2e suite.

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
