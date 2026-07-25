# Untitled UI adoption — ship-and-note follow-ups

Recorded while adopting Untitled UI as the dashboard's component foundation
(`worktree-monitor-5b-followups`). Nothing here blocks the merge. (The two
resolved entries — the dark-mode markArea label bug and `EventsPanel.jump()`'s
silent close — were fixed by Monitor Plan 5c Tasks 11 and 10; pruned
2026-07-25.)

1. **Untitled UI's `Tag` drops `textValue`.** Its prop destructuring has no
   `...rest` capture, so an explicit `textValue` prop is silently discarded;
   the component only derives one itself, and only when `children` is a
   plain string. Non-string children (the app's chip labels wrap in a `<span
   data-testid=...>`, see item 2) trigger a harmless dev-only console warning
   ("A `textValue` prop is required..."). Unfixable short of hand-editing the
   vendored source — see `web/src/pages/SeriesPanel.tsx`'s header comment for
   the full reasoning already recorded there.

2. **Vendored components forward props inconsistently — worth knowing before
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

3. **`tsconfig.json`'s `noUnusedLocals`/`noUnusedParameters` are now OFF.**
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
