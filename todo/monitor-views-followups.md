# Monitor views (Plan 3) — ship-and-note follow-ups

From the final whole-branch review of `worktree-monitor-ui-scaffold` (Plan 3, `2579fc0..babbb38`,
2026-07-11). Branch verdict: ready to merge; none of these block it.

## Needs a design decision (top item)

1. **Slot ≥ 8 series have no palette color — `% length` can cycle.**
   `web/src/charts/options.ts` colors series with `theme.series[s.slot % theme.series.length]`.
   With a 9+-series chart (element with many members), unchecking a low slot lets slot 8 render
   in slot 0's color while slot 0 is still visible — two identical-color lines, violating the
   plan's "never cycled" rule. Unreachable with the committed fixtures; reachable with real
   imports. Options: render only `slot < MAX_SERIES_PER_CHART` and reword the overflow notice,
   or define an explicit overflow treatment. The overflow notice overcount (counts checked
   series with zero in-range points as "shown") lives in the same code path — fix together.

## Mechanical follow-ups

2. `elementRollup(element, healths, session?)` — make `session` required (`web/src/data/health.ts`);
   omitting it silently degrades to id-order. Only caller already passes it.
3. Export ordered member ids from `health.ts` and reuse in `OverviewPage` — the slot-then-id sort
   is duplicated inline there; a desync-by-edit would misalign rollup segments vs tiles silently.
4. Extract a `displayName(s: SeriesNode)` helper — the `s.key === s.label ? s.label : s.host`
   ternary is duplicated in `SeriesPanel.tsx` and `SubjectPage.tsx`.
5. Extract an `evalWindow(session, range)` helper — the from/to clamp is duplicated in
   `healthForHosts` and `headlineFor`.
6. Strengthen the `filterTree` slot-preservation test with a synthetic multi-series chart —
   today's fixture charts have one series each, so a re-index-from-0 regression would pass.
7. One-line comment in `collectSeriesPoints` on the `"host|label"` separator: safe only because
   host ids come from `slug()` (strips `|`); a future non-slugged id source would silently merge series.
8. `SubjectPage` calls `filterTree` twice per render with identical args — pass the `filtered`
   local to `SeriesPanel`.
9. All-series-unchecked leaves an empty chart stack with no message — extend the empty-state
   condition to "no rendered charts" (UX §13 intent).
10. Hoist ONE `CSS.escape` polyfill into a vitest `setupFiles` and delete the three per-file copies
    (`ui.test.tsx`, `events_panel.test.tsx` variant, +1).

## Live-hookup notes (not actionable in review mode)

- Memoize chart options in `SubjectPage` — today every keystroke re-runs `buildStackOption` and
  `setOption(notMerge)` on all visible charts; fine with `animation:false`, wrong at 1 Hz ticks.
  (Also the reason ChartPanel's groupId-change repaint gap is currently moot.)
- Index metrics by host — `healthForHosts`/`headlineFor` are O(hosts × charts × metrics) per render.
- `EventsPanel` maps a `source` field it never renders (the §11 source column) and its
  "No events" state is unreachable while the AppBar hides the button at zero events.
