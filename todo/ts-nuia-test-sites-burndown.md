# `noUncheckedIndexedAccess` — the deferred test-file sites

**Opened:** 2026-07-28, closing Tier 4 Task 5 of the TS-strictness work
(`docs/superpowers/plans/2026-07-28-ts-strictness-tier-4.md`).

`noUncheckedIndexedAccess` is **ON** in `web/tsconfig.json` and every `src/`
site is clear. Test files are exempt from **this one flag** — and nothing else
— via the deferral block in `scripts/typecheck_web.sh`. This file is the record
of what was deferred, why, and what ends it.

The exclusion is not silent: `scripts/typecheck_web.sh` prints the remaining
count on every run, pass or fail.

## The count

**305 sites, 35 files**, measured 2026-07-28 at `90a799e1`
(182 × TS2532, 73 × TS2345, 49 × TS18048, 1 × TS2322).

| File | Sites |
| ---- | ---- |
| `src/__tests__/exportdoc.test.ts` | 34 |
| `src/__tests__/chartoptions.test.ts` | 30 |
| `src/__tests__/chartpanel.test.tsx` | 27 |
| `src/covapp/contexts.test.ts` | 20 |
| `src/__tests__/topology.test.ts` | 19 |
| `src/covapp/pages/DirectoryPage.test.tsx` | 19 |
| `src/__tests__/events_panel.test.tsx` | 17 |
| `src/__tests__/seriestree.test.ts` | 14 |
| `src/__tests__/eventapi.test.ts` | 13 |
| `src/data/topology.tunnels.test.ts` | 12 |
| `src/covapp/tickets.test.ts` | 11 |
| …24 more | ≤ 8 each |

Note the concentration list is **not** the work list. See the criterion below —
the top two files are the two explicitly judged not worth touching.

## How to re-measure

```bash
cd /path/to/otto-sh
# The count alone (also printed by `make check-ts`):
scripts/typecheck_web.sh > /tmp/tc.out 2>&1; echo "exit=$?"; tail -1 /tmp/tc.out
# The sites themselves:
TYPECHECK_WEB_SHOW_DEFERRED=1 scripts/typecheck_web.sh > /tmp/def.out 2>&1
grep 'error TS' /tmp/def.out | sed 's/(.*//' | sort | uniq -c | sort -rn
```

Note the redirect. A pipe would report `tail`'s exit status, not the script's.

The list is computed differentially — tsc runs a second time with the flag
forced off, and only a diagnostic whose **location** appears in the flag-on run
and not the flag-off one is attributed to the flag. There is no error code to
grep for: this flag has none of its own, and TS2345/TS2322 are among the
commonest codes tsc emits for ordinary type errors.

## The criterion: does the read cross a boundary?

**Fix a site only when a positional read crosses a structure or function
boundary.** Not by site count, and not by file.

That criterion is measured, not stylistic. Task 4 cleared all **72 `src/`
sites** and scored each one: **3 were genuine latent fragilities, 0 were live
bugs, and 69 were the type checker failing to see an invariant a reader
confirms in seconds.** The 3 that paid share one shape:

- `lanes[i]` paired with `spans[i]` **across a function call**;
- `targets[i]` paired with `bucket[i]`, held together only by construction
  order established elsewhere;
- `recs[idx]` indexed by a position discovered in a *different* Map's array.

And where nothing crossed a boundary the flag found nothing at all:
**`src/topo/measure.ts` scored 0 in 29 sites**, because every read sat inside
the same function that established the invariant. Twenty-nine restructures,
zero findings. That file is the control.

Applied to test files, the same question is: *can a reader confirm this index
is in range without leaving the function they are looking at?*

- **Fix**: a helper or fixture builder whose return type carries `| undefined`
  into callers hundreds of lines away; a loop bound that restates a length
  established by a different call; a hard-coded index into another function's
  return value.
- **Leave**: `fixture[0].foo` on a fixture built three lines above in the same
  function. The invariant is in eyeshot, and a break surfaces as a test failure
  either way.

Scored this way, two files were worth fixing (`toporouting.test.ts`,
`health.test.ts`, 56 sites, done in `90a799e1`) — and `toporouting.test.ts`
turned out to carry a real one: its private copy of `measure.ts`'s path sampler
had no arity check, so an unrecognised path grammar produced `(NaN, NaN)`
samples, every containment test read false, and the occlusion assertions — the
entire reason the file exists — passed while measuring nothing.

The two **largest** files, `exportdoc.test.ts` (34) and `chartoptions.test.ts`
(30), scored zero against the criterion and were left alone. Working this list
by count would start with exactly the wrong two files.

## Rules of engagement

Same as Task 4's, and they are the point of the exercise:

- **No bare `!`, no `as`.** `arr[i]!` converts a checked access into an
  unchecked one — strictly worse than not having the flag on. A fix that reads
  as ordinary code has hidden the signal, not removed it.
- A localized `// @ts-expect-error` naming the rule and a real reason is
  sanctioned and preferable to a bad fix. It stays greppable.
- Prefer restructuring so the index disappears, then a guard that names the
  invariant once.

## Exit condition

**When the count reaches zero, delete the deferral.** Concretely:

1. Delete the block in `scripts/typecheck_web.sh` between the
   `THE DEFERRED TEST-SITE EXCLUSION` banner and the `n_deferred` reporting —
   including the second `npx tsc` baseline pass, `TEST_DIAG_RE`, the awk split,
   and the `TYPECHECK_WEB_SHOW_DEFERRED` hook. That restores the script to a
   single tsc run with only the vendored filter, which is the one exclusion
   that is permanent.
2. Drop the "for `src/` ONLY" paragraph from `web/tsconfig.json`'s comment
   block; the flag itself stays.
3. Delete this file.

The script says so itself at zero, so this cannot be forgotten:
`0 deferred test site(s) -- the burn-down is DONE: delete the deferral block…`.

Reaching zero is **not** required. If the remaining sites keep scoring zero
against the criterion, the honest outcome is to say so and keep the deferral —
but record that judgement here rather than leaving the list to rot. What is not
acceptable is letting it grow: this is our own code, it is fixable today, and
that is exactly what separates it from the vendored exclusion, which covers
code we are forbidden to touch and can never fix.
