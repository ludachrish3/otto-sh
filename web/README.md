# Web Frontend

## Third-party

Topology canvas: [React Flow](https://reactflow.dev) (@xyflow/react, MIT) — attribution panel disabled for the air-gap; credited here instead.

## Vendored source (Untitled UI) — the boundary and the never-hand-edit rule

The following paths are **copy-in vendored source** from [Untitled UI](https://www.untitledui.com/react)'s free tier, placed via the `untitledui` CLI (`npx untitledui@latest add/init ...`):

- `src/components/**`
- `src/styles/**` (currently `theme.css`)
- `src/utils/cx.ts`
- `src/utils/is-react-component.ts` (a shared helper the button/badge/button-group components import)
- `src/hooks/use-breakpoint.ts`
- `src/hooks/use-resize-observer.ts` (a shared helper the select/combobox components import)

**Never hand-edit these files. No exceptions.** Untitled UI ships as copy-in
source — no version, no manifest, no lockfile entry — so Dependabot cannot
track it the way it tracks the ordinary npm packages Untitled UI pulls in
(`.github/dependabot.yml`'s `npm//web` entry). The only substitute is an
upstream-drift check that re-vendors with a pinned CLI version and diffs the
result against this tree (`scripts/check_untitledui_drift.sh`,
`web/untitledui.lock.json`). That check only works if the vendored tree is
**byte-identical** to what the CLI emits — a single hand-edit (e.g. renaming a
class, reformatting) destroys that property permanently and silently, because
nothing else notices until the next drift check produces a false positive.

If a vendored file collides with something ours, reconcile the collision on
**our** side, not theirs. For example, `theme.css` gates its dark tokens on a
`.dark-mode` class on `<html>`; rather than shadow it with a second class of
our own, `web/src/theme.ts`'s `applyTheme()` toggles `.dark-mode` only, and
`app.css`'s `@custom-variant dark` points at that same class — one class, one
name, no coupling nobody pays for. Anything we author instead lives in
`web/src/ui/**`, which is not vendored and is fully lint/format/coverage-gated.

**Why this lives here and not as a comment in `web/biome.json`:** the vendored
paths above are excluded from Biome's format/lint checks (`biome.json`'s
`files.includes`) and from vitest's coverage measurement
(`vite.config.ts`'s `test.coverage.exclude`) — formatting or coverage-gating
copy-in source we don't own would itself be a hand-edit pressure, and Biome's
config is strict JSON (no comments), so the rationale can't live inline next
to the exclusion list. This section is that rationale; if you're looking at
either exclusion list wondering why a path is there, this is where the answer
lives.

**Why `tsconfig.json` doesn't set `noUnusedLocals`/`noUnusedParameters`:**
`tsc` has no per-directory compiler-option override within a single project
(project references solve this but would split the one flat `tsc --noEmit`
this repo relies on into a multi-project build, for no gain here), and
`noUnusedLocals`/`noUnusedParameters` diagnostics are reported for every file
`tsc` actually type-checks — including a vendored file the moment anything
imports it — regardless of `include`/`exclude`. Untitled UI's own vendored
source is not written to satisfy that flag (e.g. `components/base/buttons/button.tsx`
imports `React` for side-effect/type reasons it never reads as a bare
identifier under the `react-jsx` runtime), and it cannot be hand-edited to
fix it. Biome already draws this exact line for lint/format — its
`noUnusedVariables`/`noUnusedImports`/`noUnusedFunctionParameters` rules are
all in its `recommended` preset, and `files.includes` above already keeps
Biome off every vendored path — so those two `tsc` flags are switched off
project-wide and Biome is the sole enforcer of "no unused locals/params" for
code we own. If you vendor a new component and `npm run typecheck` complains
about something inside `src/components/**` (or another vendored path) that
looks like an unused-variable-shaped complaint, this is why — don't turn the
flags back on; that would just make the next `untitledui add` un-vendorable
without a hand-edit.
