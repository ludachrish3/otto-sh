# TypeScript/vitest strictness — design

**Date:** 2026-07-27
**Status:** Approved design, pending implementation plan
**Origin:** "Is there a way to ratchet up the strictness of the vitest harness?
Or does it already have all levels of checking already turned on? I'd like to
eventually get to the same place as the Python code where linting and other
quality metrics are turned as high as is feasible with targeted exceptions."

The Python tree runs ruff `select = ["ALL"]` with a curated, commented ignore
list, `ty` at `all = "error"`, pyright `strict`, pytest `filterwarnings =
["error"]`, and randomized test order via pytest-randomly. The `web/` tree runs
Biome's `recommended` preset, `tsc --strict` with two strictness flags
explicitly disabled, and a vitest config with no isolation or ordering
settings at all. This closes that gap, in the same everything-on-with-targeted-
exceptions shape.

The audit found one defect of the same class as the bug that prompted the
question: **`biome check .` exits 0 today with 7 outstanding warnings.** A
console warning nobody fixes is what this whole workstream is about.

## Guiding principle

**The goal is better code — more readable, harder to break. The tools' numbers
are a way to find where to look. They are not the target.**

Chris, 2026-07-28, stated emphatically. It governs every decision in this
document and overrides anything below that appears to conflict with it.

What that means in practice, because a principle nobody can act on is just a
sentiment:

- **The test is whether the signal stays visible, not whether the diagnostic
  disappears.** Adding `!` to satisfy `noUncheckedIndexedAccess`, an `as` cast
  to satisfy `exactOptionalPropertyTypes`, an `_`-prefixed rename, or an
  arbitrary function split to get under a line limit are failures even though
  the count drops: each converts a visible signal into code that reads as
  ordinary, leaving the tree worse than before the flag was turned on.
- **A localized ignore annotation is a legitimate tool, used judiciously**
  (Chris, 2026-07-28). A genuinely nasty site may be annotated with the rule
  name and a real reason and revisited later. That is *not* the same as a bare
  assertion: the annotation is greppable, so the deferral stays findable and
  the debt stays countable. Prefer an annotated deferral over a bad fix.
- **Global rule-off entries are for stylistic disagreement** — rules we do not
  want anywhere, the way `.ruff.toml`'s ignore block records 30-odd reviewed
  decisions. A global off is the wrong tool for dodging a handful of hard
  sites.
- **A tier may end with a flag NOT adopted.** If working through
  `noUncheckedIndexedAccess`'s 441 sites shows that most fixes are noise rather
  than real defect-prevention, the honest conclusion is to decline the flag and
  record why — not to grind out 441 assertions.
- **Every rule decision is justified by its bug class or readability gain**, and
  that justification goes in the comment next to it. "Reduces the count" is not
  a justification.
- **The review question for each task** is "is this code better to read and
  harder to break than before?" — not "did the number reach zero?"

## Goals

- Fewer bugs and more readable code in `web/`. Everything below is in service
  of that; see the guiding principle above.
- Every quality gate that *can* fail on a defect *does* — no warn tier that
  silently accumulates.
- Exceptions are curated, commented at the point of exemption, and visible in
  diff review, exactly as `.ruff.toml`'s ignore block is.
- Each tier lands **complete** — flag on, every violation fixed — rather than
  being parked behind a budget file. The tier sequence is the staging; no
  ratchet machinery is built, because a cap is a place for work to sit
  indefinitely and this workstream is meant to finish.
- No `ci.yml` change: gates wire into `make check-ts` / `make coverage-ts-unit`,
  which `tests/unit/test_ci_web_gate.py` pins CI to invoke.

## Non-goals

- Vendored Untitled UI source (`web/src/components/**`, `web/src/styles/**`,
  and the four vendored `utils`/`hooks` files). It stays excluded from Biome,
  knip, and coverage. Not ours to lint; see `web/README.md`.
