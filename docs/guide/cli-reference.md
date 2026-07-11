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
| `--show-time` | | `False` | Show per-line timestamps on the live console (log files are always timestamped) |
| `--dry-run, -n` | | `False` | Preview without running commands |
| `--as-user USERNAME` | | current user | Check reservations as USERNAME instead of the current user |
| `--skip-reservation-check, -R` | | `False` | Bypass the reservation check entirely (emergency use only) |
| `--list-labs` | | | List available lab names and exit |
| `--list-hosts` | | | List host IDs in the loaded lab and exit |
| `--show-lab` | | | Print full lab details and exit |
| `--lab-depth` | | `3` | Nesting depth for `--show-lab` output — how deep the lab's host details are expanded (0 = unlimited) |
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

## Shell completion

After `otto --install-completion`, tab completion covers the dynamic,
otto-specific values a static shell script couldn't know: suite and
instruction names, host ids and their per-class verbs, transfer/term
backends, reservation usernames, and — comma-separated lists included —
`--lab` names and `--tests` names.  It is served from a per-repo cache so the
process answering the keystroke never runs your init modules or test code.
`--tests` completes by base name and layers a static source scan (the instant
floor) with a pytest-collected set that also includes dynamically-generated
tests; that set warms itself from any real `otto test --list-tests` run, or
from a one-time bounded collection on the first `--tests` TAB (see
{doc}`test`).  `--clear-autocomplete-cache` drops the cache if it ever goes
stale.

## Output directories

Most commands create a per-invocation output directory under `--xdir`
before the command body runs; the run's log files and artifacts are
written there, and the path is printed at the end of the run
(`Output directory: ...`):

```text
<xdir>/<command>/<timestamp>_<subcommand>/
```

- `<command>` is the top-level subcommand (`run`, `test`, `host`, ...)
  and `<subcommand>` is the leaf — the instruction name, suite name, or
  host verb.  Commands with no distinct leaf (`monitor`) omit the
  suffix: `monitor/<timestamp>/`.
- `<timestamp>` is UTC with millisecond precision
  (`YYYYMMDD_HHMMSS_mmm`), so directories sort chronologically.
- Hyphens in command names become underscores (`write-file` →
  `write_file`).

Read-only commands create no directory: `otto cov`, `otto reservation`,
`otto schema`, and `otto init` opt out entirely, as do read-only host verbs
such as `ls`, `exists`, `read-file`, `is-installed`, and `is-uninstalled`.
Third-party commands control this with the `output_dir=` flag at
registration — see {doc}`extending-cli`.

## otto init

Scaffold a new otto repo, or validate an existing one's setup. See
{doc}`../getting-started` for the full walkthrough.

```text
otto init [--all | --lab | --tests | --instructions] [--name NAME]
          [--version X.Y.Z] [--path DIR]
```

