(coverage-tier-kinds)=
# Coverage tiers

![The report's directory tree with one coverage column per tier — System,
Unit, Manual, Bench — beside each directory's and file's combined line and
branch percentages](../../_static/generated/coverage-tiers.png)

Every tier's `kind` selects how `otto cov report` collects its data:

| Kind | Collected by | Storage |
|------|---------------|---------|
| `e2e` | `otto test --cov` / `otto cov get` | `<output_dir>/cov/<host_id>/<product>/capture.json` — not committed, same lifecycle as other run artifacts ({ref}`the run tree <run-tree>`) |
| `unit` | Nothing otto runs for you — build and run your instrumented unit tests as usual; `otto cov report` harvests `.gcda` from the tier's `harvest_dirs` in the **current build tree** at report time and counts only your products' sources ({ref}`what the unit report counts <coverage-unit-filter>`) | no capture file |
| `manual` | `otto cov get --tier <name> --ticket <ref>` | `.otto/coverage/manual/<utc-timestamp>-<ticket-slug>-<board-slug>-<product-slug>.json`, committed to the project's git repository |

**Only manual captures are committed to the project's git repository** — every capture
(manual or e2e) is anchored to a `base_commit`.  E2E data comes from
the output directories of previous otto runs; unit data is swept fresh
from the build tree every time a report is generated — there is no run
discipline imposed on it.

## Declarative Tiers

A *tier* is a named layer of coverage data.  Tiers are declared under
`[coverage.tiers.<name>]` in `.otto/settings.toml`:

```toml
[coverage.tiers.system]
kind = "e2e"                 # collected by `otto test --cov` / `otto cov get`
precedence = 1                # lower number = wins winner-take-all coloring
color = "green"                # CSS color name or "#RRGGBB"; per-kind default if omitted

[coverage.tiers.unit]
kind = "unit"
precedence = 2
harvest_dirs = ["build"]     # swept for .gcda at report time; relative to the project root
color = "yellow"

[coverage.tiers.manual]
kind = "manual"
precedence = 3
max_age = "180d"             # optional; flag-only aging
color = "orange"

[[coverage.exclusions.rules]]  # optional
kind = "marker"
name = "MYPROJ_NO_COV"         # adds _LINE / _START / _STOP to the LCOV_EXCL_* set
```

Each `[coverage.tiers.<name>]` block:

| Field | Meaning |
|-------|---------|
| `kind` | One of `e2e`, `unit`, `manual`. Selects the collection machinery — see {ref}`coverage-tier-kinds`. |
| `precedence` | Integer; lower wins the winner-take-all row coloring when multiple tiers cover the same line. |
| `color` | Optional CSS named color or `#RRGGBB` hex, validated at settings load. Defaults to a per-`kind` color when omitted (`e2e` = green, `unit` = yellow, `manual` = orange). |
| `harvest_dirs` | `unit`-kind only: build directories swept for `.gcda` at report time. Relative paths resolve against the project root (see {doc}`../../configuration/settings`). The sweep counts the sources of every product of the project ({ref}`coverage-unit-filter`). |
| `products` | `unit`-kind only: a `[coverage.tiers.<name>.products]` sub-table of `<product> = [<dirs>]`. Each named view is swept separately, counts only the sources of the product it is named after, and becomes its own run, tagged with that product; `harvest_dirs` stays the unnamed view. Name each build directory in one place only, here or in `harvest_dirs` ({ref}`one place per directory <coverage-unit-one-place>`). Keys follow {ref}`the product-name rule <run-tree>`. |
| `toolchain` | `unit`-kind only: the `[coverage.tiers.<name>.toolchain]` sub-table naming the tools the tier's captures run (`coverage_tool`, `lcov`, `lcov_args`); unset, the build's own stamps and `PATH` choose them ({ref}`coverage-unit-toolchain`). |
| `max_age` | `manual`-kind only: `"<days>d"` (e.g. `"180d"`); enables the *aging* flag (see {ref}`coverage-validity`). Optional, off by default. |

Tier **names are free-form** and multiple tiers may share a `kind` —
for example two manual tiers, `manual_qa` and `manual_dev`, both
`kind = "manual"`, distinguished by name, precedence, and color.

A settings file with no `[coverage.tiers]` section gets an implicit
`system` tier (`kind = "e2e"`, precedence 1).

