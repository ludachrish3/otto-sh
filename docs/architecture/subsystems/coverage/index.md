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
    merge [label="pair + capture\npair each .gcda with its .gcno by path,\none lcov capture per product,\nresolve source paths"];
    capture [label="capture.json or capture.failed\nper host per product: parsed hits,\ngit-anchored coordinates"];
    render [label="otto cov report\ncaptures + unit harvest\n+ manual store → HTML"];
    err [label="CoverageDataMismatchError\nstale build → instructions,\nnot a wrong report", shape=note, style=dashed];

    test -> fetch -> merge -> capture -> render;
    merge -> err [style=dashed, label=" stamp\nmismatch"];
}
```

The stages (packages `otto.coverage.fetcher` → `merge` → `capture` →
`renderer` → `reporter`):

1. **Fetch** — pull `.gcda` data from each covered host's instrumented
   **products** after the run, each from its own `cov_dir`, into
   `cov/<host>/<product>/` ({ref}`the run tree <run-tree>`).
   Fetchers are per-family: file transfer for Unix hosts and containers,
   console extraction for embedded targets. Which hosts are covered is
   *project-declared* — the `[coverage].hosts` regex in `settings.toml` — never
   inferred, so hop hosts and uninstrumented beds can't sneak into a report.
   Which *products* on those hosts contribute is decided locally before
   anything runs, by scanning each artifact for the compiler's coverage
   markers (`otto.coverage.instrumentation`); that same verdict is what
   turns retrieval on for a plain `otto test`.

   The `--cov`/auto/`--no-cov` decision is asymmetric on purpose. An
   explicit `--cov` refuses before the first test runs when nothing is
   instrumented or the project has no `[coverage]` table: a run worth the lab
   time only because it would produce coverage should not finish and then
   report that it could not. Auto mode degrades the same situations to a
   warning and no coverage, because a plain `otto test` asked for a test run
   and must not die of a coverage misconfiguration.
2. **Pair and capture** — pair each fetched `.gcda` with its own `.gcno` by
   path, using the object list read from the product's artifact, run one lcov
   capture per product, and resolve each source path to the project file
   ({doc}`merging` walks the steps).
3. **Capture file** — freeze the result into one `capture.json` per host
   per product: parsed hits in committed-code coordinates, anchored to the
   checkout's `HEAD` (`base_commit`) and per-file blob SHAs, and tagged with the
   host and product they came from. A product that fails writes
   `capture.failed` instead and keeps its counters on the host.
4. **Render / report** — `otto cov report` assembles every tier — e2e
   captures, a fresh unit-tier harvest, the committed manual store — into an
   HTML report plus summary tiers.

The pairing stage's core invariant is *build/counter identity*: `.gcda` files
are only meaningful against the exact `.gcno` graph the binary was compiled
with. That pairing happens once, at **collection** — the capture holds
parsed hits, so the report step never touches the build tree again and a
later rebuild cannot invalidate it (a capture's own guard is its
`base_commit`, which must match `HEAD` at report time). When the raw pairing disagrees — a
stale or partially rebuilt product tree at collection time, or a product
directory captured late, at report time, after a rebuild — that product
stops with a diagnostic error that names the mismatch and the rebuild that
fixes it, rather than a gcov stack trace or a silently wrong report. That
fail-with-instructions posture is a house rule ({doc}`../../principles`).

## How this section is organized

The pipeline above is one axis; the five pages below are the other.
{doc}`types` is the vocabulary every stage speaks — tiers, stat types, line
states, thresholds — and the store schema that carries them. {doc}`merging`
is what `otto cov report` actually does with the pieces: the fold order,
run accumulation, and the rules that keep one piece of evidence from being
counted twice. {doc}`manual` is the one tier whose data is committed, and
therefore the one that has to be re-proved against a moving tree on every
report. {doc}`attribution` answers a different question about the same
committed history — not "is this evidence still valid" but "which ticket
does this line belong to" — with its own git log walk and its own
filesystem-operation budget. {doc}`renderer` is the last mile — an
assembled store to a self-contained report directory.

```{toctree}
:maxdepth: 1

