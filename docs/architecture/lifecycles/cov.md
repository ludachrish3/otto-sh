# otto cov — the coverage pipeline

The problem: embedded and cross-compiled products execute where no coverage
tooling runs. gcov counters (`.gcda`) accumulate on the target — in memory or
on an on-device filesystem — while the compile-time graph (`.gcno`) and
sources live in the build tree on the runner. Neither side alone can make a
report; this pipeline marries them.

```{admonition} Roadmap items pending
:class: note

The coverage-tier collection model — declarative tiers, `otto cov get` /
`otto cov clean`, per-board captures anchored to `base_commit`, the committed manual store —
ships in this release. Remaining roadmap items (`todo/coverage_roadmap.md`
and the plan's later phases) — fail-under thresholds, console summaries,
per-ticket rollups, embedded counter reset for `cov clean`, wiring custom
exclusion markers into lcov's percentages — are explicitly not in it.
```

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
any other name — declared in `.otto/settings.toml` under `[coverage.tiers]`
with a `kind` (`e2e` / `unit` / `manual`) that selects how otto collects that
tier's data. Only the **manual** tier's data is committed into the
repo (every tier's data is anchored to `base_commit`): selecting a manual-kind tier on `otto cov get` copies the capture into
the repo's committed store at `.otto/coverage/manual/` — proof of a manual
test session that travels with the code and is PR-reviewable. E2e data lives
in each test run's output directory, and unit data is harvested fresh from
the build tree's `harvest_dirs` at report time.

`otto cov report` assembles a store from all three sources per tier `kind`:
e2e captures from the given output directories (behind the base_commit guard above),
the unit harvest, and every committed manual capture — loaded automatically,
no path needed. A report-time **validity pass** (`otto.coverage.validity`)
anchors each manual capture's lines against the current tree by git blob
SHA: unchanged lines stay **valid**, changed/deleted lines go **stale**
(coverage revoked — the evidence no longer describes this code), and
valid-but-old lines past the tier's `max_age` are flagged **aging** without
losing coverage credit.

## What is unique about `cov`

`otto cov report` runs *after* the fact, over directories `otto test --cov`
or `otto cov get` already wrote: it still loads the lab — per-host toolchain
resolution (`gcov`, `lcov`) comes from host configuration, with the `.gcno`
header's gcov version stamp as the fallback (a clang stamp routes counters
through `llvm-cov gcov`) — but it creates **no output directory of its
own** and runs **no gate**: reporting on yesterday's run must never be
blocked by today's reservations ({doc}`index`). Its siblings do touch the
lab: `otto cov get` fetches counters — into the standard per-invocation
output directory, or `--output` — and `otto cov clean` zeroes them on the
remotes.

## `otto cov --help`

```{raw} html
:file: ../../_static/generated/termynal/help-cov.html
```
