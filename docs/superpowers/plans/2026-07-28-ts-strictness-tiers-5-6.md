# TypeScript strictness, Tiers 5-6 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take Biome from its `recommended` preset to the everything-on,
curated-exceptions shape `.ruff.toml` already has, and move per-file coverage
floors onto the gate that can actually see them.

**Architecture:** Biome's rule surface is opened one *group* at a time, each its
own commit. Each group's commit records a decision for every rule in it — fixed,
or turned off with a comment giving the count it suppresses and why. Only the
final commit flips `preset` to `"all"`, at which point the accumulated
`"off"` entries ARE the config, in the same shape as `.ruff.toml`'s ignore
block. Tier 6 is separate and small.

**Tech Stack:** Biome 2.5.5, TypeScript 7.0.2, nyc, vitest 4.1.10, Playwright.

## Guiding principle — read before every task

**The goal is better code: more readable, harder to break. These tools' numbers
tell you where to look. They are not the target.** (Chris, 2026-07-28.)

- **The test is whether the signal stays VISIBLE.** A bare `!`, an `as`, an
  `_`-rename or an arbitrary function split are failures.
- **A localized ignore annotation is a legitimate tool, used judiciously**, with
  the rule name and a real reason. **Preferable to a bad fix.**
- **A global rule-off is for stylistic disagreement.** This tier is where most of
  them get made — that is expected and correct, not a failure.
- **Every off-list entry records the diagnostic count it suppresses**, so a later
  reader can re-measure and challenge it.
- **Prove a setting live differentially** — error with it on, no error with it
  off.
- **Check exit codes without a pipe.** `cmd | tail; echo $?` reports `tail`.

Tier 4 is the evidence for all of this: across 72 `src/` `noUncheckedIndexedAccess`
sites it found **0 live bugs, 3 latent fragilities, 69 cases of the type-checker
failing to see an invariant** — and `topo/measure.ts` scored **0 in 29**. Volume
is not value.

## Tool ownership — the collision policy

Applies to every rule decision in Tier 5. Full text in
`docs/superpowers/specs/2026-07-27-ts-strictness-design.md`.

1. tsc owns type correctness.
2. Biome owns style, idiom, local bug patterns, and the unused-code family.
3. knip owns cross-module reachability.
4. **On a contradiction, the tool carrying type information wins**; the losing
   rule goes off with a comment naming its counterpart.
5. **A Biome rule that merely restates a tsc flag is turned off**, unless the
   duplication is deliberate and documented at both ends.

Rule 4 already fired once: `complexity/useLiteralKeys` is the exact inverse of
`noPropertyAccessFromIndexSignature` and is already `"off"`. **Expect more.**
The tsc flags now enabled and available to collide with: `strict`,
`noImplicitOverride`, `noImplicitReturns`, `verbatimModuleSyntax`,
`erasableSyntaxOnly`, `noPropertyAccessFromIndexSignature`,
`exactOptionalPropertyTypes`, `noUncheckedIndexedAccess`,
`noFallthroughCasesInSwitch`, `noUncheckedSideEffectImports`.

## Global Constraints

- **Never hand-edit vendored Untitled UI source.** Biome already excludes it via
  `files.includes`; leave that alone.
- **`web/biome.json` is STRICT JSON.** A `//` comment there does not error —
  Biome silently discards the ENTIRE config and lints with defaults, so the
  vendored exclusions vanish (179 files clean becomes 409 files / 924 errors,
  with nothing saying the config was ignored). **All rationale goes in
  `web/tsconfig.json`'s jsonc comment block, cross-referenced from the rule
  name.** Verify after every `biome.json` edit:
  `python3 -c "import json;json.load(open('web/biome.json'))"` **and** that
  `npx biome check .` still reports ~180 files, not ~409.
- **Do not add a step to `.github/workflows/ci.yml`.**
  `tests/unit/test_ci_web_gate.py` pins the job command and `web/package.json`'s
  scripts — run `uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q`
  after touching either.
- **`web/tsconfig.json` and `web/vite.config.ts` have consumers beyond
  `make check-ts`** (`scripts/check_untitledui_drift.sh` copies both; `make web`
  reads both). Run `make web` after editing either.
