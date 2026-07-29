# TypeScript/vitest strictness, Tiers 0-3 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the `web/` quality gates that currently pass while defects are
present — Biome's warn tier, six unset tsc flags, and a vitest harness with no
mock isolation and no test-order randomization.

**Architecture:** Four independent tiers, each landing complete (gate on, every
violation fixed) rather than behind a budget. Tier 0 stops the gate leaking
warnings; Tier 1 turns on tsc flags that cost 0-2 errors; Tier 2 adds vitest
isolation settings that cost nothing; Tier 3 fixes two order-dependent tests
and enables shuffled test order. No new scripts, no new config files — every
change is to an existing `tsconfig.json`, `vite.config.ts`, `vitest.setup.ts`,
`Makefile`, or a test file.

**Tech Stack:** TypeScript 7.0.2, Biome 2.5.5, vitest 4.1.10, React 19.2.8,
@testing-library/react, knip 6.29.0.

## Guiding principle — read before every task

**The goal is better code: more readable, harder to break. These tools' numbers
tell you where to look. They are not the target.** (Chris, 2026-07-28.)

This overrides any instruction below that appears to conflict with it.

- **The test is whether the signal stays VISIBLE**, not whether the diagnostic
  disappears. A bare `!`, an `as` cast, an `_`-prefixed rename, or an arbitrary
  function split are failures — they read as ordinary code, so the problem
  becomes unfindable by anyone who did not write it.
- **A localized ignore annotation is a legitimate tool, used judiciously.** If a
  specific site is genuinely nasty to fix, annotate it with the rule name and a
  real reason and move on; we can address it later. Unlike a bare assertion it
  stays greppable, so the deferral is honest and recoverable. Prefer this over
  a bad fix.
- **A global rule-off is for stylistic disagreement** — a rule we do not want
  anywhere. Do not reach for one to dodge a handful of hard sites; that is what
  the localized annotation is for.
- If a whole flag turns out not to earn its keep, **stop and report it**
  rather than grinding through the remaining sites.
- The review question at the end of every task is *"is this code better to read
  and harder to break?"* — not *"did the count reach zero?"*
- **Prove a setting is live differentially.** When a task claims to have
  enabled a check, demonstrate the check errors with the setting on and does
  NOT error with it off. Observing an error proves only that *something* is
  strict, and this codebase already has `strict: true`, a console guard, and 5
  escalated Biome rules that will happily produce a plausible-looking failure
  for the wrong reason.

## Global Constraints

- **Never hand-edit vendored Untitled UI source.** `web/src/components/**`,
  `web/src/styles/**`, `web/src/utils/cx.ts`,
  `web/src/utils/is-react-component.ts`, `web/src/hooks/use-breakpoint.ts`,
  `web/src/hooks/use-resize-observer.ts`. `make check-ts` runs
  `scripts/check_untitledui_hash.sh`, which fails on any byte change. Reconcile
  at the call site instead — see `web/README.md`.
- **Do not add a step to `.github/workflows/ci.yml`.**
  `tests/unit/test_ci_web_gate.py` pins the `check-ts` job to
  `runs == ["make check-ts coverage-ts-unit"]` exactly. Adding a leg to the
  Makefile target is how a check reaches CI.
- **No `overrides` block relaxing rules for test files.** Exemptions are added
  per rule, with evidence, when a rule proves noisy — not pre-emptively by glob.
- **Never `git push`.** Commit on this worktree branch only. Commit messages use
  a conventional prefix and end with the trailer `Assisted-by: Claude Opus 5`.
- **Full-gate verification for every task** is `make check-ts` from the repo
  root (`/home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert`).
  All `npx` commands below run from `web/`.
- **Baseline:** the suite is 928 tests in 77 files. Measurements in this plan
  were taken at `345cb701`; the commits since are docs-only, so they still hold.
  Any task that changes the test count must say why.
- **`web/tsconfig.json` AND `web/vite.config.ts` have consumers beyond
  `make check-ts`.** `scripts/check_untitledui_drift.sh` copies both into a
  throwaway project for the Untitled UI CLI (tsconfig is read json5-backed, so
  `//` comments are tolerated; vite.config is presence-based framework
  detection only — both verified), and `make web` reads both as build inputs. Neither runs under `make check-ts`, and
  the drift script is wired to run on every push by `45b1b013`, so a break there
  would surface late and somewhere else. Any task editing this file must also
  run `make web`.
