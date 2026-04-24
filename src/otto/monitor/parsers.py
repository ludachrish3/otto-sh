"""
Metric parsers — convert raw command output (CommandStatus.output) to numeric values.

Built-in parsers cover the most common Linux host metrics. To add support for a
custom command, subclass MetricParser and override parse()::

    class MyAppParser(MetricParser):
        chart   = 'Connections'
        unit    = ''
        command = 'ss -s | grep estab'

        def parse(self, output: str) -> float | None:
            # output is the raw stdout/stderr string from the command
            for line in output.splitlines():
                if 'estab' in line.lower():
                    return float(line.split()[0])
            return None

To associate custom parsers with a specific host, call register_host_parsers()
from an init module listed in .otto/settings.toml::

    from otto.monitor.parsers import DEFAULT_PARSERS, TopCpuParser, register_host_parsers
    from my_repo.parsers import NvidiaGpuParser

    register_host_parsers('gpu-01', {
        **DEFAULT_PARSERS,
        'nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits': NvidiaGpuParser(),
    })

Hosts with no registered parsers fall back to DEFAULT_PARSERS.
"""

import copy
import re
from abc import ABC, abstractmethod
from typing import Any, NamedTuple

_float_re_str = r"[\d]+(\.\d+)?"
_mem_size_re_str = rf"{_float_re_str}(?:\s*[KMGT]B?)?"
_percent_re_str = rf"{_float_re_str}%"


class MetricDataPoint(NamedTuple):
    """A single data point returned by MetricParser.parse()."""

    value: float
    """The numeric measurement for this tick."""

    meta:  dict[str, Any] | None = None
    """Optional supplementary data forwarded to the dashboard as hover text
    (e.g. ``{'used': '4.2 GB', 'total': '16 GB'}`` for memory)."""


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
    suffix = 'P'
    for suffix in ('B', 'K', 'M', 'G', 'T', 'P'):
        if value < 1024.0 or suffix == 'P':
            break
        value /= 1024.0
    formatted = f'{value:.{precision}f}'.rstrip('0').rstrip('.')
    return (formatted or '0') + f' {suffix}'


