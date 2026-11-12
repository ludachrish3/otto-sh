"""MetricStore — the monitor's in-memory time-series and event state.

Series are keyed ``"hostname/label"`` (bare ``label`` for historical imports
without a host column). Extracted from MetricCollector; the event-id counter
stays ahead of every assigned id (see test_store.py).
"""

from collections import deque

from ..models.monitor import MetricPoint
from .event_ops import EventIdCounter
from .events import MonitorEvent
from .parsers import LogEvent

# Rows kept in memory per (host, tab) table — the DB keeps everything.
_LOG_RING_MAX = 1000

# Points kept in memory per series — the DB keeps everything, so only the
# LIVE view and live exports age out (a ~27h horizon at 2 s ticks); a review
# opened from the DB archive is always complete. Memory backstop for
# multi-day live runs, which previously grew without bound.
_SERIES_POINTS_MAX = 50_000


class MetricStore:
    """In-memory series, chart map, and events for one collector."""

    def __init__(self) -> None:
        self.series: dict[str, deque[MetricPoint]] = {}
        self.chart_map: dict[str, str] = {}
        self._events: list[MonitorEvent] = []
        self._ids = EventIdCounter()
        self.log_events: dict[tuple[str, str], deque[LogEvent]] = {}

    def append_point(self, key: str, point: MetricPoint, *, label: str, chart: str) -> None:
        """Store one point, creating the series lazily, and record its chart group."""
        if key not in self.series:
            self.series[key] = deque(maxlen=_SERIES_POINTS_MAX)
        self.series[key].append(point)
        self.chart_map[label] = chart

    def append_log_event(self, host: str, tab: str, event: LogEvent) -> None:
        """Store one log-event row in the per-(host, tab) ring (oldest drop at capacity)."""
        ring = self.log_events.setdefault((host, tab), deque(maxlen=_LOG_RING_MAX))
        ring.append(event)

    def snapshot_log_events(self) -> list[tuple[str, str, LogEvent]]:
        """Return every ring's rows as (host, tab, event) triples, insertion-ordered per ring."""
        return [
            (host, tab, event) for (host, tab), ring in self.log_events.items() for event in ring
        ]

    def snapshot_series(self) -> dict[str, list[MetricPoint]]:
        """Return a copy of every series, safe for a caller to mutate."""
        return {key: list(pts) for key, pts in self.series.items()}

    def snapshot_chart_map(self) -> dict[str, str]:
        """Return a copy of the series-label → chart-key map."""
        return dict(self.chart_map)

    def events(self) -> list[MonitorEvent]:
        """Return a copy of all recorded events in insertion order."""
        return list(self._events)

    def find_event(self, event_id: int) -> MonitorEvent | None:
        """Return the event with this id, or ``None`` if it is not tracked."""
        return next((e for e in self._events if e.id == event_id), None)

    def add_event(self, event: MonitorEvent, rowid: int) -> MonitorEvent:
        """Assign the id (DB rowid wins, else the counter) and store; the counter stays ahead."""
        if rowid:
            self._ids.note(rowid)
            event.id = rowid
        else:
            event.id = self._ids.take()
        self._track(event)
        return event

    def note_imported_event(self, event: MonitorEvent) -> None:
        """Track an event loaded from a file, keeping the id counter ahead of it."""
        self._track(event)

    def _track(self, event: MonitorEvent) -> None:
        """Store ``event`` and keep the counter ahead of its id (never lowered)."""
        self._ids.note(event.id)
        self._events.append(event)

    def allocated(self, event_id: int) -> bool:
        """Report whether ``event_id`` was handed out or imported (``EventIdCounter.allocated``)."""
        return self._ids.allocated(event_id)

    def remove_event(self, event_id: int) -> bool:
        """Remove the event with this id. Returns True if found and removed."""
        for i, ev in enumerate(self._events):
            if ev.id == event_id:
                self._events.pop(i)
                return True
        return False

    def hosts_from_series(self) -> list[str]:
        """Return sorted unique hostnames from series keys and log-event rings."""
        hosts = {key.split("/")[0] for key in self.series if "/" in key}
        hosts.update(host for host, _tab in self.log_events)
        return sorted(hosts)
