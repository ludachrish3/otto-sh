# otto cov

Otto collects gcov coverage data from remote hosts and renders
multi-tier HTML coverage reports.  Coverage tiers — `system` (e2e),
`unit`, `manual`, or any other name — are declared in
`.otto/settings.toml`; three commands drive the workflow:

1. **`otto cov get`** (also run implicitly by `otto test --cov`) —
   fetches `.gcda` counters from each instrumented **product** on each
   coverage host and writes a `capture.json` per host per product,
   anchored to `base_commit`.
2. **`otto cov clean`** — resets every instrumented product's counters on
   each coverage host, each its own way (see {doc}`clean`), ahead of a
   fresh collection session.
3. **`otto cov report`** — assembles every tier's data (e2e captures,
   harvested unit counters, the committed manual store) into an HTML
   report.

![The coverage report's directory page: the app bar, a sortable tree of
the lib/ and product/ source directories and their files with
threshold-colored Line % and Branch % bars and one percentage column per
tier, and the per-node stats card](../../_static/generated/coverage-report.png)

See {doc}`../../architecture/subsystems/coverage/index` for how the fetch → pair →
capture → render pipeline fits together, and for the design behind tiers,
validity, and why only manual captures are committed.

```{raw} html
:file: ../../_static/generated/termynal/help-cov.html
```

## Synopsis

```text
otto cov get    [OPTIONS]
otto cov clean
otto cov report [OUTPUT_DIR...] [OPTIONS]
otto cov kmodcov export DIR
otto cov kmodcov check DIR
```

| Subcommand | Description |
| ---------- | ----------- |
| `get` | Fetch `.gcda` counters from each coverage host's instrumented products and write one `capture.json` per host per product, anchored to `base_commit` (also run implicitly by `otto test --cov`) |
| `clean` | Zero each product's remote `.gcda` counters ahead of a fresh session, each product its own way — see {doc}`clean` |
| `report` | Assemble every tier — e2e captures, unit harvest, committed manual store — into an HTML report |
| `kmodcov` | Vendor the `otto_kmodcov` kernel-module library into a project (`export`), or compare a vendored copy with this otto (`check`: exit 0 current, 1 differs, 2 absent). Lab-free. See {doc}`instrumenting/kernel-modules`. |

## Examples

```text
otto --lab my_lab cov get --tier manual --ticket PROJ-123 --note "verified failover"
otto --lab my_lab cov report runs/2026-05-16_T1200/ --dir ./report
otto --lab my_lab cov report run_a/ run_b/ run_c/ --dir ./combined
otto --lab my_lab cov report --tier unit=unit.info --tier manual=manual.info --dir ./tracefiles
```

On success, otto logs the overall coverage percentage, the file count,
and the path to `index.html`. If no coverage data is found anywhere —
supplied directories, unit harvest, or the manual store — the command
logs an error naming the searched locations and exits non-zero.

## Setting up your product

The collection workflow on this page is the same for every product; what
differs is how the product itself is built and instrumented. Each build type
has its own setup page — see {doc}`instrumenting/index`.

## Prerequisites

The following system packages must be installed on the **otto host**
(the machine running `otto test` and `otto cov`):

| Package | Purpose                            | Required |
|---------|------------------------------------|----------|
| `lcov`  | Capture `.gcda` into `.info` files | Yes      |
| `gcov`  | Process `.gcda` files into `.info` | Yes      |

On **remote hosts** (the machines running the instrumented product):

- The product must be compiled with `gcc --coverage` or
  `clang --coverage` (both spell `-fprofile-arcs -ftest-coverage`).
- `.gcda` files must be written under the product's `cov_dir`, which is
  what `GCOV_PREFIX` in its run or install command is for.
- `mktemp` and a `tar` that can create an archive (GNU or BusyBox both
  do). otto archives the product's whole `cov_dir` with a plain `tar -cf`
  into one file `mktemp` makes in the host's temp dir (`$TMPDIR`, else
  `/tmp`), fetches that one archive, keeps only the `.gcda` from it, and
  removes it from the host. The fetching user needs read access to
  everything in `cov_dir` (nothing is written there, so a root-owned
  `cov_dir` with readable counters is fine) and a writable temp dir. If any
  step exits non-zero, that product's fetch fails with the command's
  message, and its counters stay on the host rather than being
  half-collected. That covers a read-only or missing temp dir, an unreadable
  file or directory, and no `tar` at all.

For clang-built products the otto host additionally needs `llvm-cov`
(the `llvm` package) — see {doc}`instrumenting/clang`.