types
merging
manual
attribution
renderer
```

## What is unique about `cov`

`otto cov report` runs *after* the fact, over directories `otto test --cov`
or `otto cov get` already wrote: it still loads the lab — per-host toolchain
resolution (`gcov`, `lcov`) comes from host configuration first, else the
product's own `.gcda` stamp per `<host>/<product>` directory (`llvm-cov`
for `402*`, `408*` and `B11*`, `gcov-<major>` for another gcc major's; see
{ref}`coverage-gcov-resolution`); a unit tier's build directories take the
tier's own `toolchain` setting in place of the host record, resolved with
every unit view in the preflight before the destination is touched
(`otto.coverage.unit_toolchain`) — but it creates **no output directory of
its own** and runs **no gate**: reporting on yesterday's run must never be
blocked by today's reservations ({doc}`../../lifecycle`). Its siblings do touch the
lab: `otto cov get` fetches counters — into the standard per-invocation
output directory, or `--output` — and `otto cov clean` zeroes them on the
remotes.

Each product carries two coverage hooks: `prepare_coverage(host)` runs before
the fetcher archives the product's `cov_dir` and puts counters on disk when they live somewhere else
first, and `reset_coverage(host)` is what every clean — `--cov-clean`, the
post-fetch clean, `otto cov clean` — issues per product. The defaults (do
nothing; `find <cov_dir> -name '*.gcda' -type f -delete`, guarded so a `cov_dir`
that does not exist yet is a no-op) are the user-space case;
a kind whose counters live in the kernel or behind a daemon overrides them.

## Where the code lives

- `otto.coverage.fetcher` — pulls `.gcda` off covered hosts, per product
  (one plain `tar` archive of the whole `cov_dir` per product on Unix and
  containers, written to a `mktemp` file in the host's temp dir, of which
  only the `.gcda` are kept; an archive command that exits non-zero fails
  the product's fetch; console extraction on embedded). Each `.gcda` keeps
  its path — relative to `cov_dir`, or the full dumped path on embedded — so
  two units sharing a basename stay apart and each pairs with its own notes
- `otto.coverage.instrumentation` — the local per-product instrumentation
  scan and the `--cov`/auto/`--no-cov` decision table
- `otto.coverage.config` — `check_destination`/`prepare_destination`: the
  check-only / create-or-clear destination gate every `cov_dir` /
  `cov_report_dir` / report `output_dir` preflight shares, raising a
  field-named `DestinationError`
- `otto.coverage.objects` — a product's object list, read from its local
  artifact (and its `DT_NEEDED` libraries)
- `otto.coverage.pairing` — pairs each fetched `.gcda` with exactly one
  `.gcno`, by path
- `otto.coverage.merge` — `LcovMerger`: stages the pairs and runs one
  `lcov --capture` per product (or, for a unit tier, per build directory)
- `otto.coverage.sources` — `SourceResolver`: resolves each `SF:` path a
  capture names to its project file
- `otto.coverage.capture` — freezes a capture into a per-(host, product)
  `capture.json`, anchored to `base_commit`, or records `capture.failed`
- `otto.coverage.tiers` — resolves the declarative `[coverage.tiers]`
  table into precedence-ordered `TierConfig` values
- `otto.coverage.store` — versioned store models (`RunRecord`,
  `LineRecord`, `Thresholds`, `STAT_TYPES`) and `CoverageStore`'s
  `save`/`load`, including a loader that accepts only the
  declared read versions
- `otto.coverage.report_config` — resolves `[coverage.report]`'s raw
  settings dict into render `Thresholds` at report time; also re-exports
  `[coverage.tickets]`'s loader (`otto.coverage.tickets`, below)
- `otto.coverage.report_inputs` — `resolve_report_inputs`: turns
  `[coverage]` into the `ReportInputs` bundle `otto cov report` and the
  post-run `otto test --cov-report` share, so the two cannot drift
- `otto.coverage.tickets` — `TicketSpec`: compiles `[coverage.tickets]`'s
  `pattern`/`url`, extracts ticket ids from commit messages ({doc}`attribution`)
- `otto.coverage.attribution` — the bounded git log walk and backward
  hunk replay that attributes every line to a commit, then a ticket
  ({doc}`attribution`)
- `otto.coverage.ticket_export` — builds and writes `tickets.json`, otto's
  first public coverage export (see the guide's per-ticket section)
- `otto.coverage.renderer` — turns an assembled store into a report
- `otto.coverage.renderer.spa_data` — pure-Python emitter for the covapp
  data chunks (`cov_data/index.js` + per-file chunks)
- `otto.coverage.renderer.spa_renderer` — `SpaRenderer`: copies the built
  covapp bundle, then calls `spa_data.emit_chunks`; the reporter's only
  renderer
- `web/src/covapp/` — the covapp SPA's TypeScript source (built by `make
  web` into `otto._webassets.COVAPP`, wheel-embedded)
- {mod}`otto.coverage.reporter` — `otto cov report`'s store assembly: the
  capture states, tiers, the base_commit guard, and the `--tier NAME=PATH`
  escape hatch
- {mod}`otto.coverage.validity` — the report-time valid/stale/aging pass over
  manual captures
- `otto.coverage.anchor` — `AnchorResolver`: the per-capture tree-diff /
  blob-fallback resolution the validity pass consumes
