# How coverage data is merged

`otto cov report` is an assembler, not a collector: every input it folds
was produced earlier, by a different command, possibly on a different day
({doc}`index`). Assembly is where the hard rules live, because the same
line of source can be reached by an e2e run on two boards, a unit harvest,
and a manual session — and the report has to keep those separable, credit
each of them once, and never let one source's failure look like another
source's absence.

## The fold order

`CoverageReporter.run` (`otto.coverage.reporter`) folds in a fixed order.
The order is not incidental; the last step depends on it.

1. **Legacy `.gcda` merge.** Only when a `system` tier was requested with no
   `.info` path *and* there are non-capture board directories to merge.
   `LcovMerger.capture_and_merge` runs each host's counters through that
   host's own toolchain — the toolchain list is positional, parallel to
   `gcda_dirs`, so a mixed bed (one clang product, one default-gcov host)
   keeps every host paired with its own `gcov` — and merges the results with
   lcov. `discover_path_mappings` then learns embedded→local path mappings
   from the merged `.info`, falling back to `.gcno` discovery when it can't.
   The whole merge registers **one** synthetic run: no board, no host
   ({doc}`types` covers why that costs the report its per-host breakdown).
2. **Explicit `--tier NAME=PATH` `.info` files.** Same loop, one run each. A
   path that doesn't exist warns and still registers the tier, so its column
   renders empty rather than vanishing.
3. **E2e captures.** `_load_captures` folds each board's `capture.json`
   under a strict guard: `capture.base_commit` must equal the repo's `HEAD`,
   or the report stops with `CoverageDataMismatchError` naming both commits.
   An e2e capture carries no anchor chain — its coordinates *are* that
   commit's line numbers — so a matching tree is the only thing that makes
   it readable. The guard proves nothing about the *working* tree, though:
   when the tree is dirty the renderer reads edited on-disk source, so
   `load_dirty_capture_into_store` remaps each file HEAD→worktree through
   the same `-U0` diff and `LineRemapper` the manual chain uses, dropping
   hits on locally-modified lines rather than misaligning everything past
   the edit.
4. **Unit harvest.** `_harvest_unit_tiers` lcov-captures each `kind ==
   "unit"` tier's `harvest_dirs` out of the *current* build tree (relative
   entries resolved against the SUT repo root, not the process CWD — `otto
   cov report` may run from anywhere). One run id per **tier**, not per
   directory: `tier_run_id` is created lazily on the first usable dir and
   reused, so a tier with three harvest dirs contributes one run. Counters
   whose newest `.gcda` predates the newest `.gcno` warn but still load —
   this data is cheap to regenerate, and refusing it would be louder than
   the problem.
5. **Manual store.** `_load_manual_store` folds every committed capture
   under `.otto/coverage/manual/`, supersede-filtered, each with the
   report-time validity states {doc}`manual` describes. **Last, always.**

Then tier colors are seeded (`_fill_tier_colors`), the **exclusion filter**
runs, ticket attribution and overrides fold in, the renderer runs, and only
then is `store.json` written.

The filter's position in that sequence is load-bearing: it sits after every
fold above, so one pass covers all five sources, and before attribution, so
an excluded line never reaches per-ticket coverage
({doc}`../../../guide/cli/cov/exclusions`). The
save coming last no longer is — rendering has no store side effect left for
it to capture ({doc}`renderer`).

## Why manual folds last

`validity.apply_manual_capture` marks a line stale only when nothing else
already covers it: both the unverifiable-file path (`if lr.state is None and
not lr.hits.is_hit()`) and the remapped path (`if not lr.hits.is_hit(): lr.state
= "stale"`) read the line's *accumulated hits across every tier* at fold
time. Fold the manual store first and those reads see an empty line, so a
line the e2e capture or the unit harvest covers perfectly well takes a
spurious `state = "stale"` — inflating the `flags.stale` rollup on every
directory node above it, and persisting into `store.json` and that file's
chunk as a wrong per-line state. (The row itself still paints the covering
tier's colour, because the precedence walk tries tier hits before states —
so the damage is in the data and the counts, not the colour.) The order is
therefore a correctness rule, not a convenience.

The run-level record is unconditional either way: `stale_runs` gains the
manual run's id even when another tier's hits suppress the line's stale
*state*. That distinction is deliberate — "this run's evidence for this
line was revoked" is a fact about the run, independent of what else happens
to cover the line.