`otto init` is **lab-free**: it needs no `--lab` and no `OTTO_SUT_DIRS`, and
it never creates an output directory.

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--all` | `False` | Scaffold every missing area without prompting |
| `--lab` | `False` | Scaffold the lab area (`lab_data/lab.json` + README) |
| `--tests` | `False` | Scaffold the tests area (example suite + conftest) |
| `--instructions` | `False` | Scaffold the instructions area (`pylib/<name>_instructions/`) |
| `--name NAME` | directory name | Product name for `settings.toml` |
| `--version X.Y.Z` | `0.1.0` | Product version for `settings.toml` |
| `--path DIR` | current dir | Repo root to operate on (must already exist) |

With no flags, `otto init` runs interactively: it prompts to confirm each
missing area (prompting for `--name`/`--version` only when
`.otto/settings.toml` itself is missing). `--all` scaffolds every missing
area with no prompts. Passing one or more of `--lab`/`--tests`/
`--instructions` scaffolds exactly those areas, plus `settings` automatically
whenever it's missing — every other area depends on it.

Areas that already exist are never modified. Instead, `otto init` validates
them with the same ingestion code otto uses elsewhere and reports each one
`✓` or `✗` in a summary table; the command exits with code 1 if any existing
area fails validation. The name used for areas scaffolded on a later run is
read from the existing `settings.toml`'s `name` field, falling back to the
directory name.

Every run also prints a "Next steps" list of the commands to run next —
`export OTTO_SUT_DIRS=...` (skipped if the repo is already listed there),
`otto --install-completion`, `otto --lab example_lab --list-hosts`,
`otto test --list-suites`, `otto test TestExample`, and `otto test --tests
test_example_function`.

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

Run registered test suites — or, without a suite name, a suite-less
selection by exact test name (`--tests`) and/or marker expression (`-m`).

```text
otto test [PARENT OPTIONS] <Suite> [SUITE OPTIONS]
otto test [PARENT OPTIONS] --tests NAME[,NAME...] [--markers EXPR]
otto test [PARENT OPTIONS] --markers EXPR
otto test --list-suites
otto test --list-tests [--markers EXPR] [<Suite>]
otto test --list-markers
```

`--tests` and/or `--markers` with no suite name select across every suite
and repo that has a match, including plain pytest `test_*` functions; bare
`otto test` with neither flag and no suite name prints help.  See {doc}`test`
for the full selection-run semantics, including how suite `Options` defaults
are applied.

### Parent options (before the suite name)

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--list-suites` | | List test suites with run syntax and exit |
| `--list-tests` | | List the selected tests and exit; narrow with a suite name and/or `--markers` |
| `--list-markers` | | List the markers available to `--markers` and exit |
| `--markers, -m EXPR` | `""` | Pytest marker expression (e.g. `"not integration"`). With no suite name, runs the marker selection in every repo that has a match |
| `--tests NAME[,NAME...]` | `""` | Run specific tests by exact name across all suites/repos, no suite name needed; `TestClass::name` disambiguates |
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

