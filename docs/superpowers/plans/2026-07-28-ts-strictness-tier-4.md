# TypeScript strictness, Tier 4 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw the vendored-source boundary in `tsc` — the one tool missing it —
and then adopt the three strictness flags that boundary makes affordable.

**Architecture:** One new script, `scripts/typecheck_web.sh`, runs `tsc` and
discards diagnostics whose path is in the vendored set (derived from
`web/untitledui.lock.json`'s `paths`, the same source
`scripts/check_untitledui_hash.sh` uses, so the two cannot disagree about what
"vendored" means). Diagnostics with no file path — config errors, crashes — are
always surfaced, so the filter can never hide a failure it did not classify.
With that in place, four flags land one per commit, ordered by how mechanical
they are rather than by raw count: `noUnusedLocals` (0 sites), then
`noPropertyAccessFromIndexSignature` (77, but uniformly one error code), then
`exactOptionalPropertyTypes` (23 heterogeneous sites needing per-site
judgement), then `noUncheckedIndexedAccess` (441, split across two tasks).

**Tech Stack:** TypeScript 7.0.2, bash, jq, Biome 2.5.5, vitest 4.1.10.

## Guiding principle — read before every task

**The goal is better code: more readable, harder to break. These tools' numbers
tell you where to look. They are not the target.** (Chris, 2026-07-28.)

This overrides any instruction below that appears to conflict with it.

- **The test is whether the signal stays VISIBLE.** A bare `!`, an `as` cast, an
  `_`-prefixed rename, or an arbitrary function split are failures — they read
  as ordinary code, so the problem becomes unfindable.
- **A localized ignore annotation is a legitimate tool, used judiciously.** A
  genuinely nasty site may be annotated with the rule name and a real reason and
  revisited later. Unlike a bare assertion it stays greppable. **Prefer this
  over a bad fix.**
- **A global rule-off is for stylistic disagreement**, not for dodging hard sites.
  Task 2 hit the textbook case: Biome's `complexity/useLiteralKeys` is the exact
  INVERSE of `noPropertyAccessFromIndexSignature`, telling the reader to undo
  every honest bracket access. tsc's rule carries type information, Biome's is
  cosmetic, so Biome's was turned off. Watch for more of these in Tier 5.
- **A flag may be declined outright.** If its sites want suppressions rather than
  restructuring, stop and report.
- **This tier is where that risk is real.** `noUncheckedIndexedAccess` has enough
  volume to make `arr[i]!` tempting, and that "fix" converts a checked access
  into an unchecked one — strictly worse than not enabling the flag.
- **Prove a setting live differentially:** it must error with the setting on and
  NOT error with it off. This tree has `strict: true`, a console guard, five
  escalated Biome rules and now a filter script, any of which will produce a
  plausible-looking failure for the wrong reason.
- **A flag's site count goes STALE as earlier tasks land.** Task 2's fixes
  created four NEW `exactOptionalPropertyTypes` errors (annotating
  `stateColors` as `Partial<IndexPayload["state_colors"]>` traded 4 × TS4111 for
  4 × TS2375), taking Task 3 from 23 to 27. Re-measure at the start of every
  task. A delta is something to **attribute** — revert the suspected change,
  re-measure, confirm — not something to adapt to silently, and not
  automatically a reason to stop.
- **Check exit codes without a pipe.** `cmd | tail -3; echo $?` reports `tail`'s
  status, not `cmd`'s. Redirect to a file and echo `$?` on the next line.
- **A tool that did not run is not a tool that found nothing.** Task 1's script
  was prototyped against five cases and still shipped a false green: `|| true`
  after `npx tsc` meant a crash — no `error TS` text at all — produced zero
  diagnostics in both buckets and exited 0, so the gate would pass on a
  type-checker that checked nothing. Whenever you wrap a tool, test the case
  where the tool DIES, not just where it reports problems. A config error still
  prints `error TS…`; a crash prints nothing recognisable, and those are
  different tests.

## Global Constraints

- **Never hand-edit vendored Untitled UI source** (`web/src/components/**`,
  `web/src/styles/theme.css`, `web/src/utils/cx.ts`,
  `web/src/utils/is-react-component.ts`, `web/src/hooks/use-breakpoint.ts`,
  `web/src/hooks/use-resize-observer.ts`). `make check-ts` runs
  `scripts/check_untitledui_hash.sh`, which fails on any byte change. This tier
  exists precisely so those files stop blocking our own strictness.
- **Do not add a step to `.github/workflows/ci.yml`.**
  `tests/unit/test_ci_web_gate.py` pins the `check-ts` job to
  `runs == ["make check-ts coverage-ts-unit"]`. It also pins
  `web/package.json`'s scripts — run
  `uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q` after touching them.
- **`web/tsconfig.json` and `web/vite.config.ts` have consumers beyond
  `make check-ts`:** `scripts/check_untitledui_drift.sh` copies both into a
  throwaway project, and `make web` reads both as build inputs. Run `make web`
  after editing either.
- **No `overrides` block or blanket test-tier exemption** without evidence for
  that specific rule. Task 5 is where such evidence gets weighed.
