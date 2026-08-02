# Coverage Collection

Otto collects gcov coverage data from remote hosts and renders
multi-tier HTML coverage reports.  Coverage tiers — `system` (e2e),
`unit`, `manual`, or any other name — are declared in
`.otto/settings.toml`; three commands drive the workflow:

1. **`otto cov get`** (also run implicitly by `otto test --cov`) —
   fetches `.gcda` counters from the lab and writes a `capture.json`
   per board, anchored to `base_commit`.
2. **`otto cov clean`** — zeroes remote `.gcda` counters ahead of a
   fresh collection session.
3. **`otto cov report`** — assembles every tier's data (e2e captures,
   harvested unit counters, the committed manual store) into an HTML
   report.

![The coverage report's directory page: a sortable tree of covered source
directories and files, per-tier percentage columns with threshold-colored
bars, and the per-node stats card](../_static/generated/coverage-report.png)

*The screenshot is generated from the live report renderer at docs build
time by `scripts/capture_docs_media.py` — the same pipeline that captures
the monitor dashboard — so it can never drift from what `otto cov report`
actually produces.*

See {doc}`../architecture/subsystems/coverage/index` for how the fetch → merge →
capture → render pipeline fits together, and for the design behind tiers,
validity, and why only manual captures are committed.

## `otto cov --help`

```{raw} html
:file: ../_static/generated/termynal/help-cov.html
```

## Setting up your product

The collection workflow on this page is the same for every product; what
differs is how the product itself is built and instrumented. Each build
type has its own setup page:

```{toctree}
:maxdepth: 1

coverage-gcc
coverage-clang
coverage-embedded
```

## Prerequisites

The following system packages must be installed on the **otto host**
(the machine running `otto test` and `otto cov`):

| Package | Purpose                            | Required |
|---------|------------------------------------|----------|
| `lcov`  | Capture and merge `.info` files    | Yes      |
| `gcov`  | Process `.gcda` files into `.info` | Yes      |

On **remote hosts** (the machines running the instrumented product):

- The product must be compiled with `gcc --coverage` or
  `clang --coverage` (both spell `-fprofile-arcs -ftest-coverage`).
- `.gcda` files must be written to a known directory.

For clang-built products the otto host additionally needs `llvm-cov`
(the `llvm` package) — see {doc}`coverage-clang`.

Install on Debian/Ubuntu:

```bash
sudo apt-get install lcov
```

Install on RHEL/CentOS:

```bash
sudo yum install lcov
```

`gcov` is included with GCC.  Ensure the `gcov` version matches the GCC
version used to compile the product.

(coverage-configuration)=
## Configuration

Add a `[coverage]` section to your repo's `.otto/settings.toml`:

```toml
[coverage]
# Required: where .gcda files live on remote hosts
gcda_remote_dir = "/var/coverage/myproduct"
```

This is the only *required* configuration.  The source root is
auto-detected by walking up from the current directory to find the
`.otto/` directory.  Path mappings between build-host paths and local
source paths are auto-discovered from the `.info` and `.gcno` files.

An optional `hosts` regex scopes collection to a subset of the lab
(matched against each host id) — this is how an SSH hop that fronts a
coverage target is kept out of the coverage set without otto having to
guess which hosts emit `.gcda`:

```toml
[coverage]
gcda_remote_dir = "/var/coverage/myproduct"
hosts = "^device.*"
```

### Declarative Tiers

A *tier* is a named layer of coverage data.  Tiers are declared under
`[coverage.tiers.<name>]` in `.otto/settings.toml` — no more ad-hoc
`--tier NAME=PATH` flags for data otto can collect itself:

```toml
[coverage.tiers.system]
kind = "e2e"                 # collected by `otto test --cov` / `otto cov get`
precedence = 1                # lower number = wins winner-take-all coloring
color = "green"                # CSS color name or "#RRGGBB"; per-kind default if omitted

[coverage.tiers.unit]
kind = "unit"
precedence = 2
harvest_dirs = ["build"]     # swept for .gcda at report time; relative to the repo root
color = "yellow"

[coverage.tiers.manual]
kind = "manual"
precedence = 3
max_age = "180d"             # optional; flag-only aging
color = "orange"

[coverage.exclusions]
markers = ["MYPROJ_NO_COV"]  # optional additions to the LCOV_EXCL_* set
```

Each `[coverage.tiers.<name>]` block:

| Field | Meaning |
|-------|---------|
| `kind` | One of `e2e`, `unit`, `manual`. Selects the collection machinery — see {ref}`coverage-tier-kinds`. |
| `precedence` | Integer; lower wins the winner-take-all row coloring when multiple tiers cover the same line. |
| `color` | Optional CSS named color or `#RRGGBB` hex, validated at settings load. Defaults to a per-`kind` color when omitted (`e2e` = green, `unit` = yellow, `manual` = orange). |
| `harvest_dirs` | `unit`-kind only: build directories swept for `.gcda` at report time. Relative paths resolve against the repo root (see {doc}`setup/repo-setup`). |
| `max_age` | `manual`-kind only: `"<days>d"` (e.g. `"180d"`); enables the *aging* flag (see {ref}`coverage-validity`). Optional, off by default. |

Tier **names are free-form** and multiple tiers may share a `kind` —
for example two manual tiers, `manual_qa` and `manual_dev`, both
`kind = "manual"`, distinguished by name, precedence, and color.

**Backward compatibility:** a settings file with no `[coverage.tiers]`
section behaves exactly as before — an implicit `system` tier
(`kind = "e2e"`, precedence 1) is assumed.

### Per-Host Toolchain

Each host can specify its own toolchain (``gcov``, ``lcov``) for
coverage processing.  This is configured via the ``toolchain`` field in
``lab.json`` — see the [host guide](per-host-toolchain) for
the full syntax.

When no explicit toolchain is configured, otto resolves tools in this
order:

