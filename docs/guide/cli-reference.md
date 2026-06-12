# CLI Reference

Complete reference for all `otto` command-line options.

## Global options

These options are available on every `otto` command:

| Option | Env var | Default | Description |
| ------ | ------- | ------- | ----------- |
| `--lab, -l` | `OTTO_LAB` | *(required)* | Lab name(s), comma-separated |
| `--xdir, -x` | `OTTO_XDIR` | current dir | Output directory for logs and artifacts |
| `--field / --debug` | `OTTO_FIELD_PRODUCTS` | `--debug` | Use field or debug products |
| `--log-days` | `OTTO_LOG_DAYS` | `30` | Number of days to retain logs |
| `--log-level` | `OTTO_LOG_LEVEL` | `INFO` | Logging level |
| `--rich-log-file / --no-rich-log-file` | `OTTO_LOG_RICH` | `--no-rich-log-file` | Rich formatting in log files |
| `--verbose, -v` | | `False` | Verbose console output |
| `--dry-run, -n` | | `False` | Preview without running commands |
| `--as-user USERNAME` | | current user | Check reservations as USERNAME instead of the current user |
| `--skip-reservation-check, -R` | | `False` | Bypass the reservation check entirely (emergency use only) |
| `--list-labs` | | | List available lab names and exit |
| `--list-hosts` | | | List host IDs in the loaded lab and exit |
| `--show-lab` | | | Print full lab details and exit |
| `--clear-autocomplete-cache` | | | Delete the shell-completion cache file and exit |
| `--version` | | | Show version and exit |
| `--install-completion` | | | Install shell completion and exit |
| `--show-completion` | | | Print shell completion script and exit |
| `-h, --help` | | | Show help and exit |

```{important}
**Option placement matters.**  Global options (including `--lab`/`-l`) must
appear **before** the subcommand — they are parsed by the top-level `otto`
command, not the subcommand.  For example:

- ✅ `otto --lab my_lab run deploy --debug`
- ❌ `otto run deploy --debug --lab my_lab`

The same rule applies to `--dry-run`, `--xdir`, `--log-level`, and every
other option listed above.  Subcommand-specific options (like `--firmware`
for a suite, or `--interval` for `monitor`) go **after** the subcommand.
```

## otto run

Run registered instructions.

```text
otto run <instruction> [OPTIONS]
otto run --list-instructions
```

| Option | Description |
| ------ | ----------- |
| `--list-instructions` | List all available instructions and exit |

Each instruction defines its own options via Typer annotations.  Use
`otto run <instruction> --help` to see them.

## otto test

Run registered test suites.

```text
otto test [PARENT OPTIONS] <Suite> [SUITE OPTIONS]
otto test --list-suites
```

### Parent options (before the suite name)

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--list-suites` | | List test suites with run syntax and exit |
| `--markers, -m EXPR` | `""` | Pytest marker expression (e.g. `"not integration"`) |
| `--iterations, -i N` | `0` | Repeat each test N times in one setup/teardown cycle |
| `--duration, -d SECONDS` | `0` | Repeat tests for SECONDS in one setup/teardown cycle |
| `--threshold FLOAT` | `100.0` | Minimum per-test pass rate percent in stability mode (0-100) |
| `--results PATH` | auto | JUnit XML output path |
| `--cov` | off | Collect gcov coverage from remotes after the run |
| `--cov-dir PATH` | `<output>/cov` | Override coverage destination (implies `--cov`) |
| `--overwrite-cov-dir` | off | Allow `--cov-dir` to clear an existing non-empty dir |
| `--cov-clean / --no-cov-clean` | on | Delete `.gcda` on remotes before the run |
| `--cov-report, -r` | off | Generate an HTML coverage report after the run (implies `--cov`) |
| `--cov-report-dir PATH` | `<output>/cov_report` | Override HTML report destination (implies `--cov-report`) |
| `--overwrite-cov-report-dir` | off | Allow `--cov-report-dir` to clear an existing non-empty dir |
| `--project-name NAME` | `Coverage Report` | Title shown in the HTML report header (with `--cov-report`) |
| `--monitor / --no-monitor` | off | Collect host performance metrics for the entire run |
| `--monitor-interval SECONDS` | `5.0` | Sampling interval for `--monitor` (minimum 1.0) |
| `--monitor-output PATH` | `<output>/monitor.json` | Override monitor data destination (`.json` or `.db`) |
| `--monitor-hosts REGEX` | all hosts | Regex restricting which hosts `--monitor` samples |

Each suite also defines its own options via its `Options` dataclass.
Use `otto test <Suite> --help` to see them.

## otto host

Run commands, transfer files, and log in to lab hosts.

```text
otto host <HOST_ID> run <COMMANDS...>
otto host <HOST_ID> put <SRC...> <DEST>
otto host <HOST_ID> get <SRC...> <DEST>
otto host <HOST_ID> login
otto host --list-hosts
```

### Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `run` | Execute one or more commands on the host |
| `put` | Upload local files to the host |
| `get` | Download files from the host |
| `login` | Open an interactive shell session on the host |

### Host-level options

| Option | Description |
| ------ | ----------- |
| `HOST_ID` (argument) | Host ID to operate on (see `--list-hosts`) |
| `--hop HOST_ID` | Route through an intermediate SSH hop host |
| `--term TYPE` | Override the terminal protocol for this session |
| `--transfer TYPE` | Override the file transfer protocol for this session |
| `--list-hosts` | List all available host IDs and exit |

## otto monitor

Launch the interactive performance dashboard.

```text
otto monitor [OPTIONS]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--hosts REGEX` | all hosts | Regex matched against host IDs via `re.search` |
| `--interval, -i SECS` | `5.0` | Collection interval (minimum 1.0) |
| `--file, -f PATH` | | Load historical data from `.db` or `.json` |
| `--db PATH` | | Persist live data to SQLite for later viewing |

Docker container hosts are excluded from the default monitored fleet.

## otto cov

Generate multi-tier HTML coverage reports from `otto test --cov` output
directories.  This is the **report** half of the coverage workflow — the
**collect** half is `otto test --cov` (see below).  For the full
walkthrough, prerequisites (`lcov`/`gcov`), and tier recipes, see the
[coverage guide](coverage.md).

```text
otto cov report <OUTPUT_DIR...> [OPTIONS]
```

`report` is currently the only `otto cov` subcommand.

### Arguments

| Argument | Description |
| -------- | ----------- |
| `OUTPUT_DIR...` | One or more `otto test` output directories, each containing a `cov/` subdirectory of per-host `.gcda` files. List several to stitch multiple runs into one report. Each must exist or the command exits with an error. |

### Options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--report, -r PATH` | `./cov_report` | Directory for the generated HTML report (`index.html` is written inside it) |
| `--project-name NAME` | `Coverage Report` | Title shown in the HTML report header |
| `--tier NAME[=PATH]` | `system` | Add a coverage tier; repeatable. See tier rules below. |