- **Never `git push`.** Commit on this worktree branch; messages end with
  `Assisted-by: Claude Opus 5`. Never use bare `git stash` / `git stash pop`.
- **Baseline:** 928 tests in 77 files, green at `1dddf565`. `make check-ts`,
  `make coverage-ts-unit` and `make web` all green. `scripts/typecheck_web.sh`
  reports 88 vendored diagnostics ignored and 305 deferred
  `noUncheckedIndexedAccess` test sites.
- **A rule's count goes stale as earlier groups land.** Re-measure at the start
  of every task and **attribute** any delta rather than adapting to it. Tier 4
  saw this twice.

## Measured baseline

Measured 2026-07-28 against `1dddf565`, with `preset: "all"` and
`domains: {react: all, test: all, types: all, tailwind: all}` and the eight
irrelevant framework domains (`solid`, `qwik`, `next`, `svelte`, `vue`,
`reactNative`, `drizzle`, `turborepo`, `playwright`) set to `"none"`.

**3,691 diagnostics across 59 rules.** (An earlier draft said 3,720/641 for
`correctness`. That was inflated by counting `grep -oE "lint/<group>/<rule>"`
over the whole report, which also matches rule names echoed inside OTHER rules'
code frames — this tree carries 7 justified `biome-ignore
lint/correctness/useExhaustiveDependencies` comments that get quoted in
snippets. **Count header-anchored lines only:**
`grep -cE '^[a-zA-Z0-9_/.-]+:[0-9]+:[0-9]+ lint/<group>/'`.) By group:

| Group | Diagnostics | Rules | Task |
| ---- | ---- | ---- | ---- |
| `style` | 2,518 | 27 | 4 |
| `correctness` | 629 | 9 | 1 |
| `suspicious` | 194 | 10 | 2 |
| `complexity` | 181 | 7 | 3 |
| `performance` | 179 | 5 | 3 |
| `security` | 7 | 1 | 1 |
| `a11y` | **0** | 0 | — |
| `nursery` | **0** (not in `preset: "all"`) | 0 of 83 | 5 |

Two of those zeros matter:

- **`a11y` fires nothing.** The tree is already clean on Biome's 40
  accessibility rules. Nothing to do, and worth stating so nobody goes looking.
- **`nursery` fires nothing because `preset: "all"` does NOT include it.**
  Verified: `noFloatingPromises` stays silent under `preset: "all"` and fires
  only when named explicitly. Floating promises are caught by **neither** tsc
  nor Biome today — see Task 5.

Highest-count rules (the shape of the work):

```
529 correctness/useImportExtensions      116 complexity/noExcessiveLinesPerFunction
443 style/useBlockStatements             106 performance/noJsxPropsBind
433 style/noMagicNumbers                  90 suspicious/noEmptyBlockStatements
404 style/useNamingConvention             65 correctness/noNodejsModules
310 style/noTernary                       63 style/noImplicitBoolean
299 style/useGlobalThis                   55 style/noContinue
199 style/noJsxLiterals                   53 performance/useTopLevelRegex
```

Note `correctness` is 629 but 594 of that is two rules (`useImportExtensions`
529, `noNodejsModules` 65), both configuration questions rather than defects.
The genuine correctness surface is ~35, and `useExhaustiveDependencies` fires
**0** — the "12 pre-existing hook bugs" an earlier draft claimed were an
artifact of the un-anchored count.

**Domain hazard, measured:** with `react: "all"`, adding `next: "none"` to the
domain block **silently disables `useExhaustiveDependencies`**. The only visible
symptom is this tree's 7 justified suppressions being reported as
`suppressions/unused`. The domain block below is otherwise correct, but do not
add `next: "none"` to it.

`style/noNonNullAssertion` now fires **5** times, down from 16 before Tier 4 —
Tier 4 removed assertions rather than adding them.

---

### Task 1: `correctness` + `security` (650 diagnostics, 10 rules)

The highest-value group and the smallest real surface. Do this one first so the
bug-class rules land before any style churn.

**Files:** `web/biome.json` (rule entries), `web/tsconfig.json` (rationale
comments), plus whatever sources the kept rules touch.

**Interfaces:**
- Consumes: nothing.
- Produces: an off-list convention — `"ruleName": "off"` in `biome.json` with a
  matching commented entry in `tsconfig.json` giving the count and reason.
  Tasks 2-4 follow the same shape.