## Dedupe is a key hoist, not a load-order hoist

The same capture can be reachable twice: once as a committed manual capture
and once as a leftover copy inside a cov directory handed to `otto cov
report`. `run()` therefore computes `_run_key(capture)` — `(tier,
base_commit, board, captured_at)` — for **every** committed manual capture,
supersede losers included, and seeds `seen_runs` with those keys *before*
`_load_captures` runs. A cov-dir duplicate is then skipped by key, before
the e2e `base_commit` guard ever looks at it. Without the hoist that
duplicate is an unrecognized run, which crashes the report once the tree has
moved past its `base_commit` and silently double-counts while it hasn't.

Note the two dedupe keys are different on purpose. `_run_key`'s 4-tuple
identifies **one capture artifact** (two files with the same contents are
the same capture). Supersede's 3-tuple below identifies **one capture
context** (two captures of the same board, same tier, taken a week apart).

## Run accumulation

The run table is derived fresh at every report: `CoverageStore.add_run`
appends a `RunRecord` and returns its index, and that index is the run id
written into per-line data. Two properties fall out of the code:

- **Only executed lines credit a run.** `LCOVLoader.load` and
  `validity._insert_lines` both write `run_hits[run_id] += count` under a
  `count > 0` guard, while the tier's own `LineHits` takes the zero too. A
  run that instrumented a line but never hit it is not listed for that line.
- **Per-host is derived, never stored.** `validity.register_capture_run`
  passes `host=capture.board`, and one capture is exactly one board and
  exactly one run, so grouping a line's `run_hits` by `RunRecord.host`
  reconstructs per-host line counts with no per-host table in the schema.
  The SPA does exactly that grouping (`contexts.ts` groups runs by label and
  reads `run.host || run.board` per member). Runs that have no single host
  behind them — the legacy lcov merge, the unit harvest, an explicit
  `--tier` `.info` — carry `host = ""` and contribute nothing to derive.

## Supersede: same context replaces, never accumulates

**Supersede-on-recapture**
(`otto.coverage.capture.supersede.select_manual_captures`) runs before
folding: manual captures are deduplicated by `(tier, label, host)`, the
newest `captured_at` wins, and a replaced capture drops out of the runs
table entirely rather than having its credits accumulate alongside the
newer one.

The key is built in `_key` as `(tier, display_name or board, board)` — the
label falls back to the board id when the capture carries no host display
name, and the third component is the board id itself, which is the same
value store v4 carries in `RunRecord.host`. "Newest" is a plain string
comparison on `captured_at`, which is sound because the stamp is a
zero-padded UTC `%Y-%m-%dT%H:%M:%SZ` and therefore sorts lexicographically.
Losers never reach `register_capture_run`, so they leave no row for the runs
page to show — a superseded session is gone, not struck through.

Only manual captures go through supersede. E2e captures live in per-run
output directories; re-running the suite writes a new directory rather than
a second capture of the same context, but same-context reruns still fold
together additively like any other capture — they just never supersede
each other.

## What "merged" means within a tier, and across tiers

**Within a tier, merging is additive.** `LineHits.add`/`merge` sum counts
per tier name; `BranchHits.merge` unions `(block, branch)` keys and ORs
reachability (`False` → `True` only). Two boards that both execute a line
sum their counts under one tier — that is not double counting, it is two
runs' worth of evidence for the same line, and `run_hits` keeps them
separable for the drilldown. What *would* be double counting is the same
context folded twice, which is what supersede and the dedupe hoist above
exist to prevent.

**Across tiers, nothing merges at all.** Counts live in a dict keyed by tier
name, so a tier can never add into another one; there is no code path that
sums two tiers' counters. What looks like cross-tier merging in the report
is *precedence*: the row walk paints a line with the first tier in
`tier_order` that has a nonzero count ({doc}`types`), and the overall
percentage asks `LineHits.is_hit(None)` — "any tier at all". Adding a tier
can raise the overall number and can repaint rows, but it can never change
another tier's own column.

**File identity is canonicalized once.** `CoverageStore.get_or_create_file`
resolves a path to its canonical form whenever the file exists on disk, so
the same source reached through a symlinked build tree, a relative `SF:`
path out of lcov, and an absolute path out of a capture all collapse into
one `FileRecord` instead of three near-duplicate rows.