- `skipLibCheck: false`. It measures 0 errors today and is still declined —
  see "Deliberate exceptions" below.
- A relaxed test tier. `.ruff.toml` has a substantial `tests/**` per-file-ignore
  block; the TS side deliberately starts with **no** `overrides` entry and adds
  exemptions only when a specific rule proves noisy in test code, one commented
  entry at a time.
- Per-file coverage floors on the unit leg. Structurally wrong; see Tier 6.
- A budget/ratchet mechanism for the expensive tsc flags. An earlier draft
  proposed `scripts/check_ts_strict_budget.py` modelled on
  `scripts/import_budget.py`. Rejected, in Chris's words (2026-07-28): *"I
  don't want to get too comfortable with otto's code growing defects that are
  difficult to track."* A cap is a place for our own defects to accumulate
  under a green gate — the same shape as the `ACCEPTED` allow-list this
  workstream started by emptying. The import budget guards a quantity that
  legitimately grows with features; strictness violations are not that.

  Filtering **vendored** diagnostics out of tsc is categorically different and
  IS approved (see Tier 4): *"vendored code is what it is and is out of our
  control."* One mechanism tolerates defects we can fix; the other excludes
  code we are forbidden to touch.

## Tool ownership

Chris asked (2026-07-28) whether these tools are complementary like ruff and ty,
or whether we are accidentally running two tools at the same goal. Measured by
running one probe file through both: **mostly complementary, with one family of
exact duplicates and one class of direct contradiction.**

| Defect class | tsc | Biome | Verdict |
| ---- | ---- | ---- | ---- |
| unused import / local / param | `TS6133` | 3 named rules | exact duplicate |
| index-signature read via dot | `TS4111` | — | tsc only |
| declared property via bracket | — | `useLiteralKeys` | contradicts `TS4111` |
| implicit return | `TS7030` | — | tsc only |
| non-null assertion | — | `noNonNullAssertion` | Biome only |
| always-true condition | — | `noUnnecessaryConditions` (`types` domain) | Biome only |
| floating promise | — | `noFloatingPromises` (nursery) | neither, today |

The seam is that Biome 2.x deliberately moves into type-aware territory with its
`types` domain — the move ruff avoided by leaving inference to ty. Those rules
are not duplicates of tsc (they fire where tsc is silent), but they need the
same type graph, so they are slow and they can disagree with tsc.

### The policy

1. **tsc owns type correctness** — anything objectively right or wrong given the
   types.
2. **Biome owns style, idiom, and local bug patterns**, including the
   unused-code family: it names the three cases separately where tsc emits one
   `TS6133` for all of them.
3. **knip owns cross-module reachability** — unused exports, files, dependencies.
   No overlap with Biome's within-file unused checks.
4. **On a contradiction, the tool carrying type information wins.** The losing
   rule is turned off with a comment naming its counterpart. `useLiteralKeys`
   vs `noPropertyAccessFromIndexSignature` was decided this way.
5. **A Biome rule that merely restates a tsc flag is turned off**, unless the
   duplication is deliberate AND documented at both ends.

Rule 5 cuts both ways, and it cost a reversal: `noUnusedLocals` and
`noUnusedParameters` were enabled in Tier 4 and then removed under this policy.
Their stated justification — "different gate legs, so a misconfiguration of one
cannot silently drop the check" — did not survive scrutiny, since both legs run
inside `make check-ts`. Autofix does not favour tsc either: Biome's fixes for
this family are classified `Unsafe`, so `biome check --write` does not apply
them. Coverage after removal was verified, not assumed — a file with an unused
import, local and parameter leaves tsc silent and fails Biome's gate on all
three rules.

Rule 4 is the one that matters for Tier 5, which turns on 53 more rules. A
contradiction is worse than a duplicate: the gate can be green while the two
tools want opposite code, and whichever runs with autofix last wins.

## Measured baseline

