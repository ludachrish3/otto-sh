"""Log-sourced metric parsers — data-carried timestamps from files on the host.

Some systems don't expose live values: a cron job digests performance numbers
into a timestamped CSV file every few minutes, or the interesting record is a
log file's event stream. Both ride the existing shell acquisition path via
:meth:`~otto.monitor.parsers.MetricParser.parse_tick` — the command IS the
reduction step (``cat``/``tail``/``awk``/``grep``/``jq`` on the host ships
back only the lines otto needs; the design assumes source data is always
textually reducible on the host).

Register instances exactly like any other parser (one instance per file;
distinct commands are distinct registry keys)::

    from otto.monitor.log_sourced import CsvMetricParser
    from otto.monitor.parsers import register_parsers

    register_parsers(
        [
            CsvMetricParser(
                "cat /var/log/perf/net.csv",
                columns=["rx_kbps", "tx_kbps"],
                chart="Cron net digest",
                tab="network",
                tab_label="Network",
                unit="kb/s",
                interval=60,
            ),
        ]
    )

Timestamp convention: naive values are treated as UTC.

This module is never imported by otto's eager import chain (import-budget
guard) — import it explicitly from init modules or test code.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from typing_extensions import override

from .parsers import (
    LogEvent,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    TickResult,
    TimedSample,
)

T = TypeVar("T")


def _as_utc(dt: datetime) -> datetime:
    """Apply the naive-means-UTC convention."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ProvisionalTail:
    """Hold back the final line of each read until a later read confirms it.

    The shell transport strips trailing newlines before parsers see output,
    so a torn (mid-write) final line is indistinguishable from a complete
    one by text alone — and a torn line can still parse, which would poison
    the high-water mark and persist a wrong row. Unless the output still
    carries its final newline (then the last line is provably complete),
    the final line is provisional: it emits once a subsequent read shows it
    unchanged, with or without newer lines after it. Worst-case latency for
    the newest row is one poll interval; a torn line never emits (its
    completed form replaces it and emits after stabilizing in turn).
    """

    def __init__(self) -> None:
        self._pending: str | None = None

    def lines(self, output: str) -> list[str]:
        """Split *output* into lines, holding back an unconfirmed final line."""
        lines = output.splitlines()
        if not lines or output.endswith("\n"):
            self._pending = None
            return lines
        if lines[-1] != self._pending:
            # First sighting of this final line: hold it for one read.
            self._pending = lines[-1]
            del lines[-1]
        return lines


def parse_timestamp(text: str, fmt: str = "auto") -> datetime | None:
    """Parse a data-carried timestamp; ``None`` (skip the row) when it doesn't parse.

    ``fmt``:

    - ``"auto"``: epoch seconds, else ISO-8601 (the CSV first-column convention);
    - ``"epoch"``: Unix epoch seconds (int or float);
    - ``"iso"``: ISO-8601 (a ``Z`` suffix is accepted on Python 3.10);
    - anything else: a ``strptime`` format. For a format without a year
      directive (classic syslog), the current UTC year is injected before
      parsing; if that lands the result more than 2 days in the future
      (a "Dec 31" line read just after New Year), one year is subtracted.

    Naive results are treated as UTC in every mode.
    """
    text = text.strip()
    if fmt in ("auto", "epoch"):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            if fmt == "epoch":
                return None
    if fmt in ("auto", "iso"):
        try:
            return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    injected = False
    if "%Y" not in fmt and "%y" not in fmt:
        # Year-less formats (classic syslog): inject the current UTC year
        # BEFORE parsing — strptime defaults to 1900, a non-leap year, which
        # would reject Feb 29 rows outright instead of reaching a fix-up.
        text = f"{datetime.now(tz=timezone.utc).year} {text}"
        fmt = f"%Y {fmt}"
        injected = True
    try:
        parsed = _as_utc(datetime.strptime(text, fmt))  # noqa: DTZ007 — naive-means-UTC applied by _as_utc
    except (ValueError, re.error):
        return None
    if injected and parsed - datetime.now(tz=timezone.utc) > timedelta(days=2):
        # Rollover guard: a "Dec 31" line read just after New Year would
        # otherwise land ~a year in the future and wedge the high-water mark.
        try:
            parsed = parsed.replace(year=parsed.year - 1)
        except ValueError:
            return None  # Feb 29 with no such day the year before
    return parsed


class HighWaterMark:
    """Timestamp high-water mark: makes re-reads of rolling files idempotent.

    Tracks the newest emitted row timestamp per parser instance (parser
    instances are per-target deep copies, so state never leaks across
    hosts). Rows at or below the mark were already emitted on a previous
    tick and are dropped; survivors come back sorted ascending and the mark
    advances to the newest survivor. Keyed on row timestamps, not file
    offsets, so log rotation/truncation needs no special handling — new
    rows are still newer.

    The final line of each read is held back by the parser BEFORE this
    filter (see :class:`ProvisionalTail`) until a later read confirms it
    unchanged, so a torn (mid-write) last line never reaches the mark —
    only its stabilized, completed form does. Boundary rule: a new row
    bearing exactly the mark's timestamp is dropped as already-seen
    (accepted trade-off; real sources have per-row-unique or sub-second
    timestamps).
    """

    def __init__(self) -> None:
        self._mark: datetime | None = None

    def advance(self, rows: Iterable[tuple[datetime, T]]) -> list[tuple[datetime, T]]:
        """Return only the rows newer than the mark, ascending; advance the mark."""
        fresh = sorted(
            (row for row in rows if self._mark is None or row[0] > self._mark),
            key=lambda row: row[0],
        )
        if fresh:
            self._mark = fresh[-1][0]
        return fresh


