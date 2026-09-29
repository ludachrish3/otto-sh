# otto test

`otto test` runs your repos' pytest tests, chosen by name or by marker
expression. A **repo** is a directory with a `.otto/settings.toml` at its
root, and its **test directories** are the ones that file's `tests` setting
lists ({doc}`../../configuration/settings`). pytest collects them as it
always does: by default every `test_*.py` file in them, with its
`Test`-prefixed classes and `test_`-prefixed functions. Writing those tests
is {doc}`../../cookbook/authoring/writing-tests`.

`otto test` is one command: the tests to run are positional name
arguments, and `--list-tests` lists what they select.

## `otto test --help`

```{raw} html
:file: ../../_static/generated/termynal/help-test.html
```

## Synopsis

```text
otto test [OPTIONS] [NAMES...]
otto test --list-tests [NAMES...] [--markers EXPR]
otto test --list-markers
```

- **`NAMES`** are tests to run, any number of them: a test name
  (`test_reboot`), a class name (`TestDevice`, every test in it) or both
  (`TestDevice::test_reboot`). Each name is looked up in every configured
  repo. {doc}`selection` has the forms and how they match.
- **A run needs a name or `-m`.** With neither, `otto test` exits 2 with
  `otto test needs at least one test name or -m EXPR`. `-m` alone selects by
  marker; with names, it narrows the named tests.
- **Options and names mix freely.** `otto test TestDevice --firmware 2.1` and
  `otto test --firmware 2.1 TestDevice` are the same command.
- **`--help` and the `--list-*` flags** answer and exit before that check, so
  they need no name. `--list-tests` and `--list-markers` also need no
  `--lab`: they read what pytest collects from the test files, and load no
  lab.