- **Never `git push`.** Commit on this worktree branch. Messages use a
  conventional prefix and end with `Assisted-by: Claude Opus 5`.
- Never use bare `git stash` / `git stash pop` — the stash stack is shared.
- **Baseline:** 928 tests in 77 files, green at `fe4248c0`. `make check-ts`,
  `make coverage-ts-unit` and `make web` all green. Any task changing the test
  count must say why.

## Measured baseline

Taken at `fe4248c0`. TOTAL is what plain `tsc` reports; OURS is what remains
after vendored paths are filtered — the only number that represents work.

| Flag | Total | Vendored | OURS | Task |
| ---- | ---- | ---- | ---- | ---- |
| `noUnusedLocals` | 1 | 1 | 0 | ~~1~~ dropped: Biome owns this |
| `noPropertyAccessFromIndexSignature` | 77 | 0 | 77 | 2 |
| `exactOptionalPropertyTypes` | 104 | 77 | 27 | 3 |
| `noUncheckedIndexedAccess` | 452 | 11 | **433** (re-measured after Task 3) | 4 + 5 |

Error-code composition of the OURS columns:

- `noPropertyAccessFromIndexSignature`: 77 × TS4111, uniform.
- `exactOptionalPropertyTypes`: 14 × TS2375, 5 × TS2322, 4 × TS2379.
- `noUncheckedIndexedAccess`: **re-measured after Task 3 landed — 433 ours,
  split 72 in `src/` vs 361 in test files.** The plan originally said 63/378.
  Both moved, for different reasons, and neither is drift to adapt around:
  the test count fell to 361 because Task 2 fixed fixtures, and the `src/`
  count ROSE to 72 because of a flag interaction — `noUncheckedIndexedAccess`
  makes an index read `T | undefined`, and `exactOptionalPropertyTypes` (Task
  3) then rejects that at a `dotColor?: string` target. So **~9 of the 72 are
  widen-the-target work, not indexed-access work** (`covapp/format.ts` ×4, plus
  `tickets.ts`, `AppShell`, `RunsPage`, `TicketsPage`, `FilePage`), and they
  carry TS2322/TS2375 rather than an indexed-access code.

`src/` concentration for `noUncheckedIndexedAccess`: `topo/measure.ts` 29,
`topo/layout.ts` 11, `data/health.ts` 8, `charts/options.ts` 8,
`data/topology.ts` 3, then five files with 1 each.

---

### Task 1: The vendored-source filter (DONE — see the note on `noUnusedLocals`)

`tsconfig` cannot scope rules by directory, and `exclude` does not suppress
diagnostics in a file reached through an import (verified: 40 of 77 vendored
`exactOptionalPropertyTypes` errors survive it). A wrapper is the only
config-expressible option.

**This script was prototyped and verified against five cases before this plan
was written — and still shipped a false green.** The five cases all had `tsc`
actually running; none covered `tsc` dying. Step 3 now includes that case as F.
Reproduce all of them; do not take any of it on trust.

**Files:**
- Create: `scripts/typecheck_web.sh`
- Modify: `Makefile:767-769` (the `typecheck-ts` recipe)
- Modify: `web/tsconfig.json` (add `noUnusedLocals`)
- Modify: `web/package.json` — its `build` script is `tsc && vite build`, i.e.
  the UNFILTERED checker on the same tsconfig, so `make web` reddens on the
  vendored `noUnusedLocals` error the moment the flag goes on. Route that leg
  through the filter too. Do not simply drop the `tsc`, or `make web` would
  happily compile type-broken code.

**Interfaces:**
- Consumes: nothing.
- Produces: `scripts/typecheck_web.sh`, invoked with no arguments, exiting 0 iff
  our own code typechecks. Tasks 2-4 rely on it existing and on `make check-ts`
  routing through it.

- [ ] **Step 1: Write `scripts/typecheck_web.sh`**

```bash
#!/usr/bin/env bash
# Type-check web/ with the vendored Untitled UI source filtered OUT.
#
# The vendored boundary is drawn in Biome (files.includes), knip (ignore) and
# coverage (exclude) -- tsc was the one tool missing it, and tsconfig cannot
# express it: `exclude` only removes a file from the program's ROOT set, and a
# file reached through an import is added and checked regardless (verified --
# 40 of 77 vendored exactOptionalPropertyTypes errors survived it). The only
# per-file suppression TypeScript offers is `// @ts-nocheck`, which would mean
# hand-editing vendored source -- forbidden by check_untitledui_hash.sh.
#
# This is NOT a budget or a ratchet. A ratchet is a place for OUR OWN defects
# to accumulate under a green gate, and was rejected for that reason. This
# excludes code we are forbidden to touch and can never fix. The distinction
# is whether the excluded work is ours to do.
#
# The vendored path list is DERIVED from untitledui.lock.json's `paths`, using
# the same expansion check_untitledui_hash.sh uses ("<prefix>/**" means
# everything under <prefix>; anything else is a literal path), so the two
# cannot drift on what counts as vendored.
#
# Diagnostics with NO file path -- config errors, tsc crashes -- are ALWAYS
# surfaced. A filter that can only recognise the thing it drops must never be
# able to hide a failure it did not classify.
#
# Usage: scripts/typecheck_web.sh [extra tsc flags]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
LOCKFILE="$WEB_DIR/untitledui.lock.json"

