# Coverage — the collection pipeline

The problem: embedded and cross-compiled products execute where no coverage
tooling runs. gcov counters (`.gcda`) accumulate on the target — in memory or
on an on-device filesystem — while the compile-time graph (`.gcno`) and
sources live in the build tree on the runner. Neither side alone can make a
report; this pipeline marries them.

```{graphviz}
digraph coverage {
    rankdir=LR;
    node [shape=box];

    test [label="otto test --cov /\notto cov get\ninstrumented run or retrieval"];
    fetch [label="fetch\n.gcda from covered hosts\n(transfer on Unix,\nconsole extraction embedded)"];
    merge [label="merge\nmatch .gcda ↔ .gcno graph,\nremap sysroot paths,\nmerge hosts + runs (lcov)"];
    capture [label="capture.json\nper board: parsed hits,\ngit-anchored coordinates"];
    render [label="otto cov report\ncaptures + unit harvest\n+ manual store → HTML"];
    err [label="CoverageDataMismatchError\nstale build → instructions,\nnot a wrong report", shape=note, style=dashed];

    test -> fetch -> merge -> capture -> render;
    merge -> err [style=dashed, label=" stamp\nmismatch"];
}
```

The stages (packages `otto.coverage.fetcher` → `merge` → `capture` →
`renderer` → `reporter`):

1. **Fetch** — pull `.gcda` data from each covered host after the run.
   Fetchers are per-family: file transfer for Unix hosts, console extraction
   for embedded targets. Which hosts are covered is *repo-declared* — the
   `[coverage].hosts` regex in `settings.toml` — never inferred, so hop hosts
   and uninstrumented beds can't sneak into a report.
2. **Merge** — match counters to the build tree's `.gcno` graph and remap
   embedded/sysroot paths back to source paths, merging counters across hosts
   and runs (lcov semantics).
3. **Capture** — freeze the merged result into a per-board `capture.json`:
   parsed hits in committed-code coordinates, anchored to the repo's
   `HEAD` (`base_commit`) and per-file blob SHAs.
4. **Render / report** — `otto cov report` assembles every tier — e2e
   captures, a fresh unit-tier harvest, the committed manual store — into an
   HTML report plus summary tiers.

The merge stage's core invariant is *build/counter identity*: `.gcda` files
are only meaningful against the exact `.gcno` graph the binary was compiled
with. That pairing happens once, at **collection** — the capture holds
parsed hits, so the report step never touches the build tree again and a
later rebuild cannot invalidate it (a capture's own guard is its
`base_commit`, which must match `HEAD` at report time). When the raw pairing disagrees — a
stale or partially rebuilt product tree at collection time, or a
pre-capture run directory re-merged via the legacy fallback — the pipeline
stops with a diagnostic error that names the mismatch and the rebuild that
fixes it, rather than a gcov stack trace or a silently wrong report. That
fail-with-instructions posture is a house rule ({doc}`../principles`).

## Tiers and what is committed

Coverage is organized into **tiers** — `system` (e2e), `unit`, `manual`, or
any other name — each with a `kind` (`e2e` / `unit` / `manual`) that selects
how otto collects that tier's data; declaring tiers and driving the
three-tier workflow is covered in {doc}`../../guide/coverage`.

Only the **manual** tier's data is committed into the repo, even though
every tier's data is anchored to `base_commit`: e2e and unit data are
reproducible — a fresh `otto test --cov` or a rebuilt unit harvest
regenerates them — but a manual session (a human at a GDB prompt, poking at
running hardware) produces evidence nothing else can regenerate. Selecting a
manual-kind tier on `otto cov get` copies the capture into the repo's
committed store at `.otto/coverage/manual/` — proof of that session that
travels with the code and is PR-reviewable. E2e data instead lives in each
test run's output directory, and unit data is harvested fresh from the build
tree's `harvest_dirs` at report time, so neither needs a permanent home.

`otto cov report` assembles a store from all three sources per tier `kind`:
e2e captures from the given output directories (behind the base_commit guard
above), the unit harvest, and every committed manual capture — loaded
automatically, no path needed. Because manual evidence outlives the commit
it was captured against, a report-time **validity pass**
(`otto.coverage.validity`) re-anchors each manual capture's lines against the
current tree by git blob SHA rather than trusting the stored line numbers
forever: unchanged lines stay **valid**, changed/deleted lines go **stale**
(coverage revoked — the evidence no longer describes this code), and
valid-but-old lines past the tier's `max_age` are flagged **aging** without
losing coverage credit. See {doc}`../../guide/coverage` for the full
valid/stale/aging/unverifiable state table and how each renders in a report.