1. **Explicit config** — ``toolchain`` object in ``lab.json``.
2. **Auto-discovery** — otto reads the gcov *version stamp* from the
   build's ``.gcno`` headers (a ``.gcno`` embeds no compiler path, but
   every compiler stamps the format version it wrote).  A clang stamp
   resolves to ``llvm-cov`` from ``PATH``; a GCC stamp means the default
   ``gcov`` already applies — a *cross*-GCC toolchain cannot be located
   from the ``.gcno`` alone and must be configured on the host.
3. **System default** — ``/usr/bin/gcov`` and ``/usr/bin/lcov``.

When the resolved tool cannot actually read the build's counters —
classically a clang build captured with GNU ``gcov`` — the capture
stops with a typed error naming both versions and the fix, instead of
producing an empty or wrong report.

### Clang Builds

Clang-compiled products emit counters in a format GNU ``gcov`` cannot
read; otto routes them through ``llvm-cov`` instead — setup and caveats
on the {doc}`coverage-clang` page.

## Retrieving Coverage: `otto cov get`

`otto cov get` is the single retrieval command.  It fetches `.gcda`
counters from every host matched by `[coverage].hosts` — Unix hosts
over the network, embedded boards over the console — parses them with
the discovered toolchain, and writes one `capture.json` per board
(anchored to `base_commit`) plus debug artifacts (the raw `.gcda` and the
`.info` tracefiles lcov captured from them) into the command's output
directory:

```text
<output>/
  cov/
    <board_id>/
      capture.json
      *.gcda
      board.info
      board.resolved.info
```

By default `otto cov get` targets the lab's sole `e2e`-kind tier and
writes a capture that is **not** committed anywhere — it lives in the
output directory, the same as a run's other artifacts.  Selecting a
`manual`-kind tier switches the command into manual-capture mode: it
requires `--ticket`, annotates tester identity onto the capture, and
additionally copies the capture into the repo's committed store at
`.otto/coverage/manual/`:

```bash
# Default: retrieve against the sole e2e-kind tier.
otto cov get

# Manual session: anchor a capture, attach a ticket, commit it.
otto cov get --tier manual --ticket PROJ-123 --note "verified failover via GDB"
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `--output, -o PATH` | Directory to write fetched coverage and per-board captures into | the command's standard per-invocation output directory |
| `--tier NAME` | Coverage tier to annotate onto each capture | the lab's sole `e2e`-kind tier (error if ambiguous or unknown, listing the configured tiers) |
| `--ticket STR` | Ticket reference annotated onto each capture. **Required** when `--tier` resolves to a `manual`-kind tier | none |
| `--note STR` | Free-text note annotated onto each capture (`manual`-kind tiers only) | none |
| `--tester-name STR` | Tester name annotated onto each capture (`manual`-kind tiers only) | `getpass.getuser()` |
| `--tester-email STR` | Tester email annotated onto each capture (`manual`-kind tiers only) | `git config user.email`, omitted entirely (not annotated empty) when unset |
| `--clean` | Zero the fetched Unix hosts' remote `.gcda` counters after a successful retrieval — for use before starting a manual session | off |

`--ticket`, `--note`, `--tester-name`, and `--tester-email` are only
meaningful for a `manual`-kind retrieval; passing them against an
`e2e`-kind tier has no effect (an automated pull has no human tester to
attribute).

Retrieval requires a git repository — resolving `base_commit` and, for
a dirty tree, the offset remap both need it.  Outside a git repo,
`otto cov get` refuses with a clean error; `otto cov report`'s
`--tier NAME=PATH` escape hatch remains available for git-less flows
(see {ref}`coverage-tier-name-path`).  The SUT directory does not have
to be the repository root: a SUT checked out as a subdirectory of a
larger repository (a monorepo layout) anchors its captures against the
enclosing repo — its `HEAD` is the `base_commit`, and its working-tree
state decides dirtiness.

(coverage-dirty-remap)=
### Locally-modified builds

Manual testing frequently happens against a **locally modified**
build — printf-and-recompile, a GDB session poking at a running
binary.  These sessions still run instrumented code, so real counters
exist, but their line numbers describe the modified tree, not the
committed one.  `otto cov get` detects a dirty working tree
(`git status --porcelain` non-empty) automatically and remaps the
retrieved hits onto **committed-code line numbers** before writing the
capture — added/changed lines' hits are dropped (crediting untested
code would be wrong), unchanged lines remap exactly even when they've
shifted.  The capture records `dirty_remap: true`, which shows up in
the report's run table (see {ref}`coverage-runs`); no diff is
stored.

### The capture file

Each board's `capture.json` records line/branch hits in
committed-code coordinates, the commit they're anchored to, and — for
a manual capture — the human metadata:

```json
{
  "schema": 2,
  "tier": "manual",
  "base_commit": "<commit sha>",
  "dirty_remap": true,
  "captured_at": "2026-07-02T18:40:00Z",
  "tester": {"name": "chris", "email": "chriscoll93@gmail.com"},
  "ticket": "PROJ-123",
  "note": "verified failover via GDB",
  "labs": ["lab1"],
  "board": "mps2_an385",
  "files": {
    "src/foo.c": {
      "blob": "<git blob sha of src/foo.c at base_commit>",
      "lines": {"12": 3, "13": 1},
      "branches": {"12": [[0, 0, 2], [0, 1, 0]]}
    }
  }
}
```

`base_commit` is the commit whose coordinates the line numbers mean;
each file's `blob` is the git blob SHA of that file at `base_commit`
— the rebase-tolerant anchor {ref}`coverage-validity` checks against.
An `e2e`-kind capture has the same shape but omits `tester`/`ticket`/
`note`; at report time its `base_commit` acts as a strict guard — it
must equal the tree's current `HEAD` — and a dirty working tree only
triggers a line-number remap onto the current tree, never the manual
tier's validity pass (see {ref}`coverage-report-stale-builds`).

## Collecting Coverage During a Test Run: `otto test --cov`

```bash
otto test --cov TestMyDevice
```

This runs the test suite normally, fetches `.gcda` files from every
matched host, and — on a best-effort basis — produces a `capture.json`
per board, anchored to `base_commit`, against the lab's default `e2e`-kind tier
using the same capture-production machinery as `otto cov get`.  This
tail never fails an otherwise-successful test run: a non-git SUT,
misconfigured tiers, or a stamp mismatch during merge are logged and
swallowed, leaving the raw `.gcda` artifacts on disk for manual
recovery via `otto cov get`.  The files land in a `cov/` directory in
the suite's output directory, organized by board:

```text
<log_dir>/
  cov/
    <board_id_1>/
      capture.json
      *.gcda
    <board_id_2>/
      capture.json
      *.gcda
