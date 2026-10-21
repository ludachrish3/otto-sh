# How coverage data is merged

`otto cov report` is an assembler, not a collector: every input it folds
was produced earlier, by a different command, possibly on a different day
({doc}`index`). Assembly is where the hard rules live, because the same
line of source can be reached by an e2e run on two boards, a unit harvest,
and a manual session — and the report has to keep those separable, credit
each of them once, and never let one source's failure look like another
source's absence.

## From counters to a capture

Before anything folds, each fetched product has to become a `capture.json`.
Collection does this right after the fetch (`otto test --cov`, `otto cov
get`), and the report does it for any product directory collection left
uncaptured. Both go through one function,
`otto.coverage.capture.produce.produce_captures`, which takes each
`<cov>/<host>/<product>/` directory through the same steps. The user's side
of it, the one build requirement and the errors, is in the GCC page's
{ref}`coverage-build-requirement` section.

1. **Object list.** `otto.coverage.objects` reads the product's local
   artifact and lists every object built into it. An instrumented object
   embeds the absolute path its `.gcda` is written at, as a NUL-terminated
   string. Scanning the artifact's bytes for those strings gives each
   object's full coverage path, so pairing never has to guess from a file
   name. The artifact may be a file, a directory, a tar or zip archive, a
   compressed file, or a `docker save` tarball. For the tarball, the scan
   reads the image's final filesystem, with layers and whiteouts applied. An
   ELF executable's `DT_NEEDED` libraries are followed when exactly one file
   under the project root carries the soname. A product with no readable
   artifact yields an `Unavailable` with the reason, and its `.gcda` are
   paired by path alone. `ObjectListCache` reads each artifact once per
   capture run.
2. **Pairing.** `otto.coverage.pairing.pair_product` takes each `.gcda`'s
   path relative to the product directory, which is its path relative to the
   product's `cov_dir` on the host. It then looks for the listed objects whose
   path ends with it. Matching is component-wise, so `xmain.cpp` never
   matches `main.cpp`. Several objects mean the device merged them into one
   file (`PairingError`, kind `merged-on-device`). One object means its own
   `<object>.gcno`, or the kernel's `.tmp_<name>.gcno`, is the only
   candidate when it exists. A stamp mismatch with that file is a rebuild
   (`CoverageDataMismatchError`), never a reason to look elsewhere. When the
   object's notes are not on this machine (a container or CI build), or the
   `.gcda` is not on the list (a `dlopen` plugin), `GcnoIndex` searches the
   search root for notes with the object's file name. The search root is the
   project root, or an embedded board's `build_dir`, and `GcnoIndex` walks it
   once. The search keeps the notes whose version, stamp and, for clang,
   per-function checksums fit, then those with the longest common path tail.
   One survivor, or several byte-identical files, is the answer. Several
   differing files are `several-trees`, and none is `no-notes`. Each error
   names every candidate and why it lost. There is no basename fallback.
3. **Staging.** `LcovMerger.capture_pairs` lays the pairs out under
   `<cov>/.otto-stage/<host>/<product>/`. Each `.gcda` is copied to its
   relative path with a symlink to its `.gcno` beside it, which is the
   layout `geninfo` expects, wherever the build happened. The staging tree
   is scratch: it is removed once the product is captured or has failed,
   and `.otto-stage` goes with its last product, so a finished run's `cov/`
   holds only `<host>/<product>/` directories and the metadata sidecar.
4. **One capture.** One `lcov --capture --directory <staging>` per product,
   with the product's gcov tool ({ref}`coverage-gcov-resolution`),
   `--rc branch_coverage=1`, `--ignore-errors source` and the host's
   `lcov_args`. The capture does not depend on source files, so a build done
   on another machine still captures. When the sources are present,
   `geninfo` still reads them, and `LCOV_EXCL` markers keep working. lcov's
   output goes to the run's log file only. A failure is summarised as its
   first `ERROR:` line, the last 20 lines and the log path, typed when the
   output names a stamp mismatch or an incompatible gcov.
5. **Source resolution.** `otto.coverage.sources.SourceResolver` rewrites
   each `SF:` path on its own. A path under the staging tree (clang records
   relative source names, which `geninfo` resolves against the staged
   `.gcda`) loses the staging prefix and its `..` parts, then is
   tail-matched. An absolute path that exists is kept as is, without a log
   line. An absolute path that does not exist (a build done elsewhere) is
   tail-matched against the project's files. A match counts only when the
   candidate's whole project-relative path is a suffix of the recorded one;
   of several matches (`/build/src/a.c` against both `a.c` and `src/a.c`)
   the longest wins. No match keeps the raw path with one WARNING. `build_capture` then
   drops every file outside the project root and counts them in one INFO
   line.
6. **Capture states.** A product that got this far writes `capture.json`.
   One that failed at any step writes `capture.failed`, holding the short
   reason, and the loop moves on. After the loop, one
   `CoverageCaptureError` lists every failed product, and its `result`
   carries every outcome. Collection then cleans only the products that
   captured, so a failed product keeps its counters on the host for the
   next `otto cov get`.