### Tiers

A *tier* is a named layer of coverage data. `--tier` is repeatable, and
the **order of flags is the precedence order** — the first tier is
highest precedence and wins row coloring on the annotated source view.

- `--tier system` (no path) — the implicit tier built by merging the
  supplied `OUTPUT_DIR` `.gcda` files with `lcov`. Only the `system`
  tier may omit a path.
- `--tier NAME=PATH` — any other tier; `PATH` must be a pre-existing
  lcov-format `.info` tracefile. A non-`system` tier without a path is
  rejected.
- Duplicate tier names are rejected.
- If no `--tier` flag is given, the report defaults to a single
  `system` tier.

### Examples

```text
otto cov report runs/2026-05-16_T1200/ --report ./report
otto cov report run_a/ run_b/ run_c/ --report ./combined
otto cov report runs/ --tier unit=unit.info --tier system --tier manual=manual.info
```

On success, otto logs the overall coverage percentage, the file count,
and the path to `index.html`. If no valid coverage data is found in the
supplied directories, the command logs an error and exits non-zero.

### Collecting coverage (`otto test --cov`)

`otto cov` only generates reports — it consumes `cov/` directories
produced by a prior `otto test` run. Collect coverage with the `--cov`
family of options on `otto test`:

| Option | Description |
| ------ | ----------- |
| `--cov` | Fetch `.gcda` files from remote hosts after the run into `<output>/cov/` |
| `--cov-dir PATH` | Write coverage artifacts to an explicit directory (implies `--cov`) |
| `--overwrite-cov-dir` | Allow `--cov-dir` to clear an existing non-empty directory |
| `--cov-clean / --no-cov-clean` | Delete stale `.gcda` on remotes before the run (on by default; `.gcda` counters are additive) |
| `--cov-report, -r` | Also render the HTML report inline after the run (implies `--cov`) |
| `--cov-report-dir PATH` | Explicit destination for the inline HTML report (implies `--cov-report`) |

See the [`otto test`](#otto-test) section above for the full option
table, and the [coverage guide](coverage.md) for end-to-end examples.

## otto docker

Build images and orchestrate compose stacks on docker-capable lab hosts.

```text
otto docker build [--repo NAME] [--on HOST] [--rebuild] [<IMAGE>...]
otto docker up    [--repo NAME] [--on HOST] [--no-build]
otto docker down  [--repo NAME] [--on HOST]
otto docker ps    [--on HOST]
```

### Docker subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `build` | Build container images |
| `up` | Bring compose stacks up |
| `down` | Tear compose stacks down |
| `ps` | List running containers on docker-capable hosts |

### Docker options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `--repo NAME` | `build`, `up`, `down` | Restrict to a single repo by name |
| `--on HOST` | all | Lab host id to operate on (default: all docker-capable hosts) |
| `--rebuild` | `build` | Force rebuild even if a context-hash tag exists |
| `--no-build` | `up` | Skip the implicit build step before compose up |
| `<IMAGE>...` (argument) | `build` | Image names to build (default: all) |

## otto reservation

Inspect and verify lab reservations.

```text
otto reservation whoami
otto reservation check
```

### Reservation subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `whoami` | Show the resolved reservation identity and backend |
| `check` | Verify the current reservation for the loaded lab |