- **`tests/unit/test_ci_web_gate.py` pins `web/package.json`'s scripts and the
  CI job's command list.** Any task touching either must run
  `uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q` before committing.
  Task 1 changed the `check` script and passed only because the assertion is
  `startswith("biome check")` rather than an equality check — luck, not design.

---

### Task 1: Tier 0 — make Biome's warn tier fail the gate

`npx biome check .` exits **0** today while reporting 7
`lint/style/noNonNullAssertion` warnings. Fix the warnings, then close the hole
that let them accumulate.

All 7 are the same shape: `scopeTreeToTicket` returns `DirNode | null`, and the
test asserts on the result with a `!` non-null assertion. Replacing `!` with an
explicit assertion makes the test *check* the contract instead of asserting it
away — and produces a better failure message if the function ever returns null.

**Files:**
- Modify: `web/src/covapp/tickets.test.ts` (lines 45, 51, 76, 114, 172, 338, 350)
- Modify: `web/package.json` (the `check` script)
- Modify: `Makefile:734-738` (comment only, above the `lint-ts` recipe)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `web/src/covapp/tickets.test.ts` gains a module-level helper
  `scopedOrThrow(node: DirNode, ticketLines: Record<string, number[]>,
  ticketHits: Record<string, number[]>, ticketTiers?: Record<string,
  Record<string, number>>): DirNode`. No later task depends on it.

- [ ] **Step 1: Confirm the gate is leaking — run the check that should already be failing**

```bash
cd web && npx biome check --error-on-warnings . ; echo "exit=$?"
```

Expected: `exit=1`, with 7 `lint/style/noNonNullAssertion` diagnostics in
`src/covapp/tickets.test.ts`. Then confirm the current gate passes anyway:

```bash
cd web && npx biome check . ; echo "exit=$?"
```

Expected: `exit=0`. That gap is the defect this task closes.

- [ ] **Step 2: Add the helper to `web/src/covapp/tickets.test.ts`**

Place it immediately after the existing imports:

```ts
// `scopeTreeToTicket` returns `DirNode | null`. Every call below expects a
// tree back; asserting that explicitly (rather than with `!`) turns a
// contract break into a named failure instead of a TypeError three lines
// later, and keeps `noNonNullAssertion` enforced in test code.
function scopedOrThrow(
  node: DirNode,
  ticketLines: Record<string, number[]>,
  ticketHits: Record<string, number[]>,
  ticketTiers?: Record<string, Record<string, number>>,
): DirNode {
  const scoped = scopeTreeToTicket(node, ticketLines, ticketHits, ticketTiers);
  if (scoped === null) throw new Error("scopeTreeToTicket returned null; expected a scoped tree");
  return scoped;
}
```

`DirNode` is already imported by this file (line 18,
`import type { DirNode, FileChunk, TicketChunk } from "./types";`), so no
import change is needed.

- [ ] **Step 3: Replace all 7 `!` call sites**

Each site changes from `scopeTreeToTicket(<args>)!` to `scopedOrThrow(<args>)`.
Two sites (lines 338 and 350 in the original file) span multiple lines — change
only the function name and drop the trailing `!` after the closing paren.

```bash
cd web && grep -n "scopeTreeToTicket(" src/covapp/tickets.test.ts
```

Expected after editing: zero matches with a trailing `!`. Verify:

```bash
cd web && grep -n ")!;" src/covapp/tickets.test.ts
```

Expected: no output.

- [ ] **Step 4: Verify the warnings are gone and the tests still pass**

```bash
cd web && npx biome check --error-on-warnings . ; echo "exit=$?"
cd web && npx vitest run src/covapp/tickets.test.ts
```

Expected: `exit=0`, and the tickets suite green with the same test count as
before.

- [ ] **Step 5: Wire `--error-on-warnings` into the gate**

`web/package.json`'s `check` script currently reads `biome check .`. Change it
to:

```json
"check": "biome check --error-on-warnings .",
```

