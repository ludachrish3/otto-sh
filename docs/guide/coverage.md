# Coverage Collection

Otto can collect gcov coverage data from remote hosts after an
OttoSuite run and generate multi-tier HTML coverage reports.

Coverage works in two steps:

1. **Collect** — `otto test --cov` fetches `.gcda` files from remote
   hosts into the suite's output directory.
2. **Report** — `otto cov` merges `.gcda` files from one or more test
   runs and renders an HTML report.

## Prerequisites

The following system packages must be installed on the **otto host**
(the machine running `otto test` and `otto cov`):

| Package | Purpose                            | Required |
|---------|------------------------------------|----------|
| `lcov`  | Capture and merge `.info` files    | Yes      |
| `gcov`  | Process `.gcda` files into `.info` | Yes      |

On **remote hosts** (the machines running the instrumented product):

- The product must be compiled with `gcc --coverage` (or `-fprofile-arcs
  -ftest-coverage`).
- `.gcda` files must be written to a known directory.

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

## Configuration

Add a `[coverage]` section to your repo's `.otto/settings.toml`:

```toml
[coverage]
# Required: where .gcda files live on remote hosts
gcda_remote_dir = "/var/coverage/myproduct"
```

This is the only required configuration.  The source root is
auto-detected by walking up from the current directory to find the
`.otto/` directory.  Path mappings between build-host paths and local
source paths are auto-discovered from the `.info` and `.gcno` files.

### Per-Host Toolchain

Each host can specify its own toolchain (``gcov``, ``lcov``) for
coverage processing.  This is configured via the ``toolchain`` field in
``hosts.json`` — see the [host guide](per-host-toolchain) for
the full syntax.

When no explicit toolchain is configured, otto resolves tools in this
order:

1. **Explicit config** — ``toolchain`` object in ``hosts.json``.
2. **Auto-discovery** — otto inspects ``.gcno`` files with ``strings``
   to find the compiler path, then derives the matching ``gcov``.
   Both GCC and Clang families are detected.  For Clang, a wrapper
   script is generated automatically (``lcov`` requires a single-command
   ``--gcov-tool``).
3. **System default** — ``/usr/bin/gcov`` and ``/usr/bin/lcov``.

## Step 1: Collecting Coverage

```bash
otto test --cov TestMyDevice
```

This runs the test suite normally, then fetches `.gcda` files from all
remote hosts.  The files are placed in a `cov/` directory in the suite's
output directory, organized by host ID:

```
<log_dir>/
  cov/
    <host_id_1>/
      *.gcda
    <host_id_2>/
      *.gcda
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

### Pre-Run Cleanup

By default, `--cov` deletes stale `.gcda` files on remote hosts
**before** the test run.  This is important because `.gcda` counters are
**additive** — without cleanup, coverage data from previous runs
contaminates the current results.

To skip pre-run cleanup and accumulate coverage across runs:

```bash
otto test --cov --no-cov-clean TestMyDevice
```

## Step 2: Generating Reports

```bash
otto cov report <output_dir> --report ./my_report
```

The `otto cov report` command takes one or more `otto test` output directories
and produces an HTML coverage report.

### Stitching Multiple Runs

To combine coverage from separate test runs into a single report:

```bash
otto cov report run1_output/ run2_output/ run3_output/ --report ./combined_report
```

### Options

| Option                    | Description                                                          | Default             |
|---------------------------|----------------------------------------------------------------------|---------------------|
| `OUTPUT_DIRS`             | One or more `otto test` output dirs with `cov/`                      | Required            |
| `--report PATH`           | Where to place the HTML report                                       | `./cov_report`      |
| `--project-name STR`      | Title shown in the report header                                     | `Coverage Report`   |
| `--tier NAME[=PATH]`      | Add a coverage tier (repeatable; order = precedence, first highest)  | `--tier system`     |

### How It Works

1. Discovers `.gcda` directories from each output directory's `cov/`
   subdirectory.
2. Auto-detects the source root by finding the `.otto/` directory.
3. Resolves per-host toolchains from coverage metadata (originally
   written from ``hosts.json`` config or auto-discovered from
   ``.gcno`` files).
4. Merges `.gcda` files across hosts using `lcov --capture` and
   `lcov --add-tracefile`, using the correct `gcov` per host.
5. Auto-discovers path mappings between build-host paths and local
   source paths.
6. Loads coverage data into a store, layering in any additional tiers
   from `--tier NAME=PATH` flags in the order they were given.
7. Renders a multi-tier HTML report.

## Coverage Tiers

A *tier* is a named layer of coverage data — `system`, `unit`, `manual`,
`integration`, `smoke`, or anything else you wire up.  Tier names are
free-form: any string is a valid tier.

Tiers are added with the `--tier` flag, which is repeatable.  The
**order** of `--tier` flags is the **precedence order** — the first flag
is the highest-precedence tier and wins the row coloring on the
annotated source view when a line is hit by multiple tiers.

The implicit `system` tier (produced by merging the supplied `.gcda`
directories with `lcov`) is referenced by `--tier system` with no path.
Any other tier requires a path to a pre-existing `.info` file.

If `--tier` is not specified at all, the report defaults to a single
`system` tier.

### Worked Example

```bash
otto cov report runs/ \
    --tier unit=u.info \
    --tier system \
    --tier integration=i.info \
    --tier manual=m.info \
    --report ./cov_report
