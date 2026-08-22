# Writing test suites

A **suite** is a `Test`-prefixed subclass of
{class}`~otto.suite.suite.OttoSuite`, which registers itself and becomes an
`otto test` subcommand. This page is how to write one. For running suites,
see {doc}`../guide/cli/test/index`.

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
    firmware: Annotated[
        str,
        typer.Option(
            help="Firmware version to validate against.",
        ),
    ] = "latest"

    check_interfaces: Annotated[
        bool,
        typer.Option(
            help="When True, verify all expected interfaces are up.",
        ),
    ] = True


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

This all happens at import time, and *where* otto looks is narrower than
where pytest does — deliberately.

:::{important}
otto registers suites from the **top level** of each directory in `tests`:
`tests/test_device.py` yes, `tests/device/test_device.py` no. To nest, add
the subdirectory to the list — `tests` is a top-level key in
`.otto/settings.toml` and takes several paths:

```toml
# .otto/settings.toml
tests = ["tests", "tests/device"]
```

The reason is blast radius, not speed. These files are **executed** at
bootstrap on every otto command so that `__init_subclass__` fires, and a
failure in any of them exits non-zero for *every* command — so one broken
test file stops `otto host list`. Listing the directories keeps that surface
one you chose.

This bounds *registration* only. `otto test` hands the same directories to
pytest, which recurses as usual, so a nested `test_*` function still runs and
still completes under `--tests` — including the methods of a nested `Test*`
`OttoSuite`. Only the `otto test <Suite>` subcommand needs the file at the
top level of a listed directory.
:::

Auto-registration is one seam among many; see
{doc}`Extension points <../architecture/subsystems/extension-points>` for the
registry machinery behind this and every other way otto can be extended.

## Options classes

A suite's inner `Options` class is expanded into `otto test <Suite>` flags and
handed to each test method as `suite_options` — the test-suite stage of
otto's options lifecycle. See {doc}`options-classes` for how to define,
validate, and share an options class (including inheriting a repo-wide base
across suites).

## Fixtures otto provides

Every suite run under `otto test` gets these fixtures from otto's own pytest
plugin — request them by name like any other fixture. Your repo's `conftest.py`
fixtures are unaffected and sit alongside them.

| Fixture | Scope | Gives you |
| ------- | ----- | --------- |
| `suite_options` | class | The suite's `Options` instance (see above) |
| `ctx` | function | The active {class}`~otto.context.OttoContext` for this invocation |
| `ensure_installed` | function | A lab converged to fully-installed before the test |
| `ensure_uninstalled` | function | A lab converged to fully-uninstalled before the test |
| `ensure_clean` | function | A lab with no products, dev tools, toolchain tools, impairments or tunnels left |

The three `ensure_*` fixtures declare a test's *starting state* instead of
scripting it:

```python
class TestWidget(OttoSuite):
    async def test_service_answers(self, ensure_installed) -> None:
        """Runs against a fully-installed lab, whatever the last test left."""
        self.logger.info("lab is installed")

    async def test_installs_from_scratch(self, ensure_clean) -> None:
        """Runs against a lab with nothing of ours on it."""
        self.logger.info("lab is clean")
```

Each is a one-line wrapper over the same `otto.project` converge functions
`otto run install --ensure` calls, so a fixture and the command can never
diverge. They are function-scoped because the guarantee is per test *case*;
when the state already holds the cost is one status sweep. A convergence that
fails **errors the test with the failing host named** — never a skip
({class}`~otto.errors.EnsureStateError`). See {doc}`../guide/cli/run/defaults` for what each
one converges and how a repo customizes it.

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
