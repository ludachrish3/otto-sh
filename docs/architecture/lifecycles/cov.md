# otto cov — the coverage pipeline

The problem: embedded and cross-compiled products execute where no coverage
tooling runs. gcov counters (`.gcda`) accumulate on the target — in memory or
on an on-device filesystem — while the compile-time graph (`.gcno`) and
sources live in the build tree on the runner. Neither side alone can make a
report; this pipeline marries them.

```{admonition} Roadmap items pending
:class: note

A coverage-tier collection model is being designed (see
`todo/coverage_roadmap.md`); items there — a `capture` subcommand for the
manual tier, fail-under thresholds, console summaries — are explicitly not
in the current release. This page describes what ships today.
```

```{graphviz}
digraph coverage {
    rankdir=LR;
    node [shape=box];

    test [label="otto test --cov\ninstrumented run"];
    fetch [label="fetch\n.gcda from covered hosts\n(transfer on Unix,\nconsole extraction embedded)"];
    correlate [label="correlate\nmatch .gcda ↔ .gcno graph,\nremap sysroot paths,\nmerge hosts + runs (lcov)"];
    render [label="render / report\nHTML + summary tiers"];
    err [label="CoverageDataMismatchError\nstale build → instructions,\nnot a wrong report", shape=note, style=dashed];

    test -> fetch -> correlate -> render;
    correlate -> err [style=dashed, label=" stamp\nmismatch"];
}
```

The stages (`otto cov`, packages `otto.coverage.fetcher` → `correlator` →
`renderer` → `reporter`):

1. **Fetch** — pull `.gcda` data from each covered host after the run.
   Fetchers are per-family: file transfer for Unix hosts, console extraction
   for embedded targets. Which hosts are covered is *repo-declared* — the
   `[coverage].hosts` regex in `settings.toml` — never inferred, so hop hosts
   and uninstrumented beds can't sneak into a report.
2. **Correlate** — match counters to the build tree's `.gcno` graph and remap
   embedded/sysroot paths back to source paths, merging counters across hosts
   and runs (lcov semantics).
3. **Render / report** — an HTML report plus summary tiers.

The correlator's core invariant is *build/counter identity*: `.gcda` files
are only meaningful against the exact `.gcno` graph the binary was compiled
with. When they disagree — a stale or partially rebuilt product tree — the
pipeline stops with a diagnostic error that names the mismatch and the
rebuild that fixes it, rather than a gcov stack trace or a silently wrong
report. That fail-with-instructions posture is a house rule
({doc}`../principles`).

## What is unique about `cov`

`otto cov` runs *after* the fact, over directories `otto test --cov` already
wrote: it still loads the lab — per-host toolchain resolution (`gcov`,
`lcov`) comes from host configuration, with `.gcno` inspection as the
fallback — but it creates **no output directory of its own** and runs **no
gate**: reporting on yesterday's run must never be blocked by today's
reservations ({doc}`index`).

## `otto cov --help`

```{raw} html
:file: ../../_static/generated/termynal/help-cov.html
```