```

```{note}
Both `otto cov get` and this `otto test --cov` tail wrap one async library
function — `collect_coverage()` — paired with `run_coverage_report()` for the
HTML report. To drive collection and reporting from your own Python (CI glue or
a custom pipeline), see the *Collecting coverage from Python* section of
{doc}`../library/index`.
```

### Choosing a Destination

Use `--cov-dir` to write coverage artifacts to an explicit location —
for example, a persistent CI directory:

```bash
otto test --cov-dir /var/artifacts/myrun TestMyDevice
```

`--cov-dir` implies `--cov`, so the `--cov` flag is optional when you
supply a path.  The destination directory is created if it does not
already exist.  If it exists and is non-empty, the run aborts to avoid
mixing stale coverage into the new results; pass `--overwrite-cov-dir`
to clear it first:

```bash
otto test --cov-dir /var/artifacts/myrun --overwrite-cov-dir TestMyDevice
```

Omitting both `--cov` and `--cov-dir` disables coverage collection.

### Inline Reports

`--cov-report` renders the HTML report immediately after the run,
without a separate `otto cov report` invocation.  It goes through the
same collection model: the configured tiers (colors, precedence,
custom exclusion markers), the unit-tier harvest, and the committed
manual store all apply, exactly as they would in a standalone report.
Like the capture tail, inline report generation is best-effort — a
report-side problem is logged and never fails an otherwise-successful
test run.

### Pre-Run Cleanup

By default, `--cov` deletes stale `.gcda` files on remote hosts
**before** the test run.  This is important because `.gcda` counters are
**additive** — without cleanup, coverage data from previous runs
contaminates the current results.

To skip pre-run cleanup and accumulate coverage across runs:

```bash
otto test --cov --no-cov-clean TestMyDevice
```

## Cleaning Counters: `otto cov clean`

`otto cov clean` zeroes `.gcda` counters on the lab's coverage hosts
without fetching anything — useful ahead of a manual session when the
previous capture has already been retrieved:

```bash
otto cov clean
```

It targets the same host selection `otto cov get` fetches from, but
**Unix hosts only**.  Embedded targets expose no counter-reset hook; when
the lab has any embedded coverage hosts, the command logs a note and
exits `0` rather than failing.  A lab with *only* embedded coverage hosts
is likewise not an error — on embedded hosts it is simply a no-op.

(coverage-tier-kinds)=
## Coverage Tiers

Every tier's `kind` selects how `otto cov report` collects its data:

| Kind | Collected by | Storage |
|------|---------------|---------|
| `e2e` | `otto test --cov` / `otto cov get` | `<output_dir>/cov/<board_id>/capture.json` — not committed, same lifecycle as other run artifacts |
| `unit` | Nothing otto runs for you — build and run your instrumented unit tests as usual; `otto cov report` harvests `.gcda` from the tier's `harvest_dirs` in the **current build tree** at report time | no capture file |
| `manual` | `otto cov get --tier <name> --ticket <ref>` | `.otto/coverage/manual/<utc-timestamp>-<ticket-slug>-<board-slug>.json`, committed to the SUT repo |

**Only manual captures are committed to the repo** — every capture
(manual or e2e) is anchored to a `base_commit`.  E2E data comes from
the output directories of previous otto runs; unit data is swept fresh
from the build tree every time a report is generated — there is no run
discipline imposed on it.

### Three-tier walkthrough

**e2e** — run the suite with coverage on:

```bash
otto test --cov TestMyDevice
```

**unit** — build your unit tests with coverage instrumentation and run
them as you always have; `.gcda` files land next to the `.gcno` files
under the tier's configured `harvest_dirs` (e.g. `build/`):

```bash
cmake -DCMAKE_C_FLAGS="--coverage" -DCMAKE_CXX_FLAGS="--coverage" \
      -DCMAKE_EXE_LINKER_FLAGS="--coverage" -B build ..
