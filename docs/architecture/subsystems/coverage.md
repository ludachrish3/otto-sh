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
- `otto.coverage.renderer` — turns an assembled store into the HTML
  report
- {mod}`otto.coverage.reporter` — `otto cov report`'s store assembly: tiers,
  the base_commit guard, and the `--tier NAME=PATH` escape hatch
- {mod}`otto.coverage.validity` — the report-time valid/stale/aging pass over
  manual captures