`run_coverage_report` sorts each run directory's product directories by
those files before the fold. A `capture.json` is folded. A `capture.failed`
is named in the one warning line that lists failed products and the path
of each `capture.failed`, and is not retried. A directory with `.gcda` but
neither marker is captured now through the same
`produce_captures`, with the tier and `base_commit` collection recorded in
`.otto_cov_meta.json`. A directory with no recorded tier or commit is a
`CoverageInputError`, never a guess, and is raised, like every other input
refusal, before the destination is cleared. A directory with no `.gcda` and
neither marker has nothing to capture and is skipped. The products captured here are not
cleaned on their hosts: reporting never touches the lab. Their object lists
come from the product definitions of the active lab (`_declared_products`),
so report with the same `--lab` the run used. Without it, every product has
no object list and is paired by path alone, with a search of the project
root, which a second build tree can turn into a `several-trees` error that
collection would not have raised.

## The fold order

`CoverageReporter.run` (`otto.coverage.reporter`) folds in a fixed order.
The order is not incidental; the last step depends on it.

1. **Explicit `--tier NAME=PATH` tracefiles.** One run each, recording the
   tier only: a tracefile someone else produced names no host, product or
   commit. A path that doesn't exist warns and still registers the tier, so
   its column renders empty rather than vanishing.
2. **E2e captures.** `_load_captures` folds each board's `capture.json`
   under a strict guard: `capture.base_commit` must equal the checkout's `HEAD`,
   or the report stops with `CaptureCommitError` naming both commits. That
   error is separate from `CoverageDataMismatchError` on purpose: a moved
   commit and a rebuilt product have different remedies.
   An e2e capture carries no anchor chain — its coordinates *are* that
   commit's line numbers — so a matching tree is the only thing that makes
   it readable. The guard proves nothing about the *working* tree, though:
   when the tree is dirty the renderer reads edited on-disk source, so
   `load_dirty_capture_into_store` remaps each file HEAD→worktree through
   the same `-U0` diff and `LineRemapper` the manual chain uses, dropping
   hits on locally-modified lines rather than misaligning everything past
   the edit.
3. **Unit harvest.** `_harvest_unit_tiers` lcov-captures each `kind ==
   "unit"` tier's `harvest_dirs` out of the *current* build tree (relative
   entries resolved against the project root, not the process CWD — `otto
   cov report` may run from anywhere). A unit build writes each `.gcda`
   beside its own `.gcno`, so `LcovMerger.capture_dir` captures in place,
   with no staging and no search, after checking that every `.gcda` has its
   notes. One run per **view** — the unnamed `harvest_dirs`, or one named
   `products` entry — not per directory: `_harvest_one` creates the run id
   lazily on the first usable dir and reuses it, so a view with three dirs
   contributes one run. Counters
   whose newest `.gcda` predates the newest `.gcno` warn but still load —
   this data is cheap to regenerate, and refusing it would be louder than
   the problem.
4. **Manual store.** `_load_manual_store` folds every committed capture
   under `.otto/coverage/manual/`, supersede-filtered, each with the
   report-time validity states {doc}`manual` describes. **Last, always.**

Then tier colors are seeded (`_fill_tier_colors`), the **exclusion filter**
runs, ticket attribution and overrides fold in, the renderer runs, and only
then is `store.json` written.

The filter's position in that sequence is load-bearing: it sits after every
fold above, so one pass covers all four sources, and before attribution, so
an excluded line never reaches per-ticket coverage
({doc}`../../../cli/cov/exclusions`). The
save coming last no longer is — rendering has no store side effect left for
it to capture ({doc}`renderer`).

The `preprocessor` exclusion rule never evaluates a condition. It does not
need to know which arm the preprocessor selected, because a dead arm has no
coverage records at all — the compiler emitted no code for it, so deleting
it removes nothing; gcov already resolved liveness. That observation keeps
the feature free of a macro environment, of `compile_commands.json`, and of
having to agree with the user's build system about anything. It is also why
`||` over-matches and an `#else` arm cannot be named: both would need real
evaluation.

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
  reads `run.host || run.board` per member). Every e2e and manual run has
  one. Runs that have no single host behind them — the unit harvest, an
  explicit `--tier` `.info` — carry `host = ""` and contribute nothing to
  derive.

## Supersede: same context replaces, never accumulates

**Supersede-on-recapture**
(`otto.coverage.capture.supersede.select_manual_captures`) runs before
folding: manual captures are deduplicated by
`(tier, label, host, product)`, the newest `captured_at` wins, and a replaced
capture drops out of the runs table entirely rather than having its credits
accumulate alongside the newer one.

The key is built in `_key` as
`(tier, display_name or board, board, product)` — the label falls back to the
board id when the capture carries no host display name, the third component
is the board id itself, which is the same value store v4 carries in
`RunRecord.host`, and the fourth is the capture's `product` (store v8). A
host can carry several instrumented products, each producing its own
capture; without `product` in the key the second would supersede the first
and its coverage would vanish from the report — silently, because the
reporter seeds its cross-source dedupe set from the committed manual
captures, so the loser's cov-dir copy is skipped too. "Newest" is a plain string
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
