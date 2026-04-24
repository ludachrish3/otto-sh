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
| `--log-rich` | `OTTO_LOG_RICH` | `False` | Rich formatting in log files |
| `--verbose, -v` | | `False` | Verbose console output |
| `--dry-run, -n` | | `False` | Preview without running commands |
| `--list-labs` | | | List available lab names and exit |
| `--list-hosts` | | | List host IDs in the loaded lab and exit |
| `--show-lab` | | | Print full lab details and exit |
| `--version` | | | Show version and exit |
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
| `--markers, -m EXPR` | `""` | Pytest marker expression (e.g. `"not integration"`) |
| `--iterations, -i N` | `0` | Repeat each test N times in one setup/teardown cycle |
| `--duration, -d SECONDS` | `0` | Repeat tests for SECONDS in one setup/teardown cycle |
| `--threshold FLOAT` | `100.0` | Minimum per-test pass rate percent in stability mode (0-100) |
| `--results PATH` | auto | JUnit XML output path |
| `--cov` | off | Collect gcov coverage from remotes after the run |
| `--cov-dir PATH` | `<output>/cov` | Override coverage destination (implies `--cov`) |
| `--overwrite-cov-dir` | off | Allow `--cov-dir` to clear an existing non-empty dir |
| `--cov-clean / --no-cov-clean` | on | Delete `.gcda` on remotes before the run |

Each suite also defines its own options via its `Options` dataclass.
Use `otto test <Suite> --help` to see them.

## otto host

Run commands and transfer files on lab hosts.

```text
otto host <HOST_ID> run <COMMANDS...>
otto host <HOST_ID> put <SRC...> <DEST>
otto host <HOST_ID> get <SRC...> <DEST>
otto host --list-hosts
```

### Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `run` | Execute one or more commands on the host |
| `put` | Upload local files to the host |
| `get` | Download files from the host |

### Host-level options

| Option | Description |
| ------ | ----------- |
| `HOST_ID` (argument) | Host ID to operate on (see `--list-hosts`) |
| `--hop HOST_ID` | Route through an intermediate SSH hop host |
| `--list-hosts` | List all available host IDs and exit |

## otto monitor

Launch the interactive performance dashboard.

```text
otto monitor [HOSTS] [OPTIONS]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `HOSTS` (argument) | all hosts | Comma-separated host IDs to monitor |
| `--interval, -i SECS` | `5.0` | Collection interval (minimum 1.0) |
| `--file, -f PATH` | | Load historical data from `.db` or `.json` |
| `--db PATH` | | Persist live data to SQLite for later viewing |