```

This produces a four-tier report with precedence
`unit > system > integration > manual`.  A line that was hit only by the
manual tier is colored manual; a line hit by all four tiers is colored
unit (the highest-precedence hit wins).  The summary table and per-file
table both grow a column per tier in the same left-to-right order.

## Output

The HTML report is written to the `--report` directory (default:
`./cov_report/index.html`).  The report shows:

- **Project summary** with aggregate (all-tier) and per-tier breakdowns
- **Sortable file table** with one column per configured tier
- **Per-file pages** with the same summary structure plus annotated
  source: per-tier hit counts, branch pills (taken/not-taken/
  unreachable), and winner-take-all row coloring driven by the
  configured tier precedence

## Cookbook: Producing `.info` Files for Tiers

The `--tier NAME=PATH` flag expects an `lcov`-format `.info` tracefile.
This section shows how to produce them for the two most common tiers.

### Unit Test Tier (gtest + lcov)

For a typical googletest unit-test binary built with
`-fprofile-arcs -ftest-coverage`:

```bash
# 1. Build the unit tests with coverage instrumentation.
cd build/
cmake -DCMAKE_C_FLAGS="--coverage" \
      -DCMAKE_CXX_FLAGS="--coverage" \
      -DCMAKE_EXE_LINKER_FLAGS="--coverage" \
      ../src
make my_unit_tests

# 2. Reset any stale .gcda counters from previous runs.
lcov --directory . --zerocounters

# 3. Run the unit tests — they write .gcda files next to the .gcno files.
./my_unit_tests

# 4. Capture into an .info file.
lcov --capture --directory . --output-file unit.info

# 5. Feed the .info file into the report as the "unit" tier.
otto cov report runs/ --tier unit=$(pwd)/unit.info --tier system
```

### Manual Test Tier (running the instrumented product directly)

To capture coverage from an interactive or ad-hoc session on a remote
host — say, manually clicking through a UI or running shell scripts
against a service — point the running binary at a writable directory
and use `GCOV_PREFIX` / `GCOV_PREFIX_STRIP` to relocate the `.gcda`
output away from the original build paths:

```bash
# On the remote host: run the instrumented binary in manual mode.
# GCOV_PREFIX_STRIP drops N leading components from each .gcda path so
# they land under /tmp/manual_cov instead of the build host's absolute
# paths (which usually don't exist on the device).
ssh device "GCOV_PREFIX=/tmp/manual_cov \
            GCOV_PREFIX_STRIP=4 \
            /opt/myproduct/bin/myproduct --interactive"

# Pull the .gcda files back to the otto host.
mkdir -p ./manual_gcda
scp -r device:/tmp/manual_cov/. ./manual_gcda/

# Capture into an .info file using the same .gcno files used for the
# system tier (the build directory).
lcov --capture \
     --directory ./manual_gcda \
     --base-directory /path/to/build \
     --output-file manual.info

# Layer the manual tier into the report.
otto cov report runs/ \
    --tier unit=$(pwd)/unit.info \
    --tier system \
    --tier manual=$(pwd)/manual.info
```

### Naming Convention

A useful convention when juggling several tiers is to keep all the
captured `.info` files alongside each test run's output directory:

```text
runs/
  2026-04-09_T1234/
    cov/                       # system tier (.gcda files)
    tiers/
      unit.info
      manual.info
      integration.info
```

That way a single `otto cov report runs/2026-04-09_T1234/` invocation
has everything it needs in a single tree.