The Makefile's `lint-ts` target calls `npm run check`, so it needs no edit. Add
a comment above the `lint-ts` recipe in `Makefile:734` recording why:

```make
# --error-on-warnings lives in web/package.json's `check` script, not here, so
# a bare `npm run check` in web/ enforces the same bar as CI. Biome exits 0 on
# warnings by default: 7 noNonNullAssertion warnings sat in tickets.test.ts
# under a green gate until 2026-07-28. There is no warn tier on the Python
# side (ruff errors only, pytest filterwarnings=error) and there is not one here.
```

- [ ] **Step 6: Prove the wiring works**

Confirm the gate now fails on a warning, using a throwaway file so no tracked
source is touched:

```bash
cd web && cat > src/__scratch_warn.ts <<'EOF'
export const probe = (v: string | null): string => v!;
EOF
npm run check ; echo "exit=$?"
rm src/__scratch_warn.ts
```

Expected: `exit=1`, citing `lint/style/noNonNullAssertion` in
`src/__scratch_warn.ts`. If it exits 0, `--error-on-warnings` did not take
effect — stop and fix before committing. Confirm the tree is clean afterwards
with `git status --porcelain`.

- [ ] **Step 7: Run the full gate**

```bash
make check-ts
```

Expected: biome, knip, tsc, and the vendored-hash check all green.

- [ ] **Step 8: Commit**

```bash
git add web/src/covapp/tickets.test.ts web/package.json Makefile
git commit -m "fix(web): fail lint-ts on Biome warnings, fix the 7 that accumulated

biome check exits 0 on warnings by default, so 7 noNonNullAssertion warnings
in tickets.test.ts sat under a green gate. --error-on-warnings closes that;
the 7 are fixed rather than exempted, by asserting scopeTreeToTicket's
non-null contract explicitly instead of with \`!\`.

Assisted-by: Claude Opus 5"
```

---

### Task 2: Tier 1 — turn on the free tsc flags

Four flags measure 0 errors and one measures 2. **`noUnusedLocals` is not
included here** — it is deferred to Tier 4, not declined; see Step 5.

**Files:**
- Modify: `web/tsconfig.json`
- Modify: `web/src/__tests__/bootstrap.test.ts:42`
- Modify: `web/src/__tests__/stream.test.ts:12`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Measure each flag to confirm the baseline still holds**

```bash
cd web && for f in noImplicitOverride noImplicitReturns verbatimModuleSyntax noUnusedParameters erasableSyntaxOnly; do
  printf "%-26s %s\n" "$f" "$(npx tsc -p tsconfig.json --$f 2>&1 | grep -c 'error TS')"
done
```

Expected: `0 0 0 0 2`. If any number differs, stop and report — the tree has
moved since the spec was measured.

- [ ] **Step 2: Fix the two `erasableSyntaxOnly` errors**

Both are TypeScript parameter properties in a test fake, which have no
JavaScript equivalent and so cannot be erased by a type-stripping transform.

In `web/src/__tests__/bootstrap.test.ts` line 42, change:

```ts
  constructor(public url: string) {}
```

to:

```ts
  url: string;
  constructor(url: string) {
    this.url = url;
  }
```

In `web/src/__tests__/stream.test.ts` line 12, change:

```ts
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
```

to:

```ts
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.last = this;
  }
```

- [ ] **Step 3: Verify the fixes**

```bash
cd web && npx tsc -p tsconfig.json --erasableSyntaxOnly 2>&1 | grep -c "error TS"
```

Expected: `0`.

- [ ] **Step 4: Add the flags to `web/tsconfig.json`**

Replace the existing `"noUnusedLocals": false,` and `"noUnusedParameters": false,`
lines and add the rest, so `compilerOptions` contains:

```json
    "strict": true,
    "noImplicitOverride": true,
    "noImplicitReturns": true,
    "verbatimModuleSyntax": true,
    "erasableSyntaxOnly": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedSideEffectImports": true,
```

Note `noUnusedLocals` is now absent entirely rather than set to `false`.

- [ ] **Step 5: Document the two deliberate exceptions in `web/tsconfig.json`**

Add above `compilerOptions` (tsconfig permits `//` comments):

