# Suite Recipes

Common patterns for writing test suites with
{class}`~otto.suite.suite.OttoSuite`.

## Parametrized tests

Use `@pytest.mark.parametrize` to run a test once per value.  Each
parameter combination gets its own artifact directory:

```python
import pytest
from otto.suite import OttoSuite, register_suite

@register_suite()
class TestInterfaces(OttoSuite):

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Runs 3 times — once per interface."""
        result = await host.oneshot(f"ip link show {interface}")
        assert "UP" in result.output
```

## Non-fatal assertions with expect

Sometimes you want to check multiple conditions without stopping at the
first failure.  Use `self.expect()`:

```python
async def test_device_config(self, suite_options) -> None:
    result = (await host.run("show running-config")).only

    self.expect("hostname" in result.output, "Config should contain hostname")
    self.expect("ntp server" in result.output, "Config should have NTP configured")
    self.expect("logging" in result.output, "Config should have logging enabled")
    # All three are checked; failures are reported together at the end
```

You can also use {class}`~otto.suite.expect.ExpectCollector` directly
outside of a suite:

```{doctest}
>>> from otto.suite.expect import ExpectCollector
>>> collector = ExpectCollector()
>>> collector.expect(1 == 1)
>>> collector.expect(2 + 2 == 4)
>>> len(collector.failures)
0
```

## Timeout and retry markers

```python
import pytest

@pytest.mark.timeout(30)
async def test_firmware_version(self, suite_options) -> None:
    """Fail if the test takes longer than 30 seconds."""
    result = (await host.run("show version")).only
    assert suite_options.firmware in result.output

@pytest.mark.retry(3)
async def test_flaky_connection(self) -> None:
    """Retry up to 3 times before reporting failure."""
    result = (await host.run("ping -c 1 gateway")).only
    assert result.status == Status.Success
```

## Inheriting shared options

Suite `Options` and instruction `options=` dataclasses are independent
but *compatible* — both decorators run the same dataclass-field
expansion, so you have three postures to choose from:

1. **Different** — each side defines its own dataclass. Fine when the
   flags don't overlap.
2. **Shared base (recommended for repo-wide flags)** — define one
   `RepoOptions` dataclass in a shared pylib module and inherit it from
   both the suite's inner `Options` and the instruction's `options=`
   dataclass, each extending with its own local fields.
3. **Same class** — both sides pass the exact same dataclass when the
   repo-wide flags are all either side needs.

Define a base `Options` dataclass in a shared module (listed in your
`init` setting) and inherit from it in each suite:

```python
# pylib/my_shared/options.py
from dataclasses import dataclass
from typing import Annotated
import typer

@dataclass
class RepoOptions:
    device_type: Annotated[str, typer.Option(help="Device type.")] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment.")] = "staging"
```

```python
# tests/test_device.py
from dataclasses import dataclass
from typing import Annotated
import typer
from my_shared.options import RepoOptions
from otto.suite import OttoSuite, register_suite

@dataclass
class _Options(RepoOptions):
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"

@register_suite()
class TestDevice(OttoSuite[_Options]):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        # suite_options has device_type, lab_env, AND firmware
        self.logger.info(f"Testing {suite_options.device_type} fw={suite_options.firmware}")
```

All fields from `RepoOptions` and `_Options` appear as CLI flags:

```bash
otto test TestDevice --device-type switch --firmware 2.1
```

The very same `RepoOptions` dataclass can be inherited by **instructions**
— see
[Sharing repo-wide options](../guide/run.md#sharing-repo-wide-options-across-instructions-and-suites).
Defining it once in a shared pylib module (e.g.
`pylib/<repo>_common/options.py`) is the recommended way to expose
repo-wide flags uniformly across every `otto test` and `otto run`
subcommand.

## Monitoring from a test

Start the performance monitor around a workload to capture metrics:

```python
async def test_performance_under_load(self, suite_options) -> None:
    hosts = [get_host("server1"), get_host("server2")]
    await self.startMonitor(hosts=hosts)

    await self.addMonitorEvent("load started", color="green")
    # ... run workload ...
    await self.addMonitorEvent("load complete", color="red")

    await self.stopMonitor()
```

Events appear as vertical markers on the dashboard timeline, making it
easy to correlate metric spikes with specific test actions.

## Per-test artifact directories

Every test gets a `self.testDir` directory for storing artifacts.
Parametrized tests get unique names:

```python
async def test_capture_logs(self, suite_options) -> None:
    # self.testDir is e.g. <xdir>/test/TestDevice/<timestamp>/test_capture_logs/
    log_file = self.testDir / "device.log"
    result = (await host.run("show log")).only
    log_file.write_text(result.output)
```