Each suite also defines its own options via its `Options` dataclass — these
flags only exist on that suite's own subcommand (`otto test <Suite>
--flag`), not on a `--tests`/`-m` selection run. Use `otto test <Suite>
--help` to see them. Selection runs default-construct each suite's
`Options`; a suite with a required option fails its own tests with a hint
to run the suite form directly.

## otto host

Run commands, transfer files, log in, and invoke capability verbs on lab hosts.

```text
otto host <HOST_ID> run [--sudo] [--timeout SECS] <COMMANDS...>
otto host <HOST_ID> put <SRC...> <DEST>
otto host <HOST_ID> get <SRC...> <DEST>
otto host <HOST_ID> login
otto host <HOST_ID> reboot [--hard] [--wait] [--timeout SECS]
otto host <HOST_ID> install [--stage-only]
otto host <HOST_ID> power [STATE]
otto host <HOST_ID> ls [PATH] [--all]
otto host --list-hosts
```

### Subcommands

All `otto host` subcommands are synthesized from `@cli_exposed` host methods
using the same signature-driven mechanism — `run`, `put`, `get`, and `login`
included.  The full set varies by host class; run `otto host <HOST_ID> --help`
to see what is available for a specific host.

| Subcommand | Description |
| ---------- | ----------- |
| `run` | Execute one or more commands on the host |
| `put` | Upload local files to the host |
| `get` | Download files from the host |
| `login` | Open an interactive shell session on the host |
| `reboot` | Reboot the host (soft or hard power-cycle) |
| `shutdown` | Power off the host from its own shell |
| `power` | Turn the host on/off or toggle (requires a power controller) |
| `stage` | Stage products onto the host without installing |
| `install` | Stage then install products |
| `uninstall` | Uninstall products |
| `is-installed` | Exit 0 if all products are installed |
| `is-uninstalled` | Exit 0 if no products are installed |
| `exists` | Exit 0 if a path exists on the host |
| `ls` | List directory contents on the host |
| `mkdir` | Create a directory on the host |
| `rm` | Remove a path on the host |
| `cp` | Copy a path on the host |
| `mv` | Move/rename a path on the host |
| `read-file` | Print a file's text contents |
| `write-file` | Write text to a file on the host |

See {doc}`host/capabilities` for class-scoping rules and which verbs each host
type exposes.

### `run` options

```text
otto host <HOST_ID> run [OPTIONS] COMMANDS...
```

| Option | Description |
| ------ | ----------- |
| `COMMANDS...` | One or more shell commands (space-separated, each quoted as needed) |
| `--sudo / --no-sudo` | Run every command through `sudo` |
| `--timeout SECS` | Cumulative timeout in seconds across all commands |

### `put` / `get` arguments

```text
otto host <HOST_ID> put SRC... DEST
otto host <HOST_ID> get SRC... DEST
```

`SRC...` is one or more source paths (space-separated); `DEST` is the
destination directory.  For `put`, sources are local paths; for `get`, sources
are remote paths.

### `reboot` options

```text
otto host <HOST_ID> reboot [--hard] [--wait] [--timeout SECS]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--hard / --no-hard` | `--no-hard` | Power-cycle via the power controller instead of an in-shell reboot |
| `--wait / --no-wait` | `--no-wait` | Block until the host is reachable again after reboot |
| `--timeout SECS` | `600.0` | Maximum seconds to wait when `--wait` is set |

### `install` options

```text
otto host <HOST_ID> install [--stage-only]
```

| Option | Description |
| ------ | ----------- |
| `--stage-only / --no-stage-only` | Transfer products but skip the install step |

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

Retrieve, reset, and report gcov coverage across the lab's tiers.  For
the full walkthrough, prerequisites (`lcov`/`gcov`), and tier recipes,
see the [coverage guide](coverage.md).

```text
otto cov get    [OPTIONS]
otto cov clean
otto cov report [OUTPUT_DIR...] [OPTIONS]
```

| Subcommand | Description |
| ---------- | ----------- |
| `get` | Fetch `.gcda` counters from the lab and write one `capture.json` per board, anchored to `base_commit` (also run implicitly by `otto test --cov`) |
| `clean` | Zero remote `.gcda` counters ahead of a fresh session (Unix coverage hosts only — embedded reset is a later phase) |
| `report` | Assemble every tier — e2e captures, unit harvest, committed manual store — into an HTML report |

### `otto cov get` options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--output, -o PATH` | the per-invocation output dir | Directory for fetched counters and per-board captures |
| `--tier NAME` | the lab's sole `e2e`-kind tier | Tier stamped onto each capture; a `manual`-kind tier switches to manual-capture mode (commits into `.otto/coverage/manual/`) |
| `--ticket STR` | none | Ticket reference; **required** for a `manual`-kind tier |
| `--note STR` | none | Free-text note (`manual`-kind only) |
| `--tester-name STR` | `getpass.getuser()` | Tester name (`manual`-kind only) |
| `--tester-email STR` | `git config user.email` | Tester email (`manual`-kind only) |
| `--clean` | off | Zero the fetched Unix hosts' counters after a successful retrieval |

Retrieval requires a git repository (captures are anchored to `HEAD` via `base_commit`);
a dirty working tree is remapped onto committed-code coordinates
automatically.

### `otto cov report` arguments and options