cmake --build build --target my_unit_tests
./build/my_unit_tests
```

No lcov invocation and no `--tier unit=...` flag are needed — as long
as `[coverage.tiers.unit].harvest_dirs` points at `build`, `otto cov
report` finds and merges the counters itself.

**manual** — retrieve and anchor a session against the instrumented
target, attaching a ticket:

```bash
otto cov get --tier manual --ticket PROJ-123 --note "verified failover via GDB"
git add .otto/coverage/manual/
git commit -m "cov: manual verification for PROJ-123"
```

Then generate a single report covering all three:

```bash
otto cov report path/to/e2e_run_output/ --dir ./cov_report
```

`otto cov report` reads the e2e capture(s) from the given output
directory, harvests the unit tier's `harvest_dirs` from the current
build tree, and loads every committed manual capture automatically —
no path arguments needed for the unit or manual tiers.

(coverage-validity)=
### Staleness and aging

Manual captures are anchored evidence — as the repo moves on, otto must
decide whether that evidence still applies.  A tree-wide diff against the
capture's `base_commit` resolves every file's lines in one pass — renames
followed, whitespace ignored.  Only when `base_commit` itself cannot be
resolved (a squash-merged branch, a shallow clone) does otto fall back to
checking each file individually against its recorded blob SHA.  Either
path resolves each capture's lines to one of these states at report time:

| State | Meaning | Effect on coverage |
|-------|---------|---------------------|
| **valid** | Line unchanged since the capture's `base_commit` (verified by blob SHA, which survives rebases, or by diffing against `base_commit` when the blob is unreachable) | Counts normally |
| **stale** | Code changed since the capture — the evidence no longer describes this line | Coverage is **revoked**; rendered as "needs re-verification" |
| **aging** | Code is unchanged (still *valid*), but the capture is older than the tier's `max_age` | Coverage is **retained** (flag-only — `max_age` never silently drops data) and tallied/rendered separately, flagging the line for re-verification because surrounding behavior may have drifted |
| **unverifiable** | Neither the blob nor `base_commit` can be resolved | Treated as **stale**, with a loud per-capture warning naming the remedy (re-capture) |

Stale vs. aging, precisely: **stale = the code changed** out from under
the evidence; **aging = the code is unchanged but the evidence is
old**.

The anchor-chain diff is **whitespace-insensitive** (`git diff -w`), so a
pure reformat — reindentation, tabs↔spaces, trailing-whitespace strips —
does not stale a manually-covered line: the evidence carries through, and
lines merely shifted by such edits remap to their new numbers.  Only a
change to the code itself revokes coverage, and only the lines that
actually changed — the rest of the file stays valid.  (The SUTs are
C/C++, where whitespace is not semantically load-bearing; the single case
this also forgives — a whitespace change *inside a string literal* — is
treated as immaterial to coverage.)  Line-ending-only changes (a file
flipped CRLF↔LF) are immune the same way — `-w` treats them as
whitespace, not content.

Encoding changes are not exempt from that revocation: a BOM addition or
a charset transcode changes the file's bytes, and `-w` only ignores
whitespace, not arbitrary byte differences — the affected lines revoke
and must be re-proven, the same as any other edit.  Because only the
byte-differing lines are affected, a transcode that leaves most of a
file's bytes untouched (adding a BOM, re-encoding otherwise-ASCII
content) revokes only the handful of lines it actually changed, not the
whole file.

```{warning}
A conversion that trips git's own binary-file heuristic — encoding to
UTF-16, or any charset that introduces NUL bytes — is **not detected**
by the anchor chain today. `git diff` reports the file as
`Binary files ... differ` with no line hunks, so the tree diff drops it
entirely; a file present on disk but absent from that diff reads as
unchanged, so coverage on it stays valid even though every byte was
rewritten.
Re-prove coverage by hand after this kind of charset conversion — the
anchor chain will not catch it for you.
```

Renames are followed as far as `git diff -M` tracks them: a capture taken
against `foo.c` still resolves cleanly after a plain `git mv foo.c
bar.c`.  File **splits or copies** are not rename-tracked by git and so
are not followed either — restructuring code that way means re-proving
coverage against the new files.

A few more rulings that fall out of how captures are anchored and
resolved:

- A **newer manual capture with the same run label and host** entirely
  replaces the older one — the superseded capture's credits do not
  accumulate, and it drops out of the run table (see
  {ref}`coverage-runs`).
- On a **shallow clone**, a capture older than the clone's fetch depth has
  a `base_commit` git cannot resolve here; validity falls back to the
  per-file blob check instead of crashing — files whose current blob
  still matches the recorded one stay valid, and only the files (or
  lines) that no longer match degrade, with the report naming the fix
  (`git fetch --unshallow`) rather than failing silently.
- A capture whose `base_commit` has been **squash-merged away** (the
  commit was garbage-collected once its branch folded into `main`) can no
  longer be diffed against directly, so otto verifies each of that
  capture's files individually against its recorded blob SHA instead.
  That per-file fallback is batched into a small, constant number of git
  calls per capture regardless of file count, so validity checking stays
  fast even on large repos served over NFS.

(coverage-runs)=
### Runs: which run covered this line?

Every coverage input becomes a **run** at report time: each manual
or e2e capture is one run (labelled by the host's display name; hover for
tier, ticket, note, date, and base_commit), and each unit-tier harvest or legacy
`.info` load gets a synthetic per-tier run.  On a file's annotated page,
the right-hand **runs** column expands per line to list every run that hit
it, colored by tier, with per-run hit counts.  A stale line lists the
revoked run struck through — the ticket to re-verify.  The index's
Captures table is the full run table, and `store.json` carries it
(`runs` plus per-line `run`/`stale_run`) for downstream consumers.

`--ticket` and `--note` on `otto cov get` annotate captures of **every**
tier kind (`--ticket` remains required for manual-kind tiers; tester
attribution stays manual-only).

Validity only applies to the **manual** tier. E2E captures use a
strict `base_commit` **merge guard** instead — see
{ref}`coverage-report-stale-builds`.  Unit tiers carry no validity
states; they're harvested fresh every report, so there's nothing to go
stale (a `.gcda` older than its `.gcno` only produces a "may be stale"
warning, never a revoke).

## Generating Reports: `otto cov report`

```bash
otto cov report <output_dir> --dir ./my_report
```

`otto cov report` assembles a store from every source available:

1. **E2E captures** — `capture.json` files under each given output
   directory's `cov/<board_id>/`, subject to the base_commit guard below. Board
   directories with no `capture.json` fall back to the legacy
   `.gcda`-merge path (back-compat with pre-tier output directories).
2. **Unit harvest** — every `unit`-kind tier's `harvest_dirs`, swept
   fresh from the current build tree.
3. **Manual store** — every capture committed under the repo's
   `.otto/coverage/manual/`, loaded automatically with the validity
   pass applied.

`OUTPUT_DIRS` is now optional: with none given, the report is built
from the committed manual-capture store (and any configured unit
tiers) alone.

A report whose assembled store ends up **empty** — no captures, no
harvested counters, no manual store — exits `1` with a one-line error
naming every location that was searched, so a misconfigured CI job
fails loudly instead of publishing a blank report.

### Stitching Multiple Runs

To combine coverage from separate test runs into a single report:

```bash
otto cov report run1_output/ run2_output/ run3_output/ --dir ./combined_report
```

### Options

| Option                    | Description                                                          | Default             |
|---------------------------|----------------------------------------------------------------------|---------------------|
| `OUTPUT_DIRS`             | `otto test`/`otto cov get` output dirs with `cov/` subdirectories    | none — report is built from the manual store alone |
| `--dir, -d PATH`          | Where to place the generated coverage report                        | `./cov_report`      |
| `--project-name STR`      | Title shown in the report header                                     | `Coverage Report`   |
| `--tier NAME[=PATH]`      | Git-less escape hatch (see below); repeatable, order = precedence    | the configured tiers (or `system` with none configured) |

(coverage-report-stale-builds)=
### Stale Builds: "stamp mismatch" and the e2e base_commit guard

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
clean error naming both commits, rather than silently reporting
numbers for the wrong tree; the recovery is to collect fresh coverage
with `otto test --cov` (or `otto cov get`) and report on the new
output.  A working tree that is merely **dirty** at report time (same
`HEAD`, uncommitted edits) does not fail: the e2e capture's hits are
remapped from committed-code coordinates onto the current tree — the
report-time mirror of the {ref}`dirty-tree remap at retrieval
<coverage-dirty-remap>` — with a warning, and hits on
locally-modified lines are omitted rather than misattributed.

(coverage-tier-name-path)=
### The `--tier NAME=PATH` escape hatch

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
other tier requires a path.  Flag order is precedence order — the
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

## Exclusion Markers

lcov's `geninfo` honors the standard exclusion markers natively —
excluded lines never enter the parsed data, so they never enter a
denominator:

- `LCOV_EXCL_LINE` — exclude one line.
- `LCOV_EXCL_START` / `LCOV_EXCL_STOP` — exclude a block.
- `LCOV_EXCL_BR_LINE`, `LCOV_EXCL_BR_START` / `LCOV_EXCL_BR_STOP` —
  branch-only variants (line/block still counted, only its branches
  excluded).

The renderer additionally re-scans each rendered source file for
these markers so excluded lines and blocks are visually distinct
(grey, with a per-file excluded count) instead of reading as ordinary
uncovered code.  In the row-coloring precedence (see
{ref}`coverage-colors`), excluded **always wins**, even over a covered,
stale, or aging line.

Extend the recognized marker set with custom strings via
`[coverage.exclusions] markers`:

```toml
[coverage.exclusions]
markers = ["MYPROJ_NO_COV"]
```

Custom markers are **render-only today**: a line marked
`// MYPROJ_NO_COV` is scanned by the renderer alongside the built-in
`LCOV_EXCL_*` set, so it renders grey and excluded like any other
excluded line — but unlike the built-in markers (which `lcov`'s
`geninfo` strips from the parsed data before it ever reaches otto),
a custom marker is *not* passed to the `lcov` capture as an `rc`
override. The line still counts toward the coverage percentages;
only its visual presentation changes.