```jsonc
  // Two strictness flags are deliberately absent, both measured rather than
  // assumed (2026-07-28, tree at 345cb701):
  //
  // noUnusedLocals — DEFERRED to tier 4, not declined. Its 1 error is in
  // VENDORED source (src/components/base/buttons/button.tsx: unused `React`
  // import) and our own tree has ZERO. tsc has no per-directory rule scoping
  // and `exclude` does not help (a file reached through an import is added to
  // the program and checked anyway -- verified), so this flag becomes free the
  // moment scripts/typecheck_web.sh filters vendored diagnostics. Until then
  // Biome's noUnusedVariables covers our tree at error level and does exclude
  // src/components/**, so nothing is unguarded in the meantime.
  //
  // skipLibCheck stays true despite measuring 0 errors with it off. Turning
  // it off makes this gate hostage to third-party .d.ts quality, so a
  // Dependabot bump could redden CI for a defect we cannot fix in this tree.
```

- [ ] **Step 6: Verify the whole typecheck is green**

```bash
cd web && npx tsc --noEmit ; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 7: Prove each flag is actually enforced — differentially**

A probe that merely produces *an* error proves nothing about *which* setting
produced it. Each flag must be shown to error with the flag on and NOT error
with it off. (An earlier version of this step used a single probe with an
annotated return type; that emits TS2366 from `strictNullChecks`, which was
already on, so it would have "failed" identically either way.)

```bash
cd web && cat > src/__scratch_flagcheck.ts <<'EOF'
// noImplicitReturns (TS7030): return type must be INFERRED, not annotated --
// an annotation makes this TS2366 from strictNullChecks instead.
export function implicitReturn(x: number) {
  if (x > 0) {
    return 1;
  }
}
EOF
echo "on =$(npx tsc -p tsconfig.json 2>&1 | grep -c 'TS7030')"
echo "off=$(npx tsc -p tsconfig.json --noImplicitReturns false 2>&1 | grep -c 'TS7030')"
rm src/__scratch_flagcheck.ts
```

Expected: `on =1` and `off=0`. Anything else means the flag is not doing what
the config claims — stop and investigate before committing.

Repeat the same on/off differential for `noImplicitOverride` (TS4114),
`verbatimModuleSyntax` (TS1484), `noUnusedParameters` (TS6133) and
`erasableSyntaxOnly` (TS1294). Confirm `git status --porcelain` no longer lists
the scratch file afterwards.

- [ ] **Step 8: Run the full gate**

```bash
make check-ts
```

Expected: green.

- [ ] **Step 9: Commit**

```bash
git add web/tsconfig.json web/src/__tests__/bootstrap.test.ts web/src/__tests__/stream.test.ts
git commit -m "feat(web): enable the tsc strictness flags that cost nothing

noImplicitOverride, noImplicitReturns, verbatimModuleSyntax and
noUnusedParameters all measure 0 errors; erasableSyntaxOnly measures 2, both
parameter properties in test fakes. noImplicitOverride is the direct partner
to the Python tree's @override adoption (#55).

noUnusedLocals is deferred rather than declined: its single error is in
vendored source and our own tree has zero, so it costs nothing once tier 4's
vendored-diagnostic filter exists. tsc has no per-directory scoping and
exclude does not suppress errors in files reached through imports (verified).
Both that and the skipLibCheck decision are documented in tsconfig.json so a
later audit does not silently reverse them.

Assisted-by: Claude Opus 5"
```

---

### Task 3: Tier 2 — vitest mock and global isolation

Four settings, all measured at 928/928 passing. Today a `vi.spyOn` or
`vi.stubGlobal` in one test survives into the next.

**Files:**
- Modify: `web/vite.config.ts` (the `test` block)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Add the settings**

In `web/vite.config.ts`, immediately after `environment: "jsdom",`:

```ts
    // Isolation, measured free at 928/928 (2026-07-28). Without these a
    // vi.spyOn/vi.stubGlobal/vi.stubEnv in one test survives into the next,
    // so a suite can pass because of a leak rather than in spite of one.
    // requireAssertions additionally fails any test that reaches its end
    // without asserting — every existing test already does.
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
    expect: { requireAssertions: true },