- [ ] **Step 1: Measure this group alone**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
cp web/biome.json /tmp/bj.bak
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("web/biome.json"); c = json.loads(p.read_text())
c["linter"]["rules"]["preset"] = "all"
c["linter"]["domains"] = {"react":"all","test":"all","types":"all","tailwind":"all",
  "solid":"none","qwik":"none","next":"none","svelte":"none","vue":"none",
  "reactNative":"none","drizzle":"none","turborepo":"none","playwright":"none"}
p.write_text(json.dumps(c, indent=2))
EOF
(cd web && npx biome lint --max-diagnostics=20000 . 2>&1) > /tmp/all.txt
grep -oE "lint/(correctness|security)/[a-zA-Z]+" /tmp/all.txt | sort | uniq -c | sort -rn
cp /tmp/bj.bak web/biome.json && rm -f /tmp/bj.bak
python3 -c "import json;json.load(open('web/biome.json'));print('restored')"
```

Expected: `useImportExtensions` 529, `noNodejsModules` 65, `noSecrets` 7, plus
~47 spread over the remaining seven rules. Attribute any delta.

- [ ] **Step 2: Decide the two big ones first — both are config questions**

`useImportExtensions` (529) wants `./foo.js` on relative imports. This tree is
bundled by Vite with `moduleResolution: "bundler"`; extensionless imports are
correct here and adding 529 `.js` suffixes to `.ts` files would be actively
wrong. **Turn it off**, comment recording the bundler rationale and the count.

`noNodejsModules` (65) is correct for `src/**` shipped to a browser and wrong
for test files and `vite.config.ts`, which legitimately import `node:fs`.
Biome supports per-path overrides via `overrides` in `biome.json` — scope it to
non-test source rather than turning it off wholesale. If that cannot be
expressed cleanly, turn it off and say why; do NOT add 65 annotations.

- [ ] **Step 3: Triage the remaining rules individually**

The complete group, so nothing is triaged by memory:

| Rule | Count | Note |
| ---- | ---- | ---- |
| `correctness/useUniqueElementIds` | 18 | duplicate DOM ids — an a11y AND test-selector defect; likely keep |
| `correctness/useExhaustiveDependencies` | 12 | already `"error"` in the current config; these 12 are cases the `react`/`types` domains detect that `recommended` does not. **Pre-existing hook dependency bugs, not new rule noise.** |
| `correctness/useJsonImportAttributes` | 6 | |
| `correctness/noUnresolvedImports` | 6 | real — an import that does not resolve |
| `correctness/noGlobalDirnameFilename` | 3 | |
| `correctness/useSingleJsDocAsterisk` | 1 | |
| `correctness/noProcessGlobal` | 1 | |
| `security/noSecrets` | 7 | inspect each personally |

For each rule: keep and fix, or off with count and reason. `noSecrets` (7) is
security — inspect each hit personally; a false positive on a fixture is
plausible but must be confirmed, not assumed. Apply the ownership policy to any
rule that restates a tsc flag.

- [ ] **Step 4: Verify**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
python3 -c "import json;json.load(open('web/biome.json'));print('json ok')"
(cd web && npx biome check . 2>&1 | tail -2)   # must say ~180 files, NOT ~409
make check-ts && make web
(cd web && npx vitest run 2>&1 | grep -E "Tests +[0-9]")
uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q
```

Expected: json valid, ~180 files checked, both gates green, 928 tests.

- [ ] **Step 5: Differential proof for one kept rule**

Pick a rule you kept ON, write a scratch file violating it, confirm
`npx biome check --error-on-warnings .` exits 1, then confirm it exits 0 with
that rule set to `"off"`. Delete the scratch file; `git status --porcelain`
clean.

- [ ] **Step 6: Commit** — message states, per rule, kept-and-fixed vs off-with-count.

---

### Task 2: `suspicious` (194 diagnostics, 10 rules)

Highest bug-class density after `correctness`. Three named in advance because
they are worth keeping:

- `noEmptyBlockStatements` (90) — usually a swallowed error or an unfinished branch.
- `noUnnecessaryConditions` (27) — type-aware; flags a condition that cannot vary.
  This one needs the `types` domain and is the closest Biome analogue to `ty`.
- `noEqualsToNull` (37) — `== null` is often *deliberate* (it catches both null
  and undefined). Decide once, explicitly; do not mechanically rewrite 37 sites.

**Files:** `web/biome.json`, `web/tsconfig.json`, plus sources.

**Interfaces:** Consumes Task 1's off-list convention. Produces nothing new.

- [ ] **Step 1: Measure the group alone** — same harness as Task 1 Step 1, with
  `grep -oE "lint/suspicious/[a-zA-Z]+"`. Attribute any delta from 194/10.
- [ ] **Step 2: Triage every rule** — keep-and-fix or off-with-count-and-reason.

The complete group:
`noEmptyBlockStatements` (90), `noEqualsToNull` (37),
`noUnnecessaryConditions` (27), `noLeakedRender` (20), `noShadow` (5),
`noBitwiseOperators` (5), `useArraySortCompare` (3), `noMisplacedAssertion` (3),
`noConsole` (3), `useAwait` (1).

`noLeakedRender` (20) is the `{count && <X/>}` bug that renders a bare `0` on
screen — a real user-visible defect class, likely the most valuable rule in
this group. `useArraySortCompare` is worth attention given Tier 4 found a
NaN-sensitive comparator in `charts/options.ts`. `noMisplacedAssertion` (3) is
a test-quality rule: an assertion outside the test body it belongs to.
- [ ] **Step 3: Fix the kept rules' sites**, per the guiding principle. A
  localized annotation is fine and preferable to a bad fix; report each.
- [ ] **Step 4: Verify** — same block as Task 1 Step 4.
- [ ] **Step 5: Differential proof** for one kept rule.
- [ ] **Step 6: Commit.**

---

### Task 3: `complexity` + `performance` (360 diagnostics, 12 rules)

**Files:** `web/biome.json`, `web/tsconfig.json`, plus sources.

**Interfaces:** Consumes Task 1's convention.

The two large ones need a judgement rather than a sweep:

`noExcessiveLinesPerFunction` (116) and `noExcessiveCognitiveComplexity` are
real signals, but **splitting a function to satisfy a line count is explicitly
a failure** under the guiding principle. Either the split genuinely clarifies —
in which case do it and say what the extracted piece is *for* — or the rule goes
off, or its threshold is raised to a number this codebase can defend. All three
are legitimate; a mechanical split is not.

`noJsxPropsBind` (106) flags inline arrow props in JSX. That is idiomatic React
and the alternative (`useCallback` everywhere) is usually worse. Expect this to
go off; confirm rather than assume, since the tree has real render-count guards
(`chart_memo.test.tsx`, `subjecthealthbanner.test.tsx`) that could make a
measured case either way.

`useTopLevelRegex` (53) is a genuine performance rule — a regex literal rebuilt
per call. Likely keep.

- [ ] **Step 1: Measure both groups alone.** Attribute any delta from 181/7 and 179/5.
- [ ] **Step 2: Triage every rule.**

The complete groups:
`complexity`: `noExcessiveLinesPerFunction` (116),
`noExcessiveCognitiveComplexity` (24), `noVoid` (22),
`useSimplifiedLogicExpression` (12), `useMaxParams` (4),
`noUselessUndefined` (2), `noImplicitCoercions` (1).
`performance`: `noJsxPropsBind` (106), `useTopLevelRegex` (53), `noDelete` (11),
`noNamespaceImport` (5), `noAwaitInLoops` (4).

`noVoid` (22) needs care: this tree uses `void someCall()` deliberately to mark
a deliberately-unawaited result (`perf_budget.test.ts`, `clock.test.tsx`).
Banning it would push those toward a worse form.

- [ ] **Step 3: Fix the kept rules' sites.** For any function you split, state in
  the commit what the extracted function is for. If you cannot name it, do not
  split it.
- [ ] **Step 4: Verify** — same block as Task 1 Step 4, plus
  `cd web && npx vitest run` specifically re-checking the render-count guards.
- [ ] **Step 5: Differential proof** for one kept rule.
- [ ] **Step 6: Commit.**

---

### Task 4: `style` (2,518 diagnostics, 27 rules)

**The largest group and the one most likely to produce churn for its own sake.**
Most of these will go off. That is the correct outcome, not a capitulation —
`.ruff.toml` ignores about thirty rules for exactly this reason.

Expected off, with counts to record: `useBlockStatements` (443),
`noMagicNumbers` (433), `useNamingConvention` (404), `noTernary` (310),
`useGlobalThis` (299), `noJsxLiterals` (199), `noImplicitBoolean` (63),
`noContinue` (55), `useDestructuring` (44), `noIncrementDecrement` (41),
`useExportsLast` (35), `useNumericSeparators` (33), `noExcessiveLinesPerFile`
(30), `useFilenamingConvention` (the tree deliberately mixes PascalCase
components with camelCase modules).

Worth genuinely considering keeping: `noHexColors` (28) — this repo has a
brand-token gate (`scripts/check_brand_tokens.sh`), so a raw hex may be a real
violation of it rather than a style preference. Check whether the 28 hits are in
chart config (legitimate) or in component styling (should be a token).
`noNonNullAssertion` (5) should be **kept** — it is the tripwire that makes
Tier 4's discipline durable.

**Files:** `web/biome.json`, `web/tsconfig.json`, plus sources for kept rules.

**Interfaces:** Consumes Task 1's convention.

- [ ] **Step 1: Measure the group alone.** Attribute any delta from 2,518/27.
- [ ] **Step 2: Triage every one of the 27 rules.** Every `"off"` gets its count
  and a reason. "Too many hits" is not a reason; "this rule's preferred form is
  worse here, and here is an example" is.

The complete group, so none is missed:
`useBlockStatements` (443), `noMagicNumbers` (433), `useNamingConvention` (404),
`noTernary` (310), `useGlobalThis` (299), `noJsxLiterals` (199),
`noImplicitBoolean` (63), `noContinue` (55), `useDestructuring` (44),
`noIncrementDecrement` (41), `useExportsLast` (35), `useNumericSeparators` (33),
`noExcessiveLinesPerFile` (30), `noHexColors` (28),
`useComponentExportOnlyModules` (25), `noNegationElse` (21),
`useFilenamingConvention` (12), `useExplicitLengthCheck` (11),
`noNestedTernary` (11), `noNonNullAssertion` (5),
`noExcessiveClassesPerFile` (4), `noDefaultExport` (4), `useAtIndex` (3),
`useErrorCause` (2), `useConsistentMethodSignatures` (1),
`useConsistentArrayType` (1), `noProcessEnv` (1).

Besides `noHexColors` and `noNonNullAssertion` called out above,
`useComponentExportOnlyModules` (25) is worth considering: it is a
react-refresh correctness rule, not a style one — a module exporting both a
component and non-component values breaks fast refresh. And `useErrorCause` (2)
is cheap and genuinely useful for debugging.
- [ ] **Step 3: Fix the kept rules' sites.**
- [ ] **Step 4: Verify** — same block as Task 1 Step 4.
- [ ] **Step 5: Differential proof** for `noNonNullAssertion` specifically —
  a scratch file with `v!` must fail the gate. That rule is Tier 4's guard.
- [ ] **Step 6: Commit.**

---

### Task 5: Flip to `preset: "all"`, and opt into `noFloatingPromises`

By now every rule in every group has a recorded decision. This task makes the
config say so.

**Files:** `web/biome.json`, `web/tsconfig.json`.

**Interfaces:** Consumes Tasks 1-4's off-list.

- [ ] **Step 1: Set `preset: "all"` and the domains**, then delete every
  now-redundant explicit `"error"` entry, leaving the `"off"` list as the config
  — the `.ruff.toml` shape. The eight irrelevant framework domains stay listed
  as `"none"` explicitly: that records they were considered, so a future Biome
  version changing domain defaults cannot quietly reintroduce ~700 diagnostics.

- [ ] **Step 2: Prove the flip changed nothing**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert/web
npx biome lint --max-diagnostics=20000 . > /tmp/after.txt 2>&1; echo "exit=$?"
grep -c "lint/" /tmp/after.txt
```

Expected: the same diagnostic count as immediately before the flip. A change
means a rule's decision was lost in the rewrite — find it, do not accept it.

- [ ] **Step 3: Opt into `nursery/noFloatingPromises` — BY NAME, never the group**

`preset: "all"` does not include nursery (verified). Enable exactly this one
rule: `"nursery": { "noFloatingPromises": "error" }`. **It costs 2
diagnostics**, both in `src/covapp/data.test.ts`. Floating promises are caught
by neither tsc nor Biome today, in a codebase full of streaming and chunk loads.

**Do NOT enable the nursery group.** Measured, all 83 rules on: **3,356
diagnostics**, which nearly doubles the whole of Tier 5, and four distinct
hazards make it a bad trade:

1. **Instability is definitional.** Nursery rules are still being designed —
   renamed, promoted to other groups, or dropped between minor versions. A
   config naming them breaks on upgrade.
2. **It is uncurated, and the volume is rules that are wrong here.**
   `noUndeclaredClasses` alone is 1,949 — a CSS rule flagging every Tailwind
   utility as undeclared (confirmed firing on `.tsx`; wrong by construction for
   Tailwind v4). `useExplicitType` (417) and `useExplicitReturnType` (398) would
   mandate annotating every function. Those three are ~80% of the total.
3. **Framework leakage.** `noReactNativeRawText` fires **204 times on a web
   project** — the same class of noise as the Solid rule that flagged every
   `className`. Nursery rules do not reliably respect domain gating.
4. **A config-rejection false green.** The nursery group's schema contains a
   non-rule key (`preset`); setting it makes Biome **discard the entire config,
   check zero files, and exit 1** — while a naive diagnostic count reads as
   clean. Always assert the `Checked N files` line, not just the exit code.

- [ ] **Step 4: Verify** — `make check-ts`, `make web`, 928 tests, the CI-gate
  test, and `npx biome check .` reporting ~180 files rather than ~409.

- [ ] **Step 5: Commit.**

---

### Task 6: Per-file coverage floors on the merged gate

Per-file floors do **not** belong on the unit leg: measured there, 17 files fail
and the failures are structural — `src/topo/LinkEdge.tsx` reports 0% because it
is exercised only by the Playwright leg, which a vitest-only run cannot see.
They belong on the merged `nyc` gate, which folds both legs in.

**This task's baseline is deliberately unmeasured.** Taking it requires
`make coverage-ts`, which runs the Playwright e2e lane — too heavy to run
speculatively while writing a plan. Measure it as Step 1 rather than trusting a
number invented here.

**Files:** `web/package.json` (the `coverage:merged` script).

**Interfaces:** Consumes nothing from Tier 5. Can be done before or after it.

- [ ] **Step 1: Take the baseline**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
make coverage-ts > /tmp/cov.log 2>&1; echo "exit=$?"; tail -20 /tmp/cov.log
```

Expected: green at the current merged floors (statements 91, branches 78,
functions 94, lines 95). Record the run's duration — it decides whether this
gate can stay on every push or needs to be nightly.

- [ ] **Step 2: Measure the per-file cost**

Add `--per-file` to `web/package.json`'s `coverage:merged` script, re-run, and
count how many files fail and by how much. **Do not fix anything yet.**

- [ ] **Step 3: Decide the floor**

A per-file floor equal to the aggregate floor is almost always wrong — one small
file with two uncovered branches fails a gate the whole tree passes. Either set
per-file floors lower than the aggregate (`nyc` supports separate values), or
identify the specific files that should be exempt and say why. Report the
options with numbers before implementing.

- [ ] **Step 4: Verify and commit** — `make coverage-ts` green, and
  `uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q` green since
  `package.json` changed.

---

## Risks

- **Task 4 is where this workstream could do net harm.** 2,518 diagnostics in
  rules that are mostly matters of taste. The mitigation is that the unit of
  work is 27 rule decisions, not 2,518 edits — and that "off, with the count and
  a reason" is a first-class outcome.
- **The off-list is the same act of reasoning that parked the `textValue`
  warning on an allow-list for a week.** Every entry records its count precisely
  so a later reader can re-measure and challenge it.
- **`biome.json` cannot carry comments**, so every off-list entry is two edits in
  two files that can drift apart. Task 1's convention must make the
  cross-reference explicit, and later tasks must follow it rather than inventing
  a second style.
- **Tier 6's numbers are unknown**, deliberately. If `make coverage-ts` turns out
  to be slow enough that per-file floors make the gate impractical on every
  push, that is a legitimate reason to scope it to nightly — decide with the
  measured duration, not in advance.
