# otto cov report

```bash
otto cov report <output_dir> --dir ./my_report
```

`otto cov report` assembles a store from every source available:

1. **E2E captures** — `capture.json` files under each given output
   directory's `cov/<host_id>/<product>/` ({ref}`the run tree <run-tree>`),
   subject to the base_commit guard below. A product directory with no
   `capture.json` falls back to the legacy `.gcda`-merge path (back-compat
   with pre-tier output directories). A directory that holds `.gcda` or a
   `capture.json` *directly* under the host — the pre-product one-level tree
   — is refused by name rather than misread as a product; there is no
   migration shim, so re-collect with this version of otto.
2. **Unit harvest** — every `unit`-kind tier's `harvest_dirs`, plus each of
   its named `products` views, swept fresh from the current build tree.
3. **Manual store** — every capture committed under the repo's
   `.otto/coverage/manual/`, loaded automatically with the validity
   pass applied.

`OUTPUT_DIRS` is optional: with none given, the report is built
from the committed manual-capture store (and any configured unit
tiers) alone.

A report whose assembled store ends up **empty** — no captures, no
harvested counters, no manual store — exits `1` with a one-line error
naming every location that was searched.

## Stitching Multiple Runs

To combine coverage from separate test runs into a single report:

```bash
otto cov report run1_output/ run2_output/ run3_output/ --dir ./combined_report
```

## Options

| Option                    | Description                                                          | Default             |
|---------------------------|----------------------------------------------------------------------|---------------------|
| `OUTPUT_DIRS`             | `otto test`/`otto cov get` output dirs with `cov/` subdirectories    | none — report is built from the manual store alone |
| `--dir, -d PATH`          | Where to place the generated coverage report                        | `./cov_report`      |
| `--project-name STR`      | Title shown in the report header                                     | `Coverage Report`   |
| `--tier NAME[=PATH]`      | Git-less escape hatch (see below); repeatable, order = precedence    | the configured tiers (or `system` with none configured) |
| `--tickets-json PATH`     | Also write a per-ticket coverage summary (see {ref}`coverage-tickets-json`).  Requires `[coverage.tickets]` to have attributed at least one ticket; fails loud (exit 1) otherwise | not written |

(coverage-report-stale-builds)=
## Stale Builds: "stamp mismatch" and the e2e base_commit guard

gcov embeds a build stamp in both the `.gcno` notes files (written at
compile time) and the `.gcda` data files (written at run time).  Raw
counters are therefore only meaningful against the exact build that
produced them — and the moment they are paired is **collection**, when
`otto cov get` (or the `otto test --cov` tail) merges the fetched
`.gcda` against the local `.gcno` graph.  If the product was rebuilt
in between, gcov refuses the data (`stamp mismatch with notes file`)
and otto raises a `CoverageDataMismatchError` explaining the cause
instead of dumping raw `lcov` output:

> Coverage data does not match the current product build (gcov reports a
> stamp mismatch between .gcda data and .gcno notes files). The product
> was likely rebuilt after `otto test --cov` collected this data —
> coverage must be reported against the exact build that produced it.
> Re-run `otto test --cov` and report on the new output directory.

Once a `capture.json` exists, the build tree no longer matters: the
capture holds parsed hits, not raw counters, so **reporting on a
capture-bearing run directory is immune to rebuilds** — recompiling
the product between collection and `otto cov report` changes nothing.
The same rebuild against a *pre-capture* run directory (an older otto's
output, loaded via the legacy `.gcda`-merge fallback) still re-pairs
raw counters at report time and fails with the error above.

The ship-step variant of this mistake — deploying a binary, then
rebuilding the local tree it will be decoded against — can be caught in
the build itself, before any test time is spent: see the
{ref}`.gcno stamp guard <coverage-gcc-stamp-guard>` for GCC products,
its {ref}`embedded flavor <coverage-embedded-stamp-guard>`, and the
{ref}`clang differences <coverage-clang-stale-deploys>` (clang's
stale-deploy failure is silent at the toolchain level, so otto verifies
the file pairing structurally at collection instead).

A capture carries its own, git-based guard instead: its recorded
`base_commit` must equal the tree's current `HEAD`.  A capture taken at a different
commit — the tree moved on since collection — fails the report with a
clean error naming both commits; the recovery is to collect fresh coverage
with `otto test --cov` (or `otto cov get`) and report on the new
output.  A working tree that is merely **dirty** at report time (same
`HEAD`, uncommitted edits) does not fail: the e2e capture's hits are
remapped from committed-code coordinates onto the current tree — the
report-time mirror of the {ref}`dirty-tree remap at retrieval
<coverage-dirty-remap>` — with a warning, and hits on
locally-modified lines are omitted rather than misattributed.

(coverage-tier-name-path)=
## The `--tier NAME=PATH` escape hatch

