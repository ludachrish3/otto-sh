# otto test

`otto test` runs **test suites** -- classes that extend
{class}`~otto.suite.suite.OttoSuite` and are registered with
{func}`@register_suite() <otto.suite.register.register_suite>`.  Each suite
becomes its own subcommand with typed CLI options.

## Defining a test suite

Create a `test_*.py` file in one of your repo's `tests` directories:

```python
from dataclasses import dataclass
from typing import Annotated

import pytest
import typer

from otto.suite import OttoSuite, register_suite


@dataclass
class _Options:
    firmware: Annotated[str, typer.Option(
        help="Firmware version to validate against.",
    )] = "latest"

    check_interfaces: Annotated[bool, typer.Option(
        help="When True, verify all expected interfaces are up.",
    )] = True


@register_suite()
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

The `@register_suite()` decorator:

1. Reads the inner `Options` dataclass
2. Converts each field into a Typer CLI parameter
3. Creates a runner function with the matching signature
4. Adds the suite as a subcommand of `otto test`

This all happens at import time when otto scans your `tests` directories.

## Options dataclass

Suite-specific options are defined as a `@dataclass` with
`Annotated[T, typer.Option(...)]` fields.  They automatically appear in
`otto test <Suite> --help`:

```python
@dataclass
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
```

### Inheriting options

You can share options across suites by inheriting from a base dataclass:

```python
@dataclass
class RepoOptions:
    device_type: Annotated[str, typer.Option(help="Device type.")] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment.")] = "staging"

@dataclass
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
```

## Parent command options

These options live on `otto test` itself and must appear **before** the
suite name on the command line:

`--markers / -m EXPRESSION`
: Pytest marker expression.  Example: `--markers "not integration" TestDevice`

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
self.expect("expected" in result.output, "Output should contain 'expected'")
```

All failed expectations are reported at the end of the test.

### Monitoring from a suite

Start the monitor during a test to collect metrics:

```python
async def test_performance(self) -> None:
    await self.startMonitor(hosts=[host1, host2])
    # ... run workload ...
    await self.addMonitorEvent("workload started", color="blue")
    # ... wait for results ...
    await self.stopMonitor()
```
