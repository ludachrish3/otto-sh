# Monitor topology — ship-and-note follow-ups

From the final whole-branch review of `worktree-monitor-topology`
(`2026-07-11-monitor-topology-design.md`, 2026-07-11). Branch verdict: ready
to merge; none of these block it.

## Top item

1. **LinkInspector occlusion at ≤1280 px width.** The NON-MODAL choice is what
   keeps the review bar interactive; the aside's `fixed inset-y-0` full-height
   geometry is the remaining problem — at narrow widths it visually covers the
   review bar's Apply control. Change to `top-[6.5rem] bottom-0` (matches the
   page's chrome offset) so the review bar stays clickable. Batch with items
   2–3 below — all touch `LinkInspector`/`TopologyPage`, one dashboard-lane
   re-run covers them.

## Mechanical follow-ups

2. Link-less edges (intra `hop:*`, reports-for) open a degenerate inspector
   (raw id title, no fact rows) — exclude from selection or render a
   one-line summary.
3. Escape listener fires whenever topology is mounted (guard
   `if (edge === null) return;` in the effect); React Flow's edge `selected`
   stroke can diverge from the inspector's viewKey state — unify if
   selection grows semantics.
4. Retire the fixture-stem enumeration class: derive the two Python stem
   lists and the exportdoc fixture list from one source (`build_all()` keys /
   fixtures glob). Three near-misses this phase.
5. MiniMap (+ `onlyRenderVisibleElements`) when labs outgrow tens of nodes —
   deferred from the spec with Chris's veto invited.
6. Cosmetics: dangling "unreachable · " separator in `HostNode` when no
   badge/board; export `pairKey` from `topology.ts` (`TopologyPage`
   duplicates the sort-join twice); `--topo-edge-implicit`/
   `--topo-edge-reports` could alias.