`--tier NAME=PATH` remains available as a **git-less** fallback for
data the declarative model doesn't produce — a foreign `lcov` `.info`
file, or a report built outside a git repository (retrieval and the
validity pass both require git; this flag does not).  When any
`--tier` flag is given, `otto cov report` **bypasses the declarative
tiers model entirely** — settings tiers, the manual store, and unit
harvesting are not consulted; only the exact tiers named on the
command line are loaded.

`NAME` is a free-form label; `PATH` is an lcov `.info` tracefile.  The
bare form `--tier system` (no path) refers to the implicit tier
produced by merging the supplied `.gcda` directories with `lcov`; every
other tier requires a path.  A non-`system` tier without a path,
and a repeated tier name, are both rejected.  Flag order is precedence order — the
first flag is highest-precedence and wins the row coloring when
multiple tiers hit the same line.

```bash
otto cov report runs/ \
    --tier unit=u.info \
    --tier system \
    --tier integration=i.info \
    --tier manual=m.info \
    --dir ./cov_report
```

This produces a four-tier report with precedence
`unit > system > integration > manual`.  A line hit only by the
manual tier is colored manual; a line hit by all four is colored unit
(the highest-precedence hit wins).  The summary table and per-file
table both grow a column per tier in the same left-to-right order.

(coverage-colors)=
## Colors and Legend

Each tier renders in its configured `color` — a CSS named color or
`#RRGGBB` hex, validated when settings load (an invalid value is a
settings error, not a report-time surprise).  A tier that declares no
explicit `color` gets a default keyed by its `kind`:

| Kind | Default color |
|------|-----------------|
| `e2e` | green |
| `unit` | yellow |
| `manual` | orange |

Line **states** — as opposed to tiers — use fixed, non-configurable
colors:

| State | Color |
|-------|-------|
| uncovered | light red |
| excluded | grey |
| stale | violet |
| aging | tan |

Each annotated source line resolves to exactly one color, in this
precedence order: **excluded** (grey, always wins) → the
highest-precedence **tier** color among tiers with valid evidence on
that line → **aging** (tan — the winning evidence is valid manual data
past its `max_age`, i.e. a faded manual orange) → **stale** (violet —
the only evidence was manual and the code changed since) →
**uncovered** (light red).

A **legend** mapping every tier name and state to its color is always one
click away, in the app bar's **⋮** overflow menu present on every page.

## Output

`otto cov report` writes a self-contained **single-page app** to the
`--dir` directory (default: `./cov_report/index.html`) — there is no
build step and nothing to serve: open `index.html` straight off disk
(`file://`) or point any static host or CI artifacts browser at the
directory. Routes are **hash-based** (`#/coverage/...`, `#/runs`), so deep
links resolve identically from disk, from a CI job's artifacts browser, or
from a GitLab Pages subpath at any URL depth — no server rewrite rule to
configure.

- **Directory pages** (`#/coverage`, `#/coverage/<dir>`) — a tree view from
  the current node down, one row per child directory or file, with
  per-directory **rollups**: hit/total, threshold-colored line and branch
  percentage (see {ref}`coverage-report-thresholds`), one column per
  configured tier, and flag badges for stale/aging/excluded counts. Every
  column is sortable. The root page also shows a collapsed **Runs &
  captures** summary — the full table lives at `#/runs`.
- **File pages** (`#/coverage/<file>`) — annotated source: per-tier hit
  columns, branch pills (taken/not-taken/unreachable), winner-take-all row
  coloring per {ref}`coverage-colors`, and the per-line **runs** drilldown
  from {ref}`coverage-runs`, listing every run that hit the line with
  revoked/aging credits marked.

  ![Annotated source view: winner-take-all row tinting, branch pills, and
  per-line run drilldowns](../../_static/generated/coverage-file.png)

- **Code search** — press <kbd>Ctrl</kbd>+<kbd>K</kbd> (<kbd>⌘</kbd>+<kbd>K</kbd> on
  macOS) or click the search box in the app bar on any page. Substring
  search over every source file in the report, grouped by file, with a
  **Regex** chip (JavaScript syntax) and an **Uncovered only** chip that
  keeps just the lines no tier ever hit — both off by default, both
  remembered. A query with no uppercase letter is case-insensitive; any
  uppercase letter makes it exact. Type `@name` to jump to a function
  definition instead (from the compiler's own `FN:` records; C++ names
  appear as `lcov` emitted them). <kbd>Enter</kbd> opens the file with the
  line scrolled into view and every match painted; <kbd>Ctrl</kbd>+<kbd>Enter</kbd>
  keeps the palette open so you can step through results. The query rides
  along in the link (`?q=`), so a hit is shareable, and a pinned ticket
  narrows the search to the files it touched.

  ![The search palette over an example report: matches grouped by file,
  the Regex and Uncovered-only chips, and the total-count
  footer](../../_static/generated/coverage-search.png)

