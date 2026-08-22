# otto cov

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
bars, and the per-node stats card](../../../_static/generated/coverage-report.png)

*The screenshot is generated from the live report renderer at docs build
time by `scripts/capture_docs_media.py` — the same pipeline that captures
the monitor dashboard — so it can never drift from what `otto cov report`
actually produces.*

See {doc}`../../../architecture/subsystems/coverage/index` for how the fetch → merge →
capture → render pipeline fits together, and for the design behind tiers,
validity, and why only manual captures are committed.

```{raw} html
:file: ../../../_static/generated/termynal/help-cov.html
```

## Synopsis

```text
otto cov get    [OPTIONS]
otto cov clean
otto cov report [OUTPUT_DIR...] [OPTIONS]
```

| Subcommand | Description |
| ---------- | ----------- |
| `get` | Fetch `.gcda` counters from the lab and write one `capture.json` per board, anchored to `base_commit` (also run implicitly by `otto test --cov`) |
| `clean` | Zero remote `.gcda` counters ahead of a fresh session (Unix coverage hosts only) |
| `report` | Assemble every tier — e2e captures, unit harvest, committed manual store — into an HTML report |

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
- `.gcda` files must be written to a known directory.

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

An optional `hosts` regex scopes collection to a subset of the lab — this is
how an SSH hop that fronts a coverage target is kept out of the coverage set
without otto having to guess which hosts emit `.gcda`:

```toml
[coverage]
gcda_remote_dir = "/var/coverage/myproduct"
hosts = "device.*"
```

The regex is **fully matched** against each host id (`re.fullmatch`), never
searched within it: `device` selects the host whose id is exactly `device`, so
write `device.*` to match a family.  A pattern that matches none of the hosts
the run may walk fails the command with the pattern and the wildcard hint,
rather than collecting from nothing and reporting a coverage run that happened.

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