Every number below was measured against the tree at `477f0da7`, not estimated.
A future session should not need to re-measure to plan the burn-down.

### tsc flags (`npx tsc -p tsconfig.json --<flag>`)

| Flag | Errors | Tier |
| ---- | ---- | ---- |
| `noImplicitOverride` | 0 | 1 |
| `noImplicitReturns` | 0 | 1 |
| `verbatimModuleSyntax` | 0 | 1 |
| `noUnusedParameters` | 0 | 1 (currently explicitly `false`) |
| `noUnusedLocals` | 1 (all vendored; 0 ours) | 4, then free |
| `erasableSyntaxOnly` | 2 | 1 |
| `skipLibCheck: false` | 0 | declined — see below |
| `noPropertyAccessFromIndexSignature` | 77 (0 vendored) | 4 |
| `exactOptionalPropertyTypes` | 100 (77 vendored; 23 ours) | 4 |
| `noUncheckedIndexedAccess` | 452 (11 vendored; 441 ours) | 4 |

### vitest knobs (full suite, 928 tests)

| Knob | Result |
| ---- | ---- |
| `restoreMocks: true` | 928 passed |
| `unstubEnvs: true` | 928 passed |
| `unstubGlobals: true` | 928 passed |
| `expect: { requireAssertions: true }` | 928 passed |
| `sequence: { shuffle: true }` | **2 failed** |
| `coverage.thresholds.perFile: true` | **17 files fail** |

`requireAssertions` passing outright means all 928 existing tests already
assert. The two shuffle failures reproduce across seeds 11/22/33 (seed 22
surfaces only the first):

- `src/covapp/pages/FilePage.test.tsx > FilePage > shows a minimal loading
  state before the chunk resolves` — a synchronous test that starts a chunk
  load resolving after its body returns, so React applies the state update
  outside `act()`.
- `src/__tests__/clock.test.tsx > useNow > advances at the collection interval,
  not faster`.

### Biome

`biome check .` exits **0** with 7 `lint/style/noNonNullAssertion` warnings in
`src/covapp/tickets.test.ts`.

`"preset": "all"` measured two ways, because the difference is the whole
design decision:

| Configuration | Diagnostics |
| ---- | ---- |
| `preset: "all"`, domains unscoped | 4,473 |
| `preset: "all"`, irrelevant framework domains `"none"` | 3,757 |

The 716-diagnostic delta is noise from frameworks this project does not use,
chiefly `noSolidDestructuredProps` (124), `useQwikValidLexicalScope` (81),
`useSolidForComponent` (46), and `noReactSpecificProps` (455), the last of
which flags every `className` in the tree. Domain scoping is mandatory, not
cosmetic.

Of the remaining 3,757, roughly 2,900 sit in ten rules. Eight are opinionated
style rules that `.ruff.toml`'s philosophy would ignore anyway:
`useImportExtensions` (523, and actively wrong for a bundler),
`noMagicNumbers` (442), `useBlockStatements` (440), `useNamingConvention`
(404), `noTernary` (308), `useGlobalThis` (299), `noJsxLiterals` (199), and
`noJsxPropsBind` (106). The other two — `noExcessiveLinesPerFunction` (117)
and `noEmptyBlockStatements` (90) — are high-count but worth keeping ON, and
are called out here so curation does not sweep them up by count alone.

With those eight rules disabled, **1,026 diagnostics across 53 distinct rules**
remain — the real size of Tier 5, and roughly 3x the first draft's estimate.
The per-rule breakdown is in Tier 5 below.

Biome 2.5.5 also exposes four unused domains worth enabling: `test` (vitest
rules), `types` (type-inference-backed rules — the closest Biome analogue to
`ty`), `react` (currently `recommended`, not `all`), and `tailwind`.

## Approach

Seven tiers (0–6), landing in order. Tiers 0–2 are one commit; each later tier
is its own.

### Tier 0 — stop the leaks

