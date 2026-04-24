"""
MetricCollector — collects metrics from multiple RemoteHosts via asyncio.gather().

On each tick all hosts are polled simultaneously so results share one timestamp.

Supports three data sources:
  1. Live collection from multiple RemoteHosts (asyncio.gather() per tick)
  2. Historical JSON files
  3. Historical SQLite databases (written by a previous live collection)
"""

import asyncio
import copy
import fcntl
import json
import logging
import os
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

import aiosqlite

from .events import MonitorEvent
from .parsers import DEFAULT_PARSERS, MetricParser

from ..host.host import RunResult

if TYPE_CHECKING:
    from ..host.remoteHost import RemoteHost
    from ..utils import CommandStatus, Status

logger = logging.getLogger('otto')


@dataclass
class MonitorTarget:
    """Pairs a RemoteHost with the parser dict to use when collecting from it.

    By default all hosts use DEFAULT_PARSERS.  Pass a custom dict to add new
    metrics or override built-in commands for a specific host::

        MonitorTarget(
            host=gpu_host,
            parsers={
                **DEFAULT_PARSERS,
                'nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits': NvidiaGpuParser(),
            },
        )

    In most cases you do not construct these directly — use
    :func:`~otto.monitor.parsers.register_host_parsers` from an init module and
    let the CLI build targets automatically.
    """

    host:       'RemoteHost'
    parsers:    dict[str, MetricParser] = field(default_factory=lambda: copy.deepcopy(DEFAULT_PARSERS))
    core_count: int = field(default=1)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    host      TEXT    NOT NULL DEFAULT '',
    label     TEXT    NOT NULL,
    value     REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    end_ts    TEXT,
    label     TEXT    NOT NULL,
    source    TEXT    NOT NULL DEFAULT 'manual',
    color     TEXT    NOT NULL DEFAULT '#888888',
    dash      TEXT    NOT NULL DEFAULT 'dash'
);
"""


class MetricCollector:
    """
    Collects numeric metrics from multiple RemoteHosts and stores time-series data.

    On each tick, all hosts are polled simultaneously via asyncio.gather() so that
    results from every host share the same timestamp.

    Series keys have the form ``"hostname/metric_label"`` (e.g. ``"router1/CPU %"``).

    Args:
        hosts: Remote hosts to monitor. Pass ``None`` (or omit) when loading historical data.
        parsers: Metric parser instances. Defaults to DEFAULT_PARSERS (cpu, mem, disk, load).
        db_path: Optional SQLite file for persistence. If ``None``, data is in-memory only.
    """

    def __init__(
        self,
        hosts: 'list[RemoteHost] | None' = None,
        parsers: list[MetricParser] | None = None,
        db_path: str | None = None,
        targets: 'list[MonitorTarget] | None' = None,
    ) -> None:
        if targets is not None:
            self._targets = targets
            # Build a unified parser dict (union by command) for metadata endpoints.
            seen_commands: set[str] = set()
            unified: list[MetricParser] = []
            for t in targets:
                for p in t.parsers.values():
                    if p.command not in seen_commands:
                        seen_commands.add(p.command)
                        unified.append(p)
            self._parsers: dict[str, MetricParser] = {p.command: p for p in unified}
        else:
            parser_dict: dict[str, MetricParser] = (
                {p.command: p for p in parsers}
                if parsers is not None
                else dict(DEFAULT_PARSERS)
            )
            self._targets = [MonitorTarget(host=h, parsers=parser_dict) for h in (hosts or [])]
            self._parsers = parser_dict

        self._hosts = [t.host for t in self._targets]
        self._db_path = db_path

        # All series keyed by "hostname/label" — e.g. "router1/CPU %" for regular
        # metrics or "router1/proc/python" for per-process CPU.  Each point is a
        # (timestamp, value, metadata_or_None) triple so that every series shares
        # exactly one data structure regardless of its type.
        # Series are created lazily in _process_host_results() as data arrives,
        # so multi-series parsers (which produce labels not known until parse() runs)
        # are handled naturally without upfront enumeration.
        self._series: dict[str, deque[tuple[datetime, float, dict[str, Any] | None]]] = {}
        self._chart_map: dict[str, str] = {}  # series_label → chart_key

        self._events: list[MonitorEvent] = []
        self._next_event_id: int = 1

        # SSE subscribers: one asyncio.Queue per connected dashboard tab.
        # _publish() uses put_nowait() — safe because collection and the SSE
        # route handlers all run in the same event loop.
        self._subscribers: list['asyncio.Queue[dict[str, Any]]'] = []

        # Persistent async DB connection — opened by init_db(), closed by close_db().
        self._db_conn: aiosqlite.Connection | None = None
        self._lock_fd: int | None = None

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        """Open a persistent aiosqlite connection with WAL mode and file lock.

        Must be awaited before any DB writes.  Called automatically by
        :meth:`run`; callers that skip ``run()`` (e.g. tests) should call
        this explicitly.
        """
        if self._db_conn is not None or not self._db_path:
            return

        # Acquire an exclusive file lock so two live collectors can't
        # write to the same database simultaneously.
        lock_path = self._db_path + '.lock'
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except OSError:
            raise RuntimeError(
                f"Another otto monitor instance is already writing to '{self._db_path}'. "
                "Use a different --db path, or stop the other instance."
            )

        conn = await aiosqlite.connect(self._db_path)
        await conn.execute('PRAGMA journal_mode=WAL')
        await conn.execute('PRAGMA busy_timeout=5000')
        await conn.executescript(_SCHEMA)
        # Migrate: add end_ts column if the events table predates span support
        col_names = {row[1] async for row in await conn.execute('PRAGMA table_info(events)')}
        if 'end_ts' not in col_names:
            await conn.execute('ALTER TABLE events ADD COLUMN end_ts TEXT')
        await conn.commit()
        self._db_conn = conn

    async def close_db(self) -> None:
        """Close the persistent DB connection and release the file lock."""
        if self._db_conn is not None:
            await self._db_conn.close()
            self._db_conn = None
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    async def close(self) -> None:
        """Close the DB connection and all live host sessions.

        Must be awaited before the surrounding event loop exits — otherwise
        subprocess transports owned by LocalSession are GC'd after the loop
        is closed and raise "Event loop is closed" from __del__.
        """
        await asyncio.gather(
            *(t.host.close() for t in self._targets),
            return_exceptions=True,
        )
        await self.close_db()

    async def _db_write_point(self, ts: datetime, host: str, label: str, value: float) -> None:
        if not self._db_conn:
            return
        await self._db_conn.execute(
            'INSERT INTO metrics (ts, host, label, value) VALUES (?, ?, ?, ?)',
            (ts.isoformat(), host, label, value),
        )
        await self._db_conn.commit()

    async def _db_write_event(self, event: MonitorEvent) -> int:
        """Insert event into the DB and return the rowid (0 if no DB configured)."""
        if not self._db_conn:
            return 0
        cursor = await self._db_conn.execute(
            'INSERT INTO events (ts, end_ts, label, source, color, dash) VALUES (?, ?, ?, ?, ?, ?)',
            (
                event.timestamp.isoformat(),
                event.end_timestamp.isoformat() if event.end_timestamp else None,
                event.label, event.source, event.color, event.dash,
            ),
        )
        await self._db_conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def _db_delete_event(self, event_id: int) -> None:
        if not self._db_conn:
            return
        await self._db_conn.execute('DELETE FROM events WHERE id = ?', (event_id,))
        await self._db_conn.commit()

    async def _db_update_event(self, event: MonitorEvent) -> None:
        if not self._db_conn:
            return
        await self._db_conn.execute(
            'UPDATE events SET label = ?, color = ?, dash = ?, end_ts = ? WHERE id = ?',
            (
                event.label, event.color, event.dash,
                event.end_timestamp.isoformat() if event.end_timestamp else None,
                event.id,
            ),
        )
        await self._db_conn.commit()

    # ------------------------------------------------------------------
    # Live collection
    # ------------------------------------------------------------------

    async def _collect_one(
        self,
        target: MonitorTarget,
        timeout: float,
    ) -> 'RunResult | None':
        """Collect metrics from a single host with a cumulative timeout.

        The *timeout* is passed to :meth:`~otto.host.host.BaseHost.run` as
        a deadline-based budget shared across all parser commands.  Each
        command receives the remaining budget so fast commands donate surplus
        time to slower ones.  When a command exceeds its budget,
        ``run``'s own ``wait_for`` fires, triggering Ctrl+C session
        recovery (see :meth:`ShellSession._recover_session`) — the session
        stays healthy for the next tick.
        """
        return await target.host.run(
            list(target.parsers.keys()),
            timeout=timeout,
        )

    async def run(self,
        interval: timedelta = timedelta(seconds=5),
        duration: Optional[timedelta] = None,
    ) -> None:
        """
        Collect metrics from all hosts on each tick.

        Each host's collection is bounded by the interval — if a host does not
        respond in time, it is skipped for that tick and the session is
        recovered automatically.  This prevents a single slow host from
        blocking collection on all other hosts.

        Runs inside the caller's event loop.  Cancel the task wrapping this
        coroutine (or cancel it directly) to stop collection gracefully.

        Args:
            interval: Collection interval as a timedelta.
            duration: Optional total run time.  ``None`` means run forever.
        """
        if not self._targets:
            raise RuntimeError('Cannot start live collection: no hosts provided')

        await self.init_db()

        # One-time setup: determine core count for each host and propagate to
        # any parser that uses it for normalization (e.g. TopCpuParser).
        # grep -c ^processor /proc/cpuinfo is universally available on Linux.
        setup_results = await asyncio.gather(
            *[target.host.run(['grep -c ^processor /proc/cpuinfo']) for target in self._targets],
            return_exceptions=True,
        )
        for target, result in zip(self._targets, setup_results):
            match result:
                case RunResult(statuses=[cmd_status]):
                    try:
                        target.core_count = int(cmd_status.output.strip())
                    except ValueError:
                        pass  # keep default of 1
                case BaseException():
                    logger.warning(
                        'Monitor: could not determine core count for %s, defaulting to 1',
                        target.host.name,
                    )
        for target in self._targets:
            for parser in target.parsers.values():
                parser.core_count = target.core_count

        secs = interval.total_seconds()
        start = datetime.now()

        # Initial collection: no sleep, publishes first data as soon as commands return
        initial_results = await asyncio.gather(
            *[self._collect_one(target, secs) for target in self._targets],
            return_exceptions=True,
        )
        ts = datetime.now()
        for target, result in zip(self._targets, initial_results):
            match result:
                case RunResult(statuses=cmd_statuses):
                    await self._process_host_results(target.host.name, ts, cmd_statuses, target.parsers)
                case BaseException():
                    logger.warning('Monitor: error collecting from %s: %s', target.host.name, result)
                case _:
                    continue

        # Sleep is first so results[0] is the sleep and results[1:] are host results
        while duration is None or datetime.now() - start < duration:
            results = await asyncio.gather(
                asyncio.sleep(secs),
                *[self._collect_one(target, secs) for target in self._targets],
                return_exceptions=True,
            )
            ts = datetime.now()

            for target, result in zip(self._targets, results[1:]):
                match result:
                    case RunResult(statuses=cmd_statuses):
                        await self._process_host_results(target.host.name, ts, cmd_statuses, target.parsers)

                    case BaseException():
                        logger.warning('Monitor: error collecting from %s: %s', target.host.name, result)
                        continue

                    case _:
                        continue


    async def _process_host_results(
        self,
        host_name: str,
        ts: datetime,
        cmd_statuses: 'list[CommandStatus]',
        parsers: dict[str, MetricParser],
    ) -> None:
        for cmd_status in cmd_statuses:
            parser = parsers.get(cmd_status.command)
            if parser is not None:
                points = parser.parse(cmd_status.output)
                if not points:
                    continue
                parser_chart = parser.chart
                for label, dp in points.items():
                    key = f'{host_name}/{label}'
                    if key not in self._series:
                        self._series[key] = deque()
                    self._series[key].append((ts, dp.value, dp.meta))
                    self._chart_map[label] = parser_chart
                    await self._db_write_point(ts, host_name, label, dp.value)
                    msg: dict[str, Any] = {
                        'type':    'metric',
                        'host':    host_name,
                        'label':   label,
                        'chart':   parser_chart,
                        'y_title': parser.y_title,
                        'unit':    parser.unit,
                        'key':     key,
                        'ts':      ts.isoformat(),
                        'value':   dp.value,
                    }
                    if dp.meta is not None:
                        msg['meta'] = dp.meta
                    self._publish(msg)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def add_event(
        self,
        label: str,
        timestamp: datetime | None = None,
        color: str = '#888888',
        dash: str = 'dash',
        source: str = 'manual',
        end_timestamp: datetime | None = None,
    ) -> MonitorEvent:
        """Record a labeled event and push it to all dashboard subscribers."""
        event = MonitorEvent(
            timestamp=timestamp or datetime.now(),
            label=label,
            source=source,
            color=color,
            dash=dash,
            end_timestamp=end_timestamp,
        )
        rowid = await self._db_write_event(event)
        event.id = rowid if rowid else self._next_event_id
        self._next_event_id += 1
        self._events.append(event)
        self._publish({'type': 'event', **event.to_dict()})
        return event

    async def delete_event(self, event_id: int) -> bool:
        """Remove an event by id. Returns True if found and removed, False otherwise."""
        for i, ev in enumerate(self._events):
            if ev.id == event_id:
                self._events.pop(i)
                await self._db_delete_event(event_id)
                self._publish({'type': 'event_deleted', 'id': event_id})
                return True
        return False

    async def update_event(
        self,
        event_id: int,
        label: str,
        color: str,
        dash: str,
        end_timestamp: datetime | None = None,
    ) -> 'MonitorEvent | None':
        """Update an existing event's label, color, dash, and end_timestamp. Returns the updated event or None."""
        for ev in self._events:
            if ev.id == event_id:
                ev.label         = label
                ev.color         = color
                ev.dash          = dash
                ev.end_timestamp = end_timestamp
                await self._db_update_event(ev)
                self._publish({'type': 'event_updated', **ev.to_dict()})
                return ev
        return None

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_series(self) -> dict[str, list[tuple[datetime, float, dict[str, Any] | None]]]:
        """Return a snapshot of all series (metrics and per-process).

        Format: ``{"hostname/label": [(ts, value, meta), ...]}``
        """
        return {key: list(pts) for key, pts in self._series.items()}

    def get_chart_map(self) -> dict[str, str]:
        """Return a mapping of series label → chart group key.

        Used by the dashboard to assign historical series (loaded from ``/api/data``)
        to their correct Plotly chart containers without requiring ``series_labels()``
        on each parser.  The map is built lazily as data arrives in
        ``_process_host_results()``.
        """
        return dict(self._chart_map)

    def get_events(self) -> list[MonitorEvent]:
        """Return all recorded events in chronological order."""
        return list(self._events)

    def get_meta(self) -> dict[str, Any]:
        """Return metadata for the dashboard (host names, metric labels/units, tabs)."""
        # Derive host names from series keys (all series, including proc)
        hosts = sorted({
            key.split('/')[0] for key in self._series
            if '/' in key
        })
        # Fall back to the list of live hosts if no data has arrived yet
        if not hosts and self._hosts:
            hosts = [h.name for h in self._hosts]

        # Build ordered tabs list from parsers, preserving first-encountered tab order
        tabs_map: dict[str, dict[str, Any]] = {}
        for p in self._parsers.values():
            tab_id    = getattr(p, 'tab',       'metrics')
            tab_label = getattr(p, 'tab_label', 'Metrics')
            if tab_id not in tabs_map:
                tabs_map[tab_id] = {'id': tab_id, 'label': tab_label, 'metrics': []}
            tabs_map[tab_id]['metrics'].append(p.chart)

        result: dict[str, Any] = {
            'hosts': hosts,
            'live':  bool(self._hosts),   # False when loaded from --file (no live collection)
            'metrics': [
                {
                    'label': p.chart, 'y_title': p.y_title, 'unit': p.unit, 'command': p.command,
                    'chart': p.chart,
                }
                for p in self._parsers.values()
            ],
            'tabs': list(tabs_map.values()),
        }
        return result

    # ------------------------------------------------------------------
    # SSE pub/sub
    # ------------------------------------------------------------------

    def subscribe(self) -> 'asyncio.Queue[dict[str, Any]]':
        """Register a new SSE subscriber and return its queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: 'asyncio.Queue[dict[str, Any]]') -> None:
        self._subscribers = [sq for sq in self._subscribers if sq is not q]

    def _publish(self, payload: dict[str, Any]) -> None:
        """Push a JSON-safe dict to all SSE subscriber queues."""
        for q in list(self._subscribers):
            q.put_nowait(payload)

    # ------------------------------------------------------------------
    # Historical data loaders (class methods)
    # ------------------------------------------------------------------

    @classmethod
    def from_json(
        cls,
        path: str,
        parsers: list[MetricParser] | None = None,
    ) -> 'MetricCollector':
        """
        Load historical metrics from a JSON file.

        Expected format::

            {
              "metrics": [{"timestamp": "...", "host": "...", "label": "...", "value": 42.0}, ...],
              "events":  [{"timestamp": "...", "label": "...", "source": "...",
                           "color": "...", "dash": "..."}, ...]
            }

        The ``host`` field is optional for backward compatibility.
        """
        collector = cls(hosts=[], parsers=parsers)
        with open(path) as f:
            data = json.load(f)
        for point in data.get('metrics', []):
            try:
                ts    = datetime.fromisoformat(point['timestamp'])
                host  = point.get('host', '')
                label = point['label']
                value = float(point['value'])
                key   = f'{host}/{label}' if host else label
                meta  = point.get('meta') or None
                if key not in collector._series:
                    collector._series[key] = deque()
                collector._series[key].append((ts, value, meta))
            except (KeyError, ValueError):
                continue
        for label, chart in data.get('chart_map', {}).items():
            collector._chart_map[label] = chart
        for ev in data.get('events', []):
            try:
                end_ts_str = ev.get('end_timestamp')
                event = MonitorEvent(
                    timestamp=datetime.fromisoformat(ev['timestamp']),
                    label=ev.get('label', ''),
                    source=ev.get('source', 'manual'),
                    color=ev.get('color', '#888888'),
                    dash=ev.get('dash', 'dash'),
                    id=ev.get('id', collector._next_event_id),
                    end_timestamp=datetime.fromisoformat(end_ts_str) if end_ts_str else None,
                )
                collector._next_event_id = max(collector._next_event_id, event.id) + 1
                collector._events.append(event)
            except (KeyError, ValueError):
                continue
        return collector

    @classmethod
    async def from_sqlite(
        cls,
        path: str,
        parsers: list[MetricParser] | None = None,
    ) -> 'MetricCollector':
        """Load historical metrics and events from a SQLite database."""
        collector = cls(hosts=[], parsers=parsers)
        async with aiosqlite.connect(path) as conn:
            conn.row_factory = aiosqlite.Row
            # Support both the new schema (with host column) and the old schema (without)
            col_names = {row[1] async for row in await conn.execute('PRAGMA table_info(metrics)')}
            has_host  = 'host' in col_names
            query = (
                'SELECT ts, host, label, value FROM metrics ORDER BY ts'
                if has_host else
                'SELECT ts, label, value FROM metrics ORDER BY ts'
            )
            async for row in await conn.execute(query):
                try:
                    ts    = datetime.fromisoformat(row['ts'])
                    host  = row['host'] if has_host else ''
                    label = row['label']
                    value = float(row['value'])
                    key   = f'{host}/{label}' if host else label
                    if key not in collector._series:
                        collector._series[key] = deque()
                    collector._series[key].append((ts, value, None))
                except (KeyError, ValueError):
                    continue
            event_cols = {row[1] async for row in await conn.execute('PRAGMA table_info(events)')}
            has_end_ts = 'end_ts' in event_cols
            events_query = (
                'SELECT id, ts, end_ts, label, source, color, dash FROM events ORDER BY ts'
                if has_end_ts else
                'SELECT id, ts, label, source, color, dash FROM events ORDER BY ts'
            )
            async for row in await conn.execute(events_query):
                try:
                    end_ts_val = row['end_ts'] if has_end_ts else None
                    event = MonitorEvent(
                        timestamp=datetime.fromisoformat(row['ts']),
                        label=row['label'],
                        source=row['source'],
                        color=row['color'],
                        dash=row['dash'],
                        id=row['id'],
                        end_timestamp=datetime.fromisoformat(end_ts_val) if end_ts_val else None,
                    )
                    collector._events.append(event)
                except (KeyError, ValueError):
                    continue
            if collector._events:
                collector._next_event_id = max(e.id for e in collector._events) + 1
        return collector

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_json(self, path: str) -> None:
        """Export all collected data to a JSON file."""
        with open(path, 'w') as f:
            f.write(self.to_json())

    def to_json(self) -> str:
        """Serialize all metrics and events to a JSON string compatible with ``--file``."""
        metrics: list[dict[str, Any]] = []
        for key, pts in self._series.items():
            host  = key.split('/')[0] if '/' in key else ''
            label = key.split('/', 1)[1] if '/' in key else key
            for ts, value, meta in pts:
                record: dict[str, Any] = {
                    'timestamp': ts.isoformat(),
                    'host':  host,
                    'label': label,
                    'value': value,
                }
                if meta:
                    record['meta'] = meta
                metrics.append(record)
        return json.dumps(
            {
                'metrics':   metrics,
                'events':    [e.to_dict() for e in self._events],
                'chart_map': dict(self._chart_map),
            },
            indent=2,
        )