Install on Debian/Ubuntu:

```bash
sudo apt-get install lcov
```

Install on RHEL/CentOS:

```bash
sudo yum install lcov
```

`gcov` is included with GCC; the section below is how otto picks the one
that can read a given product's counters.

(coverage-gcov-resolution)=
## Which gcov reads the counters

The gcov that processes a product's `.gcda` files must be one that reads their
format: another GCC major's gcov, or GNU gcov on clang's `408*` files, fails
`otto cov report` with geninfo's *"Incompatible GCC/GCOV version"*. For each
`<host>/<product>` directory of a run otto picks the tool in this order:

1. **The host record's `toolchain.gcov`, when it names one.** A record
   whose gcov is not the default (`usr/bin/gcov` under sysroot `/`) is used
   as is; a cross gcov can only come from here, and otto does not
   second-guess it. A record that names only an `lcov` is silent about
   gcov and falls to step 2.
2. **The data's own stamp.** Every `.gcda` header carries a gcov format
   version, its *stamp*, and the stamp alone chooses the reader:

   | Stamp | Read by |
   |---|---|
   | `402*`, `408*`, `B11*` | `llvm-cov gcov` (`llvm-cov`, or the highest `llvm-cov-<N>`, on `PATH`) |
   | a GCC stamp of the system `gcov`'s major | that `gcov` |
   | any other GCC stamp | `gcov-<major>` from `PATH` — `apt install gcc-12` ships `gcov-12` |
   | a stamp that decodes to no GCC major (GCC 4.x's `407*` form) | the system gcov, with one warning naming the directory and the word |

   clang writes `408*` up to clang 20 and `B11*` from clang 21
   ({doc}`instrumenting/clang`). **gcc 11.1.0 writes `B11*` too**, and
   llvm-cov silently loses counts on the data of gcc 11 and later, so a
   gcc 11.1.0 build names its `gcov-11` by hand: in the host record's
   `toolchain.gcov` (step 1), or a unit tier's `coverage_tool`
   ({ref}`coverage-unit-toolchain`). gcc 11.2 and later write their own
   stamps and need nothing.

   A tool the data needs that is not installed is named before lcov runs —
   host, product, stamp and the package to install: `otto test --cov` logs
   it as a warning and writes no capture, `otto cov get` and
   `otto cov report` fail with it.
3. **lcov's own diagnostics** for a directory with no readable `.gcda`.

So the common cases need no configuration: several gccs on one build
machine, or clang. Only a cross gcov, which no stamp can name, and a gcc
11.1.0 build are configured — see {doc}`../../configuration/lab/coverage-toolchain` — and the per-compiler pages under
{doc}`instrumenting/index` say what each build needs.

A unit tier's build directories take the tier's own `toolchain` setting in
place of the host record: its `coverage_tool` when set, else the same stamp
rule as step 2, read from the notes (`.gcno`) of the directory's kept sources.
There is no step 3: a directory with no readable stamp is read by `gcov` from
`PATH`, and a stamp that decodes to no GCC major does so without a warning
({ref}`coverage-unit-toolchain`).

(coverage-configuration)=
## Configuration

Two things, in two places. **Where the counters live on a host** belongs to
the product that writes them — its `cov_dir`, defaulting to `/tmp/<name>`,
which its `install` command hands the program as `GCOV_PREFIX`.
{ref}`declared-placeholders` shows the full product entry, and
{doc}`../../setup/coverage` walks through it.

**Everything lab-wide** belongs to a `[coverage]` table in the project's
`.otto/settings.toml`; {ref}`settings-coverage` says which keys switch
collection on.  The source root is
auto-detected by walking up from the current directory to find the
`.otto/` directory.  Source paths need no mapping either: each source file a
capture names is resolved on its own, and one recorded on another machine
(a container or CI build) is matched by its trailing path components under
the project root.

An optional `hosts` regex scopes collection to a subset of the lab — use it to
keep, for example, an SSH hop that fronts a coverage target out of the
coverage set:

```toml
[coverage]
hosts = "device.*"
```

The regex is **fully matched** against each host id (`re.fullmatch`), never
searched within it: `device` selects the host whose id is exactly `device`, so
write `device.*` to match a family.  A pattern that matches none of the hosts
the run may walk fails the command with the pattern and the wildcard hint.

```{toctree}
:caption: Subcommands
:hidden:

get
report
clean
```

```{toctree}
:caption: Topics
:hidden:

tiers
tickets
exclusions
thresholds
during-tests
instrumenting/index
```
