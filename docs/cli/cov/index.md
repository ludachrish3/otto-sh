# otto cov

Otto collects gcov coverage data from remote hosts and renders
multi-tier HTML coverage reports.  Coverage tiers — `system` (e2e),
`unit`, `manual`, or any other name — are declared in
`.otto/settings.toml`; three commands drive the workflow:

1. **`otto cov get`** (also run implicitly by `otto test --cov`) —
   fetches `.gcda` counters from each instrumented **product** on each
   coverage host and writes a `capture.json` per host per product,
   anchored to `base_commit`.
2. **`otto cov clean`** — zeroes each product's remote `.gcda` counters
   ahead of a fresh collection session.
3. **`otto cov report`** — assembles every tier's data (e2e captures,
   harvested unit counters, the committed manual store) into an HTML
   report.

![The coverage report's directory page: the app bar, a sortable tree of
the lib/ and product/ source directories and their files with
threshold-colored Line % and Branch % bars and one percentage column per
tier, and the per-node stats card](../../_static/generated/coverage-report.png)

See {doc}`../../architecture/subsystems/coverage/index` for how the fetch → merge →
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
otto cov kgcov export DIR
otto cov kgcov check DIR
```

| Subcommand | Description |
| ---------- | ----------- |
| `get` | Fetch `.gcda` counters from each coverage host's instrumented products and write one `capture.json` per host per product, anchored to `base_commit` (also run implicitly by `otto test --cov`) |
| `clean` | Zero each product's remote `.gcda` counters ahead of a fresh session (Unix coverage hosts only) |
| `report` | Assemble every tier — e2e captures, unit harvest, committed manual store — into an HTML report |
| `kgcov` | Vendor the `otto_kgcov` kernel-module library into a repo (`export`), or compare a vendored copy with this otto (`check`: exit 0 current, 1 differs, 2 absent). Lab-free. See {doc}`instrumenting/kernel-modules`. |

## Examples

```text
otto cov get --tier manual --ticket PROJ-123 --note "verified failover"
otto cov report runs/2026-05-16_T1200/ --dir ./report
otto cov report run_a/ run_b/ run_c/ --dir ./combined
otto cov report runs/ --tier unit=unit.info --tier system --tier manual=manual.info
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
| `lcov`  | Capture and merge `.info` files    | Yes      |
| `gcov`  | Process `.gcda` files into `.info` | Yes      |

On **remote hosts** (the machines running the instrumented product):

- The product must be compiled with `gcc --coverage` or
  `clang --coverage` (both spell `-fprofile-arcs -ftest-coverage`).
- `.gcda` files must be written under the product's `cov_dir`, which is
  what `GCOV_PREFIX` in its run or install command is for.

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

The gcov that processes a product's `.gcda` files must be the one its
compiler ships with: another GCC major, or GNU gcov on clang's files, fails
`otto cov report` with geninfo's *"Incompatible GCC/GCOV version"*. For each
`<host>/<product>` directory of a run otto picks the tool in this order:

1. **The host record's `toolchain.gcov`, when it names one.** A record
   whose gcov is not the default (`usr/bin/gcov` under sysroot `/`) is used
   as is; a cross gcov can only come from here, and otto does not
   second-guess it. A record that names only an `lcov` is silent about
   gcov and falls to step 2.
2. **The data's own stamp.** Every `.gcda` header carries the gcov format
   version its compiler wrote. A clang stamp is read by `llvm-cov gcov`
   (`llvm-cov`, or the highest `llvm-cov-<N>`, on `PATH`); a GCC stamp
   from the same major as the system `gcov` uses that gcov; a GCC stamp
   from another major is read by `gcov-<major>` from `PATH` —
   `apt install gcc-12` ships `gcov-12`. A stamp that decodes to no GCC
   major (GCC 4.x's `407*` form) falls back to the system gcov, with one
   warning naming the directory and the word. A tool the data needs that is
   not installed is named before lcov runs — host, product, stamp and the
   package to install: `otto test --cov` logs it as a warning and writes no
   capture, `otto cov get` and `otto cov report` fail with it.
3. **lcov's own diagnostics** for a directory with no readable `.gcda`.

So the common cases need no configuration: several gccs on one build
machine, or clang. Only a cross gcov, which no stamp can name, is
configured — the "Coverage toolchain" table in
{doc}`../../configuration/lab-config` — and the per-compiler pages under
{doc}`instrumenting/index` say what each build needs.

(coverage-configuration)=
## Configuration

Two things, in two places. **Where the counters live on a host** belongs to
the product that writes them — its `cov_dir`, defaulting to `/tmp/<name>`
(see {doc}`../../configuration/declared-products-tools`, and
{doc}`../../getting-started/coverage` for the walkthrough):

```toml
[[products]]
name = "myproduct"
kind = "shell"
artifact = "build/myproduct"
dest_dir = "/opt/myproduct"          # where staging puts the artifact
cov_dir = "/var/coverage/myproduct"
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 /opt/myproduct/myproduct &"
```

**Everything lab-wide** belongs to a `[coverage]` table in the repo's
`.otto/settings.toml`.  The table can be empty, but it has to exist: its
presence is what says this repo collects coverage at all.  The source root is
auto-detected by walking up from the current directory to find the
`.otto/` directory.  Path mappings between build-host paths and local
source paths are auto-discovered from the `.info` and `.gcno` files.

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
