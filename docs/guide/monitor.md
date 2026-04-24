# otto monitor

`otto monitor` launches an interactive performance dashboard that collects
CPU, memory, disk, and network metrics from remote hosts in real time.

## Live mode

By default, `otto monitor` polls all hosts in the lab:

```bash
otto --lab my_lab monitor
```

### Selecting hosts

Pass a comma-separated list of host IDs to monitor a subset:

```bash
otto --lab my_lab monitor router1,switch1
```

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

## Historical mode

View previously collected data by passing `--file`:

```bash
otto --lab my_lab monitor --file metrics.db
otto --lab my_lab monitor --file metrics.json
```

Supported formats: `.db` (SQLite) and `.json`.

## Web dashboard

In both modes, otto serves an interactive web dashboard.  By default it
listens on port 8000.  The dashboard shows:

- Live metric graphs (CPU, memory, disk, network)
- Timeline with events
- Per-host breakdowns

## Monitoring from test suites

You can start the monitor programmatically from within a test suite:

```python
class TestPerformance(OttoSuite[_Options]):

    async def test_load(self, suite_options: _Options) -> None:
        await self.startMonitor(hosts=[host1, host2])
        await self.addMonitorEvent("Load started", color="green")

        # ... run workload ...

        await self.addMonitorEvent("Load complete", color="red")
        await self.stopMonitor()
```

Events appear as markers on the dashboard timeline, making it easy to
correlate metric changes with test actions.

## Custom parsers

The monitor uses parsers to extract metrics from command output.  By default,
all hosts use `DEFAULT_PARSERS`.  You can register custom parsers for
specific hosts:

```python
from otto.monitor.collector import MonitorTarget
from otto.monitor.parsers import DEFAULT_PARSERS

MonitorTarget(
    host=gpu_host,
    parsers={
        **DEFAULT_PARSERS,
        "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits":
            NvidiaGpuParser(),
    },
)
```

See {mod}`otto.monitor.parsers` for the built-in parsers and the
{class}`~otto.monitor.parsers.MetricParser` protocol.