(coverage-report-thresholds)=
## Report Thresholds

`otto cov report` colors every coverage percentage cell it renders —
the project summary, each tier's column, the sortable file table, and
every per-file page — against gcovr-style cutoffs: a cell at or above
`high` renders **green**, at or above `medium` renders **yellow**, and
below `medium` renders **red**. Configure the cutoffs under
`[coverage.report]`:

```toml
[coverage.report]
high = 80
medium = 70
```

| Field | Meaning | Default |
|-------|---------|---------|
| `high` | Percentage at or above which a cell renders green | `80` |
| `medium` | Percentage at or above which a cell renders yellow; below it renders red | `70` |

Both values must fall within `0`-`100`, and `medium` must not exceed
`high` — an inverted or out-of-range `[coverage.report]` block is a
settings error, rejected at parse time rather than at report time.
These are the only two keys; a repo with no `[coverage.report]`
section gets the defaults shown above. This is distinct from the
per-tier {ref}`legend colors <coverage-colors>`, which color source
lines and table columns by which tier covered them, not by
percentage.

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

Because tier names are free-form, multiple tiers can share a `kind`,
and colors are configurable, the report never relies on convention to
explain itself: a **legend** mapping every tier name and state to its
color is always one click away, in the app bar's **⋮** overflow menu
present on every page.

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
  per-line run drilldowns](../_static/generated/coverage-file.png)

- **Runs & contexts page** (`#/runs`) — one row per run (see
  {ref}`coverage-runs`); multi-host runs show host pills with an
  expandable per-host lines breakdown, filterable by tier and free-text
  search over label/host/ticket/board. Per-run **branch** contribution
  isn't part of the stored data (**branch** hits are recorded per line, not
  per line-and-run — unlike line hits, which are) and renders as "not
  tracked per-run".

  ![Runs & contexts page: one row per context with per-host breakdowns and
  filters](../_static/generated/coverage-runs.png)

- **Report-wide context focus** — pin a run's context from its row on the
  runs page, or from the app bar's **⋮** overflow menu (also home to
  keyboard shortcuts and the tier/state color key); every stat, percentage,
  and file-page tint recomputes to that run's coverage alone, with
  everything else reading as uncovered/neutral. The pinned context encodes
  into `?ctx=<run-label>` on the current route — a focused view is
  bookmarkable and shareable — and persists per report in `localStorage`.
  Branch cells show "—" while a focus is active; focus mode filters line
  stats only.
- **Tickets page** (`#/tickets`) — present only when `[coverage.tickets]`
  is configured (see {ref}`coverage-tickets`); one row per ticket id, sorted
  worst-uncovered-first, with the same overall stats card scoped to every
  attributed line. Each row carries owned/covered/uncovered counts, a
  threshold-colored line percentage, one column per tier, and a pin control;
  every column is sortable. Expanding a row lists its missing lines grouped
  by file as ranges, each linking straight into the annotated source.

  ![Tickets page: one row per ticket sorted by uncovered lines, an expanded
  row's missing-line ranges, and the overall attributed-lines stats
  card](../_static/generated/coverage-tickets.png)

- **Ticket context** — pin a ticket from its row on the tickets page, or from
  the ticket **search box** in the app bar (press <kbd>/</kbd> to jump straight
  to it); unlike run focus, which dims non-participating code, pinning a
  ticket **hides** files (and directories) it never touched from the tree
  entirely, and every remaining percentage — including the per-tier rows —
  recomputes over that ticket's owned lines alone. A hidden-count row above
  the tree names what was removed, so the narrowing is never silent. The file page keeps the opposite rule: it
  **dims**, never hides, a non-owned line, because code inside a file must
  stay readable. Composes with run focus (`?ticket=<id>` alongside
  `?ctx=<label>`) — "this ticket's lines, as proven by that run" is a
  real, separate question from either filter alone.

  ![A pinned ticket at the directory page: utils.c's row is hidden (it
  owns none of the ticket's lines) and the banner names what was
  hidden](../_static/generated/coverage-ticket-context.png)