`make lint-ts` gains `--error-on-warnings`, so Biome's warn tier fails the
gate. The 7 `noNonNullAssertion` warnings in `src/covapp/tickets.test.ts` are
fixed, not exempted.

`vitest.setup.ts`'s `ACCEPTED` list stays empty (emptied in `477f0da7`), and
its comment already records why an entry there is a last resort.

### Tier 1 — free tsc flags

Add `noImplicitOverride`, `noImplicitReturns`, `verbatimModuleSyntax`,
`erasableSyntaxOnly`; flip `noUnusedParameters` from its current explicit
`false`. Two source fixes total, both parameter properties in test fakes.

`noUnusedLocals` waits for Tier 4's vendored-source filter: its single error is
in `src/components/base/buttons/button.tsx` and there are none in our own code,
so it costs nothing the moment the filter exists.

`noImplicitOverride` is the direct partner to the Python `@override` adoption
(issue #55) and is the reason to take it even though it costs nothing today —
it locks in a convention rather than fixing a defect.

`noUnusedLocals`/`noUnusedParameters` duplicate Biome's `noUnusedVariables`/
`noUnusedFunctionParameters`. The duplication is deliberate: the two run in
different gate legs (`typecheck-ts` vs `lint-ts`), so a future misconfiguration
of one does not silently drop the check.

### Tier 2 — vitest isolation

`restoreMocks`, `unstubEnvs`, `unstubGlobals`, `expect.requireAssertions`. All
measured free. Today, mocks and stubbed globals leak across tests within a
file; nothing in the suite currently depends on that leakage, which is exactly
why it should be closed before something does.

### Tier 3 — randomized test order

Fix the two order-dependent tests, then set `sequence: { shuffle: true }`.
This is the pytest-randomly parity item.

`FilePage`'s failure is a genuine unawaited-async bug. An earlier draft of this
spec claimed the console guard "bills the warning to whichever test runs next";
that is **false, and was never measured**. The guard names the correct test,
and throws from that test's own `afterEach`. The real order-dependence is
subtler and more interesting: under the default file order the warning is not
merely attributed elsewhere, it is **not emitted at all** (`grep -c "not
wrapped in act"` over a full default-order run returns 0). Shuffling changes
the timing enough for the late resolution to land inside a window React
notices. Fixing it is a real bug fix, not a test-ordering accommodation.

The seed is not pinned, matching pytest-randomly. Vitest prints
`Running tests with seed "<n>"` in every run's header — pass or fail, not only
on failure — so a CI failure is always replayable with
`npx vitest run --sequence.shuffle --sequence.seed=<n>`.

### Tier 4 — the expensive tsc flags, behind a vendored-source filter

**The vendor boundary is drawn in Biome, knip, and coverage — but not in tsc.**
That gap is what makes these flags look unaffordable. Measured against the
whole program vs. against our own code only:

| Flag | Total | Vendored | Ours |
| ---- | ---- | ---- | ---- |
| `noPropertyAccessFromIndexSignature` | 77 | 0 | 77 |
| `exactOptionalPropertyTypes` | 100 | 77 | 23 |
| `noUncheckedIndexedAccess` | 452 | 11 | 441 |
| `noUnusedLocals` | 1 | 1 | **0** |

`exactOptionalPropertyTypes` is 77% vendored, and `noUnusedLocals` is 100%
vendored — the latter moves back to Tier 1 once the filter exists, at zero cost.

tsconfig cannot express this. `exclude` does not suppress errors in files
reached through an import (verified: 40 of the 77 vendored
`exactOptionalPropertyTypes` errors survive it), and the only per-file
suppression TypeScript offers is `// @ts-nocheck`, which would mean editing
vendored source.

So `typecheck-ts` gains a thin wrapper, `scripts/typecheck_web.sh`: run `tsc`,
drop diagnostics whose path is in the vendored set, fail if any remain. Its
path list derives from `web/untitledui.lock.json`'s `paths` — the same source
`scripts/check_untitledui_hash.sh` already uses — so the two cannot drift on
which files count as vendored. The idiom is established here:
`scripts/build_web_no_warnings.sh` already wraps vite to turn warnings into
failures.

This is not the rejected ratchet. A ratchet tolerates *our* defects under a
green gate; this excludes code we are forbidden to touch and can never fix.

Order, cheapest first so each lands on a green tree:
`noPropertyAccessFromIndexSignature` (77), `exactOptionalPropertyTypes` (23),
`noUncheckedIndexedAccess` (441). One flag per commit.

**`noUncheckedIndexedAccess` is the one to watch against the guiding
principle.** 441 sites is enough volume that `arr[i]!` becomes tempting, and
that fix makes the code strictly worse — it converts a checked access into an
unchecked one and hides the signal. The fixes that genuinely help are
structural: iterate instead of indexing, `.at()` where a miss is expected,
destructure with a default, or an early guard that names the invariant. If
working through the sites shows most of them want an assertion rather than a
restructure, **decline the flag and record why** — 441 assertions would be
precisely the number-reduction this workstream is not for.

### Tier 5 — Biome `preset: "all"`

Set `linter.rules.preset` to `"all"`, and `linter.domains` to `react: "all"`,
`test: "all"`, `types: "all"`, `tailwind: "all"`, with `solid`, `qwik`, `next`,
`svelte`, `vue`, `reactNative`, `drizzle`, `turborepo`, and `playwright`
explicitly `"none"`.

Explicitly listing the irrelevant framework domains as `"none"` rather than
omitting them is load-bearing documentation: it records that they were
considered and rejected, so a future Biome version that changes domain
defaults cannot quietly reintroduce 716 diagnostics.

**Measured, not estimated.** With the eight expected-off style rules disabled,
**1,026 diagnostics across 53 distinct rules** remain. An earlier draft of this
spec estimated "a few hundred across ten rules" and asserted that the rule
count, not the diagnostic count, was the real unit of work. Both halves were
wrong by roughly 3x and 5x respectively. The correct figure is 53 rule
decisions *plus* the fixes for whichever of them stay on.

That splits roughly in half. Rules expected to be curated off are the arbitrary
style constraints — `noImplicitBoolean` (63), `noContinue` (57),
`noIncrementDecrement` (55), `useDestructuring` (49), `useExportsLast` (34),
`useNumericSeparators` (33), `noExcessiveLinesPerFile` (30), `noNegationElse`
(21), `useFilenamingConvention` (12, the tree mixes PascalCase components with
camelCase modules on purpose), and similar. `noNodejsModules` (65) needs
scoping rather than blanket removal: it is correct for `src/**` and wrong for
test files and vite configs, which legitimately import `node:fs`.

Rules expected to stay on and be fixed are where the payoff is, and several are
genuine bug classes rather than style:

- `noLeakedRender` (20) — the `{count && <X/>}` bug that renders a bare `0`.
- `noUnnecessaryConditions` (30) — type-aware; flags conditions that cannot vary.
- `useUniqueElementIds` (18) — duplicate DOM ids, an a11y and test-selector defect.
- `useComponentExportOnlyModules` (25) — react-refresh correctness.
- `useTopLevelRegex` (53), `noAwaitInLoops` (4) — real performance defects.
- `noSecrets` (7), `noUnresolvedImports` (6), `noShadow` (5), `noConsole` (3),
  `noMisplacedAssertion` (3) — correctness and security.
- `noExcessiveLinesPerFunction` (117), `noExcessiveCognitiveComplexity` (25) —
  real complexity signals, and the largest single block of work in the tier.

`useExhaustiveDependencies` fires 8 times despite already being at `"error"` in
the current config, which means the `react: "all"` / `types: "all"` domains
detect cases the `recommended` preset does not. Those 8 are pre-existing hook
dependency defects and should be treated as bugs, not as new rule noise.

Because 53 rule decisions is too many for one reviewable commit, this tier
lands per rule group: `correctness` and `security` first (the bug classes),
then `suspicious`, then `performance` and `complexity`, then `style` (mostly
off-list curation). Each group is one commit with its off-list entries
commented in the same shape as `.ruff.toml`'s ignore block.

### Tier 6 — per-file coverage, on the right gate

`coverage.thresholds.perFile` does **not** go on the unit leg. Measured there
it fails 17 files, and the failures are structural rather than real:
`src/topo/LinkEdge.tsx` reports 0% statements/branches/functions/lines because
it is exercised only by the Playwright leg, which the vitest-only run cannot
see. `vite.config.ts` already documents this split.

`--per-file` belongs on the merged `nyc` gate (`web/package.json`'s
`coverage:merged`), which folds in the e2e leg. It lands with its own measured
baseline, taken after Tier 3 so the numbers are stable.

## Deliberate exceptions

**`skipLibCheck` stays `true`.** It measures 0 errors, and is still declined:
turning it off makes the gate hostage to third-party `.d.ts` quality, so a
Dependabot bump could redden CI for a defect that cannot be fixed in this tree.
The flag gets a comment in `tsconfig.json` recording that this was measured and
chosen, not overlooked — otherwise a future audit re-measures 0 and turns it on.

**No test-tier `overrides` block**, per the "same bar, decide per rule later"
decision. Exemptions get added individually with evidence when a rule proves
noisy, rather than pre-emptively by file glob.

## Testing

Each tier is proven red before green, per the pattern the vendored-hash gate
followed in `45b1b013`:

- Tiers 0–2: the flag or knob currently passing is demonstrated to fail against
  the pre-fix tree.
- Tier 3: `sequence.shuffle` with a fixed seed reproduces each of the two
  failures before the fix and passes after; the shuffle is then unpinned.
- Tier 4: each of the three flags is landed on a tree where `make check-ts` is
  already green, so any error the flag reports is attributable to that flag
  alone. Fixes to `noUncheckedIndexedAccess` sites that change runtime
  behaviour (an unchecked index that really could be `undefined`) get a test;
  fixes that only add a type assertion do not.
- Tier 5: per rule group, `biome check --error-on-warnings .` exits 0, and
  removing any single off-list entry reproduces its documented diagnostic
  count — that count is what makes the entry reviewable later.
- Tier 6: the merged gate is run end-to-end (`make coverage-ts`) to take the
  baseline, since the unit leg alone cannot produce it.

Full-gate verification for every tier is `make check-ts coverage-ts-unit`, plus
`make coverage-ts` for Tier 6.

## Risks

- **Tiers 4 and 5 together are the bulk of this workstream**: 629 tsc errors
  plus 1,026 Biome diagnostics across 53 rule decisions. That is a real
  commitment, and the first draft of this spec understated the Tier 5 half by
  roughly 3x. The mitigation is the commit split — three commits for Tier 4,
  one per Biome rule group for Tier 5 — so the work is reviewable in pieces and
  can be paused between them without leaving the tree in a half-strict state.
- **The Tier 5 off-list is where this design can quietly fail.** Every rule
  moved to `"off"` is a small act of the same reasoning that put the `textValue`
  warning on an allow-list for a week. Each entry must record the diagnostic
  count it suppresses, so a later reader can re-measure and challenge it.
- **`sequence.shuffle` makes CI failures seed-dependent.** Accepted: the same
  trade-off pytest-randomly already carries on the Python side, and vitest
  reports the seed.
- **`noUncheckedIndexedAccess` invites lazy fixes.** 452 errors is enough
  volume that `arr[i]!` becomes tempting, which would convert a checked access
  into an unchecked one and leave the codebase worse than before the flag. Non-
  null assertions added during this tier are themselves reviewable —
  `noNonNullAssertion` is on Tier 5's list and will surface them.
