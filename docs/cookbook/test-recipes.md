# Test recipes

Common patterns for writing otto tests. {doc}`authoring/writing-tests` is
the guide these build on. Recipes that take a `dut` use the session-scoped
fixture from
[One connection for the whole run](host-scopes.md#one-connection-for-the-whole-run).

## Parametrized tests

Use `@pytest.mark.parametrize` to run a test once per value. Each
parameter combination gets its own `test_dir`:

```python
import pytest

from otto.config import get_host


class TestInterfaces:
    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Runs 3 times, once per interface."""
        result = (await get_host("dut1").run(f"ip link show {interface}")).only
        assert "UP" in result.value
```

## Non-fatal assertions with expect

Sometimes you want to check multiple conditions without stopping at the
first failure. Request the `expect` fixture:

```python
async def test_device_config(dut, expect) -> None:
    result = (await dut.run("show running-config")).only

    expect("hostname" in result.value, "Config should contain hostname")
    expect("ntp server" in result.value, "Config should have NTP configured")
    expect("logging" in result.value, "Config should have logging enabled")
    # All three are checked; the test fails at the end with every failure listed
```

Each failure is logged as it happens with its source line; the full report in
`expect.failures` adds the caller's locals. A hard `assert` in the body
still stops the test at once.

You can also use {class}`~otto.suite.expect.ExpectCollector` directly,
outside of a test:

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

from otto import Status

from acme_instructions import DeviceTestOptions


@pytest.mark.timeout(30)
async def test_firmware_version(dut, ctx) -> None:
    """Fail if the test takes longer than 30 seconds."""
    result = (await dut.run("show version")).only
    assert ctx.options(DeviceTestOptions).firmware in result.value


@pytest.mark.retry(3)
async def test_flaky_connection(dut) -> None:
    """Three attempts before reporting failure."""
    result = (await dut.run("ping -c 1 gateway")).only
    assert result.status == Status.Success
```

Every marker otto adds, and where your own are declared, is in
[Markers](../cli/test/index.md#markers).

## Monitoring from a test

Request the `monitor` fixture and start it around a workload to capture
metrics from the hosts it names:

```python
from otto.config import get_host


async def test_performance_under_load(monitor) -> None:
    hosts = [get_host("server1"), get_host("server2")]
    url = await monitor.start(hosts=hosts)  # the live dashboard's URL

    await monitor.event("load started", color="#2ca02c")
    # ... run workload ...
    await monitor.event("load complete", color="#d62728")

    await monitor.stop()
    assert monitor.results()  # "host/metric" -> [(timestamp, value), ...]
```

- **Stopping is automatic.** If a test started the monitor, the fixture
  stops it when the test ends, even when the test fails, so the dashboard
  server shuts down and a `db_path=` archive gets its end time. Call
  `monitor.stop()` yourself only to stop earlier.
- **The data outlives the stop.** `monitor.results()` and `monitor.events()`
  still read what was collected after `stop()`.
- **Events** appear as vertical markers on the dashboard timeline, making it
  easy to correlate metric spikes with specific test actions. `label` can't
  be blank, `color` must be a `#rrggbb` hex string (not a CSS color name),
  and `dash` must be one of the six styles the dashboard's event editor
  offers; `monitor.event` validates all three immediately and raises rather
  than persisting an unrenderable event. Under `otto test --monitor`, an
  event from a test that started no monitor of its own lands on the run's
  timeline instead.

{class}`~otto.suite.monitor_fixture.MonitorHandle` documents every
parameter of `start`.

## Per-test artifact directories

Request `test_dir` for a directory that is this test's own (parametrized
tests get unique names) and `module_dir` for the one every test in the file
shares:

```python
async def test_capture_logs(dut, test_dir) -> None:
    # test_dir is <run dir>/test_device/test_capture_logs/
    result = (await dut.run("show log")).only
    (test_dir / "device.log").write_text(result.value)
```

Both are created when first requested, like `tmp_path`; a test that never
names them leaves nothing behind. What `<run dir>` is, and where they sit in a class, in a nested
test directory, in a repeating run and in a run across several repos, are in
[Where a run's files go](../cli/test/index.md#where-a-runs-files-go).

## Docker from instructions and tests

The CLI is a thin wrapper around `otto.docker`. Project instructions and
tests import the same library directly:

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
{doc}`../cli/docker/use-cases` for what each one does and
{mod}`otto.docker.deployment` for the signatures.

The per-repo primitives stay public and supported —
{func}`~otto.docker.compose.composed`, `compose_up`, `compose_down`,
`build_images`. `composed(repo, lab, own=True)` scopes **one repo's** compose
files with the same sharing contract, and is what `deploy` is built from;
reach for it when you genuinely want a single repo's stack rather than a
use-case.