```

- [ ] **Step 2: Run the full suite**

```bash
cd web && npx vitest run
```

Expected: `Test Files 77 passed (77)`, `Tests 928 passed (928)`.

- [ ] **Step 3: Prove `requireAssertions` is live — differentially**

```bash
cd web && cat > src/__tests__/zz_scratch.test.ts <<'EOF'
import { test } from "vitest";
test("asserts nothing", () => {});
EOF
echo "--- ON ---";  npx vitest run src/__tests__/zz_scratch.test.ts 2>&1 | tail -4
echo "--- OFF --- "; npx vitest run src/__tests__/zz_scratch.test.ts --expect.requireAssertions=false 2>&1 | tail -4
rm src/__tests__/zz_scratch.test.ts
```

Expected: ON fails with `expected any number of assertion, but got none` —
confirm it is that error and not an incidental one — and OFF passes.

- [ ] **Step 4: Prove `restoreMocks` is live — differentially**

`restoreMocks` has no CLI override (`--restoreMocks` is `CACError: Unknown
option`), so the OFF half needs a throwaway config that imports the real one
and overrides the single key. Same tree, same test, one variable:

```bash
cd web && cat > src/__tests__/zz_scratch.test.ts <<'EOF'
import { expect, test, vi } from "vitest";
const target = { fn: () => "real" };
test("a spies", () => {
  vi.spyOn(target, "fn").mockReturnValue("faked");
  expect(target.fn()).toBe("faked");
});
test("b sees the real thing", () => {
  expect(target.fn()).toBe("real");
});
EOF
cat > vite.zzscratch.config.ts <<'EOF'
import base from "./vite.config";
const cfg = base as { test: Record<string, unknown> };
cfg.test.restoreMocks = false;
export default base;
EOF
echo "--- ON ---";  npx vitest run src/__tests__/zz_scratch.test.ts 2>&1 | tail -4
echo "--- OFF ---"; npx vitest run -c vite.zzscratch.config.ts src/__tests__/zz_scratch.test.ts 2>&1 | tail -6
rm src/__tests__/zz_scratch.test.ts vite.zzscratch.config.ts
```

Expected: ON passes both tests; OFF fails test "b" with
`expected 'faked' to be 'real'`. **The OFF half is the whole proof** — a green
ON run alone is exactly what a typo'd config key also produces.

- [ ] **Step 4b: Prove `unstubGlobals` and `unstubEnvs` are live**

Same differential shape, stubbing a global in one test and asserting it is gone
in the next, then the same for `vi.stubEnv`. Expected: ON passes all four; OFF
fails the two "sees no stub" tests.

Note when reading the result: `vi.stubEnv` appears **zero** times in the tree
today, so `unstubEnvs` guards a pattern nobody currently uses. That is cheap
insurance, not an observed leak — do not describe it as fixing something.

- [ ] **Step 5: Run the full gate**

```bash
make check-ts && cd web && npx vitest run
```

Expected: both green.

- [ ] **Step 6: Commit**

```bash
git add web/vite.config.ts
git commit -m "test(web): isolate mocks, globals and env between vitest tests

restoreMocks/unstubEnvs/unstubGlobals stop a spy or stub in one test leaking
into the next; expect.requireAssertions fails a test that asserts nothing.
All four measured free at 928/928 -- nothing in the suite currently depends
on the leakage, which is the moment to close it.

Assisted-by: Claude Opus 5"
```

---

### Task 4: Tier 3 — register Testing Library cleanup globally

This project does not set vitest's `globals: true`, so
`@testing-library/react`'s automatic `afterEach(cleanup)` **is never
registered** — `vitest.setup.ts` already documents this for the
`IS_REACT_ACT_ENVIRONMENT` flag. Every test file must therefore call
`cleanup` itself, and `src/__tests__/clock.test.tsx` does not. Its mounted
components survive into the next test, and its render counters then count
renders from both trees.

One global registration fixes it and prevents the whole class. Verified: this
alone fixes `clock.test.tsx` under shuffle seeds 11, 22 and 33.

**Files:**
- Modify: `web/vitest.setup.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `vitest.setup.ts`'s `afterEach` now unmounts before restoring the
  console. Task 5 depends on this being in place — do not reorder.

