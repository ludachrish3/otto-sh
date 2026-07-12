# otto test

`otto test` runs **test suites** -- classes that extend
{class}`~otto.suite.suite.OttoSuite` with a `Test`-prefixed name, which
registers them automatically.  Each suite becomes its own subcommand with
typed CLI options.

## Defining a test suite

Create a `test_*.py` file in one of your repo's `tests` directories:

```python
from typing import Annotated

import pytest
import typer

from otto import options
from otto.suite import OttoSuite


@options
class _Options:
    firmware: Annotated[str, typer.Option(
        help="Firmware version to validate against.",
    )] = "latest"

    check_interfaces: Annotated[bool, typer.Option(
        help="When True, verify all expected interfaces are up.",
    )] = True


class TestDevice(OttoSuite[_Options]):
    """Validate device configuration and connectivity."""

    Options = _Options

    async def test_device_reachable(self, suite_options: _Options) -> None:
        """Verify the device responds to basic connectivity checks."""
        self.logger.info(f"firmware={suite_options.firmware!r}")
        assert True

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, suite_options: _Options) -> None:
        """Verify the running firmware matches the expected version."""
        assert True

    @pytest.mark.retry(2)
    async def test_management_plane(self) -> None:
        """Verify management-plane access (retried up to 2 times)."""
        assert True

    @pytest.mark.integration
    async def test_interface_state(self, suite_options: _Options) -> None:
        """Verify all expected interfaces are up (requires live device)."""
        if not suite_options.check_interfaces:
            pytest.skip("Interface check disabled via --no-check-interfaces")
        assert True

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Parametrized -- runs once per interface name."""
        assert True
```

## Suite registration

`OttoSuite.__init_subclass__` auto-registers any subclass whose name starts
with `Test` (matching pytest's own `python_classes = Test*` collection
rule). Registration:

1. Reads the inner `Options` dataclass
2. Converts each field into a Typer CLI parameter
3. Creates a runner function with the matching signature
4. Adds the suite as a subcommand of `otto test`

This all happens at import time when otto scans your `tests` directories.

## Options classes

A suite's options class is expanded into `otto test <Suite>` flags and handed to
each test method as `suite_options` — the test-suite stage of otto's options
lifecycle ({doc}`options`).

Suite-specific options are defined with `@options` using
`Annotated[T, typer.Option(...)]` fields.  They automatically appear in
`otto test <Suite> --help`:

```python
from otto import options

@options
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
```

### Inheriting options

You can share options across suites by inheriting from a base options class:

```python
from otto import options

@options
class RepoOptions:
    device_type: Annotated[str, typer.Option(help="Device type.")] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment.")] = "staging"

@options
class _Options(RepoOptions):
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
```

Import the base from a shared module listed in your `init` setting.

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

