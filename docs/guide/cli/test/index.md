# otto test

`otto test` runs **test suites** -- classes that extend
{class}`~otto.suite.suite.OttoSuite` with a `Test`-prefixed name, which
registers them automatically.  Each suite becomes its own subcommand with
typed CLI options.

## `otto test --help`

```{raw} html
:file: ../../../_static/generated/termynal/help-test.html
```

## Running suites

```bash
otto --lab my_lab test TestDevice
otto --lab my_lab test TestDevice --firmware 2.1
otto --lab my_lab test TestDevice --no-check-interfaces
otto test --list-suites                     # list suites with run syntax
otto test --list-markers                    # list markers available to --markers
otto test --list-tests                      # list every registered test
otto test --list-tests --markers slow TestDevice   # narrow by marker and/or suite
```

`otto test <SuiteName>` gets registry-backed completion and `--list-suites`
for free, like every other registry — these candidates are the demo repo's
registered suites, resolved by the real completion machinery:

```{raw} html
:file: ../../../_static/generated/termynal/complete-suites.html
```

Suites can also run as a plain library call, with no CLI/Typer involved — see
[Running suites from Python](../../../library/index.md#running-suites-from-python)
in the Python library guide.

## Synopsis

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
`otto test` with neither flag and no suite name prints help.  See {doc}`selection`
for the full selection-run semantics, including how suite `Options` defaults
are applied.

## Options

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
| `--cov-tickets-json PATH` | not written | Also write a per-ticket coverage summary after the run (implies `--cov-report`; see {ref}`coverage-tickets-json`). Requires `[coverage.tickets]` to be configured — checked before the test run starts, so a misconfiguration fails fast rather than after a long run |
| `--monitor / --no-monitor` | off | Collect host performance metrics for the entire run |
| `--monitor-interval SECONDS` | `5.0` | Sampling interval for `--monitor` (minimum 1.0) |
| `--monitor-output PATH` | `<output>/monitor.json` | Override monitor data destination (`.json` or `.db`) |
| `--monitor-hosts REGEX` | all hosts | Regex FULLY matched against host IDs (`re.fullmatch`) restricting which hosts `--monitor` samples — `sensor` does not select `sensor-1`; write `sensor.*` |

Each suite also defines its own options via its `Options` dataclass — these
flags only exist on that suite's own subcommand (`otto test <Suite>
--flag`), not on a `--tests`/`-m` selection run. Use `otto test <Suite>
--help` to see them. Selection runs default-construct each suite's
`Options`; a suite with a required option fails its own tests with a hint
to run the suite form directly.

### Repeating a test

`--iterations` repeats each test N times inside a single setup/teardown cycle;
`--duration` repeats for N seconds in the same cycle. Given both, testing stops
at whichever limit is reached first. `--threshold` sets the minimum per-test
pass rate the repeated run must clear:

```bash
otto --lab my_lab test TestDevice --iterations 50 --threshold 95
```

### Monitoring a run

`--monitor` samples every host — or those `--monitor-hosts` matches — on a
fixed interval for the whole run, emitting per-test start and end events
automatically. At the end a `format:1` JSON snapshot of every metric and event
is written to `<output_dir>/monitor.json`, loadable with `otto monitor <path>`.

`--monitor-output` overrides that destination and infers the format from the
suffix: `.json` writes the self-contained snapshot, `.db` a SQLite session
archive. Both load the same way.

`--monitor-hosts` is fully matched against host ids (`re.fullmatch`), so
`sensor` does not select `sensor-1` — write `sensor.*`. A pattern that matches
none of the hosts the run may walk **stops the run before any test executes**,
naming the pattern, the size of the set it was matched against, and the
wildcard to add: you asked for a monitored run over hosts that are not there,
and running unmonitored would answer a different question. Hosts that matched
but cannot be sampled — an embedded console has no shell for the collector to
read — are a different thing: that logs a warning naming them, disables
collection, and lets the tests run.

## Markers

`@pytest.mark.integration`
: Requires live Vagrant VMs.  Skip with `--markers "not integration"`.

`@pytest.mark.timeout(seconds)`
: Fail the test if it runs longer than *seconds*.

`@pytest.mark.retry(n)`
: Retry a failing test up to *n* times before reporting failure.

`@pytest.mark.parametrize("arg", [values])`
: Run the test once per value.  Each parameter combination gets its own
  artifact directory.

```{toctree}
:caption: Topics
:hidden:

selection
```
