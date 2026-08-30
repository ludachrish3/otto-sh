# Custom monitor parsers

Teaching `otto monitor` to collect values it has no built-in metric for:
per-host and project-level parsers, their health reporting, custom SNMP
descriptors, and driving the collector from a suite. For running the
monitor, see {doc}`../guide/cli/monitor/index`.

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
input such as the current collection timestamp; most parsers ignore it.  See
{mod}`otto.monitor.parsers` for the built-in parsers and the
{class}`~otto.monitor.parsers.MetricParser` protocol.

### Per-host parsers

Register a custom parser dict for one host — or a family of hosts matched by
a compiled regex — from an init module listed in `.otto/settings.toml`.
Registration matches on the host **id** (the unique key, as in `lab.hosts`),
not the human-readable display name shown in the dashboard:

```python
from otto.examples.monitor import UptimeParser
from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers

register_host_parsers(
    "router1",
    {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
)
```

`UptimeParser` (in `otto.examples.monitor`) is a complete, runnable example:
it charts `cat /proc/uptime` as a single "Uptime" series in seconds, and
otto's own test suite registers it exactly this way.

A compiled pattern instead of a host id scopes the same registration to
every host whose id matches — for example, giving a family of `busybox-*`
hosts (whose `ss` doesn't support `-s`) a `netstat`-based sockets parser in
place of the default `ss -s` one:

```python
import re

from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers
from my_repo.parsers import NetstatSocketsParser  # your own ss-free implementation

parsers = {k: v for k, v in DEFAULT_PARSERS.items() if k != "ss -s"}
parsers[NetstatSocketsParser.command] = NetstatSocketsParser()
register_host_parsers(re.compile(r"busybox-.*"), parsers)
```

Patterns are matched with `re.fullmatch` against the host id.  Precedence is
exact id > pattern > project-level > `DEFAULT_PARSERS`: an exact-id
registration always wins outright for that host, and a host matched by two
different patterns raises at resolution time rather than picking a silent,
import-order-dependent winner.

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

## Per-parser collection intervals

Set `interval` (seconds) on a parser class to poll its command on its own
cadence; parsers without one use the global `--interval`:

```python
class SocketParser(MetricParser):
    command = "ss -s"
    interval = 30.0  # poll sockets every 30s regardless of --interval
    ...
```

## Parser health

The collector watches each parser's command for two kinds of trouble and
logs a warning — edge-triggered, so a flapping command logs every
transition while a steady outage logs only once.

**Failing command.** The first tick a command starts failing (nonzero exit)
logs a warning naming the metrics that will be missing; recovery logs once
more when the command starts succeeding again:

```text
Monitor: 'ss -s' failed on test1 (exit 127): ss: command not found — Sockets metrics will be missing
Monitor: 'ss -s' recovered on test1 after 4 failed tick(s)
```

**Silent command.** A command that keeps exiting 0 but never yields a data
point — a bad regex, an unfamiliar output format, nothing to report — gets a
one-time backstop warning after three succeeding ticks with no output:

```text
Monitor: parser SocketsParser ('ss -s') has produced no data on test1 after 3 ticks
```

Only succeeding ticks count toward those three; a failing command is already
covered by the warning above and isn't double-counted here.  The same
backstop watches SNMP OIDs that never return a value.

Either way, a missing tool or unreachable metric is not an error otto tries
to recover from: the affected series is simply absent from the dashboard,
same as any other tick that produced no data.

## Extending: registering custom descriptors

Register a descriptor for a private or device-specific OID from an init module
listed in `.otto/settings.toml`:

```python
from otto.monitor.snmp import SnmpMetric, register_snmp_metric

register_snmp_metric(
    SnmpMetric(
        oid="1.3.6.1.4.1.99999.1.5.0",
        label="Fan Speed",
        chart="Fan",
        y_title="RPM",
        unit="rpm",
        tab="fans",
        tab_label="Fans",
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

`register_snmp_metric` always overwrites, so the same call renames a
built-in descriptor too — including the auto-generated per-index labels
from [Per-interface and per-filesystem OIDs](../guide/cli/monitor/metrics.md#per-interface-and-per-filesystem-oids)
(`rx if0`, `fs1 used`, …): register a new `SnmpMetric` for that exact OID
with a more meaningful `label` (e.g. `rx wan0`) and it replaces the default.

## Monitoring from test suites

You can also start the monitor programmatically from within a single test:

```python
class TestPerformance(OttoSuite):
    async def test_load(self, suite_options: _Options) -> None:
        await self.start_monitor(hosts=[host1, host2])
        await self.add_monitor_event("Load started", color="#2ca02c")

        # ... run workload ...

        await self.add_monitor_event("Load complete", color="#d62728")
        await self.stop_monitor()
```

`add_monitor_event` validates through the same seam every other marking
surface uses (see
[Marking events](../guide/cli/monitor/dashboard.md#marking-events)): `label` can't
be blank, `color` must be a `#rrggbb` hex string (not a CSS color name),
and `dash` must be one of the six styles the event editor offers — a
violation raises a validation error immediately, before the collector is
ever touched.

When both per-suite and `--monitor`-driven session collectors are active,
the per-suite collector takes precedence for that test.  Events — the
automatic per-test start/pass/fail marks and any `add_monitor_event` call
— appear live on the dashboard timeline the moment they're recorded, making
it easy to correlate metric changes with test actions; see [Marking
events](../guide/cli/monitor/dashboard.md#marking-events) for what the dashboard does with a mark once it's
there.