- **Runs & contexts page** (`#/runs`) — one row per run (see
  {ref}`coverage-runs`); multi-host runs show host pills reading
  `host · product` with an expandable per-host lines breakdown, filterable by
  tier, by product, and by free-text search over label/host/ticket/board/product. Per-run **branch** contribution
  isn't part of the stored data (**branch** hits are recorded per line, not
  per line-and-run — unlike line hits, which are) and renders as "not
  tracked per-run".

  ![Runs & contexts page: one row per context with per-host breakdowns and
  filters](../../_static/generated/coverage-runs.png)

- **Report-wide context focus** — pin a run's context from its row on the
  runs page, or from the app bar's **⋮** overflow menu (also home to
  keyboard shortcuts and the tier/state color key); every stat, percentage,
  and file-page tint recomputes to that run's coverage alone, with
  everything else reading as uncovered/neutral. The pinned context encodes
  into `?ctx=<run-label>` on the current route — a focused view is
  bookmarkable and shareable — and persists per report in `localStorage`.
  Branch cells show "—" while a focus is active; focus mode filters line
  stats only.
- **Product focus** — pin a product from the chip row on the runs page, from
  the app bar's **⋮** menu, or by hand with `?product=<name>` on the current
  route; it persists per report in `localStorage` the same way a context pin
  does. Like run focus it narrows the **numerator**: only runs tagged with
  that product count, so one pin answers "how much of this code did *this*
  product prove?" across every page, whatever host or tier produced the
  evidence. The controls appear only when the report has products to pin —
  a report whose every run is an unnamed unit harvest shows none.

  It composes with the other pins. A context and a product together read as
  `nightly · agent` and intersect; a pinned ticket
  keeps owning the denominator while the product narrows the numerator, and
  where that pair is too fine to answer honestly the cell declines rather
  than guessing — the same rule the ticket-plus-context pair already
  follows.

  A product, unlike a context, **spans tiers**: an `agent` pin gathers that
  product's e2e captures and its unit views alike. So a bare product pin has
  no tier colour to lend — file rows tint neutral and the per-tier columns
  read `—`. Pin a context as well to get the per-tier breakdown back.
- **Tickets page** (`#/tickets`) — present only when `[coverage.tickets]`
  is configured (see {ref}`coverage-tickets`); one row per ticket id, sorted
  worst-uncovered-first, with the same overall stats card scoped to every
  attributed line. Each row carries owned/covered/uncovered counts, a
  threshold-colored line percentage, one column per tier, and a pin control;
  every column is sortable. Expanding a row lists its missing lines grouped
  by file as ranges, each linking straight into the annotated source.

  ![Tickets page: one row per ticket sorted by uncovered lines, an expanded
  row's missing-line ranges, and the overall attributed-lines stats
  card](../../_static/generated/coverage-tickets.png)

- **Ticket context** — pin a ticket from its row on the tickets page, or from
  the ticket **search box** in the app bar (press <kbd>/</kbd> to jump straight
  to it); unlike run focus, which dims non-participating code, pinning a
  ticket **hides** files (and directories) it never touched from the tree
  entirely, and every remaining percentage — including the per-tier rows —
  recomputes over that ticket's owned lines alone. A hidden-count row above
  the tree names what was removed. The file page keeps the opposite rule: it
  **dims**, never hides, a non-owned line. Composes with run focus
  (`?ticket=<id>` alongside `?ctx=<label>`) to show this ticket's lines as
  proven by that run.

  ![A pinned ticket at the directory page: utils.c's row is hidden (it
  owns none of the ticket's lines) and the banner names what was
  hidden](../../_static/generated/coverage-ticket-context.png)

`store.json` is written alongside the report with the same data —
validity states, colors, runs, tickets, each file's excluded lines, and
its functions with per-tier hits included — as the explicit data contract
for tooling built on top of a report without touching the pipeline.
`tickets.json` (see
{ref}`coverage-tickets-json`) is a separate, public export built for
consumers otto does not control.

## Hosting the report in CI

The report has no server-side requirements: every asset reference is
relative, there are no ES module scripts, no inline `<script>`, no
`eval`, and no WASM. That makes it render identically whether it's opened
straight from disk (`file://`, zero serving) or published by a CI job.

**GitLab** works out of the box — publish the `--dir` directory as a
Pages site, or just let GitLab's artifacts browser serve it. No
configuration is needed.

**Jenkins** applies a strict Content-Security-Policy to archived HTML by
default, which blocks all JavaScript. The report only needs that CSP
relaxed enough to run same-origin classic scripts — nothing else. Publish
the report with the HTML Publisher plugin and set this minimal policy via
the `hudson.model.DirectoryBrowserSupport.CSP` system property:

```text
default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'
```

The report runs under this policy because it only uses classic
`<script src="...">` tags loading relative, self-hosted assets.
