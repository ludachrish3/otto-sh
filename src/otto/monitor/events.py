"""
MonitorEvent — a timestamped, annotated marker for the dashboard timeline.

Events appear as vertical lines on all metric charts. They can be created by:
  - The dashboard UI (user types a label and clicks "Mark Event")
  - Test suite code via OttoSuite.addMonitorEvent()
  - Automatically by OttoSuite.setUp() / tearDown() when monitoring is active
"""

from dataclasses import dataclass, field
from datetime import datetime


VALID_DASH_STYLES = frozenset({
    'solid', 'dot', 'dash', 'longdash', 'dashdot', 'longdashdot'
})

# Default colors for automatic test lifecycle events
AUTO_EVENT_COLORS = {
    'start': '#888888',
    'pass':  '#2ca02c',
    'fail':  '#d62728',
}


@dataclass
class MonitorEvent:
    """A labeled, timestamped marker for the monitoring dashboard."""

    timestamp: datetime
    """When the event occurred."""

    label: str
    """Human-readable description shown on hover in the dashboard."""

    source: str = 'manual'
    """Origin of the event: 'manual' (dashboard UI), 'user_code' (test code), or 'auto' (lifecycle)."""

    color: str = '#888888'
    """CSS color for the vertical marker line (hex, named color, or rgb())."""

    dash: str = 'dash'
    """Plotly line dash style. One of: solid, dot, dash, longdash, dashdot, longdashdot."""

    id: int = 0
    """Unique integer id assigned by MetricCollector on creation (or loaded from SQLite)."""

    end_timestamp: datetime | None = None
    """For span events: when the span ended. None for instantaneous events."""

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict for the dashboard API."""
        return {
            'id':            self.id,
            'timestamp':     self.timestamp.isoformat(),
            'label':         self.label,
            'source':        self.source,
            'color':         self.color,
            'dash':          self.dash,
            'end_timestamp': self.end_timestamp.isoformat() if self.end_timestamp else None,
        }