if [ ! -f "$LOCKFILE" ]; then
    echo "typecheck_web: '$LOCKFILE' does not exist." >&2
    exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "typecheck_web: 'jq' is required but not on PATH." >&2
    exit 1
fi

VENDORED_RE="$(
  jq -r '.paths[]' "$LOCKFILE" | while IFS= read -r p; do
    if [[ "$p" == */\*\* ]]; then
      printf '^%s/|' "$(printf '%s' "${p%/**}" | sed 's/[][\\^$.*+?(){}|]/\\&/g')"
    else
      printf '^%s\\(|' "$(printf '%s' "$p" | sed 's/[][\\^$.*+?(){}|]/\\&/g')"
    fi
  done | sed 's/|$//'
)"
if [ -z "$VENDORED_RE" ]; then
    echo "typecheck_web: lockfile '.paths' is empty -- refusing to run unfiltered." >&2
    exit 1
fi

cd "$WEB_DIR"
# Keep tsc's status. `|| true` alone is a FALSE GREEN: a crash (OOM, npx failing
# to resolve typescript, a compiler RangeError) prints nothing matching
# `error TS`, so both buckets below come back empty and the gate passes on a
# type-check that never happened. Verified with a shim npx that exits 7.
rc=0
raw="$(npx tsc -p tsconfig.json --noEmit --pretty false "$@" 2>&1)" || rc=$?

DIAG_RE='^[^ ].*\([0-9]+,[0-9]+\): error TS'
ours="$(printf '%s\n' "$raw"  | grep -E  "$DIAG_RE" | grep -vE "$VENDORED_RE" || true)"
other="$(printf '%s\n' "$raw" | grep -E 'error TS'  | grep -vE "$DIAG_RE"     || true)"

n_ours=$(printf '%s' "$ours"   | grep -c . || true)
n_other=$(printf '%s' "$other" | grep -c . || true)
n_all=$(printf '%s\n' "$raw"   | grep -cE "$DIAG_RE" || true)
n_vendored=$(( n_all - n_ours ))

[ -n "$ours" ]  && printf '%s\n' "$ours"
[ -n "$other" ] && printf '%s\n' "$other"

if [ "$n_ours" -ne 0 ] || [ "$n_other" -ne 0 ]; then
    echo "typecheck_web: FAILED -- $n_ours error(s) in our code, $n_other unclassified." >&2
    exit 1
fi
if [ "$rc" -ne 0 ] && [ "$n_all" -eq 0 ]; then
    printf '%s\n' "$raw" >&2
    echo "typecheck_web: FAILED -- tsc exited $rc without emitting a single recognisable" >&2
    echo "                diagnostic, so nothing was actually type-checked." >&2
    exit 1
fi
echo "typecheck_web: OK -- our code is clean ($n_vendored vendored diagnostic(s) ignored)."
```

Then `chmod +x scripts/typecheck_web.sh`.

- [ ] **Step 2: Route the gate through it**

In `Makefile`, change the `typecheck-ts` recipe (line 769) from
`@cd web && npm run typecheck` to:

```make
	@scripts/typecheck_web.sh
```

Leave `web/package.json`'s `typecheck` script alone — it stays the raw,
unfiltered `tsc --noEmit` for ad-hoc use, and `tests/unit/test_ci_web_gate.py`
may assert on it.

- [ ] **Step 3: Reproduce all five prototype cases**

Each must match. Note the redirect — a pipe would report the wrong exit code.

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
# A. clean tree -> OK
scripts/typecheck_web.sh > /tmp/a.out 2>&1; echo "A exit=$?"; tail -1 /tmp/a.out
# B. a flag whose ONLY error is vendored -> OK, and says it ignored 1
scripts/typecheck_web.sh --noUnusedLocals > /tmp/b.out 2>&1; echo "B exit=$?"; tail -1 /tmp/b.out
# C. a flag with 77 vendored + 23 ours -> FAIL
scripts/typecheck_web.sh --exactOptionalPropertyTypes > /tmp/c.out 2>&1; echo "C exit=$?"; tail -1 /tmp/c.out
# D. a real error in OUR code -> FAIL
printf 'export const bad: number = "nope";\n' > web/src/__scratch_err.ts
scripts/typecheck_web.sh > /tmp/d.out 2>&1; echo "D exit=$?"; head -1 /tmp/d.out
rm web/src/__scratch_err.ts
# E. an error with NO path must not be swallowed -> FAIL
scripts/typecheck_web.sh --target notAVersion > /tmp/e.out 2>&1; echo "E exit=$?"; head -1 /tmp/e.out
# F. tsc DYING must fail, not report a clean tree. This is the case the
#    original five missed -- a crash prints nothing matching `error TS`.
mkdir -p /tmp/shimbin && printf '#!/usr/bin/env bash\necho "RangeError" >&2\nexit 7\n' > /tmp/shimbin/npx
chmod +x /tmp/shimbin/npx
PATH=/tmp/shimbin:$PATH scripts/typecheck_web.sh > /tmp/f.out 2>&1; echo "F exit=$?"; tail -1 /tmp/f.out
rm -rf /tmp/shimbin /tmp/[abcdef].out
```

Expected: `A exit=0` (`0 vendored diagnostic(s) ignored`), `B exit=0`
(`1 vendored diagnostic(s) ignored`), `C exit=1` with 23 errors listed,
`D exit=1` naming `src/__scratch_err.ts`, `E exit=1` showing TS6046, and
`F exit=1` saying nothing was actually type-checked.

**Case E is the one that matters most.** A filter that silently swallowed an
unclassifiable failure would be worse than no filter, because the gate would be
green and wrong.

- [ ] **Step 4: Confirm the filter does not OVER-match**

Case D proves the filter does not drop everything. This proves it does not drop
things that merely *look* vendored — a sloppy regex like `^src/component` would
silently exempt a file of ours:

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
printf 'export const bad: number = "nope";\n' > web/src/component_scratch.ts
scripts/typecheck_web.sh > /tmp/over.out 2>&1; echo "exit=$?"; head -1 /tmp/over.out
rm web/src/component_scratch.ts /tmp/over.out
```

Expected: `exit=1`, naming `src/component_scratch.ts`. If this passes, the
regex is anchored too loosely and is exempting our own code.

- [ ] **Step 5: ~~Enable `noUnusedLocals`~~ — SUPERSEDED**

This step was executed and then **reversed**. Under the tool-ownership policy
added to the spec on 2026-07-28, Biome owns the unused-code family;
`noUnusedLocals` and `noUnusedParameters` are exact duplicates of its
`noUnusedImports` / `noUnusedVariables` / `noUnusedFunctionParameters` and are
now absent from `tsconfig.json`. Skip this step; the rationale lives in
`tsconfig.json`'s comment block.

Original text, kept for the record:

In `web/tsconfig.json`, add `"noUnusedLocals": true,` and replace the deferral
note in the comment block with:

```jsonc
  // noUnusedLocals is enabled as of tier 4: its only error was in vendored
  // source (src/components/base/buttons/button.tsx, an unused `React` import)
  // and our own tree has zero. scripts/typecheck_web.sh now filters vendored
  // diagnostics, so the flag costs nothing. It duplicates Biome's
  // noUnusedVariables deliberately -- the two run in different gate legs, so a
  // misconfiguration of one does not silently drop the check.
```

- [ ] **Step 6: Verify**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh > /tmp/f.out 2>&1; echo "exit=$?"; tail -1 /tmp/f.out
make check-ts
make web
uv run --no-sync pytest tests/unit/test_ci_web_gate.py -q
rm -f /tmp/f.out
```

Expected: `exit=0` reporting 1 vendored diagnostic ignored; `make check-ts`
green; `make web` green; the CI-gate test green.

- [ ] **Step 7: Commit**

```bash
git add scripts/typecheck_web.sh Makefile web/tsconfig.json
git commit -m "feat(web): filter vendored source out of tsc, enable noUnusedLocals

The vendored boundary was drawn in Biome, knip and coverage but not tsc, and
tsconfig cannot express it -- exclude only removes a file from the program's
root set, and a file reached through an import is checked regardless.

scripts/typecheck_web.sh derives the vendored path list from
untitledui.lock.json's paths, the same source check_untitledui_hash.sh uses,
so the two cannot disagree about what vendored means. Diagnostics with no file
path are always surfaced: a filter must never hide a failure it could not
classify.

Not a ratchet. A ratchet is a place for our own defects to accumulate under a
green gate; this excludes code we are forbidden to touch.

noUnusedLocals rides along -- its only error was the unused React import in
vendored button.tsx, and our tree has zero.

Assisted-by: Claude Opus 5"
```

---

### Task 2: `noPropertyAccessFromIndexSignature` (77 sites, all TS4111)

TS4111 means a property came from an index signature but was read with dot
notation. The flag makes index-signature access visually distinct from
declared-property access.

**Read this before starting.** The mechanical fix is `obj.foo` → `obj["foo"]`,
and for many sites that is *less* readable, not more. Per the guiding
principle, prefer fixing the **type** where the property is genuinely part of a
known shape — declaring it makes both the access and the contract better. Use
bracket access only where the key really is dynamic. If a file's sites are all
"the type should have declared this", say so and fix the type.

**Files:**
- Modify: `web/tsconfig.json`
- Modify: whichever source files the errors name. Top five by count:
  `src/__tests__/chartpanel.test.tsx` (11), `src/__tests__/toporouting.test.ts`
  (7), `src/covapp/data.ts` (6), `src/__tests__/chartoptions.test.ts` (5),
  `src/data/bootstrap.ts` (5).

**Interfaces:**
- Consumes: `scripts/typecheck_web.sh` from Task 1.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Get the full site list**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh --noPropertyAccessFromIndexSignature > /tmp/t4111.txt 2>&1
echo "exit=$?"; grep -c "TS4111" /tmp/t4111.txt
# NB: do NOT filter to '^src/' here -- the 77th site is vite.config.ts, which
# lives at web/'s root and which the Global Constraints flag as having
# consumers beyond `make check-ts`. A '^src/' filter sums to 76 and hides it.
grep "error TS4111" /tmp/t4111.txt | sed 's/(.*//' | sort | uniq -c | sort -rn
```

Expected: `exit=1`, 77 TS4111 lines. If the count differs, STOP and report.

- [ ] **Step 2: Triage before editing**

Group the 77 by whether the underlying type *should* declare the property.
Write that split into your report. Do not start editing until you can say, per
file, which category it is in — this is the step that keeps the task from
becoming 77 mechanical substitutions.

- [ ] **Step 3: Fix, type-first**

Where the type should declare the property, declare it. Where the key is
genuinely dynamic, use bracket access. Where a site is genuinely nasty, a
localized `// @ts-expect-error TS4111: <reason>` is acceptable — but each one
must be reported.

Worked example of the *bracket-access* category — `src/data/bootstrap.ts:45-48`
is a type guard over untrusted input:

```ts
const rec = value as Record<string, unknown>;
return (
  (rec.mode === "live" || rec.mode === "review") &&
  (typeof rec.source === "string" || rec.source === null) &&
```

Here bracket access is the honest fix, not a concession: `rec` is deliberately
`Record<string, unknown>` because the value is unvalidated, and `rec["mode"]`
correctly signals "this key may not exist". Declaring a type would be wrong —
it would assert the very shape this function exists to check.

The contrasting category is a value that has a real known shape and was typed
as a bare record out of convenience. There, declare the interface. Report the
split between the two.

- [ ] **Step 4: Enable the flag and verify**

Add `"noPropertyAccessFromIndexSignature": true,` to `web/tsconfig.json`, then:

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh > /tmp/g.out 2>&1; echo "exit=$?"; tail -1 /tmp/g.out
cd web && npx vitest run 2>&1 | grep -E "Tests +[0-9]"
cd .. && make check-ts && make web
rm -f /tmp/g.out /tmp/t4111.txt
```

Expected: `exit=0`; 928 tests passing; both gates green.

- [ ] **Step 5: Differential proof the flag is live**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
cat > web/src/__scratch_4111.ts <<'EOF'
const bag: Record<string, number> = {};
export const v = bag.anything;
EOF
scripts/typecheck_web.sh > /tmp/on.out 2>&1;  echo "on =$(grep -c TS4111 /tmp/on.out)"
scripts/typecheck_web.sh --noPropertyAccessFromIndexSignature false > /tmp/off.out 2>&1
echo "off=$(grep -c TS4111 /tmp/off.out)"
rm web/src/__scratch_4111.ts /tmp/on.out /tmp/off.out
git status --porcelain
```

Expected: `on =1`, `off=0`, and a clean tree.

- [ ] **Step 6: Commit** with a message stating how many sites were fixed by
declaring the type versus by bracket access, and listing any `@ts-expect-error`
added and why.

---

### Task 3: `exactOptionalPropertyTypes` (27 sites)

This flag distinguishes "key absent" from "key present with value `undefined`" —
a real bug class when a target type uses `?:` and the source explicitly passes
`undefined`.

**27, not the 23 measured when this plan was written.** Task 2 annotated
`stateColors` as `Partial<IndexPayload["state_colors"]>` in
`src/covapp/chrome/AppShell.tsx`, which made four `KeyRow color=` reads
`string | undefined`, trading 4 × TS4111 for 4 × TS2375. Benign and same-class,
attributed by reverting that one annotation and re-measuring (23, with the
original code composition). Composition now: 18 × TS2375, 5 × TS2322, 4 × TS2379.

Top files: `src/covapp/chrome/AppShell.tsx` (5, the largest and a consequence of
Task 2), `src/__tests__/toponodes.test.tsx` (4), `src/__tests__/topolayout.test.ts`
(2), `src/shell/ReviewBar.tsx` (2), `src/covapp/tickets.ts` (2), then singles.

**Three fix categories, not two.** The first two, in order of preference:

1. **Omit the key** — a conditional spread, so the property is genuinely absent
   rather than present-and-undefined. This is the right fix whenever the target
   is vendored or third-party, because you cannot change its declaration.
2. **Widen the target** to `x?: T | undefined` — only when the target is OURS
   and genuinely accepts an explicit undefined. Widening once at a shared
   declaration can beat several spreads at the call sites (the four new
   `AppShell` sites are one `KeyRow.color` widening).

An earlier draft of this plan named `src/ui/TextInput.tsx:50` as the exemplar of
category 2. **That was wrong, and backwards.** Its target is the *vendored*
`InputBaseProps`, which by definition cannot be widened; widening our own
wrapper's props leaves the error byte-identical (verified), because the
rejection happens at the target's declaration. TextInput is a category 1 site
and conditional spreads fix it (verified: TextInput errors drop to 0).

3. **Neither end is editable.** `TicketSearch.tsx:93`, `RunsPage.tsx:481` and
   `TicketsPage.tsx:457` pass `icon={SearchMd}` to the vendored `Input`. The
   source type is `FC<Props>` from `@untitledui/icons`, which redeclares
   `color?: string; size?: number` *without* `| undefined`, overriding React's
   `SVGProps`; contravariant parameter checking then fails. The key is not
   undefined, so neither category 1 nor 2 applies, and `skipLibCheck` does not
   help — it suppresses errors *inside* a `.d.ts`, not assignability at the use
   site. Resolve with one real adapter component shared by all three call sites
   (dropping the incompatible props — an adapter, not a cast), or, if that
   cannot be written without a cast, three localized
   `// @ts-expect-error TS2322: <reason>` annotations. The annotation is the
   sanctioned option here; a silent `as` is not.

**Files:**
- Modify: `web/tsconfig.json`, plus the files the errors name.

**Interfaces:**
- Consumes: `scripts/typecheck_web.sh` from Task 1.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Get the site list**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh --exactOptionalPropertyTypes > /tmp/teopt.txt 2>&1
echo "exit=$?"; grep -cE "TS2375|TS2322|TS2379" /tmp/teopt.txt
```

Expected: `exit=1`, 27 errors (18 × TS2375, 5 × TS2322, 4 × TS2379). If the
count differs, attribute the delta to a specific earlier change before
proceeding — see the standing constraint on stale counts.

- [ ] **Step 2: Fix each site**, choosing omit-the-key or widen-the-target per
the guidance above. A vendored target type can only be accommodated, never
changed.

- [ ] **Step 3: Enable and verify**

Add `"exactOptionalPropertyTypes": true,` to `web/tsconfig.json`, then run the
same verification block as Task 2 Step 4. Expected: `exit=0`, 928 tests, both
gates green.

- [ ] **Step 4: Differential proof**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
cat > web/src/__scratch_eopt.ts <<'EOF'
interface T { a?: number }
const src: { a: number | undefined } = { a: undefined };
export const t: T = src;
EOF
scripts/typecheck_web.sh > /tmp/on.out 2>&1;  echo "on =$(grep -cE 'TS2375|TS2322' /tmp/on.out)"
scripts/typecheck_web.sh --exactOptionalPropertyTypes false > /tmp/off.out 2>&1
echo "off=$(grep -cE 'TS2375|TS2322' /tmp/off.out)"
rm web/src/__scratch_eopt.ts /tmp/on.out /tmp/off.out
git status --porcelain
```

Expected: `on =1`, `off=0`, clean tree.

- [ ] **Step 5: Commit.**

---

### Task 4: `noUncheckedIndexedAccess` — `src/` only (72 sites)

**STOP AND READ.** This flag has 433 sites in our code (re-measured after Task
3 — the plan's original 441/63/378 is superseded). Only **72 are in `src/`**;
361 are in test files. Nine of the 72 are `exactOptionalPropertyTypes`
fallout rather than indexed-access work — see the baseline section. This task does the 72. Task 5 decides what to
do about the 378 — that decision is deliberately made *after* seeing how these
63 go, not before.

Do **not** enable the flag in `tsconfig.json` in this task. It would redden the
gate on 378 test-file errors. Work against
`scripts/typecheck_web.sh --noUncheckedIndexedAccess` and leave the config for
Task 5.

**The fixes that count are structural.** Two worked examples from this tree:

`src/charts/options.ts:171` sorts an array of *indices* and then indexes back
into the source array, which TypeScript cannot prove safe:

```ts
const order = events
  .map((_, i) => i)
  .sort((a, b) => {
    const byStart = events[a].fromMs - events[b].fromMs;
    return byStart !== 0 ? byStart : events[a].toMs - events[b].toMs;
  });
```

Sorting the values and projecting the index out at the end removes the indexing
entirely, and reads better:

```ts
const order = events
  .map((event, index) => ({ event, index }))
  .sort((a, b) => {
    const byStart = a.event.fromMs - b.event.fromMs;
    return byStart !== 0 ? byStart : a.event.toMs - b.event.toMs;
  })
  .map(({ index }) => index);
```

**Keep the explicit `byStart !== 0` form.** An earlier draft of this plan
collapsed it to `a.event.fromMs - b.event.fromMs || …`, which is NOT
behaviour-preserving: the two differ whenever `fromMs` is `NaN`, because
`NaN !== 0` is true but `NaN` is falsy. Measured on 200,000 NaN-seeded arrays,
**35% sort differently**. That is reachable, not theoretical — `parseTs`
(`src/data/time.ts`) is `Date.parse`, which returns `NaN` on a malformed wire
timestamp. The tidier-looking rewrite would have introduced a real defect while
"improving" the code.

This example covers only 4 of the file's 8 sites. The rest — a `while` loop
indexing `laneEnds`, a loop body reading `events[i]`, and a `lanes[i]` read
zipped against `spans[i]` **across a function boundary** — need their own
treatment. The last of those is the one that matters; see the note on real bugs
below.

`src/topo/measure.ts:83-85` destructures four numbers after a `nums.length === 4`
guard TypeScript cannot connect to the destructure:

```ts
if (commands.length === 2 && commands[0] === "M" && commands[1] === "L" && nums.length === 4) {
  const [sx, sy, tx, ty] = nums;
```

The improving fix is a small helper that performs the length check and returns a
real tuple type, so the invariant is named once instead of being implicit in a
compound condition. **Do not** reach for `nums[0]!`.

**Files:**
- Modify: `src/topo/measure.ts` (29), `src/topo/layout.ts` (11),
  `src/data/health.ts` (8), `src/charts/options.ts` (8),
  `src/data/topology.ts` (3), `src/covapp/format.ts` (4 — these are the
  `exactOptionalPropertyTypes` fallout, not indexed-access work), and one each in
  `src/shell/SubjectHealthBanner.tsx`, `src/data/seriesIndex.ts`,
  `src/data/reviewStore.ts`, `src/data/logevents.ts`. Sums to 72.
- Do NOT modify `web/tsconfig.json` in this task.

**Interfaces:**
- Consumes: `scripts/typecheck_web.sh` from Task 1.
- Produces: a `src/`-clean tree under `--noUncheckedIndexedAccess`, which Task 5
  builds on.

- [ ] **Step 1: Get the `src/`-only site list**

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh --noUncheckedIndexedAccess > /tmp/tnuia.txt 2>&1
grep -vE '\.test\.tsx?\(|^src/__tests__/' /tmp/tnuia.txt | grep -c "error TS"
grep -vE '\.test\.tsx?\(|^src/__tests__/' /tmp/tnuia.txt | sed 's/(.*//' | sort | uniq -c | sort -rn
```

Expected: 72. If it differs, attribute the delta before proceeding.

- [ ] **Step 2: Fix `src/charts/options.ts` (8 sites) FIRST and STOP**

This file is the review sample. Fix its 8 sites, verify, commit, and **report
back before touching any other file.** Chris wants eyes on the first batch
before the rest proceeds.

Verification for this file alone:

```bash
cd /home/vagrant/otto-sh/.claude/worktrees/untitledui-vendored-revert
scripts/typecheck_web.sh --noUncheckedIndexedAccess > /tmp/h.out 2>&1
grep -c "^src/charts/options.ts" /tmp/h.out   # expect 0
cd web && npx vitest run 2>&1 | grep -E "Tests +[0-9]"   # expect 928
cd .. && make check-ts
rm -f /tmp/h.out
```

- [ ] **Step 3: After review, fix the remaining 64 sites**

One commit per file for the three large ones (`measure.ts`, `layout.ts`,
`health.ts`); the nine singles may share a commit.

- [ ] **Step 4: Report the fix taxonomy**

For the full 72, report how many were fixed by restructuring, how many by an
explicit guard that names an invariant, and how many by a localized
`// @ts-expect-error` with a reason. **If the third number is large, that is the
signal to stop and reconsider the flag** — say so rather than continuing.

- [ ] **Step 5: Verify and commit** — `scripts/typecheck_web.sh
--noUncheckedIndexedAccess` must report zero `src/` errors (test-file errors
remain and are expected), 928 tests green, `make check-ts` and `make web` green.

---

### Task 5: Decide the 378 test-file sites — DONE (option 2), `90a799e1` + `a9052c86`

**Outcome: option 2.** The flag is ON in `web/tsconfig.json`; test files are
exempt from **this one rule only**, via a deferral block in
`scripts/typecheck_web.sh`; the remaining 305 sites, the criterion and the exit
condition are in `todo/ts-nuia-test-sites-burndown.md`.

**The defect in this task as written.** "Scope test files out of it via a
second, clearly-labelled exclusion" describes a LABEL, not a MECHANISM, and the
only mechanism the wording suggests — drop diagnostics whose path looks like a
test file — is a blanket test-tier exemption in the typecheck gate. That is
what this plan's own Global Constraints forbid ("No `overrides` block or
blanket test-tier exemption without evidence for that specific rule") and what
`typecheck_web.sh`'s header says was rejected: a place for OUR OWN defects to
accumulate under a green gate. Clear labelling does not fix that; only scoping
does. **And the flag cannot be scoped by error code** — it has none of its own,
and its 305 sites carry TS2532, TS2345, TS18048 and TS2322, two of which are
among the commonest codes tsc emits for ordinary type errors. Attribution is
therefore *measured*: a second tsc pass with the flag forced off, and a
diagnostic is deferred only if its LOCATION appears in the on-run and not the
off-run. Comparison ignores code and message, so every ambiguity resolves
toward failing the gate. One extra tsc pass, 1.2s → 2.0s.

Two failure modes that mechanism could have had, both closed and both tested:
the baseline pass DYING would make every test-file error look flag-caused
(guarded, verified with a shim `npx` that dies only on its second call); and
awk's usual `NR == FNR` two-file idiom silently swallows the entire second file
when the first is EMPTY — reachable, since a zero-diagnostic baseline is
exactly what "every remaining error is this flag's doing" looks like. Keyed on
`FILENAME` instead.

**A refinement to this task's own evidence.** It names
`toporouting.test.ts`'s unguarded four-number destructure as "a real one" — it
is, but the fix is not to add the missing guard. The file carried a *private
copy* of `measure.ts`'s path sampler, evaluator and all, minus the arity check;
the guarded original is already exported. Adding a guard to the duplicate would
have kept two copies of the path grammar, which is how they diverged. Deleting
the copy also fixed the consequence, which was worse than "less safe": an
unrecognised grammar made every sample `(NaN, NaN)`, every containment test
false, and the occlusion assertions — the whole reason the file exists — pass
while measuring nothing. Demonstrated against the deleted code (same geometry:
`["box"]` as a cubic, `[]` as a quadratic).

**The test-file ratio.** 361 sites scored against the boundary-crossing test
yielded **2 files worth fixing, 56 sites, 1 genuine fragility** — the same
order as `src/` (3 in 72). `health.test.ts` (34) was the other: three fixture
builders returned `parseExportDocument(...).sessions[0]` to consumers hundreds
of lines away, and `buildSession` DECLARED `: NormalizedSession`, a claim its
`return` did not support. The two LARGEST files, `exportdoc.test.ts` (34) and
`chartoptions.test.ts` (30), scored zero and were left alone — working the list
by count would have started with exactly the wrong two files. Zero `!`, zero
`as`, zero `@ts-expect-error` added.

Original task text follows.

**This task is a decision, not a grind.** Task 4 is complete — all 72 `src/`
sites cleared across `66c57172`, `3f5f2b41`, `71c0afa2`, `d21e0ace`, `dffad860`.

**The flag is NOT yet enabled in `tsconfig.json`**, because it would redden the
gate on the 361 test sites. That means the 72 `src/` fixes currently have **no
guard** — nothing stops an unchecked index being reintroduced tomorrow. That is
a cost of leaving this decision open, and an argument for resolving it rather
than parking it.

### What Task 4 actually measured

Fix taxonomy across all 72: **59 restructure, 13 named guard, 0 annotations.**
Zero bare `!`, zero `as`, zero `@ts-expect-error` added; net **-1** on
pre-existing non-null assertions (`health.ts`'s `recs.get(key)!` became a real
check that reports both lengths when the invariant breaks).

Real-bug ratio: **0 live bugs, 3 genuine latent fragilities, 69 type-checker
blindness.** Per file — `charts/options.ts` 1 of 8, `topo/layout.ts` 1 of 11,
`data/health.ts` 1 of 8, `topo/measure.ts` **0 of 29**, remainder 0 of 16.
(Task 4's own summary said 2; its per-file breakdown sums to 3, which is the
figure to trust.)

**The pattern is consistent and sharp: the flag pays only where a positional
read crosses a structure or function boundary.** `lanes[i]` against `spans[i]`
across a function; `targets[i]` against `bucket[i]` maintained only by
construction order; `recs[idx]` indexed by a position found in a *different*
Map's array. Within a single function — which is `measure.ts` entirely — tsc is
failing to see an invariant a reader confirms in seconds, and 29 restructures
there found nothing.

### The evidence for the test sites

- 361 sites (re-measured after Task 3; the plan's original 378 predates Task 2's
  fixture work). Concentrated in `toporouting.test.ts`, `health.test.ts`,
  `exportdoc.test.ts` and `chartoptions.test.ts`; re-measure the per-file
  distribution before deciding, since two tasks have moved it already.
- Test sites are **not** obviously worthless. `toporouting.test.ts:41-44`
  destructures four numbers from a regex match with **no length guard at all**,
  where the `src/` code it mirrors (`measure.ts:83`) does guard. The test is
  genuinely less safe than the code under test.
- But a large share are `fixture[0].foo` on a fixture the test just built, where
  a failure would surface as a test failure anyway.

Three defensible outcomes, to be chosen with Task 4's experience in hand:

1. **Fix all 361**, enable the flag globally in `tsconfig.json`. On Task 4's
   ratio this buys roughly the `measure.ts` outcome at scale: nicer code,
   little found.
2. **Fix the boundary-crossing test sites, exempt the rest for this rule.**
   Start with `toporouting.test.ts`, which destructures four regex-match
   numbers with **no** length guard where the `measure.ts` code it mirrors does
   guard — a real one. Re-score the other concentrated files against the
   boundary-crossing test rather than by count. Then enable the flag and scope
   test files out of it via a second, clearly-labelled exclusion in
   `scripts/typecheck_web.sh` — distinct from the vendored one, which exists for
   code we may not touch, whereas this is code we have judged not worth
   changing. This is the "decide per rule later" case with the evidence now in
   hand, and it is the only option that leaves the 72 `src/` fixes guarded.
3. **Decline the flag.** The `src/` work stands on its own merits either way,
   but nothing then prevents regression.

Whichever is chosen, the flag's final state in `tsconfig.json` and the reasoning
go in the same commit. Present the recommendation to Chris before implementing.
