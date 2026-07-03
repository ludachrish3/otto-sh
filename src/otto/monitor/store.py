"""MetricStore — the monitor's in-memory time-series and event state.

Series are keyed ``"hostname/label"`` (bare ``label`` for historical imports
without a host column). Extracted from MetricCollector; the id-assignment
quirk in add_event is deliberately preserved (see test_store.py).
"""

from collections import deque

from ..models import MetricPoint
from .events import MonitorEvent


class MetricStore:
    """In-memory series, chart map, and events for one collector."""

    def __init__(self) -> None:
        self.series: dict[str, deque[MetricPoint]] = {}
        self.chart_map: dict[str, str] = {}
        self._events: list[MonitorEvent] = []
        self._next_event_id: int = 1

    def append_point(self, key: str, point: MetricPoint, *, label: str, chart: str) -> None:
        """Store one point, creating the series lazily, and record its chart group."""
        if key not in self.series:
            self.series[key] = deque()
        self.series[key].append(point)
        self.chart_map[label] = chart

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
        """Assign the id (DB rowid wins; the counter advances regardless) and store."""
        event.id = rowid or self._next_event_id
        self._next_event_id += 1
        self._events.append(event)
        return event

    def note_imported_event(self, event: MonitorEvent) -> None:
        """Track an event loaded from a file, keeping the id counter ahead of it."""
        self._next_event_id = max(self._next_event_id, event.id) + 1
        self._events.append(event)

    def remove_event(self, event_id: int) -> bool:
        """Remove the event with this id. Returns True if found and removed."""
        for i, ev in enumerate(self._events):
            if ev.id == event_id:
                self._events.pop(i)
                return True
        return False

    def hosts_from_series(self) -> list[str]:
        """Return sorted unique hostnames derived from ``"host/label"`` series keys."""
        return sorted({key.split("/")[0] for key in self.series if "/" in key})