| Argument | Description |
| -------- | ----------- |
| `OUTPUT_DIR...` | `otto test --cov` / `otto cov get` output directories, each containing a `cov/` subdirectory. List several to stitch multiple runs. Optional — with none given, the report is built from the committed manual store (and configured unit tiers) alone. |

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--report, -r PATH` | `./cov_report` | Directory for the generated HTML report (`index.html` is written inside it) |
| `--project-name NAME` | `Coverage Report` | Title shown in the HTML report header |
| `--tier NAME[=PATH]` | the configured tiers | Git-less escape hatch; repeatable. See tier rules below. |

### Tiers

Tiers are normally **declared** under `[coverage.tiers]` in
`.otto/settings.toml` and loaded automatically — with no `--tier`
flags, the report uses the configured tiers (or an implicit `system`
tier when none are configured).

Passing any `--tier` flag **bypasses the declarative model entirely**
(settings tiers, the manual store, and unit harvesting are not
consulted) — a git-less fallback for foreign `.info` files. `--tier`
is repeatable, and the **order of flags is the precedence order** —
the first tier is highest precedence and wins row coloring on the
annotated source view.

- `--tier system` (no path) — the implicit tier built by merging the
  supplied `OUTPUT_DIR` `.gcda` files with `lcov`. Only the `system`
  tier may omit a path.
- `--tier NAME=PATH` — any other tier; `PATH` must be a pre-existing
  lcov-format `.info` tracefile. A non-`system` tier without a path is
  rejected.
- Duplicate tier names are rejected.

### Examples

```text
otto cov get --tier manual --ticket PROJ-123 --note "verified failover"
otto cov report runs/2026-05-16_T1200/ --report ./report
otto cov report run_a/ run_b/ run_c/ --report ./combined
otto cov report runs/ --tier unit=unit.info --tier system --tier manual=manual.info
```

On success, otto logs the overall coverage percentage, the file count,
and the path to `index.html`. If no coverage data is found anywhere —
supplied directories, unit harvest, or the manual store — the command
logs an error naming the searched locations and exits non-zero.

### Collecting coverage (`otto test --cov`)

`otto test --cov` collects coverage as part of a test run — it fetches
counters after the suite and produces the same per-board captures
(anchored to `base_commit`) as `otto cov get`:

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

## otto link

Inspect and impair the lab's static links (the topology edges `otto tunnel`
rides). See {doc}`link` for the full guide (units, merge semantics, in-path
impairment, safety refusals, custom impairers).

```text
otto link impair <link> [--delay <time>] [--jitter <time>] [--loss <percent>] [--rate <rate>]
                         [--corrupt <percent>] [--duplicate <percent>] [--reorder <percent>]
                         [--from <host>] [--expire <seconds>]
otto link repair [<link>] [--all]
otto link list
```

### Link subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `impair` | Merge-apply netem parameters to a link's resolved placement(s) |
| `repair` | Clear a link's impairment(s) and cancel its timers, or every link with `--all` |
| `list` | List every static link's current impairment state |

### Link options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `<link>` (argument) | `impair`, `repair` | Link id or name |
| `--delay` | `impair` | Delay; bare number = ms, or an explicit `us`/`ms`/`s` suffix |
| `--jitter` | `impair` | Jitter; requires a delay (given now or already applied) |
| `--loss` | `impair` | Packet loss; bare number = percent, or a `%` suffix |
| `--rate` | `impair` | Rate limit; an explicit tc unit is required (e.g. `10mbit`) |
| `--corrupt` | `impair` | Corruption; bare number = percent, or a `%` suffix |
| `--duplicate` | `impair` | Duplication; bare number = percent, or a `%` suffix |
| `--reorder` | `impair` | Reorder; requires a delay (given now or already applied) |
| `--from` | `impair` | Narrow to the direction originating at this host (both by default) |
| `--expire` | `impair` | Auto-clear this impairment after N seconds |
| `--all` | `repair` | Repair every static link in the lab |

## otto tunnel

Create, list, and remove host-resident bidirectional tunnels. See
{doc}`tunnel` for the full guide (multi-hop chains, docker endpoints, host
requirements, tunnel identity).

```text
otto tunnel add    --hosts <h0[@if],h1[@if],...,hn-1[@if]> --port <P> [--protocol tcp|udp] [--dest <host[@if]>]
otto tunnel list
otto tunnel remove [<id>] [--all] [-y]
```

### Tunnel subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `add` | Create a bidirectional tunnel along an explicit host path (two or more hosts) |
| `list` | List every live tunnel discovery finds right now |
| `remove` | Remove a tunnel by id, or every tunnel with `--all` |

### Tunnel options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `--hosts` | `add` | Ordered `host[@iface]` path, two or more entries; `@iface` only needed when a host has more than one interface |
| `--port` | `add` | Service port, used at both endpoints |
| `--protocol` | `add` | `tcp` (default) or `udp` |
| `--dest` | `add` | Far-end delivery override; defaults to loopback on the last `--hosts` entry |
| `--all` | `remove` | Reap every otto tunnel |
| `-y, --yes` | `remove` | Skip the `--all` confirmation prompt |
| `<id>` (argument) | `remove` | Id of the tunnel to remove |