class CsvMetricParser(MetricParser):
    """Chart metrics from a cron-digested CSV file read over the shell.

    Line format: first column an ISO-8601 or epoch-seconds timestamp (naive
    = UTC), remaining columns numeric values matching *columns* (the series
    labels), comma-separated. Header, torn, and otherwise malformed lines
    are skipped — a mid-write read self-heals next tick because the
    high-water mark never passes a skipped line. Points carry their
    data-carried timestamps, so a file holding the last hour backfills the
    dashboard and DB with an hour of real history on monitor start.

    One instance per file: the command string is the parser registry key
    ("a couple of CSV files" = two registered instances). Register via
    :func:`~otto.monitor.parsers.register_parsers` or
    :func:`~otto.monitor.parsers.register_host_parsers`.

    Args:
        command: Shell command printing the CSV content (e.g. ``cat /var/log/perf/net.csv``).
        columns: Series label per value column, in file order (timestamp column excluded).
        chart: Chart group id the series render on (one chart per parser).
        tab: Dashboard tab id.
        tab_label: Human-readable tab button label.
        y_title: Y-axis title shown left of the chart.
        unit: Unit suffix for chart annotations.
        interval: Poll cadence override in seconds (e.g. 60 for a file written every 5 minutes).
    """

    def __init__(
        self,
        command: str,
        columns: Sequence[str],
        *,
        chart: str,
        tab: str = "metrics",
        tab_label: str = "Metrics",
        y_title: str = "",
        unit: str = "",
        interval: float | None = None,
    ) -> None:
        if not columns:
            raise ValueError("CsvMetricParser needs at least one value column")
        self.command = command
        self.chart = chart
        self.tab = tab
        self.tab_label = tab_label
        self.y_title = y_title
        self.unit = unit
        self.interval = interval
        self._columns = list(columns)
        self._hwm = HighWaterMark()
        self._tail = ProvisionalTail()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        """Unused — this parser produces timed samples via :meth:`parse_tick`."""
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        rows: list[tuple[datetime, dict[str, MetricDataPoint]]] = []
        for line in self._tail.lines(output):
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != len(self._columns) + 1:
                continue  # header/torn/partial line — self-heals next tick
            ts = parse_timestamp(parts[0])
            if ts is None:
                continue  # header line: first column isn't a timestamp
            try:
                values = [float(part) for part in parts[1:]]
            except ValueError:
                continue
            series = {
                label: MetricDataPoint(value)
                for label, value in zip(self._columns, values, strict=True)
            }
            rows.append((ts, series))
        fresh = self._hwm.advance(rows)
        return TickResult(
            samples=[TimedSample(ts=ts, series=series) for ts, series in fresh],
            events=[],
        )


class RegexLogEventParser(MetricParser):
    r"""Columnar log-event rows from a log file read over the shell.

    Each line matching *pattern* becomes a table row: the named groups
    become the columns (``table_columns``, in pattern order), except
    *ts_group*, which carries the row timestamp. Non-matching lines are
    skipped — a wrong pattern therefore produces zero rows ever, which the
    collector's silent-parser warning surfaces by tick 3. Re-reads of the
    ``tail -n N`` window dedup on the row-timestamp high-water mark, so an
    append-only log of any size fits: the window bounds every read, the
    mark discards overlap.

    This parser contributes a ``kind="table"`` dashboard tab (its own —
    table parsers must not share a tab id with chart parsers) and no chart.

    Args:
        command: Shell command printing the log window (e.g. ``tail -n 200 /var/log/syslog``).
        pattern: Line regex with named groups; ``search``\ ed per line.
        tab: Dashboard tab id for the table.
        tab_label: Human-readable tab button label.
        ts_group: Name of the group holding the timestamp.
        ts_format: ``"iso"``, ``"epoch"``, or a ``strptime`` format (see :func:`parse_timestamp`).
        interval: Poll cadence override in seconds.
    """

    def __init__(
        self,
        command: str,
        pattern: "str | re.Pattern[str]",
        *,
        tab: str,
        tab_label: str,
        ts_group: str = "ts",
        ts_format: str = "iso",
        interval: float | None = None,
    ) -> None:
        self.command = command
        self._pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        if ts_group not in self._pattern.groupindex:
            raise ValueError(f"pattern has no named group {ts_group!r} for the timestamp")
        # Narrowed alias: the base class declares table_columns as
        # `list[str] | None`; iterating through self._columns keeps the
        # non-None type visible to the type checker.
        self._columns = [g for g in self._pattern.groupindex if g != ts_group]
        if not self._columns:
            raise ValueError("pattern needs at least one named group besides the timestamp")
        self.table_columns = self._columns
        self._ts_group = ts_group
        self._ts_format = ts_format
        self.tab = tab
        self.tab_label = tab_label
        self.chart = tab_label  # never charted; names the parser in health warnings
        self.y_title = ""
        self.unit = ""
        self.interval = interval
        self._hwm = HighWaterMark()
        self._tail = ProvisionalTail()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        """Unused — this parser produces log events via :meth:`parse_tick`."""
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        rows: list[tuple[datetime, dict[str, str]]] = []
        for line in self._tail.lines(output):
            m = self._pattern.search(line)
            if m is None:
                continue
            ts = parse_timestamp(m.group(self._ts_group) or "", self._ts_format)
            if ts is None:
                continue
            rows.append((ts, {g: m.group(g) or "" for g in self._columns}))
        fresh = self._hwm.advance(rows)
        return TickResult(
            samples=[], events=[LogEvent(ts=ts, fields=fields) for ts, fields in fresh]
        )