`store.json` is written alongside the report with the same data —
validity states, colors, runs, tickets, and each file's excluded lines
included — as the explicit data contract for tooling built on top of
a report without touching the pipeline. `tickets.json` (see
{ref}`coverage-tickets-json`) is a separate, public export built for
consumers otto does not control.

(coverage-tickets)=
## Per-Ticket Coverage

Otto can answer, for every ticket named in the repo's commit history, how
much of the code it wrote is covered and exactly which lines still aren't —
the tickets page (above), a per-line gutter chip on the file page, and the
`tickets.json` export ({ref}`coverage-tickets-json`) all read from the same
attribution. The feature is entirely **opt-in**: with no `[coverage.tickets]`
block, none of it runs — no git log walk, no tickets page, no gutter column,
no ticket data anywhere in the store or an export — and the coverage
numbers themselves are exactly what they'd be without this section.

### Configuration

```toml
[coverage.tickets]
pattern = "#(?P<num>[0-9]+)"
url = "https://github.com/org/repo/issues/{num}"
```

| Field | Meaning |
|-------|---------|
| `pattern` | Required. A Python regex `finditer` over each commit's subject + body. |
| `url` | Optional. A `str.format` template rendering a tracker link for a ticket id. |

**The display id is the whole match**, not a named group — a commit that
writes `Fixes #1204` shows `#1204` in the gutter and the tickets page,
matching what the commit actually wrote. **`url` formats over `pattern`'s
named groups**, plus the positional `{0}` for the whole match, so a
template can consume only part of the id: GitHub's example above links
`#1204` to `.../issues/1204` via the named group `num`, while a Jira-style
`pattern = "(?P<key>[A-Z]{2,10}-\\d+)"` would use `{key}` — identical to the
whole match there, since Jira ids carry no leading punctuation to strip.
A commit naming several ids (`Fixes #101, relates to #205`) attributes its
lines to **all** of them — see "Overlapping tickets" below.

Both fields are validated **loudly, at settings load**, never at render
time: `pattern` must compile as a regular expression, and every field name
that `url` references must exist as a named group in `pattern` (or be `0`)
— a template naming a group the pattern doesn't define is a config error
raised before any report is built, not a blank link discovered later.

Two synthetic rows keep every owned line represented on the tickets page
and in `tickets.json`, consistent with "every attributed line belongs
somewhere": **`(uncommitted)`** for working-tree lines that haven't been
committed yet, and **`(no ticket)`** for lines whose owning commit matched
`pattern` nowhere.

### The `--first-parent` ruling

Attribution walks `git log --first-parent`: a line is credited to the
**merge** that brought it to the mainline, not the topic-branch commit that
originally wrote it. On a linear history (no merge commits — most `git
rebase`-based workflows) this is identical to `git blame`. On a
merge-heavy history it **diverges from `git blame` by design**: a merge
commit typically carries the PR/ticket reference in its message, while the
topic commits behind it are frequently "wip", "fixup", or otherwise
untraceable to a ticket on their own — following first-parent history is
what actually recovers the ticket a change belongs to. Passing
`--no-first-parent` is not offered as a config knob today: the ruling is
that first-parent is the *correct* reading for ticket attribution, not one
option among several (see {doc}`../architecture/subsystems/coverage/attribution`
for the full rationale and how this divergence is pinned as a deliberate
test case rather than a bug).

(coverage-tickets-overlap)=
### Overlapping tickets

**A ticket's owned lines are not a partition of the repo.** Because a
commit naming several ticket ids attributes its lines to all of them, two
tickets can — and in practice regularly do — both claim the same line. The
tickets page states this explicitly under its table (rows overlap and do
not sum to the stats card above them, which counts each attributed line
once regardless of how many tickets claim it) rather than leaving it as a
surprise when the numbers don't add up. The same rule applies to
`tickets.json`: summing every ticket's `lines.owned` across the file can
legitimately exceed the top-level `totals.owned`.

### Why a git log walk, not `git blame`

