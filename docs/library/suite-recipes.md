# Suite Recipes

Common patterns for writing test suites with
{class}`~otto.suite.suite.OttoSuite`.

## Parametrized tests

Use `@pytest.mark.parametrize` to run a test once per value.  Each
parameter combination gets its own artifact directory:

```python
import pytest
from otto.suite import OttoSuite


class TestInterfaces(OttoSuite):
    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Runs 3 times — once per interface."""
        result = await host.exec(f"ip link show {interface}")
        assert "UP" in result.value
```

## Non-fatal assertions with expect

Sometimes you want to check multiple conditions without stopping at the
first failure.  Request the `expect` fixture:

```python
async def test_device_config(self, suite_options, expect) -> None:
    result = (await host.run("show running-config")).only

    expect("hostname" in result.value, "Config should contain hostname")
    expect("ntp server" in result.value, "Config should have NTP configured")
    expect("logging" in result.value, "Config should have logging enabled")
    # All three are checked; the test fails at the end with every failure listed
```

Each failure is logged as it happens with its source line; the full report in
`expect.failures` adds the caller's locals. A hard `assert` in the body
still stops the test at once.

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
    assert suite_options.firmware in result.value


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
from typing import Annotated
import typer

from otto import options


@options
class RepoOptions:
    device_type: Annotated[str, typer.Option(help="Device type.")] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment.")] = "staging"
```

```python
# tests/test_device.py
import logging
from typing import Annotated
import typer

from otto import options
from my_shared.options import RepoOptions
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"


class TestDevice(OttoSuite):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        # suite_options has device_type, lab_env, AND firmware
        logger.info(f"Testing {suite_options.device_type} fw={suite_options.firmware}")
```

All fields from `RepoOptions` and `_Options` appear as CLI flags:

```bash
otto test TestDevice --device-type switch --firmware 2.1
```

The very same `RepoOptions` class can be inherited by **instructions**
— see
[Sharing repo-wide options](options-classes.md#sharing-repo-wide-options).
Defining it once in a shared module (e.g.
`pylib/<repo>_common/options.py`) is the recommended way to expose
repo-wide flags uniformly across every `otto test` and `otto run`
subcommand.
For the complete options reference — validation, the lifecycle, and the
`@options` decorator — see [Options classes](options-classes.md).

## Monitoring from a test

Start the performance monitor around a workload to capture metrics:

```python
async def test_performance_under_load(self, suite_options) -> None:
    hosts = [get_host("server1"), get_host("server2")]
    await self.start_monitor(hosts=hosts)

    await self.add_monitor_event("load started", color="#2ca02c")
    # ... run workload ...
    await self.add_monitor_event("load complete", color="#d62728")

    await self.stop_monitor()
```

Events appear as vertical markers on the dashboard timeline, making it
easy to correlate metric spikes with specific test actions. `label` can't
be blank, `color` must be a `#rrggbb` hex string (not a CSS color name),
and `dash` must be one of the six styles the dashboard's event editor
offers — `add_monitor_event` validates all three immediately and raises
rather than persisting an unrenderable event.

## Per-test artifact directories

Request `test_dir` for a directory that is this test's own — parametrized
tests get unique names — and `suite_dir` for the suite-wide one:

```python
async def test_capture_logs(self, test_dir) -> None:
    # test_dir is <run output dir>/TestDevice/test_capture_logs/
    result = (await host.run("show log")).only
    (test_dir / "device.log").write_text(result.value)
```

Both are created when first requested, like `tmp_path`; a test that never
names them leaves nothing behind.

## Docker from instructions and suites

The CLI is a thin wrapper around `otto.docker`. Project instructions and
suites import the same library directly:

```python
from otto.docker import deployed


@instruction()
async def smoke():
    async with deployed("integration", own=True) as stack:
        api = stack.hosts["api"]
        await api.run(["./run-tests"])
```

{func}`~otto.docker.deployment.deployed` is the recommended scope. It deploys
a **use-case** — the same named, cross-repo deployment `otto docker up` brings
up, with the same provider competition and placement — and hands back a
{class}`~otto.docker.deployment.UseCaseStack`: `hosts` (service -> container
host, flattened), `by_host`, the final `env` mapping, and the selection
report. On exit it tears the stack down, unless it found the stack already
running, in which case nested users share without yanking it from peers.
Ownership is stack-level and all-or-nothing.

`--on`, `--provide`, `--env` and service narrowing are all keyword arguments
here (`on=`, `provide=`, `env=`, `services=`); see
{doc}`../guide/cli/docker/use-cases` for what each one does and
{mod}`otto.docker.deployment` for the signatures.

The per-repo primitives stay public and supported —
{func}`~otto.docker.compose.composed`, `compose_up`, `compose_down`,
`build_images`. `composed(repo, lab, own=True)` scopes **one repo's** compose
files with the same sharing contract, and is what `deploy` is built from;
reach for it when you genuinely want a single repo's stack rather than a
use-case.
