"""
Metric parsers — convert raw command output (CommandResult.value) to numeric values.

Built-in parsers cover the most common Linux host metrics. To add support for a
custom command, subclass MetricParser and override parse()::

    class MyAppParser(MetricParser):
        chart = "Connections"
        unit = ""
        command = "ss -s | grep estab"

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]: ...

To associate custom parsers with a specific host, call register_host_parsers()
from an init module listed in .otto/settings.toml::

    from otto.monitor.parsers import DEFAULT_PARSERS, TopCpuParser, register_host_parsers
    from my_repo.parsers import NvidiaGpuParser

    register_host_parsers(
        "gpu-01",
        {
            **DEFAULT_PARSERS,
            "nvidia-smi --query-gpu=utilization.gpu ...": NvidiaGpuParser(),
        },
    )

Hosts with no registered parsers fall back to DEFAULT_PARSERS.

To add or override a parser for every monitored host instead of one host,
call register_parsers() the same way::

    from otto.monitor.parsers import register_parsers
    from my_repo.parsers import UptimeParser

    register_parsers([UptimeParser()])

Project-level entries merge over DEFAULT_PARSERS by command string (a command
matching a default overrides it; a new command extends the set), and yield to
any per-host registration for that host.
"""

import contextlib
import copy
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

from typing_extensions import override

from ..registry import Registry, caller_module
from .rates import RateTracker

_float_re_str = r"[\d]+(\.\d+)?"
_mem_size_re_str = rf"{_float_re_str}(?:\s*[KMGT]B?)?"
_percent_re_str = rf"{_float_re_str}%"


class MetricDataPoint(NamedTuple):
    """A single data point returned by MetricParser.parse()."""

    value: float
    """The numeric measurement for this tick."""

    meta: dict[str, Any] | None = None
    """Optional supplementary data forwarded to the dashboard as hover text
    (e.g. ``{'used': '4.2 GB', 'total': '16 GB'}`` for memory)."""


@dataclass(frozen=True)
class ParseContext:
    """Tick-local input to MetricParser.parse — extensible without signature breaks."""

    core_count: int = 1
    """Number of CPU cores on the target host for this tick. Most parsers ignore
    this; :class:`TopCpuParser` uses it to normalize per-process CPU%."""

    ts: datetime | None = None
    """Collection timestamp for this tick. Rate parsers feed it to their
    :class:`~otto.monitor.rates.RateTracker`; ``None`` (bare construction in
    tests) means the parser falls back to ``datetime.now(tz=timezone.utc)``."""


_BYTES_PER_UNIT = 1024.0  # binary prefix divisor used by human_readable


def human_readable(value: float, precision: int = 1) -> str:
    """Format a byte count as a human-readable string using binary prefixes (df -h style).

    Args:
        value:     The value in bytes.
        precision: Maximum number of decimal places in the output (trailing zeros are stripped).

    >>> human_readable(0)
    '0 B'
    >>> human_readable(1024)
    '1 K'
    >>> human_readable(1536)
    '1.5 K'
    >>> human_readable(1073741824)
    '1 G'
    """
    suffix = "P"
    for suffix in ("B", "K", "M", "G", "T", "P"):
        if value < _BYTES_PER_UNIT or suffix == "P":
            break
        value /= _BYTES_PER_UNIT
    formatted = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return (formatted or "0") + f" {suffix}"