# TODO: Consider changing this to a dataclass
class MetricParser(ABC):
    """
    Base class for metric parsers.

    Subclass this and set the class attributes, then override parse() to
    extract a single numeric value from the raw command output string.
    """

    y_title:   str
    """Y-axis title shown to the left of the chart, e.g. 'CPU'."""

    # TODO: Make the unit optionally derived from parsing
    unit:      str
    """Unit suffix for chart annotations, e.g. '%', 'MB', 'GB'. Empty string for dimensionless values."""

    command:   str
    """The exact shell command whose output this parser handles."""

    # TODO: Have a single `tab` value and derive the tab_label from the tab
    tab:       str = 'metrics'
    """Dashboard tab id this metric belongs to, e.g. 'cpu'. Defaults to 'metrics'."""

    tab_label: str = 'Metrics'
    """Human-readable label for the tab button, e.g. 'CPU'. Defaults to 'Metrics'."""

    chart:     str
    """Chart group id. Series with the same chart value share one Plotly chart.
    Single-series parsers set this to their series label; multi-series parsers set it
    to a shared group name (e.g. ``'Load'``)."""

    core_count: int = 1
    """Number of CPU cores on the target host. Set per-host by
    :class:`~otto.monitor.collector.MetricCollector` before the first tick.
    Most parsers ignore this; :class:`TopCpuParser` uses it to normalize per-process CPU%."""

    @abstractmethod
    def parse(self, output: str) -> dict[str, MetricDataPoint]:
        """
        Convert raw command output into one or more labelled data points.

        Args:
            output: The full stdout+stderr string returned by the remote command.

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
    y_title   = 'Usage %'
    unit      = '%'
    tab       = 'cpu'
    tab_label = 'CPU'
    chart     = 'CPU'

    def __init__(self, top_n: int = 5, delay: float = 0.5) -> None:
        self.top_n      = top_n
        self._delay     = delay

    @property  # type: ignore[override]
    def command(self) -> str:  # type: ignore[override]
        return f'top -d {self._delay} -bn2'

    def parse(self, output: str) -> dict[str, MetricDataPoint]:
        result:    dict[str, MetricDataPoint] = {}
        block      = 0
        in_table   = False
        proc_count = 0

        for line in output.splitlines():
            # Block boundary — "Tasks:" appears once per top iteration
            if line.lstrip().startswith('Tasks:'):
                block     += 1
                in_table   = False
                proc_count = 0
                continue

            # Aggregate CPU line (no -1): "%Cpu(s):  2.5 us, ..., 95.8 id, ..."
            if line.startswith('%Cpu(s)') and block == 2:
                m = re.search(r'(\d+\.?\d*)\s*id', line)
                if m:
                    result['Overall CPU'] = MetricDataPoint(
                        value=round(100.0 - float(m.group(1)), 2)
                    )
                continue

            # Process table header
            if 'PID' in line and '%CPU' in line:
                in_table = True
                continue

            # Parse process rows from the second block only
            # Columns: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
            if in_table and block == 2 and proc_count < self.top_n:
                parts = line.split(None, 11)
                if len(parts) < 12:
                    continue
                try:
                    result[f'proc/{parts[0]}'] = MetricDataPoint(
                        value=round(float(parts[8]) / self.core_count, 2),
                        meta={
                            'Command':  parts[11],
                            'User':     parts[1],
                            'Mem':      f'{float(parts[9]):.1f}%',
                            'RSS':      human_readable(int(parts[5]) * 1024, precision=0),
                            'Stat':     parts[7],
                            'CPU Time': parts[10],
                        },
                    )
                    proc_count += 1
                except (ValueError, IndexError):
                    continue

        return result


class MemParser(MetricParser):
    """
    Parse memory usage % from `free -b` output.

    Reads the 'Mem:' line and computes used/total as a percentage.
    """
    y_title   = 'Memory'
    unit      = '%'
    command   = 'free -b'
    tab       = 'memory'
    tab_label = 'Memory'
    chart     = 'Memory Usage'

    def parse(self, output: str) -> dict[str, MetricDataPoint]:
        for line in output.splitlines():
            if line.lower().startswith('mem:'):
                parts = line.split()
                # free -b: Mem: total used free shared buff/cache available
                if len(parts) >= 3:
                    try:
                        total = float(parts[1])
                        used  = float(parts[2])
                        if total > 0:
                            meta = {'Used': human_readable(used), 'Total': human_readable(total)}
                            return {self.chart: MetricDataPoint(
                                value=round(used / total * 100.0, 2),
                                meta=meta,
                            )}
                    except ValueError:
                        pass
        return {}


class DiskParser(MetricParser):
    """
    Parse root filesystem usage % from `df -h /` output.

    Reads the data row (second line) and extracts the Use% column.
    parse_meta() returns the already human-readable Size/Used strings from df -h.
    """
    y_title   = 'Disk'
    unit      = '%'
    command   = 'df -h'
    tab       = 'disk'
    tab_label = 'Disk'
    chart     = 'Disk Usage'

    _regex = re.compile(
        r"\s+"
        rf'(?P<Total>{_mem_size_re_str})\s+'
        rf'(?P<Used>{_mem_size_re_str})\s+'
        rf'(?P<Available>{_mem_size_re_str})\s+'
        rf'(?P<used_percent>{_float_re_str})%\s+'
        rf'(?P<Mount>\S+)'
    )

    def parse(self, output: str) -> dict[str, MetricDataPoint]:
        lines = [l for l in output.splitlines() if l.strip()]
        # Typical output (df -h):
        #   Filesystem      Size  Used Avail Use% Mounted on
        #   /dev/sda1        20G  5.1G   14G  27% /

        series: dict[str, MetricDataPoint] = {}

        for line in lines[1:]:
            m = self._regex.search(line)
            if m:
                meta = m.groupdict()
                series[m['Mount']] = MetricDataPoint(value=float(m['used_percent']), meta=meta)

        return series


class LoadParser(MetricParser):
    """
    Parse all three load averages from `cat /proc/loadavg`.

    Output format: '0.52 0.58 0.59 1/432 12345'
    Returns 1-minute, 5-minute, and 15-minute load averages as separate series.
    """
    y_title   = 'Load'
    unit      = ''
    command   = 'cat /proc/loadavg'
    tab       = 'cpu'
    tab_label = 'CPU'
    chart     = 'Load'

    def parse(self, output: str) -> dict[str, MetricDataPoint]:
        parts = output.strip().split()
        try:
            return {
                'Load (1m)':  MetricDataPoint(value=float(parts[0])),
                'Load (5m)':  MetricDataPoint(value=float(parts[1])),
                'Load (15m)': MetricDataPoint(value=float(parts[2])),
            }
        except (IndexError, ValueError):
            return {}


# ---------------------------------------------------------------------------
# Default parser registry — maps command string → parser instance
# ---------------------------------------------------------------------------

DEFAULT_PARSERS: dict[str, MetricParser] = {
    p.command: p
    for p in [TopCpuParser(), MemParser(), DiskParser(), LoadParser()]
}


# ---------------------------------------------------------------------------
# Per-host parser registry
# ---------------------------------------------------------------------------

_host_parser_registry: dict[str, dict[str, 'MetricParser']] = {}


def register_host_parsers(host_id: str, parsers: dict[str, 'MetricParser']) -> None:
    """Associate a custom parser dict with a host ID.

    Call this from an init module (listed in ``.otto/settings.toml``) to override
    or extend the default parsers for a specific host.  The *host_id* string must
    match the ID used to look up the host (i.e. the key in ``lab.hosts``).

    Hosts with no registered parsers automatically fall back to DEFAULT_PARSERS.
    """
    _host_parser_registry[host_id] = parsers


def get_host_parsers(host_id: str) -> dict[str, 'MetricParser']:
    """Return the parser dict registered for *host_id*, or a copy of DEFAULT_PARSERS."""
    return copy.deepcopy(_host_parser_registry.get(host_id, DEFAULT_PARSERS))