- [ ] **Step 1: Reproduce the order dependence**

```bash
cd web && npx vitest run src/__tests__/clock.test.tsx --sequence.shuffle --sequence.seed=11 2>&1 | tail -12
```

Expected: FAIL on `useNow > advances at the collection interval, not faster`,
with a received render count higher than the expected `1`.

- [ ] **Step 2: Register cleanup in `web/vitest.setup.ts`**

Add to the imports:

```ts
import { cleanup } from "@testing-library/react";
```

Then call `cleanup()` as the **first statement of the existing `afterEach`**,
above the `console.warn` restore:

```ts
afterEach(() => {
  // Testing Library's automatic cleanup is NOT active in this project: its
  // auto-registration requires vitest's `globals: true`, which we do not use
  // (same reason IS_REACT_ACT_ENVIRONMENT is set by hand above). Without this,
  // every mounted tree survives its own test -- harmless under a fixed file
  // order, and a wrong render count as soon as the order changes.
  //
  // It unmounts INSIDE this hook, before the console interceptor is removed
  // below, so a warning emitted during unmount is still captured and billed
  // to the test that caused it. Registering a separate `afterEach(cleanup)`
  // would work too, but only under an assumption about the order vitest runs
  // sibling hooks in; this construction needs no such assumption.
  cleanup();
  console.warn = original.warn;
  console.error = original.error;
  if (!allowed && captured.length > 0) {
    const output = captured.join("\n  ");
    captured = [];
    throw new Error(
      `test emitted console warnings/errors (fix them, or call allowConsoleOutput() if the test exercises a warning path on purpose):\n  ${output}`,
    );
  }
  captured = [];
});
```

- [ ] **Step 3: Verify the reproduction is fixed**

```bash
cd web && for s in 11 22 33; do npx vitest run src/__tests__/clock.test.tsx --sequence.shuffle --sequence.seed=$s 2>&1 | grep -E "Tests +[0-9]"; done
```

Expected: `Tests 3 passed (3)` three times.

- [ ] **Step 4: Verify nothing else regressed**

```bash
cd web && npx vitest run
```

Expected: `Tests 928 passed (928)`. A lower count means cleanup broke a test
that depended on a surviving tree — investigate rather than adjust the count.

- [ ] **Step 5: Commit**

```bash
git add web/vitest.setup.ts
git commit -m "test(web): register Testing Library cleanup globally

Auto-cleanup needs vitest's globals:true, which this project does not use, so
it has never been active -- every file has to call cleanup itself and
clock.test.tsx does not. Its trees survived into the next test and inflated
its render counters, invisibly under a fixed file order.

Registering once in the setup file fixes clock.test.tsx under shuffle seeds
11/22/33 and means a new test file cannot forget it.

Assisted-by: Claude Opus 5"
```

---

### Task 5: Tier 3 — fix FilePage's post-test state update, then shuffle

`FilePage.test.tsx`'s loading-state test is synchronous but starts a chunk load
that resolves *after* the test body returns, so React applies that state update
outside `act()`.

An earlier version of this task claimed the console guard "bills the warning to
whichever test runs next". That was written from reasoning, not measurement,
and is false: the guard names the correct test and throws from that test's own
`afterEach`. What actually varies with order is whether the warning is emitted
**at all** — under the default file order a full run emits zero `not wrapped in
act` messages.

**Files:**
- Modify: `web/src/covapp/pages/FilePage.test.tsx:331-335`
- Modify: `web/vite.config.ts` (the `test` block)

**Interfaces:**
- Consumes: the global `afterEach(cleanup)` from Task 4.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Reproduce**

```bash
cd web && npx vitest run --sequence.shuffle --sequence.seed=11 2>&1 | grep -E "^ FAIL|Tests +[0-9]"
```

Expected: exactly one FAIL, on
`FilePage > shows a minimal loading state before the chunk resolves`.

- [ ] **Step 2: Make the test await the settled render**

In `web/src/covapp/pages/FilePage.test.tsx`, replace lines 331-335:

