# A rare, unidentified `web/` vitest failure under shuffled order

**Opened:** 2026-07-28, during the TS-strictness workstream
(`docs/superpowers/plans/2026-07-28-ts-strictness-tiers-5-6.md`).

`sequence.shuffle` was enabled in `web/vite.config.ts` on 2026-07-28 (`72e16df6`)
as the pytest-randomly analogue. It immediately paid for itself, exposing two
real couplings that were fixed in that same tier. This note is about a **third**
signal that has not been identified.

## What was observed

Two full-suite runs reported `Tests 1 failed | 927 passed (928)`. Both times the
failing test's **name was lost**, because the command piped vitest's output
through `grep` for the summary line and discarded the `FAIL` line.

Everything else has been green: roughly twenty full runs across the session,
including four immediately after each observed failure, plus three isolated runs
of `src/__tests__/perf_budget.test.ts`.

## What was ruled out

- **Not a Tier 5 regression.** The first occurrence predates all Tier 5 work.
- **Not "a heavy `make` target immediately before".** Both observed failures
  followed `make check-ts` / `make web` in the same shell invocation, which
  suggested CPU contention — but two deliberate reproductions of exactly that
  sequence were green.
- **Probably not `perf_budget.test.ts` alone**, though it remains the strongest
  single candidate: it failed once at the very start of the session, before any
  of this work, with `expected 44.79 to be less than 4.18` — a 43× miss of a
  guard written as a *ratio* (`tLong < max(tShort * 4, 2)`). A ratio is meant to
  be load-robust, but contention does not hit both measurements equally, so it
  can still flake. That failure is documented; the two later ones are not
  confirmed to be the same test.

## How to catch it next time

**Never pipe the run.** Capture the whole thing, then read the file:

```bash
cd web && npx vitest run > /tmp/vitest.log 2>&1; echo "exit=$?"
grep -E "^ FAIL|Tests +[0-9]|seed" /tmp/vitest.log
```

Vitest prints `Running tests with seed "<n>"` in **every** run's header, pass or
fail. With the seed, the exact order replays:

```bash
cd web && npx vitest run --sequence.shuffle --sequence.seed=<n> > /tmp/replay.log 2>&1
```

## Why this is worth keeping open rather than closing

Shuffle exists to surface order-dependence, and it has already found two real
bugs in this tree — a missing Testing Library `cleanup` that had never been
registered, and a synchronous test starting a chunk load that resolved after
teardown. A third rare signal is more likely to be a genuine coupling than
noise. Closing it as "flaky" without a name would discard exactly the
information the setting was turned on to produce.

## Exit condition

Either the failing test is identified from a captured log and fixed, or a
deliberate seed sweep reproduces it. If it turns out to be `perf_budget.test.ts`
under contention, the fix is to make that test's guard robust to scheduling
(compare work done rather than wall-clock, or mark it as excluded from the
shuffled lane) — not to widen the ratio until it stops failing, which would
retire the regression guard it exists to be.