Attribution runs a bounded git log walk (a **constant number** of
subprocesses — a discovery pass plus a patch walk — regardless of how many
files are covered) rather than spawning `git blame` per file. This
was measured and decided on **filesystem-operation count**, not wall
clock, because otto targets NFS-backed checkouts
(`otto/filesystem.py`'s `is_network_fs`) where every git subprocess
re-touches `.git` metadata over the network and per-operation latency —
not CPU — dominates; a local SSD's wall clock understates the real cost by
orders of magnitude. `git blame` re-opens the pack indexes once per file
with no batch mode, so its cost is flat per file regardless of how many
files share history; the log walk amortizes across every covered file in
that same fixed handful of processes. See
{doc}`../architecture/subsystems/coverage/attribution`
for the measured numbers and the (corrected) engineering story behind the
walk that's actually shipped.

(coverage-overrides)=
## Manual-testing overrides

Two related, opt-in capabilities live in one hand-edited file:
**asserted manual coverage** ("the work of this ticket/commit was tested by
hand, before otto could record it — count it") and **ticket reattribution**
("this commit's message named the wrong ticket; fix it everywhere"). Both
build on {ref}`coverage-tickets-json`'s attribution walk, so both require
`[coverage.tickets]` to be configured. With no override file (and no
`[coverage.overrides]` key), neither feature runs — no store change, no
report change, byte-identical to a build without this section.

The file is TOML, not JSON or a CLI verb, on purpose: it is a deliberate,
commented, PR-reviewed record, meant to be read by humans as much as by
otto. Default location is `.otto/coverage-overrides.toml`, next to
`settings.toml`, so it's versioned alongside the SUT repo whose history it
makes claims about. Point at a different path with:

```toml
# settings.toml
[coverage.overrides]
file = "somewhere-else/coverage-overrides.toml"   # relative to sut_dir
```

An explicitly configured path that doesn't exist is a load error, not a
silent no-op — only the *absent* default is silent.

### File format

Top-level table names are manual-tier names (any tier declared
`kind = "manual"` under `[coverage.tiers]`), plus the reserved
`[[reattribute]]` table:

```toml
# Legacy manual-test record for the 3.x line. See the release sign-off docs.

[[bench]]                          # section name = the manual tier it maps to
ticket = "PROJ-412"
as_of = "a1b2c3d4e5"               # required for ticket entries
reason = "Full regression on bench rig 2, 2024-11 release sign-off"

[[bench]]
commit = "deadbeef1234"            # commit entries need no as_of — the sha is the bound
reason = "Hotfix verified by hand on the lab rig before ship"

[[field-trial]]
ticket = "PROJ-101"
as_of = "0badc0ffee0"
reason = "Covered by the customer field trial, spring 2025"

[[reattribute]]
commit = "cafe4321beef"
tickets = ["PROJ-500"]             # replaces the parsed set, everywhere
reason = "Committed under PROJ-388 by mistake; this is PROJ-500's work"
```

### Validation — loud at load, never rendered around

Every rule below fails the load with a named error instead of silently
producing a partial or misleading report:

- Every top-level table name is either `reattribute` or a tier declared
  under `[coverage.tiers]` **with `kind = "manual"`**. A typo'd tier, a
  non-manual tier, or an undeclared tier is a config error — and a manual
  tier literally named `reattribute` is a config error too (the name is
  reserved).
- Each asserted entry has **exactly one** of `ticket` / `commit`, and a
  non-empty `reason`.
- `ticket` entries require `as_of`; `commit` entries must not carry one.
- Every sha (`commit`, `as_of`) must resolve in the SUT repo. Abbreviated
  shas are accepted if unambiguous.
- A ticket entry's id must appear in at least one commit at or before its
  `as_of`. Owning zero *current* lines is legal — that's full aging (below),
  not a typo; never having appeared at all is a typo and fails loud.
- `[[reattribute]]` entries carry `commit`, `tickets` (a list; **empty is
  legal** and lands the commit's lines in `(no ticket)` — "this should
  never have named a ticket" is a real mistake), and a non-empty `reason`.
- Unknown keys in any entry are errors, matching the settings model's
  posture elsewhere.
- **An override file requires `[coverage.tickets]` to be configured.**
  Both halves operate on the attribution walk — reattribution rewrites its
  ticket extraction, asserted entries resolve against its line→commit map
  — and the walk only runs when the tickets feature is on. A present
  override file without that block is a config error, not a silent no-op.

### Semantics

**Reattribution applies first**, replacing a commit's message-parsed
ticket ids with the entry's `tickets` list before anything downstream
runs — one hook, so every consumer (tickets page, file-page gutter, ticket
context, `tickets.json`, and asserted entries keyed on the corrected
ticket) sees the fix consistently.

**An asserted entry covers the lines currently attributed** — the same
`-w -M --first-parent` rules as everything else in
{ref}`coverage-tickets-json` — to its `commit`, or to its `ticket`
restricted to that ticket's commits **at or before `as_of`** in the
first-parent walk.

The `as_of` bound exists because attribution is *live*: without it, a new
commit landing under an old ticket next month would silently inherit
asserted coverage nobody earned for it — exactly the silent-drift failure
otto otherwise designs against. A commit-keyed entry needs no bound; the
sha it names already is one.

**Aging is free and by content, not authorship.** A line that's later
rewritten migrates, by ordinary attribution supersession, to the newer
commit — and drops out of the entry's line set automatically. There's no
cache, snapshot, or invalidation surface to keep in sync. Whitespace-only
edits do not re-attribute (the same `-w -M` as the rest of the walk), so
they don't shed asserted coverage either.

### The honesty model

Asserted coverage is never indistinguishable from a recorded run:

- A line covered only by an override renders with a visually distinct,
  hollow/dashed tier marker rather than the solid "proven" marker, and the
  file page's expander shows the entry's `reason` and key on request.
- The app bar shows a small badge ("*n* overrides") whenever any override
  is active; opening the `⋮` overflow menu lists every entry (tier, key,
  reason) — the at-a-glance view of what was bypassed and why.
- The `⋮` menu's **"Hide asserted coverage"** toggle recomputes every
  percentage, bar, and tier column with override-sourced hits excluded, so
  a reader can flip to proven-only numbers. It's never silent: while
  active, the stats-card scope line says so (`· asserted hidden`). The
  tickets page's own aggregate/row stats decline to a single dash (`—`)
  under the toggle rather than subtracting — there's no deduped
  "asserted-only" total to subtract from a per-ticket row honestly, so the
  page says "no data" instead of guessing.
- Once a real recorded run covers a line in a tier, the mark disappears —
  the line is now proven, and the report says so. Two overrides (same or
  different tiers) can cover the same line; each is independently listed
  in the line's provenance.

### Prune signal

When an entry no longer contributes any asserted mark, report generation
logs an info-level line naming the entry so a maintainer can clean up the
file. The two causes are distinguished in the message because the correct
maintainer action differs:

```text
override %s (tier %r) is fully aged out — no current line is attributed to
it; prune it from %s (reason: %s)

override %s (tier %r) is fully covered by recorded runs — every line is
proven; prune it from %s (reason: %s)
```

*Fully aged out* means the entry's line set is empty — every line was
rewritten, or (for a ticket entry) superseded past `as_of` — so the
assertion no longer applies to any current code. *Fully covered* means
every line the entry covers already has real recorded coverage in its
tier — the testing was since proven, so the override adds nothing further.
An entry still contributing at least one mark logs nothing.

(coverage-tickets-json)=
## The `tickets.json` Export

`otto cov report --tickets-json PATH` (mirrored on `otto test` as
`--cov-tickets-json PATH`) writes a machine-readable per-ticket coverage
summary — otto's **first public export format**. Every other JSON otto
writes (`store.json`) is an internal, renderer-shaped artifact free to
reshape on any `otto` release; `tickets.json` has consumers otto does not
control (CI dashboards, ticket-coverage bots, ad-hoc scripts), so it is
specified and versioned as a stable contract instead:

```json
{
  "format": 2,
  "generated": "2026-07-26T21:00:00Z",
  "otto_version": "0.8.0",
  "project": "myproduct",
  "traversal": "first-parent",
  "overrides_active": true,
  "thresholds": {"high": 80, "medium": 70},
  "tiers": ["unit", "system", "manual"],
  "totals": {"owned": 17284, "covered": 16240, "uncovered": 1044},
  "tickets": [
    {
      "id": "PROJ-388",
      "url": "https://jira.example.com/browse/PROJ-388",
      "commits": ["a1b2c3d4e5f6..."],
      "lines": {"owned": 97, "covered": 61, "uncovered": 36},
      "per_tier": {"unit": 61, "system": 0, "manual": 0},
      "asserted": {"unit": 0, "system": 0, "manual": 4},
      "files": [
        {
          "path": "src/net/arp.c",
          "owned": 64,
          "covered": 41,
          "missing": [[142, 158], [204, 204], [219, 221]]
        }
      ]
    }
  ]
}
```

Each ticket's `asserted` map counts, per tier, how many of that ticket's
lines are covered in that tier *only* via a {ref}`coverage-overrides`
entry (`tier in line.asserted`) — the same distinction the report UI's
dashed marker draws, exported as numbers. `asserted` counts are additive
to, not subtracted from, `per_tier` — a line counted in `asserted[tier]`
is also counted in `per_tier[tier]`, since it is covered there. The
top-level `overrides_active` flag is `true` whenever an override file is
configured (the spec's wording) — that is, whenever `.otto/coverage-overrides.toml`
(or the path named by `[coverage.overrides]`) was found and loaded for this
report — regardless of whether it declares any asserted entries at all (a
reattribute-only file is still "active" with an empty `overrides` list) or
whether a declared entry still contributes a mark (see
{ref}`coverage-overrides`'s prune signal).

### Compatibility policy

- **`format` is its own integer, versioned independently of
  `store.json`'s `STORE_FORMAT_VERSION`.** The internal store may be
  reshaped freely for the renderer's benefit; this export's schema changes
  on its own schedule, and only when the exported shape itself changes.
  **`format` bumped 1 → 2** for the manual-overrides feature: each ticket
  object gained the additive `asserted` map and the payload gained the
  additive `overrides_active` flag — but v2 is not a *pure* addition:
  each ticket's existing `commits` array also changed **content**
  (not shape). v1 (shipped in v0.8.1) populated it from only the commits
  that currently own an attributed line for that ticket; v2 populates it
  **walk-complete** — every commit the first-parent walk visited that
  named the ticket, including a commit whose lines have since been
  rewritten or superseded. This matters for the manual-overrides feature:
  an asserted ticket entry's `as_of` bound (and the "fully aged out" prune
  signal) needs to see a ticket's full commit history, not just its
  current line owners, to tell a legitimately-aged entry from a typo'd
  id. A ticket still only appears in `tickets` at all when it owns at
  least one coverable line — that inclusion rule is unchanged from v1 —
  so this is a same-shape, same-membership, different-content change to
  one existing field, disclosed here rather than folded silently into
  "additive."
- **Output is deterministic apart from the `generated` timestamp**:
  tickets sorted by `id`, each ticket's `files` sorted by `path`, and
  `missing` ranges ascending. Two reports built from identical coverage
  data at the same `generated` stamp produce byte-identical files — this
  is what a CI diff actually compares (the timestamp aside), and is itself
  pinned by test (generating twice with a fixed `generated` and asserting
  byte equality, not just field-by-field) — so a diff between two real
  regenerations is exactly the coverage delta, never noise from key
  ordering or incidental formatting.
- **Every `path` is repo-relative POSIX**, never the internal store's
  absolute, machine-specific path — two CI runners with different
  checkout locations emit identical bytes for identical coverage, and an
  external consumer can map a path onto its own checkout without knowing
  anything about the machine that produced the file.
- **`missing` ranges are inclusive `[start, end]` pairs** — `[142, 142]`
  for a single line — using the exact same grouping the tickets page
  renders (`group_ranges`, shared code, not two independent
  implementations that could drift apart).
- **`(uncommitted)` and `(no ticket)` appear as ordinary ticket entries**,
  so the export's `totals` sum the same way the tickets page's stats card
  does.
- **Loud-fails without `[coverage.tickets]` configured, or with a
  configuration that attributed nothing.** Requesting `--tickets-json`
  asks for ticket data; writing an empty file instead of erroring would
  read as "this project has no uncovered ticket work" — exit `1` with a
  clear cause instead. `otto test --cov-tickets-json` fails this same way
  *before the suite runs* when `[coverage.tickets]` isn't configured at
  all (a known misconfiguration, worth failing fast on); a git walk that
  ran but matched nothing is only knowable after the run, so that case is
  a warning on the otherwise-successful test run instead, matching every
  other `--cov-*` post-run tail's never-fail-a-green-run policy.
- **Omitted flag → no file written**, ever — there is no implicit default
  path, so nothing appears that wasn't explicitly asked for.

This is also the natural substrate for a future per-ticket
`--cov-fail-under` variant, and the shape other planned report formats
(Cobertura, Coveralls, Codecov) are expected to follow: independent
format version, deterministic ordering, loud failure on missing inputs.
Neither is built yet.

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

This is sufficient because the report never needs more than it grants: no
inline scripts, no `eval`, no WASM (syntax highlighting runs a pure-JS
regex engine, never a WASM grammar) — only classic `<script src="...">`
tags loading relative, self-hosted assets. A browser test serves a built
report under exactly this header and asserts the app boots with zero
console errors, so a stray inline script can never silently regress
Jenkins support.

## Embedded (console) coverage

Embedded RTOS targets (Zephyr) have no filesystem otto can fetch `.gcda`
files from; coverage rides the serial console instead, via an
instrumented LLEXT extension. Product setup (embedded-gcov, the
modern-GCC patch, the `.gcno` stamp guard), configuration, and the
cross-toolchain block are covered on the {doc}`coverage-embedded` page.