## Three-tier walkthrough

Collect the three tiers in this order, which is the order `otto cov report`
accepts:

1. Build your products and unit tests with coverage instrumentation (the
   commands are in the **unit** block below), then commit, since each capture
   records the commit it was taken at.
2. Install the build on the target, then zero its counters with
   `otto cov clean`.
3. Exercise the target by hand, then retrieve that session with
   `otto cov get --tier manual --ticket …`.
4. Commit the manual store.
5. Run your tests with `otto test --cov`.
6. Run the unit tests, then `otto cov report`
   ([Report all three tiers](#report-all-three-tiers) has the command). The
   unit run can come any time after the build: its counters stay in the build
   tree, which `otto cov clean` never touches.

The manual tier comes first because committing its store moves `HEAD`, and a
report refuses an e2e capture taken at any commit but `HEAD`
({ref}`the report's commit rule <coverage-capture-commit>`). So that commit
comes before the e2e run, never between the e2e run and its report.

**manual** — install the build on the target, zero its counters
({doc}`clean`), and exercise it by hand. Then retrieve and anchor that session
against the instrumented target, attaching a ticket, and commit it:

```bash
otto --lab my_lab cov clean
# ... exercise the target by hand ...
otto --lab my_lab cov get --tier manual --ticket PROJ-123 --note "verified failover via GDB"
git add .otto/coverage/manual/
git commit -m "cov: manual verification for PROJ-123"
```

**e2e** — run your tests with coverage on:

```bash
otto --lab my_lab test --cov TestMyDevice
```

**unit** — the unit tier counts only the sources of your project's
products, so it needs those products declared ({doc}`../../setup/defining-products-and-tools`)
in the lab you report with, and built with coverage instrumentation **on this
machine, under the project root** — otto reads which files each one is built
from out of its notes (`.gcno`). Build your unit tests with coverage
instrumentation too and run them as you always have; `.gcda` files land next
to the `.gcno` files under the tier's configured `harvest_dirs` (e.g.
`build/`). The build is step 1 of the order above; running the unit tests is
step 6, or any time after the build:

```bash
cmake -DCMAKE_C_FLAGS="--coverage" -DCMAKE_CXX_FLAGS="--coverage" \
      -DCMAKE_EXE_LINKER_FLAGS="--coverage" -B build ..
cmake --build build          # the products and the unit tests
./build/my_unit_tests
```

The unit tests' `.gcda` files add up across runs, like any counters
([clean the counters](instrumenting/gcc.md#clean-the-counters-after-a-rebuild)),
and `otto cov clean` zeroes only the lab's hosts. To report one unit run
alone, delete the old counters before it:

```bash
find build -name '*.gcda' -delete
```

No lcov invocation and no `--tier unit=...` flag are needed — as long
as `[coverage.tiers.unit].harvest_dirs` points at `build`, `otto cov
report` finds and merges the counters itself, keeping only product sources
({ref}`coverage-unit-filter`). Without `--lab`, or with a lab that declares
none of the project's products, the report is refused.

### Distinct unit views

`[coverage.tiers.unit.products]` declares named views, each its own run in
the report:

```toml
[coverage.tiers.unit]
kind = "unit"
precedence = 2

[coverage.tiers.unit.products]
app = ["build/app"]
agent = ["build/agent"]
```

Each view is named after a `[[products]]` entry of the project and counts
only that product's sources; a name that is no product is refused
({ref}`coverage-unit-filter`). It is that naming that lets one
product pin show the product's end-to-end and unit evidence together.

(coverage-unit-one-place)=
Name each build directory **in one place only**: in `harvest_dirs` or in one
view. Every view is swept on its own and nothing removes the overlap, so a
directory reached by `harvest_dirs` and a view is counted twice. A sweep
includes the directories under it: with `harvest_dirs = ["build"]` beside `app =
["build/app"]`, every hit on `app`'s sources in `build/app` is counted
twice in the unit tier. The example above declares no `harvest_dirs` for
that reason.

Each view needs its **own build or object directory**, and that is a
requirement, not a tidiness preference: `.gcno` notes files sit beside the
objects they describe, so two views compiled from one tree with different
defines overwrite each other's notes and cannot be told apart afterwards.
`GCOV_PREFIX` does not help here — it relocates `.gcda` counters at run time
and has nothing to say about where the notes were written. The same holds for
the notes otto reads a product's sources from: a view's unit build and the
product it is named after may share a build tree, but must not overwrite each
other's notes.

(coverage-unit-filter)=
### What the unit report counts

A unit build compiles your products' sources and your test code side by side
— test bodies, mocks, gtest. The unit report counts only **product
sources**: the files some product of the project is built from. Test code
never reaches the report, and `lcov`'s `geninfo` never processes it.

**Which products.** The plain `harvest_dirs` view counts the sources of every
product of the project in the active lab (`--lab`), except those declared
`instrumented = false` ({doc}`../../configuration/declared-products-tools`). A
`products` view counts the sources of the product it is named after. A
product's sources are every file its objects' notes (`.gcno`) name, headers
that hold code (inline functions, templates) included. otto lists a product's objects the way collection does ([shared
libraries included](instrumenting/gcc.md#shared-libraries-and-plugins)), and
reads their notes on this machine, under the project root: at the object's own
path, or else the one notes file there whose path ends the same way. So build
the products here, before `otto cov report`; a product built only elsewhere
(in a container, on a CI machine) leaves no notes to read. An object whose
notes are not found contributes nothing; one INFO line per product counts
those objects and names the first few.

A `docker_image` product named by reference has no local artifact: its object
list is read by scanning its running container. The scan skips the virtual
and runtime top-level directories — `/proc`, `/sys`, `/dev`, `/tmp`, `/run`
and `/var/run` — and hidden top-level directories (`/.name`), so a product
binary installed only there is reported as not instrumented. Install it
elsewhere in the image.

**Two passes.** otto reads the notes under each harvest directory and hands
`lcov` one `--include` pattern per product source it found there, so no other
file is processed — gtest's `TEST()` bodies, which `lcov` 2 can reject, are
never read. A pattern matches the end of a path (`*/src/add.c`), so it can
also let `tests/src/add.c` through; the second pass keeps exactly the captured
files that are product sources.

**Limits.** The filter decides per file, not per function:

- A file holding both product and test functions is kept whole.
- A file no product is built from yet — one only a future product will
  link — is dropped.

**What it logs.** One INFO line per harvest directory, for example:

```text
unit unit: kept 41 product source files, dropped 17 not in any product
```

The second `unit` is the view: the tier's name, or `<tier>/<product>` for a
`products` view. `dropped` counts every other file the unit build's notes
name, system and gtest headers included. Under `--log-level DEBUG` one line
per file says which:

```text
unit unit: keep src/core/add.c (product core)
unit unit: drop tests/test_add.cpp (not in any product)
```

When nothing is kept, the INFO line `unit <view>: no product source files
under <dir>; nothing to capture` replaces the capture: that directory adds no
run. A directory with no `.gcda` at all is a warning that ends `run the unit
tests first; skipping`.

**Refusals.** Every unit view is checked before anything is captured, and a
refusal stops the whole report — there is no unfiltered fallback:

| The error says | What happened | What to do | Exit |
|---|---|---|---|
| *no products of project P found in the active lab (L)*, *no product named 'app' …*, or *every product … is declared instrumented = false* | The view has no product to count the sources of | Run with the lab that declares the products (`--lab`), and build them with `--coverage` | `1` |
| *product 'app' (build/app) is not instrumented: … Build it with coverage instrumentation (--coverage) …* | The product's `artifact` ({doc}`../../configuration/declared-products-tools`) is missing, or holds no coverage paths | Build it with `--coverage`, or declare it `instrumented = false` | `1` |
| *… reads its object list from container 'c', which is not running — install the product first (otto run install)* | A `docker_image` product named by reference has no local artifact; its objects are read from its running container | Start it again (`docker start`, {ref}`coverage-container-stop-to-flush`), or `otto run uninstall` then `otto run install` | `2` |
| *could not read product 'app''s object list: …* | The artifact or the container scan could not be read; a scan where `find` or `grep` failed under any directory, or that outran its 540-second limit (set only when the image has `timeout` and `date`; without them the scan runs to the host-side timeout), is refused whole | Fix what the reason names; the image needs `find` and `grep` (GNU or BusyBox) | `1` |

Under `otto test --cov --cov-report` a refusal costs that run its inline
report, never the test result: a warning names the cause, the e2e captures
stay in the run tree, and `otto cov report` on that tree makes the report once
the cause is fixed.

**Compilers.** otto reads the notes written by gcc 8 and later and by clang's
default `--coverage` format ({doc}`instrumenting/clang`). Notes in any other
format contribute no sources, so their files are dropped — the INFO line's
count shows it.

(coverage-unit-toolchain)=
#### The unit toolchain

The unit capture runs on the machine otto runs on, over build
trees no lab host owns, so its tools are a setting of the tier itself, the
optional `toolchain` sub-table — one setting for every directory of the tier,
the plain `harvest_dirs` view and every `products` view alike:

<!-- pinned by tests/unit/docs/test_unit_tier_toolchain_example.py -->
```toml
[coverage.tiers.unit.toolchain]
coverage_tool = "gcov-12"     # reads the build's .gcda counters: gcov, gcov-12, llvm-cov, ...
lcov = "/opt/lcov2/bin/lcov"
lcov_args = ["--rc", "geninfo_unexecuted_blocks=1"]
```

| Field | Meaning |
|-------|---------|
| `coverage_tool` | The executable that reads the build's counters: a gcc `gcov` of any version or cross prefix, an `llvm-cov` (name the binary — `llvm-cov` or `llvm-cov-<N>` — and otto adds its `gcov` subcommand), or a wrapper script of your own. |
| `lcov` | The `lcov` this tier's captures run. |
| `lcov_args` | Extra arguments appended to this tier's capture command, one argument per entry. A clang build's settings for real branch counts and for C++ end-line errors are in {ref}`coverage-clang-branch-counts`. |

Every field is optional, and an empty table is the same as none. A value
without a `/` is a command looked up on `PATH`; one with a `/` is a path, `~`
expanded and a relative one anchored at the project root, as `harvest_dirs`
is — so `./gcov` is the project's own file and `gcov` is the one on `PATH`.
That is not a host's `toolchain` ({ref}`per-host-toolchain`), whose `gcov` and
`lcov` are paths under its `sysroot`: a copied `lcov = "usr/bin/lcov"` would
here name a file under the project root. `coverage_tool` and `lcov` must each
be one plain word — no white space and no shell metacharacter — because lcov
runs the reader through a shell of its own; name the `llvm-cov` binary alone,
never `"llvm-cov gcov"`. Each `lcov_args` entry is one argument with no white
space, and otto quotes it, so a glob such as `["--exclude", "*/tests/*"]`
reaches lcov as written. Settings refuse a bad value when they load, naming
`coverage.tiers.<tier>.toolchain.<field>`. Whether a tool exists is checked
only when a report runs, so a missing one never breaks `otto run`.

Each directory's tools resolve before the report touches its output
directory, field by field:

1. **`coverage_tool`**: the configured one; else the build's own gcov stamp
   chooses, by the same rule as a product's counters
   ({ref}`coverage-gcov-resolution`) with the tier's `toolchain` in place of
   the host record, each tool from `PATH`. Setting only `lcov_args` keeps that
   choice.
2. **`lcov`**: the configured one; else `lcov` from `PATH`.
3. **`lcov_args`**: the configured ones; else none.

The stamps are read from the notes of every kept source, so a directory
whose notes need two readers (llvm-cov's stamps and a gcc's, or two gcc
majors) is refused whatever the setting — no single reader decodes both;
build them into separate directories. A configured `coverage_tool` whose name
proves it cannot read one of the stamps — an `llvm-cov` over a gcc stamp, or a
`gcov`, `gcov-<N>`, `<triplet>-gcov` or `<triplet>-gcov-<N>` over `402*` or
`408*` notes — is refused too, because lcov reports an `llvm-cov` run over gcc
data as a success with nothing in it. A GNU gcov over `B11*` notes passes
({ref}`coverage-gcov-resolution`). Any other name passes: a
wrapper script, and any name containing `llvm`, such as the usual clang
wrapper `llvm-gcov`; and a capture that still records nothing for a directory
that keeps product sources fails rather than adding an empty run. Within its family a
configured reader is otherwise never second-guessed: a `gcov-12` over gcc 13
data fails its capture.

A lab's declared `local` host plays no part in unit coverage.

**Toolchain refusals.** Like the refusals above, these come before the report
touches its output directory, except the last two, which come from a capture;
under `otto test --cov --cov-report` each is the same warning. Directories and
paths are printed absolute; `<project>` below is the project root:

| The error says | What happened | What to do | Exit |
|---|---|---|---|
| *coverage.tiers.unit.toolchain.coverage_tool = 'gcov-12': no `gcov-12` command is on PATH* (or *… is not an executable file*) | A configured tool cannot be found — checked for every unit tier, even one whose directories are all skipped | Install it, or correct the setting | `2` |
| *coverage.tiers.unit.toolchain.coverage_tool = 'gcov-12' resolves to /opt/my tools/gcov-12, which lcov cannot run: …* | The configured tool's resolved path holds white space or a shell metacharacter | Install it at a plain path, or name one | `2` |
| *coverage.tiers.unit.toolchain.coverage_tool = 'llvm-cov' cannot read \<project\>/build/tests: an llvm-cov reads only gcov stamps 402\*/408\*/B11\*, …* | The configured reader's name contradicts the directory's stamps | Name a reader for that stamp, or leave the setting unset | `2` |
| *unit tier unit: \<project\>/build/tests holds notes for more than one reader — …* | One directory's notes need two readers | Build each compiler's objects into its own directory | `2` |
| *unit tier unit: capturing \<project\>/build/tests needs `lcov`, but no `lcov` is on PATH* | A default tool the data needs is missing; for another gcc major's stamp, or one of llvm-cov's, the message names `gcov-<major>` or `llvm-cov` and its package | Install it, or set the key the message names | `1` |
| *unit tier unit: the tool found on PATH at /opt/my tools/lcov cannot be run by lcov: …* | The default tool found on `PATH` sits at a path with white space or a shell metacharacter | Install it at a plain path, or set the key the message names | `1` |
| *unit tier unit: the capture of \<project\>/build/tests with … recorded no source file* | The reader read nothing — one of another compiler's data that its name did not reveal | Check `coverage_tool` | `1` |
| *unit tier unit: capturing \<project\>/build/tests failed; if the counter reader is the wrong one, set …* | lcov itself failed; its own first error and last lines follow | Fix what lcov says, or name the right reader | `1` |

### Report all three tiers

With every tier collected, report them together. `--overwrite-dir` is
there because the {doc}`coverage walkthrough <../../setup/coverage>` already
left a report at `./cov_report`:

```bash
otto --lab my_lab cov report path/to/e2e_run_output/ --dir ./cov_report --overwrite-dir
```

`otto cov report` reads the e2e capture(s) from the given output
directory, harvests the unit tier's `harvest_dirs` from the current
build tree, and loads every committed manual capture automatically —
no path arguments needed for the unit or manual tiers.

(coverage-validity)=
## Staleness and aging

Manual captures are anchored evidence — as the project moves on, otto must
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

The anchor-chain diff is **whitespace-insensitive** (`git diff -w`), so a pure
reformat — reindentation, tabs↔spaces, trailing-whitespace strips — does not
stale a manually-covered line: the evidence carries through, and lines merely
shifted by such edits remap to their new numbers.  Only a change to the code
itself revokes coverage, and only the lines that actually changed — the rest
of the file stays valid.  This includes a whitespace change *inside a string
literal*, which does not stale the line even though it can change the
program's output.  Line-ending-only changes (a file flipped CRLF↔LF) are
immune too — `-w` treats them as whitespace, not content.

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

A few more rules:

- A **newer manual capture with the same run label, host, and product**
  entirely replaces the older one — the superseded capture's credits do not
  accumulate, and it drops out of the run table (see
  {ref}`coverage-runs`). Two products on one host are two identities, so
  neither supersedes the other.
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

(coverage-runs)=
## Runs: which run covered this line?

Every coverage input becomes a **run** at report time: each manual
or e2e capture is one run — one host, one product (labelled by the host's
display name, shown as `host · product` where runs are listed; hover for
tier, ticket, note, date, and base_commit) — each named unit-tier product
view is its own run, and the unnamed `harvest_dirs` sweep or a `--tier`
`.info` load gets a synthetic per-tier run that names no product.  On a file's annotated page,
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
