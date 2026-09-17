# Coverage during a test run

```bash
otto test TestMyDevice
```

Coverage during a test run is **automatic**. `otto test` looks at every
product on every host the `[coverage].hosts` selector matches, decides
locally whether each one is an instrumented build, and turns retrieval on by
itself when at least one is — logging `coverage retrieval on` with the
per-product verdicts, so a run that collected coverage says so.

Two things have to be in place for any of it: a `[coverage]` table in
`.otto/settings.toml` (a `hosts` selector is the smallest one that counts) and
a git-tracked SUT, because every capture is anchored to a commit.  Tiers are
optional — with no `[coverage.tiers]` table otto assumes one implicit tier
named `system` of the `e2e` kind, the kind a lab run collects into
({doc}`tiers`).

Retrieval then runs the suite normally, fetches each instrumented product's
`.gcda` files from its own `cov_dir`, and — on a best-effort basis — produces
a `capture.json` per host per product: one product's merged, source-resolved
coverage on one host, stamped with the `base_commit` (the SUT's commit at
capture time) it is valid against.  It is the same capture-production
machinery `otto cov get` runs.

This tail never fails an otherwise-successful test run.  A SUT that is not a
git checkout (no commit to anchor a capture to), misconfigured tiers, or a
stamp mismatch during merge — gcov reporting that the fetched `.gcda` came
from a different build than the local `.gcno` notes files — are logged and
swallowed.  What survives is the fetched `.gcda` themselves, in the run's own
local `cov/` tree: recovery re-processes those, because a successful fetch
zeroes the host's copies behind it.  They land under `cov/` in the suite's
output directory, keyed by host and then product —
{ref}`the run tree <run-tree>` is the shape.

(coverage-tristate)=
## Auto, on, off

`--cov/--no-cov` is a **tri-state**, and its default is neither on nor off:

| Mode | What it does |
|---|---|
| *(neither flag)* | Auto — retrieval runs when some product is instrumented **and** a `[coverage]` table is configured |
| `--cov` | Insist. Refuses **before any test runs** when nothing is instrumented, or when the repo has no `[coverage]` table |
| `--no-cov` | Off, regardless of what the lab carries |

`--cov-dir`, `--cov-report`, `--cov-report-dir` and `--cov-tickets-json` all
imply coverage, so pairing any of them with `--no-cov` is a usage error rather
than a precedence puzzle.

The refusal is **one line plus a table** of every product it examined and what
it concluded, so the fix is on the screen.  An excerpt, at a narrow terminal:

```text
        coverage instrumentation
╭───────┬─────────┬──────────────╮
│ host  │ product │ instrumented │
├───────┼─────────┼──────────────┤
│ test1 │ agent   │ no           │
│ test2 │ agent   │ unknown      │
╰───────┴─────────┴──────────────╯
 unknown = the product cannot tell: override Product.instrumented(),
        or set `instrumented = true` on the [[products]] entry

error: otto test --cov: no instrumented product — coverage cannot be collected.
```

A refusal is printed on the console only.  What the run log records is the
*auto* decision: retrieval switching itself on, staying off, or proceeding
with some products missing, each with the verdicts as a plain listing — a
log file cannot hold a table.

Three verdicts, because two of them are not the same "no":

- **yes** — the artifact carries the compiler's coverage markers.
- **no** — it was scanned and carries none.
- **unknown** — otto could not tell: the artifact is missing, unreadable, or
  an archive the scan cannot see inside. An unknown counts as *not*
  instrumented, and it is the only verdict with a remedy, which is why the
  table's caption appears only when one is present: override
  `Product.instrumented()`, or set `instrumented = true` on the
  `[[products]]` entry.

A product whose artifact the suite itself builds at run time is therefore
`unknown` at decision time, which is before the build: build it first, or
declare it instrumented.

When no product on any coverage host exists at all, the refusal says so
directly rather than listing an empty table. When *some* products are
instrumented and others are not, retrieval proceeds and warns, naming the
ones that will contribute nothing.

