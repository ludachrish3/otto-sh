# otto monitor

`otto monitor` launches an interactive performance dashboard that collects
CPU, memory, disk, and network metrics from remote hosts in real time.

![The live monitor dashboard: per-host CPU, process, and load charts with a
marked event](../_static/generated/dashboard-live.png)

Charts extend in place as new samples stream in over SSE, and events mark
moments on the shared timeline:

<video src="../_static/generated/dashboard-live.webm" autoplay loop muted playsinline width="100%"></video>

<!-- Both assets above are produced AT BUILD TIME by
scripts/capture_docs_media.py (hooked from docs/conf.py): the real dashboard,
seeded with deterministic dummy data, captured with headless Chromium. Do not
commit media into docs/_static/generated/. -->

## Live mode

By default, `otto monitor` polls every real host in the lab:

```bash
otto --lab my_lab monitor
```

Docker container hosts are excluded — they aren't operated on as part of
the host fleet. Embedded targets without an `snmp` block are also
excluded: the monitored set is Unix hosts (shell metrics) plus any host
that declares `snmp` (polled over SNMP — see
[SNMP monitoring](#snmp-monitoring) below).

### Selecting hosts

Pass a regex to `--hosts` (matched against host IDs via `re.search`) to
narrow the live host set:

```bash
otto --lab my_lab monitor --hosts 'router|switch'
otto --lab my_lab monitor --hosts router1
```

Omit the option to monitor every real host in the lab (Docker containers
excluded).

### Collection interval

Control how often metrics are collected with `--interval` (default: 5
seconds, minimum: 1 second):

```bash
otto --lab my_lab monitor --interval 2.0
```

### Persisting data

Use `--db` to write collected metrics to a SQLite file for later viewing:

```bash
otto --lab my_lab monitor --db metrics.db
```

### Running otto on shared/NFS storage

otto is safe to run with its log/artifact root (`OTTO_XDIR`) on a shared mount
(NFS, CIFS/SMB, sshfs, …):

- **Monitor database.** SQLite's WAL journaling is not supported over a network
  filesystem, so when the `--db` path is on one otto automatically uses the
  `DELETE` journal mode instead (logged at debug level). This is transparent and
  lossless for monitoring's write pattern.
- **Multi-machine, one shared database.** The "another instance is already
  writing" guard relies on `flock`, whose semantics on network filesystems are
  same-host only. If several machines may write to the *same* database file,
  put that database on **local disk** (or give each machine its own `--db`
  path).
- **Logs and artifacts.** Per-run log directories are fine on shared storage.
  Old-log rotation is wall-clock budgeted, so even a very large log tree cannot
  stall a run — any backlog is pruned across subsequent runs.
- **Lab data and settings** (`hosts.json`, `.otto/settings.toml`) are read once
  per run and are unaffected.

If otto cannot determine the filesystem type, it assumes local disk and keeps
its default behaviour.

## Historical mode

View previously collected data by passing `--file`:

```bash
otto --lab my_lab monitor --file metrics.db
otto --lab my_lab monitor --file metrics.json
```

Supported formats: `.db` (SQLite) and `.json`.

## Web dashboard

In both modes, otto serves an interactive web dashboard.  The server binds
an OS-assigned free port and logs the dashboard URL at startup (`Server
running at http://<ip>:<port>`, one URL per non-loopback interface).  The
dashboard shows:

- Live metric graphs (CPU, memory, disk, network)
- Timeline with events
- Per-host breakdowns

## Monitoring during a test run

Pass `--monitor` to `otto test` to collect metrics for the entire run.
Per-test start/end events are emitted automatically and the captured
data is written to `<output_dir>/monitor.json` at exit:

```bash
otto --lab my_lab test --monitor TestPerformance
otto --lab my_lab test --monitor --monitor-interval 2 --monitor-hosts router TestPerformance
otto --lab my_lab test --monitor --monitor-output run.db TestPerformance
```

Reload a captured run in the dashboard via `otto monitor --file <path>`.

## Monitoring from test suites

You can also start the monitor programmatically from within a single test:

```python
class TestPerformance(OttoSuite[_Options]):

    async def test_load(self, suite_options: _Options) -> None:
        await self.start_monitor(hosts=[host1, host2])
        await self.add_monitor_event("Load started", color="green")

        # ... run workload ...

        await self.add_monitor_event("Load complete", color="red")
        await self.stop_monitor()
```

When both per-suite and `--monitor`-driven session collectors are active,
the per-suite collector takes precedence for that test.  Events appear as
markers on the dashboard timeline, making it easy to correlate metric
changes with test actions.

## Custom parsers

The monitor uses parsers to extract metrics from command output.  By default,
all hosts use `DEFAULT_PARSERS`.  Subclass `MetricParser` and implement
`parse(self, output, *, ctx)` to extract one or more data points from a
command's raw output, then register it for specific hosts:

```python
from otto.monitor.collector import MonitorTarget
from otto.monitor.parsers import DEFAULT_PARSERS, MetricDataPoint, MetricParser, ParseContext


class NvidiaGpuParser(MetricParser):
    y_title = "Usage %"
    unit = "%"
    chart = "GPU"
    command = "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {self.chart: MetricDataPoint(value=float(output.strip()))}


MonitorTarget(
    host=gpu_host,
    parsers={
        **DEFAULT_PARSERS,
        NvidiaGpuParser.command: NvidiaGpuParser(),
    },
)
```

`ctx` (a {class}`~otto.monitor.parsers.ParseContext`) carries tick-local
input such as the target host's core count; most parsers ignore it.  See
{mod}`otto.monitor.parsers` for the built-in parsers and the
{class}`~otto.monitor.parsers.MetricParser` protocol.

### Project-level parsers

Register parsers that apply to every monitored host from an init module
(listed in `.otto/settings.toml`):

```python
from otto.monitor.parsers import register_parsers
from my_repo.parsers import SocketParser

register_parsers([SocketParser()])
```

A parser whose `command` matches a built-in overrides it; new commands
extend the set.  Per-host registrations (`register_host_parsers`) still take
total precedence for their host.  Registering the same command twice raises.

### Per-parser collection intervals

Set `interval` (seconds) on a parser class to poll its command on its own
cadence; parsers without one use the global `--interval`:

```python
class SocketParser(MetricParser):
    command = "ss -s"
    interval = 30.0  # poll sockets every 30s regardless of --interval
    ...
```

## SNMP monitoring

Some targets expose performance metrics over SNMP rather than via a shell
interface.  Otto supports SNMP v2c polling for any standards-compliant agent —
a Zephyr device running otto's test-bed agent, a Linux box running net-snmp, or
network gear — on a separate channel from command execution.

### When to use it

Use SNMP monitoring when a host either has no shell (embedded Zephyr targets) or
when you prefer to pull metrics through a dedicated management channel rather than
shell commands.  See {doc}`embedded` for embedded host setup and {doc}`lab-config`
for the `snmp` field reference.

### Configuring the `snmp` block in hosts.json

Add an `snmp` object to a host entry in `hosts.json` to enable SNMP collection
for that host:

```json
{
    "ip": "192.0.2.1",
    "element": "sprout",
    "os_type": "zephyr",
    "snmp": {
        "address": "10.10.200.14",
        "port": 16101,
        "community": "public",
        "oids": [
            "1.3.6.1.2.1.1.3.0",
            "1.3.6.1.4.1.63245.1.1.0",
            "1.3.6.1.4.1.63245.1.2.0",
            "1.3.6.1.4.1.63245.1.3.0",
            "1.3.6.1.4.1.63245.1.4.0"
        ]
    }
}
```

The `address` and `port` are the endpoint reachable from the otto host — for
an embedded device behind a hop this is typically the local end of a UDP relay
on the hop host, not the device's own address.  `community` defaults to
`"public"`.  `oids` is the bare list of OIDs to poll each tick; presentation
(label, chart group, unit) is supplied by the descriptor registry, not by lab
data.

### How otto reads SNMP data

`SnmpClient` (in `otto.monitor.snmp`) is a thin async SNMP v2c GET wrapper.  It
issues a single GET PDU per poll tick for all configured OIDs and returns a
`{oid: float | None}` mapping.  The `pysnmp` library is imported lazily inside
`SnmpClient.get`, so the SNMP path is entirely optional — otto imports cleanly
without `pysnmp` installed, and unit tests can mock at the `get` boundary.

### Built-in metric descriptors

Otto ships descriptors for a standard OID set.  Each descriptor (an `SnmpMetric`)
carries the label, chart group, y-axis title, unit, tab, and a `scale` factor
that converts the raw integer varbind to a real value.

| OID | Label | Chart | Unit | Notes |
| --- | ----- | ----- | ---- | ----- |
| `1.3.6.1.2.1.1.3.0` | Uptime | Uptime | s | Standard `sysUpTime` (TimeTicks ÷ 100); works against any compliant agent |
| `1.3.6.1.4.1.63245.1.1.0` | Overall CPU | CPU | % | Otto enterprise OID (centi-percent ÷ 100) |
| `1.3.6.1.4.1.63245.1.2.0` | Heap Used | Memory Usage | B | Otto enterprise OID |
| `1.3.6.1.4.1.63245.1.3.0` | Heap Free | Memory Usage | B | Otto enterprise OID |
| `1.3.6.1.4.1.63245.1.4.0` | Threads | Threads | — | Otto enterprise OID |

The enterprise OIDs are served by otto's Zephyr test-bed agent.  The enterprise
base is `1.3.6.1.4.1.63245` (PEN 63245, a placeholder — a real IANA PEN has not
yet been assigned).

An OID present in `oids` but without a registered descriptor falls back to
default styling via `resolve_snmp_metric`: the OID string is used as the label
and chart name on the generic `metrics` tab, so a host can poll a bare OID with
zero code and still get a chart.

### Extending: registering custom descriptors

Register a descriptor for a private or device-specific OID from an init module
listed in `.otto/settings.toml`:

```python
from otto.monitor.snmp import SnmpMetric, register_snmp_metric

register_snmp_metric(
    SnmpMetric(
        oid='1.3.6.1.4.1.99999.1.5.0',
        label='Fan Speed',
        chart='Fan',
        y_title='RPM',
        unit='rpm',
        tab='fans',
        tab_label='Fans',
        scale=1.0,
    )
)
```

This follows the same extension pattern as `register_host_parsers` and
`register_command_frame`.  The `SnmpMetric` fields are `oid`, `label`,
`chart`, `y_title`, `unit`, `tab`, `tab_label`, and `scale`; everything
after `chart` has a default, so a private OID only needs the first three:

```{doctest}
>>> from otto.monitor.snmp import SnmpMetric
>>> m = SnmpMetric(oid='1.3.6.1.4.1.99999.1.5.0', label='Fan Speed', chart='Fan',
...                y_title='RPM', unit='rpm', tab='fans', tab_label='Fans')
>>> m.tab, m.tab_label, m.scale
('fans', 'Fans', 1.0)
>>> SnmpMetric(oid='1.2.3', label='X', chart='C').tab
'metrics'
```