```tsx
  it("shows a minimal loading state before the chunk resolves", () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    expect(screen.getByTestId("file-loading").textContent).toContain("tcp.c");
  });
```

with:

```tsx
  it("shows a minimal loading state before the chunk resolves", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    expect(screen.getByTestId("file-loading").textContent).toContain("tcp.c");
    // The chunk promise resolves after this body returns. Awaiting the loaded
    // render keeps that state update inside this test's act() scope; without
    // it React applies the update after teardown and the console guard bills
    // the act() warning to whichever test happens to run next.
    //
    // NB: `expect.requireAssertions` (Task 3) counts `expect()` calls only --
    // a bare `findBy*` throws when the element never appears but does NOT
    // count as an assertion. This test is fine because the `expect` above it
    // counts; do not copy the trailing-findBy pattern into a test that has no
    // other assertion, or the failure message will talk about assertion
    // counts rather than about what actually broke.
    await screen.findByTestId("code-row-1");
  });
```

- [ ] **Step 3: Verify the fix across seeds**

```bash
cd web && for s in 11 22 33; do npx vitest run --sequence.shuffle --sequence.seed=$s 2>&1 | grep -E "Tests +[0-9]"; done
```

Expected: `Tests 928 passed (928)` three times.

- [ ] **Step 4: Enable shuffle permanently**

In `web/vite.config.ts`, add after the isolation settings from Task 3:

```ts
    // Randomized order, the pytest-randomly analogue. Fixed order hides
    // cross-test coupling: it hid a missing cleanup in clock.test.tsx and a
    // post-teardown state update in FilePage.test.tsx, both of which passed
    // for as long as the file order never changed. The seed is not pinned
    // (vitest prints it on failure), matching pytest-randomly.
    sequence: { shuffle: true },
```

- [ ] **Step 5: Run unseeded, several times**

```bash
cd web && for i in 1 2 3; do npx vitest run 2>&1 | grep -E "Tests +[0-9]"; done
```

Expected: `Tests 928 passed (928)` three times. Any failure here is a real
order dependence this plan has not yet found — fix it in the offending test
rather than pinning the seed.

- [ ] **Step 6: Run the full gate**

```bash
make check-ts && cd web && npx vitest run --coverage
```

Expected: `make check-ts` green; coverage green with all four unit-tier floors
met (statements 81, branches 73, functions 80, lines 82).

- [ ] **Step 7: Commit**

```bash
git add web/src/covapp/pages/FilePage.test.tsx web/vite.config.ts
git commit -m "test(web): randomize vitest order, fix the coupling it exposed

FilePage's loading-state test was synchronous but started a chunk load that
resolved after the body returned, so React applied the state update outside
act() and the console guard attributed the warning to an unrelated test.
Awaiting the loaded render keeps the update in scope.

sequence.shuffle is then enabled -- the pytest-randomly analogue. Fixed order
was hiding both this and the missing cleanup fixed in the previous commit.
Verified across seeds 11/22/33 and three unseeded runs.

Assisted-by: Claude Opus 5"
```

---

## Out of scope for this plan

**Tier 4** is now unblocked — a wrapper limiting tsc to non-vendored paths was
approved on 2026-07-28 — and gets its own plan. It adds
`scripts/typecheck_web.sh` (vendored path list derived from
`web/untitledui.lock.json`'s `paths`, the same source
`scripts/check_untitledui_hash.sh` uses), then lands four flags cheapest-first:
`noUnusedLocals` (0 errors in our code), `noPropertyAccessFromIndexSignature`
(77), `exactOptionalPropertyTypes` (23 ours of 100), and
`noUncheckedIndexedAccess` (441 ours of 452).

That last one is the flag most at risk from the guiding principle above. 441
sites is enough volume to make `arr[i]!` tempting, and that "fix" makes the
code worse. Declining the flag is an acceptable outcome if the sites turn out
to want assertions rather than restructuring.

**Tiers 5-6** (Biome `preset: "all"`, per-file coverage on the merged gate) get
a plan after Tier 4 lands. Writing it sooner would plan against numbers that
are about to move: changing 441 indexed accesses shifts the
`noNonNullAssertion` count, and `noUnnecessaryConditions` is type-aware and so
responds to the same edits.