- **Global options go before `test`.** `--lab`, `-n` (dry run) and
  `--log-level` belong to `otto` itself
  ([Global options](../index.md#global-options)):
  `otto --lab my_lab --log-level DEBUG test TestDevice`.

## Coming from pytest

`otto test` runs pytest, so what you know carries over:

| In pytest | With `otto test` |
| --- | --- |
| node ID `tests/test_device.py::TestDevice::test_reboot` | a name without the file: `TestDevice::test_reboot`, `TestDevice` or `test_reboot` |
| a parametrized ID, `test_up[eth0]` | its base name, `test_up`, which runs every variant |
| `-m EXPR` | `-m EXPR`, pytest's own marker expression |
| fixtures, `conftest.py`, markers, plugins | work as-is |

{doc}`selection` has the details of names and `-m`. On top of pytest, the
command line adds:

- **the lab:** `otto --lab NAME test ...` picks the lab whose hosts the
  tests reach ([Global options](../index.md#global-options));
- **options:** the flags of every options class a repo registers for `test`
  ([Options](#options));
- **repeating:** `--iterations`, `--duration` and `--threshold`
  ([Repeating a test](#repeating-a-test));
- **monitoring:** `--monitor` samples the hosts for the whole run
  ([Monitoring a run](#monitoring-a-run));
- **a results layout:** each run gets its own directory, with its logs,
  `junit.xml` and the per-module and per-test directories otto's fixtures
  hand out ([Where a run's files go](#where-a-runs-files-go)).

## Running tests

```bash
otto --lab my_lab test TestDevice                     # every test in TestDevice
otto --lab my_lab test TestDevice --firmware 2.1      # with a registered flag
otto --lab my_lab test test_login TestB::test_plain   # a test by name, any class; one test in TestB
otto --lab my_lab test -m "not integration"           # by marker expression
otto --lab my_lab test TestDevice -m slow             # TestDevice's tests marked slow
otto test --list-tests                                # every test, grouped
otto test --list-tests --markers slow TestDevice      # what that selection would run
otto test --list-markers                              # the markers pytest knows, per repo
otto --lab my_lab -n test TestDevice                  # dry run: what would run, nothing runs
```

Names tab-complete; see {doc}`selection`. Tests can also run from Python,
without the command line; see
[Running tests from Python](../../cookbook/python-library.md#running-tests-from-python)
in the Cookbook.

## Options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--list-tests` | | List what the names and `--markers` select, grouped by repo, module and class, and exit |
| `--list-markers` | | List each repo's markers, as pytest knows them, then otto's built-in markers, and exit |
| `--markers, -m EXPR` | `""` | Pytest marker expression (e.g. `"not integration"`). Narrows the named tests, or selects on its own when no name is given |
| `--random / --no-random` | on | Shuffle test order (pytest-randomly); the seed is logged at session start. `--no-random` runs tests in collection order |
| `--seed N` | drawn | Fix the random-order seed to reproduce a previous run's order (implies `--random`; refused with `--no-random`) |
| `--iterations, -i N` | `0` | Repeat each test N times in one setup/teardown cycle |
| `--duration, -d SECONDS` | `0` | Repeat tests for SECONDS in one setup/teardown cycle |
| `--threshold FLOAT` | `100.0` | Minimum per-test pass rate percent when repeating with `--iterations`/`--duration` (0-100) |
| `--results PATH` | auto | JUnit XML output path |
| `--cov / --no-cov` | auto | Collect gcov coverage from remotes after the run; with neither flag, on when an instrumented product is found ({ref}`coverage-tristate`) |
| `--cov-dir PATH` | `<run dir>/cov` | Override coverage destination (implies `--cov`) |
| `--overwrite-cov-dir` | off | Allow `--cov-dir` to clear an existing non-empty dir |
| `--cov-clean / --no-cov-clean` | on | Delete `.gcda` on remotes before the run |
| `--cov-report, -r` | off | Generate an HTML coverage report after the run (implies `--cov`) |
| `--cov-report-dir PATH` | `<run dir>/cov_report` | Override HTML report destination (implies `--cov-report`) |
| `--overwrite-cov-report-dir` | off | Allow `--cov-report-dir` to clear an existing non-empty dir |
| `--project-name NAME` | `Coverage Report` | Title shown in the HTML report header (with `--cov-report`) |
| `--cov-tickets-json PATH` | not written | Also write a per-ticket coverage summary after the run (implies `--cov-report`; see {ref}`coverage-tickets-json`) |
| `--monitor / --no-monitor` | off | Collect host performance metrics for the entire run |
| `--monitor-interval SECONDS` | `5.0` | Sampling interval for `--monitor` (minimum 1.0) |
| `--monitor-output PATH` | `<run dir>/monitor.json` | Override monitor data destination (`.json` or `.db`) |
| `--monitor-hosts REGEX` | all hosts | Regex FULLY matched against host IDs (`re.fullmatch`) restricting which hosts `--monitor` samples — `sensor` does not select `sensor-1`; write `sensor.*` |

`otto test` also takes the flags of every options class a repo registers for
the `test` verb, and `otto test --help` lists them with the rest. A test
reads their values with
[`ctx.options(Cls)`](../../cookbook/authoring/writing-tests.md#what-every-test-gets); see
{doc}`../../cookbook/authoring/options-classes`. A value that fails the
class's validation stops the run with exit code 2 before any test runs.

### Listing tests

`--list-tests` runs pytest's collection and prints what
the names and `--markers` select, grouped by repo, module and class. Each
parametrization is listed:

```text
$ otto test --list-tests TestDevice
repo1 1.0.0
└── test_device.py
    └── TestDevice
        ├── test_device_reachable
        ├── test_firmware_version
        ├── test_management_plane
        ├── test_interface_state
        ├── test_interface_up[eth0]
        ├── test_interface_up[eth1]
        └── test_interface_up[mgmt0]
```

With no names and no `--markers` it lists every collected test. Every
listing is a fresh pytest collection of the files it covers, and what it
collects updates the test-name cache
([What a run imports](selection.md#what-a-run-imports)).

### Dry run

`otto -n test NAMES` (or `--dry-run`) runs nothing. It builds and validates
the registered options exactly as a real run would, so a bad value fails the
same way, and prints what the run would do, then the tests the run would
run:

```text
$ otto --lab unix -n test -m integration TestDevice
dry run: no command body was run and no device was contacted
  would run: otto test TestDevice --markers integration
  options:
    RepoOptions: device_type='router', lab_env='staging'
    DeviceTestOptions: firmware='latest', check_interfaces=True
  lab: unix (7 hosts)
dry run: pytest collected these tests; nothing ran
repo1 1.0.0
└── test_device.py
    └── TestDevice
        ├── test_device_reachable
        ├── test_firmware_version
        ├── test_management_plane
        ├── test_interface_state
        ├── test_interface_up[eth0]
        ├── test_interface_up[eth1]
        └── test_interface_up[mgmt0]
```

The test list is the run's own pytest collection, with `--collect-only`: the
same files a run would collect, parametrizations expanded and the marker
expression evaluated. Collecting imports those test files and their
conftests, so module-level code in them runs; no test, fixture or host is
touched. An unknown name is refused with the same did-you-mean a run gives.
What a dry run means for every command, including how sensitive option
values are shown, is {doc}`../dry-run`.

### Test order and reproducibility

Tests run in a **random order** by default (pytest-randomly), so an order
dependence between two tests surfaces as a failure instead of hiding behind
source order. Before each test, pytest-randomly also resets Python's `random`
module to a seed derived from the run's seed and the test's ID, so a test
that draws random numbers draws the same ones whatever order it runs in.
Every run logs the seed it drew at session start:

```text
INFO     random test order, seed 1234 (reproduce with --seed 1234)
```

Pass that value back to replay the exact order that failed, or opt out for
tests that genuinely depend on each other:

```bash
otto --lab my_lab test --seed 1234 TestDevice      # same order as the run that logged 1234
otto --lab my_lab test --no-random TestDevice      # collection order
```

`--seed` implies `--random`; combining it with `--no-random` is a usage
error.

### Where a run's files go

Each run writes into its own output directory,
`<xdir>/test/<timestamp>/` ([Output directories](../index.md#output-directories)),
called `<run dir>` on this page, and prints the path when it ends. The run's
logs and `junit.xml` sit at the top. Below them, the directories the
[`module_dir` and `test_dir` fixtures](../../cookbook/authoring/writing-tests.md#what-every-test-gets)
hand out mirror each test's pytest ID:

```text
<xdir>/test/20260906_143205_412/
├── console.log
├── verbose.log
├── junit.xml
├── test_router/                 module_dir for tests/test_router.py
│   ├── TestRouter/
│   │   └── test_uplink/         test_dir for TestRouter::test_uplink
│   └── test_plain/              test_dir for the plain function test_plain
└── switch/
    └── test_ports/              module_dir for tests/switch/test_ports.py
```

- **A module's directory** is its path under the repo's test directory,
  without `.py`: `tests/test_router.py` gives `test_router/`, and
  `tests/switch/test_ports.py` gives `switch/test_ports/`.
- **A test's directory** is inside its module's, under its class if it has
  one. Parametrized names are made filesystem-safe: `test_up[eth0]` becomes
  `test_up_eth0_`.
- **With more than one repo configured** (each with a test directory), each
  repo's directories go under a directory named after the repo,
  `<run dir>/<repo>/test_router/...`, whichever repos the names turn out to
  match. A single-repo setup keeps the shorter layout.
- **The JUnit file** is `<run dir>/junit.xml`, or `--results PATH`. With
  several repos, each repo runs its own pytest session and writes its own
  file, so none overwrites another: `junit_<repo>.xml`, and an explicit
  `--results PATH` gets `_<repo>` appended to its stem (`--results
  custom.xml` becomes `custom_repoA.xml`, `custom_repoB.xml`, ...). A repo
  with no selected test leaves no file.
- **Directories are created when a test first asks for them,** like pytest's
  `tmp_path`. A test that never names `module_dir` or `test_dir` leaves
  nothing behind.

### Repeating a test

`--iterations` repeats each test N times inside a single setup/teardown cycle;
`--duration` repeats for N seconds in the same cycle. Given both, testing stops
at whichever limit is reached first. `--threshold` sets the minimum per-test
pass rate the repeated run must clear:

```bash
otto --lab my_lab test TestDevice --iterations 50 --threshold 95
```

Each iteration gets its own artifact directory, so one repeat's logs and
files cannot overwrite the previous one's. The `test_dir` a test writes into
gets one more level, `iteration_1/`, then `iteration_2/`, and so on, numbered
to match the `--- <test name> iteration 1 ---` banners in the log:

```text
<run dir>/
└── test_device/
    └── TestDevice/
        └── test_capture_logs/
            ├── iteration_1/
            │   └── device.log
            ├── iteration_2/
            │   └── device.log
            └── iteration_3/
                └── device.log
```

Without `--iterations` or `--duration` there is no extra level. `module_dir`
never moves either way.

### Monitoring a run

`--monitor` samples every host — or those `--monitor-hosts` matches — on a
fixed interval for the whole run, emitting per-test start and end events
automatically. At the end a `format:1` JSON snapshot of every metric and event
is written to `<run dir>/monitor.json`, loadable with `otto monitor <path>`.

`--monitor-output` overrides that destination and infers the format from the
suffix: `.json` writes the self-contained snapshot, `.db` a SQLite session
archive. Both load the same way.

`--monitor-hosts` is fully matched against host ids (`re.fullmatch`), so
`sensor` does not select `sensor-1` — write `sensor.*`. A pattern that matches
none of the hosts the run may walk **stops the run before any test executes**,
naming the pattern, the size of the set it was matched against, and the
wildcard to add. Hosts that matched but cannot be sampled — an embedded
console has no shell for the collector to read — are a different thing: that
logs a warning naming them, disables collection, and lets the tests run.

A single test can also start and stop a monitor of its own, with its own
dashboard, through the `monitor` fixture; see
[Monitoring from a test](../../cookbook/test-recipes.md#monitoring-from-a-test).

## Markers

`-m EXPR` selects by marker ({doc}`selection`). Declare your own markers,
such as `integration` or `slow`, in the repo's `pyproject.toml`, where pytest
reads them:

```toml
[tool.pytest.ini_options]
markers = [
    "integration: needs a live device",
    "slow: takes more than a minute",
]
```

`otto test --list-markers` prints one panel per repo and then one for otto.
A repo's panel lists every marker pytest knew when it last collected the
repo's tests: the ones a pytest config declares, the ones pytest itself, a
plugin or a conftest registers (such as `asyncio`), and the ones a test
applies. When a repo's test-name cache must be collected whole (the first
time, for instance; see [What a run imports](selection.md#what-a-run-imports)),
`--list-markers` collects it first. otto's panel lists the markers otto itself adds, `ensure` and `retry`, which the
repo panels leave out.

pytest, otto and the plugins otto runs with provide these markers in every
run:

`@pytest.mark.timeout(seconds)`
: Fail the test if it runs longer than *seconds*.

`@pytest.mark.retry(n)`
: Re-run a failed test body until it passes, *n* total attempts
  (`retry(2)` is one retry); the body must be idempotent.

`@pytest.mark.ensure("installed")`
: Converge the lab through the named steps before the test; see
  [Declaring lab state](../../cookbook/authoring/writing-tests.md#declaring-lab-state-the-ensure-marker).

`@pytest.mark.parametrize("arg", [values])`
: Run the test once per value.  Each parameter combination gets its own
  `test_dir`.

`@pytest.mark.asyncio(loop_scope="class")`
: Run a test, class or module on an event loop of its own (`"class"`,
  `"module"` or `"function"`) instead of the run's one loop; see
  {doc}`../../cookbook/host-scopes`.

```{toctree}
:caption: Topics
:hidden:

selection
```