In auto mode a broken `[coverage].hosts` selector is one warning and
coverage stays off — a plain `otto test` asked for a test run, not for
coverage, and must not die of a coverage misconfiguration. Under `--cov` the
same selector is an error.

```{note}
Both `otto cov get` and this `otto test --cov` tail wrap one async library
function — `collect_coverage()` — paired with `run_coverage_report()` for the
HTML report. To drive collection and reporting from your own Python (CI glue or
a custom pipeline), see the *Collecting coverage from Python* section of
{doc}`../../../library/index`.
```

## Options

| Option | Description |
| ------ | ----------- |
| `--cov / --no-cov` | Force retrieval on or off; the default is auto (see {ref}`coverage-tristate`). On, each instrumented product's `.gcda` is fetched into `<run>/cov/<host_id>/<product>/` |
| `--cov-dir PATH` | Write coverage artifacts to an explicit directory instead of `<run>/cov/` (implies `--cov`). PATH replaces that `cov/` directory and nothing else: each product's counters still land in `PATH/<host_id>/<product>/` |
| `--overwrite-cov-dir` | Allow `--cov-dir` to clear an existing non-empty directory |
| `--cov-clean / --no-cov-clean` | Delete stale `.gcda` under each instrumented product's `cov_dir` before the run (on by default; `.gcda` counters are additive) |
| `--cov-report, -r` | Also render the HTML report inline after the run (implies `--cov`) |
| `--cov-report-dir PATH` | Explicit destination for the inline HTML report (implies `--cov-report`) |
| `--cov-tickets-json PATH` | Also write the per-ticket coverage summary to PATH after the run (implies `--cov-report`). Needs `[coverage.tickets]` configured, checked before the suite starts — see {ref}`coverage-tickets-json` |

See {doc}`../test/index` for the rest of `otto test`'s options, and
{doc}`index` for the collection workflow these flags plug into.

## Choosing a Destination

Use `--cov-dir` to write coverage artifacts to an explicit location —
for example, a persistent CI directory:

```bash
otto test --cov-dir /var/artifacts/myrun TestMyDevice
```

`--cov-dir` implies `--cov`, so the `--cov` flag is optional when you
supply a path.  PATH stands in for the run directory's `cov/` subtree, keeping
its shape: a product's `.gcda`, its merged `.info` files and its
`capture.json` all land in `PATH/<host_id>/<product>/`, exactly as they would
under `<run>/cov/`.  The destination directory is created if it does not
already exist.  If it exists and is non-empty, the run aborts to avoid
mixing stale coverage into the new results; pass `--overwrite-cov-dir`
to clear it first:

```bash
otto test --cov-dir /var/artifacts/myrun --overwrite-cov-dir TestMyDevice
```

Omitting every coverage flag leaves the decision to otto (see
{ref}`coverage-tristate`); `--no-cov` is how you say no outright.

## Inline Reports

`--cov-report` renders the HTML report immediately after the run,
without a separate `otto cov report` invocation.  It goes through the
same collection model: the configured tiers (colors, precedence),
the exclusion rules, the unit-tier harvest (each `kind = "unit"` tier's build
directories, swept for `.gcda` at report time) and the committed manual store
(the captures under the repo's `.otto/coverage/manual/`, recorded by hand and
reviewed like any other file) all apply, exactly as they would in a standalone
report.
Like the capture tail, inline report generation is best-effort — a
report-side problem is logged and never fails an otherwise-successful
test run.

## Pre-Run Cleanup

By default, a run with coverage on deletes stale `.gcda` files under each
instrumented product's `cov_dir` **before** the test run.  This is important
because `.gcda` counters are **additive** — without cleanup, coverage data
from previous runs contaminates the current results.

To skip pre-run cleanup and accumulate coverage across runs:

```bash
otto test --cov --no-cov-clean TestMyDevice
```
