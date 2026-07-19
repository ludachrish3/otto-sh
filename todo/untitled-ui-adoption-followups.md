# Untitled UI adoption — ship-and-note follow-ups

Recorded while adopting Untitled UI as the dashboard's component foundation
(`worktree-monitor-5b-followups`). Nothing here blocks the merge; item 1 was a
real, pre-existing bug, left in place on purpose at the time — since resolved
(see below).

1. **RESOLVED — Monitor Plan 5c, Task 11** (`docs/superpowers/plans/2026-07-18-monitor-event-marking.md`):
   `eventOverlay`'s markArea label now carries an explicit `color: theme.ink`,
   so span labels are themed/legible in dark mode. See that task's report for
   the regression pin. Original note kept below for context.

   **A real, pre-existing dark-mode chart bug — found by the visual gate,
   deliberately NOT fixed here.** A SPAN event's ECharts `markArea` label
   renders illegible (white-on-white) in dark mode — worst where two spans
   overlap: the `kitchen-sink.json` fixture's "stress run" (09:25–09:35) and
   "log capture" (09:30–09:40) spans both cover 09:30–09:35, so their labels
   double up on screen. Reproduce: open any host's drill-in in dark mode
   against that fixture. Proven pre-existing, not a migration regression:
   `chartTheme()` in `web/src/charts/options.ts` hard-codes hex colors
   (never reads the CSS vars this branch introduced), the `markArea` built by
   `eventOverlay` in the same file carries no `label` config of its own — only
   the sibling `markLine` does (`theme.muted`) — so it inherits ECharts'
   default label styling instead, and this branch never touched
   `options.ts`. Fixing it here would have folded an unrelated bug fix into
   an already large migration diff, making it unreviewable. Leave it for a
   dedicated pass over `options.ts`'s dark-mode support.

2. **Untitled UI's `Tag` drops `textValue`.** Its prop destructuring has no
   `...rest` capture, so an explicit `textValue` prop is silently discarded;
   the component only derives one itself, and only when `children` is a
   plain string. Non-string children (the app's chip labels wrap in a `<span
   data-testid=...>`, see item 3) trigger a harmless dev-only console warning
   ("A `textValue` prop is required..."). Unfixable short of hand-editing the
   vendored source — see `web/src/pages/SeriesPanel.tsx`'s header comment for
   the full reasoning already recorded there.

3. **Vendored components forward props inconsistently — worth knowing before
   the next migration.** `Badge` drops `data-testid` (and anything else
   outside its narrow destructure) entirely; nothing reaches the DOM, not
   even a wrapper. `Checkbox` spreads its rest props onto react-aria's
   `<label>`, so a `data-testid` lands on the label, not the input. `Tag`
   drops unrecognized props the same way `Badge` does, *and* — because it
   lives inside `TagGroup`/`TagList`'s collection scanner — can't be
   testid-wrapped from outside either (a host element between `TagList` and
   `Tag` makes the scanner drop the item; verified: renders an empty
   `role="grid"`, zero tags). The app's workaround where it matters is a
   `<span data-testid=...>` around the `children` instead, which is why some
   testids live on an inner span rather than the component's own root.

4. **`tsconfig.json`'s `noUnusedLocals`/`noUnusedParameters` are now OFF.**
   `tsc` typechecks every imported file regardless of `exclude`, so a
   vendored file's unused import can't be scoped out individually — keeping
   both on failed the build over code we don't own. Biome enforces the
   equivalent (`correctness.noUnusedVariables` / `noUnusedImports`, both
   `"error"`) instead, so unused-code enforcement over *authored* code is
   unchanged; see `web/biome.json` and commit `c6e54ba`.

## Found by the final whole-branch review (not fixed here)

- **A comparison guard passes NaN.** `setRange`'s `from >= to` refusal is the
  single boundary that keeps an inverted range out of the store — but
  `NaN >= NaN` is `false`, so a NaN range would pass straight through. Reachable
  only if a malformed (non-ISO) timestamp reaches `parseTs` — i.e. if otto's own
  format:1 producer emits one, at which point `startMs`/`endMs`/the index all
  degrade together. The fix belongs in **wire validation**, not in this guard.
  Pre-existing; neither introduced nor worsened by the range work.

- **RESOLVED — Monitor Plan 5c, Task 10** (`docs/superpowers/plans/2026-07-18-monitor-event-marking.md`):
  `jump()` now shows a "Outside the session's time range" notice and stays
  open instead of closing on the no-op. Original note kept below for context.

  **`EventsPanel.jump()` closes the panel even when the store refuses the jump.**
  Now that `setRange` rejects an inverted range, a jump whose ±15min padding
  falls entirely outside the session bounds silently no-ops: the panel closes and
  nothing moves. Requires an event timestamp outside `[startMs, endMs]`, which
  `applyFragment` permits because it advances `endMs` from a fragment's *metrics*
  only, never its *events*. Strictly better than the old behaviour (which blanked
  the dashboard), but it deserves a signal — skip the close, or surface a toast.