Suites can also run as a plain library call, with no CLI/Typer involved — see
[Running suites from Python](library-usage.md#running-suites-from-python) in
the library usage guide.

## Running without a suite name

`otto test` doesn't require a suite subcommand. Passing `--tests` and/or
`-m`/`--markers` alone selects tests by exact name and/or marker expression
across every suite and repo that has a match, including plain pytest
`test_*` functions (not just `OttoSuite` classes). Bare `otto test` with no
suite name and neither flag just prints help.

```bash
otto test --tests test_login                    # every test named test_login, any suite
otto test --tests TestB::test_login,test_plain   # disambiguate + mix suite/plain names
otto test -m "not integration"                   # marker expression, no suite name
otto test --tests test_login -m slow             # narrow a name selection by marker too
```

- `--tests NAME[,NAME...]` matches on exact function name: a bare name (e.g.
  `test_login`) matches that name in every suite/repo, across all
  parametrizations; `TestClass::test_name` restricts to one suite. Unknown
  names raise an error with did-you-mean suggestions rather than silently
  running nothing.
- `-m EXPRESSION` alone (no `--tests`, no suite name) runs the marker
  selection the same way — one pytest session per repo that has a match.

### Tab-completing `--tests`

`--tests` tab-completes test names, matched by **base name** — a bare
`test_login` selects every `test_login[...]` parametrization, and
`TestClass::test_login` disambiguates. Two layers feed the candidates:

- A **static source scan** (the `def test_*` functions and `Test*` class
  methods otto can see without importing your code) is the always-available
  floor — instant, and it never runs a test at tab time.
- A **pytest-collected** set adds tests that only exist after collection —
  dynamically generated ones (`pytest_generate_tests`, conftest fixtures) a
  source scan can't see. It's warmed by any real collection: `otto test
  --list-tests` fills it for free, and otherwise the first `--tests` TAB runs
  one bounded background collection (a single, capped slow TAB; it falls back
  to the floor if it can't finish in time) and caches the result — so later
  completions are fast *and* complete. Editing a test file re-warms it
  automatically. The collection runs in a throwaway subprocess, so the shell's
  completion never runs your code directly.

For the exact, fully-expanded per-parametrization list, `otto test
--list-tests` still prints every collected id.
- Multi-repo selection runs write one JUnit file per repo
  (`junit_<repo>.xml`) instead of the single-suite `junit.xml`. An explicit
  `--results PATH` fans out the same way: `PATH`'s stem gets `_<repo>`
  appended for each participating repo (e.g. `--results custom.xml` becomes
  `custom_repoA.xml`, `custom_repoB.xml`, ...), so multiple repos' sessions
  never overwrite each other's results.
- Stability (`--iterations`/`-i`, `--duration`/`-d`, `--threshold`),
  `--cov*`, `--monitor*`, and `--results` all apply to selection runs the
  same as to a named suite.

### Suite-specific options and selection runs

Suite-specific options (declared on a suite's `Options` class) only exist as
CLI flags on that suite's own subcommand — `otto test TestDevice --flag`.
Selection runs (`--tests`/`-m` with no suite name) span multiple suites at
once, so there's no single flag set to parse; each suite's `Options` class is
instead **default-constructed** once per suite. If a suite's `Options` has a
required field (no default), its tests fail during the selection run with a
hint to re-run that suite directly:

```text
suite 'TestDevice' has required options — run `otto test TestDevice ...` to pass them (...)
```

Suites whose options are all optional (have defaults) run fine under
selection — they just get their defaults instead of CLI-provided values.

## Parent command options

These options live on `otto test` itself and must appear **before** the
suite name on the command line (when a suite name is given at all — see
[Running without a suite name](#running-without-a-suite-name) above):

`--markers / -m EXPRESSION`
: Pytest marker expression.  Example: `--markers "not integration" TestDevice`.
  With no suite name, runs the marker selection in every repo that has a
  match instead.

`--tests NAME[,NAME...]`
: Run specific tests by exact name, across all suites/repos — no suite
  subcommand needed.  Comma-separated; `TestClass::name` disambiguates.
  Combine with `--markers` to narrow further.  Unknown names raise an error
  with did-you-mean suggestions.  Example: `--tests test_login,TestB::test_plain`

`--iterations / -i N`
: Repeat each test *N* times within a single setup/teardown cycle.
  Default: 0 (disabled).  Example: `--iterations 50 TestDevice`

`--duration / -d SECONDS`
: Repeat tests for *SECONDS* seconds within a single setup/teardown cycle.
  Default: 0 (disabled).  Example: `--duration 300 TestDevice`

  When both `--iterations` and `--duration` are specified, testing stops
  when either limit is reached first.

`--threshold FLOAT`
: Minimum per-test pass rate percentage required in stability mode (0-100).
  Default: 100 (all iterations must pass).  Example:
  `--iterations 50 --threshold 95 TestDevice`

`--results PATH`
: Write JUnit XML results to PATH.  Default: auto-generated in the log
  directory.

`--monitor`
: Collect host performance metrics for the entire test run.  Samples every
  host (or those matched by `--monitor-hosts`) on a fixed interval and
  emits per-test start/end events automatically.  At the end of the run a
  `format:1` JSON snapshot of all metrics and events is written to
  `<output_dir>/monitor.json`.  The file is loadable in the dashboard via
  `otto monitor <path>`.

`--monitor-interval SECONDS`
: Sampling interval for `--monitor` (minimum 1, default 5).

`--monitor-output PATH`
: Override the destination for the captured monitor data.  Format inferred
  from the suffix: `.json` (default) writes a self-contained `format:1`
  snapshot, `.db` writes a SQLite session archive — both loadable via
  `otto monitor <path>`.  Implies `--monitor`.

`--monitor-hosts REGEX`
: Restrict `--monitor` to host IDs matching this regex (`re.search`).
  Implies `--monitor`.

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

## Suite features

### Logging

Every suite has a `self.logger` attribute:

```python
self.logger.info("Starting test")
self.logger.info("[bold]Rich markup[/bold]", extra={"markup": True})
```

### Per-test artifact directories

Each test gets a `self.testDir` directory for artifacts.  Parametrized
tests get unique directory names based on their parameter values.

### Non-fatal assertions

Use `self.expect()` to record a failure without stopping the test:

```python
self.expect(result.status == Status.Success, "Command should succeed")
self.expect("expected" in result.value, "Output should contain 'expected'")
```

All failed expectations are reported at the end of the test.

### Monitoring from a suite

Start the monitor during a test to collect metrics:

```python
async def test_performance(self) -> None:
    await self.start_monitor(hosts=[host1, host2])
    # ... run workload ...
    await self.add_monitor_event("workload started", color="blue")
    # ... wait for results ...
    await self.stop_monitor()
```