# TODO: Consider changing this to a dataclass
class MetricParser(ABC):
    """
    Base class for metric parsers.

    Subclass this and set the class attributes, then override parse() to
    extract a single numeric value from the raw command output string.
    """

    y_title: str
    """Y-axis title shown to the left of the chart, e.g. 'CPU'."""

    # TODO: Make the unit optionally derived from parsing
    unit: str
    """Unit suffix for chart annotations, e.g. '%', 'MB', 'GB'. Empty string for dimensionless values."""  # noqa: E501 — long field docstring

    command: str
    """The exact shell command whose output this parser handles."""

    # TODO: Have a single `tab` value and derive the tab_label from the tab
    tab: str = "metrics"
    """Dashboard tab id this metric belongs to, e.g. 'cpu'. Defaults to 'metrics'."""

    tab_label: str = "Metrics"
    """Human-readable label for the tab button, e.g. 'CPU'. Defaults to 'Metrics'."""

    chart: str
    """Chart group id. Series with the same chart value share one Plotly chart.
    Single-series parsers set this to their series label; multi-series parsers set it
    to a shared group name (e.g. ``'Load'``)."""

    interval: float | None = None
    """Collection interval override in seconds for this parser's command.
    ``None`` means the collector's global ``--interval`` (the default).
    Honored by :meth:`~otto.monitor.collector.MetricCollector.run`'s per-interval
    scheduling."""

    @abstractmethod
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        """
        Convert raw command output into one or more labelled data points.

        Args:
            output: The full stdout+stderr string returned by the remote command.
            ctx: Tick-local input (e.g. the target host's core count).

        Returns:
            A dict mapping series label → :class:`MetricDataPoint` for each data
            point produced this tick.  Return an empty dict if parsing fails.
            Single-series parsers return ``{self.chart: MetricDataPoint(value)}``.
            Multi-series parsers may return multiple entries in the dict.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in parsers
# ---------------------------------------------------------------------------


class TopCpuParser(MetricParser):
    """
    Parse overall and per-process CPU usage from ``top -d{delay} -bn2`` output.

    Runs two top iterations separated by *delay* seconds so that per-process %CPU
    values reflect activity during that interval (the first iteration has no baseline
    and is discarded).  Overall CPU usage and per-process traces share one chart.

    Args:
        top_n: Maximum number of processes to include per collection tick.
        delay: Seconds between top iterations (controls accuracy vs latency trade-off).
    """

    y_title = "Usage %"
    unit = "%"
    tab = "cpu"
    tab_label = "CPU"
    chart = "CPU"

    def __init__(self, top_n: int = 5, delay: float = 0.5) -> None:
        self.top_n = top_n
        self._delay = delay

    @override
    @property  # type: ignore[override]
    def command(self) -> str:  # type: ignore[override]
        return f"top -d {self._delay} -bn2"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        block = 0
        in_table = False
        proc_count = 0

        for line in output.splitlines():
            # Block boundary — "Tasks:" appears once per top iteration
            if line.lstrip().startswith("Tasks:"):
                block += 1
                in_table = False
                proc_count = 0
                continue

            # Aggregate CPU line (no -1): "%Cpu(s):  2.5 us, ..., 95.8 id, ..."
            if line.startswith("%Cpu(s)") and block == 2:  # noqa: PLR2004 — top CPU aggregate appears in block 2 of top -b output
                m = re.search(r"(\d+\.?\d*)\s*id", line)
                if m:
                    result["Overall CPU"] = MetricDataPoint(
                        value=round(100.0 - float(m.group(1)), 2)
                    )
                continue

            # Process table header
            if "PID" in line and "%CPU" in line:
                in_table = True
                continue

            # Parse process rows from the second block only
            # Columns: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
            if in_table and block == 2 and proc_count < self.top_n:  # noqa: PLR2004 — top process rows appear in block 2 of top -b output
                parts = line.split(None, 11)
                if len(parts) < 12:  # noqa: PLR2004 — top process row has 12 columns: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
                    continue
                try:
                    result[f"proc/{parts[0]}"] = MetricDataPoint(
                        value=round(float(parts[8]) / ctx.core_count, 2),
                        meta={
                            "Command": parts[11],
                            "User": parts[1],
                            "Mem": f"{float(parts[9]):.1f}%",
                            "RSS": human_readable(int(parts[5]) * 1024, precision=0),
                            "Stat": parts[7],
                            "CPU Time": parts[10],
                        },
                    )
                    proc_count += 1
                except (ValueError, IndexError):
                    continue

        return result


class MemParser(MetricParser):
    """
    Parse memory and swap usage % from `free -b` output.

    Reads the 'Mem:' and 'Swap:' lines and computes used/total as percentages.
    """

    y_title = "Memory"
    unit = "%"
    command = "free -b"
    tab = "memory"
    tab_label = "Memory"
    chart = "Memory Usage"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            lowered = line.lower()
            if not (lowered.startswith(("mem:", "swap:"))):
                continue
            parts = line.split()
            # free -b: <label> total used free [shared buff/cache available]
            if len(parts) < 3:  # noqa: PLR2004 — need at least label, total, used
                continue
            try:
                total, used = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            if total <= 0:
                continue  # swapless host: omit the series, no flat-0 line
            label = "Memory Usage" if lowered.startswith("mem:") else "Swap"
            result[label] = MetricDataPoint(
                value=round(used / total * 100.0, 2),
                meta={"Used": human_readable(used), "Total": human_readable(total)},
            )
        return result


class DiskParser(MetricParser):
    """
    Parse root filesystem usage % from `df -h /` output.

    Reads the data row (second line) and extracts the Use% column.
    parse_meta() returns the already human-readable Size/Used strings from df -h.
    """

    y_title = "Disk"
    unit = "%"
    command = "df -h"
    tab = "disk"
    tab_label = "Disk"
    chart = "Disk Usage"

    _regex = re.compile(
        r"\s+"
        rf"(?P<Total>{_mem_size_re_str})\s+"
        rf"(?P<Used>{_mem_size_re_str})\s+"
        rf"(?P<Available>{_mem_size_re_str})\s+"
        rf"(?P<used_percent>{_float_re_str})%\s+"
        rf"(?P<Mount>\S+)"
    )

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        lines = [text_line for text_line in output.splitlines() if text_line.strip()]
        # Typical output (df -h):
        #   Filesystem      Size  Used Avail Use% Mounted on
        #   /dev/sda1        20G  5.1G   14G  27% /

        series: dict[str, MetricDataPoint] = {}

        for line in lines[1:]:
            m = self._regex.search(line)
            if m:
                meta = m.groupdict()
                series[m["Mount"]] = MetricDataPoint(value=float(m["used_percent"]), meta=meta)

        return series


class LoadParser(MetricParser):
    """
    Parse all three load averages from `cat /proc/loadavg`.

    Output format: '0.52 0.58 0.59 1/432 12345'
    Returns 1-minute, 5-minute, and 15-minute load averages as separate series.
    """

    y_title = "Load"
    unit = ""
    command = "cat /proc/loadavg"
    tab = "cpu"
    tab_label = "CPU"
    chart = "Load"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        parts = output.strip().split()
        try:
            return {
                "Load (1m)": MetricDataPoint(value=float(parts[0])),
                "Load (5m)": MetricDataPoint(value=float(parts[1])),
                "Load (15m)": MetricDataPoint(value=float(parts[2])),
            }
        except (IndexError, ValueError):
            return {}


def _rate_meta(rates: dict[str, float | None]) -> dict[str, Any] | None:
    """Format auxiliary counter rates as hover meta; None when nothing rated yet."""
    meta = {label: f"{rate:.1f}/s" for label, rate in rates.items() if rate is not None}
    return meta or None


class NetDevParser(MetricParser):
    """Per-interface network throughput from ``/proc/net/dev`` counter deltas.

    Emits ``rx <iface>`` / ``tx <iface>`` byte rates; packet/error/drop rates
    ride each series' hover meta. The loopback interface is skipped. First
    tick per interface is the rate baseline and emits nothing; a counter
    reset (reboot) skips one tick and re-baselines (see
    :mod:`otto.monitor.rates`).
    """

    y_title = "Throughput"
    unit = "B/s"
    command = "cat /proc/net/dev"
    tab = "network"
    tab_label = "Network"
    chart = "Network I/O"

    def __init__(self) -> None:
        self._rates = RateTracker()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        ts = ctx.ts or datetime.now(tz=timezone.utc)
        result: dict[str, MetricDataPoint] = {}
        active: set[str] = set()
        for line in output.splitlines():
            if ":" not in line:
                continue  # header lines
            iface, _, rest = line.partition(":")
            iface = iface.strip()
            fields = rest.split()
            if iface == "lo" or len(fields) < 16:  # noqa: PLR2004 — /proc/net/dev rows carry 8 rx + 8 tx counters
                continue
            try:
                counters = [float(fields[i]) for i in (0, 1, 2, 3, 8, 9, 10, 11)]
            except ValueError:
                continue
            rx_bytes, rx_pkts, rx_errs, rx_drop, tx_bytes, tx_pkts, tx_errs, tx_drop = counters
            keys = ("rx", "rxp", "rxe", "rxd", "tx", "txp", "txe", "txd")
            active.update(f"{iface}/{c}" for c in keys)
            rx_rate = self._rates.update(f"{iface}/rx", rx_bytes, ts)
            rx_aux = {
                "Packets": self._rates.update(f"{iface}/rxp", rx_pkts, ts),
                "Errors": self._rates.update(f"{iface}/rxe", rx_errs, ts),
                "Drops": self._rates.update(f"{iface}/rxd", rx_drop, ts),
            }
            tx_rate = self._rates.update(f"{iface}/tx", tx_bytes, ts)
            tx_aux = {
                "Packets": self._rates.update(f"{iface}/txp", tx_pkts, ts),
                "Errors": self._rates.update(f"{iface}/txe", tx_errs, ts),
                "Drops": self._rates.update(f"{iface}/txd", tx_drop, ts),
            }
            if rx_rate is not None:
                result[f"rx {iface}"] = MetricDataPoint(round(rx_rate, 2), meta=_rate_meta(rx_aux))
            if tx_rate is not None:
                result[f"tx {iface}"] = MetricDataPoint(round(tx_rate, 2), meta=_rate_meta(tx_aux))
        self._rates.prune(active)
        return result


class SocketsParser(MetricParser):
    """TCP socket-state counts from the ``TCP:`` summary line of ``ss -s``.

    Hosts without ``ss`` produce a shell error the parser cannot match — the
    series simply never appears (and the collector warns once; see
    parser-health warnings). Swap in a ``netstat``-based parser per host via
    :func:`register_host_parsers` if needed.
    """

    y_title = "Sockets"
    unit = ""
    command = "ss -s"
    tab = "network"
    tab_label = "Network"
    chart = "Sockets"

    _regex = re.compile(r"^TCP:\s+\d+\s+\(estab (?P<estab>\d+),.*timewait (?P<timewait>\d+)")

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        for line in output.splitlines():
            m = self._regex.match(line.strip())
            if m:
                return {
                    "Established": MetricDataPoint(float(m["estab"])),
                    "Time-wait": MetricDataPoint(float(m["timewait"])),
                }
        return {}


_SECTOR_BYTES = 512  # /proc/diskstats counts 512-byte sectors regardless of device geometry


class DiskIoParser(MetricParser):
    """Per-device read/write throughput from ``/proc/diskstats`` sector deltas.

    Whole devices only: partitions (``sda1``, ``nvme0n1p2``, ``mmcblk0p1``)
    and virtual/noise devices (``loop*``, ``ram*``, ``dm-*``, ``zram*``,
    ``sr*``) are skipped so charts show physical disk activity once.
    """

    y_title = "Disk I/O"
    unit = "B/s"
    command = "cat /proc/diskstats"
    tab = "disk"
    tab_label = "Disk"
    chart = "Disk I/O"

    _skip = re.compile(r"^(?:loop|ram|dm-|zram|sr)")
    _partition = re.compile(r"^(?:[shv]d[a-z]+\d+|nvme\d+n\d+p\d+|mmcblk\d+p\d+)$")

    def __init__(self) -> None:
        self._rates = RateTracker()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        ts = ctx.ts or datetime.now(tz=timezone.utc)
        result: dict[str, MetricDataPoint] = {}
        active: set[str] = set()
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 10:  # noqa: PLR2004 — device rows carry >= 10 stat fields
                continue
            name = fields[2]
            if self._skip.match(name) or self._partition.match(name):
                continue
            try:
                sectors_read, sectors_written = float(fields[5]), float(fields[9])
            except ValueError:
                continue
            active.update((f"{name}/r", f"{name}/w"))
            read_rate = self._rates.update(f"{name}/r", sectors_read * _SECTOR_BYTES, ts)
            write_rate = self._rates.update(f"{name}/w", sectors_written * _SECTOR_BYTES, ts)
            if read_rate is not None:
                result[f"read {name}"] = MetricDataPoint(round(read_rate, 2))
            if write_rate is not None:
                result[f"write {name}"] = MetricDataPoint(round(write_rate, 2))
        self._rates.prune(active)
        return result


class PerCoreCpuParser(MetricParser):
    """Per-core busy %% from ``/proc/stat`` jiffies deltas.

    Far cheaper than a second ``top`` run: busy%% = 100 x (1 - Δ(idle+iowait)
    / Δtotal) per ``cpuN`` line. The aggregate ``cpu`` line is skipped —
    :class:`TopCpuParser` already charts overall CPU. Jiffies ratios need no
    wall clock (time cancels), so state is plain previous counters.
    """

    y_title = "Usage %"
    unit = "%"
    command = "cat /proc/stat"
    tab = "cpu"
    tab_label = "CPU"
    chart = "Per-core CPU"

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}  # core -> (total, idle_all)

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            fields = line.split()
            if not fields or not re.fullmatch(r"cpu\d+", fields[0]) or len(fields) < 9:  # noqa: PLR2004 — cpuN rows carry 8 jiffies fields
                continue
            try:
                jiffies = [float(f) for f in fields[1:9]]
            except ValueError:
                continue
            total, idle_all = sum(jiffies), jiffies[3] + jiffies[4]
            core = fields[0].removeprefix("cpu")
            prev = self._prev.get(core)
            self._prev[core] = (total, idle_all)
            if prev is None:
                continue
            d_total, d_idle = total - prev[0], idle_all - prev[1]
            if d_total <= 0 or d_idle < 0:
                continue  # counter reset — re-baseline, skip the tick
            result[f"core {core}"] = MetricDataPoint(round(100.0 * (1 - d_idle / d_total), 2))
        return result


class ProcCountParser(MetricParser):
    """Process counts: runnable/total from loadavg field 4, blocked from /proc/stat.

    Cats both files in one command — the command string doubles as the parser
    registry key, so it must differ from ``LoadParser``'s ``cat /proc/loadavg``
    and ``PerCoreCpuParser``'s ``cat /proc/stat``; reading both also gets
    ``procs_blocked`` for free.
    """

    y_title = "Count"
    unit = ""
    command = "cat /proc/loadavg /proc/stat"
    tab = "cpu"
    tab_label = "CPU"
    chart = "Processes"

    _loadavg = re.compile(r"^[\d.]+ [\d.]+ [\d.]+ (?P<run>\d+)/(?P<total>\d+) \d+$")

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            m = self._loadavg.match(line.strip())
            if m:
                result["Runnable"] = MetricDataPoint(float(m["run"]))
                result["Total procs"] = MetricDataPoint(float(m["total"]))
            elif line.startswith("procs_blocked"):
                with contextlib.suppress(ValueError, IndexError):
                    result["Blocked"] = MetricDataPoint(float(line.split()[1]))
        return result


# ---------------------------------------------------------------------------
# Default parser registry — maps command string → parser instance
# ---------------------------------------------------------------------------

DEFAULT_PARSERS: dict[str, MetricParser] = {
    p.command: p
    for p in [
        TopCpuParser(),
        MemParser(),
        DiskParser(),
        LoadParser(),
        NetDevParser(),
        SocketsParser(),
        DiskIoParser(),
        PerCoreCpuParser(),
        ProcCountParser(),
    ]
}


# ---------------------------------------------------------------------------
# Per-host parser registry
# ---------------------------------------------------------------------------

# Registry of host_id -> its full parser dict (command string -> MetricParser).
# Unlike otto's other backend registries this is keyed by an arbitrary lab host
# id rather than a fixed set of dialect/backend names, and re-registering a
# host_id is normal usage (see register_host_parsers) — so registration always
# overwrites rather than raising on a second call for the same host_id.
HOST_PARSERS: Registry[dict[str, "MetricParser"]] = Registry(
    "host parser set", register_hint="otto.monitor.parsers.register_host_parsers()"
)


def register_host_parsers(host_id: str, parsers: dict[str, "MetricParser"]) -> None:
    """Associate a custom parser dict with a host ID.

    Call this from an init module (listed in ``.otto/settings.toml``) to override
    or extend the default parsers for a specific host.  The *host_id* string must
    match the ID used to look up the host (i.e. the key in ``lab.hosts``).

    Hosts with no registered parsers automatically fall back to DEFAULT_PARSERS.
    """
    HOST_PARSERS.register(host_id, parsers, overwrite=True, origin=caller_module())


# ---------------------------------------------------------------------------
# Project-level parser registry
# ---------------------------------------------------------------------------

# Project-wide parser additions/overrides, keyed by command string. Unlike
# HOST_PARSERS (whole-dict per host), entries here merge over DEFAULT_PARSERS
# for every host that has no per-host registration. Re-registering the same
# command is a config bug and raises loudly (Registry dupe machinery).
PROJECT_PARSERS: Registry[MetricParser] = Registry(
    "project metric parser", register_hint="otto.monitor.parsers.register_parsers()"
)


def register_parsers(parsers: Sequence[MetricParser]) -> None:
    """Register project-level parsers that apply to every monitored host.

    Call from an init module (listed in ``.otto/settings.toml``). Each parser's
    ``command`` becomes its key: a command matching a DEFAULT_PARSERS entry
    overrides that built-in; a new command extends the set. Per-host
    registrations (``register_host_parsers``) take total precedence for their
    host. Registering the same command twice raises.
    """
    origin = caller_module()
    for p in parsers:
        PROJECT_PARSERS.register(p.command, p, origin=origin)


def default_catalog() -> dict[str, "MetricParser"]:
    """Return the parser catalog used when no per-host registration applies.

    DEFAULT_PARSERS extended/overridden by project-level registrations.
    """
    merged = dict(DEFAULT_PARSERS)
    merged.update(PROJECT_PARSERS.items())
    return merged


def get_host_parsers(host_id: str) -> dict[str, "MetricParser"]:
    """Return the parser dict for *host_id*: per-host > project-level > defaults.

    A per-host registration (see :func:`register_host_parsers`) wins outright
    for its host_id — it is a total replacement, not merged with anything else.
    Otherwise, project-level parsers (see :func:`register_parsers`) are merged
    over DEFAULT_PARSERS. Non-raising by design: a host with no registrations
    at all is normal (it just uses the defaults), not an error.
    """
    if host_id in HOST_PARSERS:
        return copy.deepcopy(HOST_PARSERS.get(host_id))
    return copy.deepcopy(default_catalog())