## Anchor resolution: two paths, one contract

The validity pass's dominant cost is git subprocess fan-out, not computation —
remaps are in-memory over `-U0` hunks, and history length is irrelevant to a
tree-to-tree diff — so `AnchorResolver` (`otto.coverage.anchor`) resolves a
whole capture per subprocess batch instead of per file. That distinction
matters most on NFS-mounted repos, where every git spawn re-touches `.git`
metadata over the wire.

**Two-path resolution.** For each capture, `AnchorResolver` runs one
tree-wide `git diff -M -w -U0 --relative <base_commit> -- .` against the
working tree. When that resolves — the common case — it answers every
file at once: a path absent from the diff is unchanged (unless it is also
absent from the working tree, e.g. a capture carried over from a different
repo, in which case it is unverifiable), a path present is modified or
renamed (`-M` is git's rename tracking, taken as policy), and a path with
no `new_path` was deleted. When `base_commit` itself cannot be resolved —
the tree diff raises `GitUnavailableError` because the commit was
squash-merged away and garbage-collected, or the repo is a shallow clone
that never fetched it — the resolver builds a batched fallback index from
each capture entry's recorded blob SHA instead.

**Batched fallback pipeline** (`otto.coverage.capture.gitio`), in order:
`hash_objects()` hashes every fallback candidate (a capture entry with a
recorded blob and a file still present in the working tree) in one `git
hash-object --stdin-paths` spawn, fast-pathing the files whose hash already
matches the capture's blob. The remaining changed files' base blobs are
existence-checked in one `git cat-file --batch-check` (`blobs_exist()`) —
a blob missing from the object database degrades that file to
unverifiable/stale rather than erroring — then fetched in one `git cat-file
--batch` (`cat_blobs()`). Base and current contents are materialized into
two sibling temp directories and diffed with a single `git diff --no-index
-w -U0` (`diff_no_index_dir_u0()`), parsed by the same multi-file `-U0`
parser (`parse_multifile_u0`) the tree path uses. A capture entry with no
recorded blob at all — or any resolution requested without a capture-wide
file map in the first place — is left to the lazy, unbatched per-file
chain (blob fast-path → blob diff → unverifiable) that the batched index
otherwise short-circuits.

**Parity contract.** Both paths return the same `AnchorResult` (a new
path or `None`, hunks, and a `verifiable` flag) for every row of the
semantics table in `AnchorResolver`'s docstring, and whitespace immunity
comes from `-w` on every diff flavor the resolver runs — tree, per-file
blob, and batched dir-level alike. `validity.apply_manual_capture`
consumes `AnchorResult` as its only resolution currency; it never branches
on which path produced one.

**Spawn budgets are pinned contracts, not aspirations.** ≤2 git spawns per
fold on the tree path and ≤6 on the fallback path are enforced by test:
`tests/unit/cov/test_git_spawn_budget.py` counts spawns through the
process chokepoint (`gitio._run_raw`) on the tree path, and
`tests/integration/cov/test_validity_scale.py` does the same for a
100-file fallback fold. Spawn count stands in for NFS round-trip cost —
deterministic on any machine, where a wall-clock budget would just be
runner noise.

That the fix is batching and not a cache is a deliberate, checkpointed
call: a content-addressed validity cache was designed, then descoped at
the 2026-07-24 checkpoint once profiling showed the batched fallback
resolving a synthetic 2,000-file, 10%-churn repo in 5 flat spawns / 49 ms
— down from 2,602 spawns / 1.53 s for the pre-batching per-file chain.
A cache could not have touched the O(N) hashing spawns that dominated
that cost (every current file still has to be hashed to know what
changed), so its residual value fell below the carrying cost of a
persistent store's pruning/corruption/atomicity surface. See §9 of
`docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md`
for the full ruling and numbers.

**Supersede-on-recapture**
(`otto.coverage.capture.supersede.select_manual_captures`) runs before
folding: manual captures are deduplicated by `(tier, label, host)`, the
newest `captured_at` wins, and a replaced capture drops out of the runs
table entirely rather than having its credits accumulate alongside the
newer one.

**RepoTimeline** (`tests/_fixtures/_repo_timeline.py`) is the executable
spec for anchor and aging behavior: it scripts a real git repo through
`commit → capture → mutate → fold` and asserts the resulting per-line
disposition, table-driven across renames, squash-merge/GC, shallow
clones, reverts, and aging boundaries.

## The store (v4)

`store.json` is the canonical, versioned artifact `otto cov report`
writes for downstream consumers — external tooling, a foreign report
viewer — to read back; the in-process renderer consumes the same store
directly, in memory, before it is ever serialized. `CoverageStore.save`/
`.load` (`otto.coverage.store.model`) stamp every file with a top-level
`"format"` key equal to `STORE_FORMAT_VERSION` (`4`). The loader is
**exact-match**: a file whose `"format"` is missing, the wrong type, or
any version other than the one the running otto expects fails loud with
a `ValueError` naming both versions and telling the caller to
regenerate, rather than attempting to read renamed or reshaped keys
under old assumptions. There is no migration shim, by design —
`store.json` is a cheap-to-regenerate report artifact, not a long-lived
source of truth, so "delete and regenerate" beats accreted,
rarely-exercised migration code.

Version 4 adds three things to the schema. Each `RunRecord` grows an
explicit **`host`** identity (the capture's board id; `""` for a
synthetic or legacy-merged run with no single host behind it) —
sharpening the `(tier, label, host)` context identity the supersede
logic above already keys on from an implicit board-string convention
into an explicit field, with `label` unchanged as the display string a
drilldown chip shows. The store gained top-level
**`thresholds`** (`Thresholds.high`/`.medium`, sourced from
`[coverage.report]`; see {doc}`../../guide/coverage`) — the render
cutoffs the HTML renderer used to hard-code (`75.0`/`50.0`) are now
part of the persisted contract — and **`stat_types`**, the
type-extensible stats vocabulary `("line", "branch", "decision")`:
`decision` is a declared slot with no producer yet, so a `store.json`
consumer should render "no decision data" rather than assume every
declared type carries values. Each `LineRecord` also grows a reserved
**`ticket`** slot, `None` until the per-commit ticket plumbing exists —
nothing writes it today.

Per-host breakdowns are **derived, not stored** — the schema adds no
new per-line data for them. One capture is exactly one host and exactly
one run, so grouping a line's existing `run_hits` (run id → hit count)
by that run's `RunRecord.host` reconstructs per-host line counts
without a persisted per-host table; this is pinned by
`tests/unit/cov/test_model.py::TestRunHost::test_per_host_lines_derivable_from_run_hits`.

**Known limitation:** the legacy multi-host `.gcda`-merge fallback
(board directories with no `capture.json` — back-compat with pre-tier
output directories) still collapses every host it merged into one
synthetic run with `host = ""`. `lcov`'s counter merge combines hosts
before otto ever sees per-board data, so there is no host identity left
to attribute by the time a run is registered; host attribution
requires the per-board capture path (`otto cov get` / `otto test
--cov`, one `capture.json` per board). A report built solely from the
legacy fallback therefore has no per-host data to derive.

## The renderer

`SpaRenderer` (`otto.coverage.renderer.spa_renderer`) replaced `HtmlRenderer`
at `otto.coverage.reporter`'s single renderer construction site — the report
`otto cov report` emits is now the covapp single-page app, not
server-rendered Jinja HTML. The swap is duck-typed: both renderers expose
the same `render(store)` entry point, so the reporter's call site needed no
branching, only the one construction-site change.

`SpaRenderer.render` does two things, in order:

1. **Copy the bundle.** `make web` builds a second Vite app
   (`web/src/covapp/`) into
   `src/otto/coverage/renderer/static/covapp/` — a classic-script IIFE
   bundle with no ES modules, no inline scripts, and only relative asset
   paths, so it boots the same from `file://`, a CI artifacts browser, or
   behind Jenkins' minimal CSP. That directory isn't committed; it's built
   by `make web` and wheel-embedded (`make wheel-check` asserts the
   bundle's `index.html` is present in the built wheel). `SpaRenderer` copies
   it into the report directory as-is (sourcemaps excluded — kept out of
   every emitted report, used only by the TS coverage fold in CI).
2. **Emit the data chunks** (`otto.coverage.renderer.spa_data`, pure Python
   — no Jinja, no `jinja2` import) — `cov_data/index.js`, one classic-script
   assignment to `window.__OTTO_COV__` carrying the report-wide payload:
   config (thresholds, tier colors/labels, state colors), the run table,
   and a directory tree with **per-directory rollup stats precomputed in
   Python** (hit/total per stat type, per-tier breakdowns, stale/aging/
   excluded flag counts) so the frontend never re-aggregates the whole
   store client-side. Alongside it, one `cov_data/files/<mangled-path>.js`
   classic-script chunk per source file, each calling
   `window.__OTTO_COV_FILE__({...})` with that file's annotated source and
   per-line hit/branch/state/run data. Chunks load lazily on navigation to
   a file page, not up front, so page-load cost stays constant regardless
   of report size. Every index and file chunk carries the same **stamp** (a
   UTC timestamp plus a short random suffix, freshly generated per
   `render()` call) and the same `OTTO_COV_DATA_FORMAT` constant — the
   frontend's `EXPECTED_DATA_FORMAT` must match it exactly
   (`otto.coverage.renderer.spa_data.OTTO_COV_DATA_FORMAT` and the
   TypeScript constant are bumped together or never). A stamp mismatch
   between the index and a stale cached chunk, or a format the running
   bundle doesn't recognize, renders a "this report needs to be
   regenerated" guard screen instead of a wrong or partial report.

Exclusion display stays render-time, not store-time, for the same reason it
always has: a single-valued `LineRecord.state` can't express "excluded
always wins" over covered/stale/aging, so the reporter never bakes
`state == "excluded"` into the store — it only forwards the configured
extra marker strings. `emit_chunks` re-scans each file's source for
exclusion markers and annotates `FileRecord.excluded_lines` on the store as
a side effect of building that file's chunk. This is why `otto cov report`
calls `store.save(...)` for `store.json` **after** `renderer.render(store)`
returns — saving first would miss every file's excluded-line list.

The Jinja lane (`otto.coverage.renderer.html_renderer`'s `HtmlRenderer`,
its templates, and `report.css`) is **retained but no longer wired** — its
own unit tests still pass because they construct `HtmlRenderer` directly,
bypassing the reporter. Deleting it (and the old `web/src/covreport/` Vite
config) is tracked as follow-up work, not part of this change.

## What is unique about `cov`

`otto cov report` runs *after* the fact, over directories `otto test --cov`
or `otto cov get` already wrote: it still loads the lab — per-host toolchain
resolution (`gcov`, `lcov`) comes from host configuration, with the `.gcno`
header's gcov version stamp as the fallback (a clang stamp routes counters
through `llvm-cov gcov`) — but it creates **no output directory of its
own** and runs **no gate**: reporting on yesterday's run must never be
blocked by today's reservations ({doc}`../lifecycle`). Its siblings do touch the
lab: `otto cov get` fetches counters — into the standard per-invocation
output directory, or `--output` — and `otto cov clean` zeroes them on the
remotes.

## Where the code lives

- `otto.coverage.fetcher` — pulls `.gcda` off covered hosts (file
  transfer on Unix, console extraction on embedded)
- `otto.coverage.merge` — pairs counters to the `.gcno` build graph and
  merges hosts and runs
- `otto.coverage.capture` — freezes a merge into a per-board
  `capture.json`, anchored to `base_commit`
- `otto.coverage.store` — versioned store models (`RunRecord`,
  `LineRecord`, `Thresholds`, `STAT_TYPES`) and `CoverageStore`'s
  `save`/`load`, including the `STORE_FORMAT_VERSION` exact-match
  loader
- `otto.coverage.report_config` — resolves `[coverage.report]`'s raw
  settings dict into render `Thresholds` at report time
- `otto.coverage.renderer` — turns an assembled store into a report
- `otto.coverage.renderer.spa_data` — pure-Python emitter for the covapp
  data chunks (`cov_data/index.js` + per-file chunks); no Jinja
- `otto.coverage.renderer.spa_renderer` — `SpaRenderer`: copies the built
  covapp bundle, then calls `spa_data.emit_chunks`; the reporter's active
  renderer
- `web/src/covapp/` — the covapp SPA's TypeScript source (built by `make
  web` into `otto.coverage.renderer`'s `static/covapp/`, wheel-embedded)
- `otto.coverage.renderer.html_renderer` — `HtmlRenderer` and its templates
  remain but are no longer constructed by the reporter (see "The renderer"
  above)
- {mod}`otto.coverage.reporter` — `otto cov report`'s store assembly: tiers,
  the base_commit guard, and the `--tier NAME=PATH` escape hatch
- {mod}`otto.coverage.validity` — the report-time valid/stale/aging pass over
  manual captures
