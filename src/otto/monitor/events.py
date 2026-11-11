"""
MonitorEvent — a timestamped, annotated marker for the dashboard timeline.

Events appear as vertical lines on all metric charts. They can be created by:
  - The dashboard UI (user types a label and clicks "Mark Event")
  - Test code via the ``monitor`` fixture's ``monitor.event(...)``
  - Automatically at each test's start and end when monitoring is active
"""

from dataclasses import dataclass
from datetime import datetime

from typing_extensions import final

from ..models.monitor import (
    AUTO_EVENT_COLORS,
    DEFAULT_EVENT_COLOR,
    DEFAULT_EVENT_DASH,
    check_event_dash,
    normalize_event_color,
)
from ..utils import as_utc
from .event_ops import EventValidationError

__all__ = ["AUTO_EVENT_COLORS", "MonitorEvent"]


@final
@dataclass
class MonitorEvent:
    """A labeled, timestamped marker for the monitoring dashboard.

    Both timestamps are timezone-aware once constructed: a naive value means
    UTC and an existing offset is kept (:func:`otto.utils.as_utc`), applied in
    ``__post_init__`` so the collector's add, a revert's restore and
    ``dataclasses.replace`` all share it. This is what lets a span's two ends
    and a stored event against a write be compared at all.

    The SQLite ``events`` table still orders by the persisted *text* of
    ``ts`` (``ORDER BY ts``), a string sort, not an instant sort: events
    written with different offsets can sort out of instant order there.
    Normalising makes every stored value carry an offset; it does not make
    that sort instant-based.
    """

    timestamp: datetime
    """When the event occurred; naive means UTC."""

    label: str
    """Human-readable description shown on hover in the dashboard."""

    source: str = "manual"
    """Who created this event — ``'manual'`` (dashboard UI), ``'user_code'`` (test code), or ``'auto'`` (lifecycle)."""  # noqa: E501 — long field docstring

    color: str = DEFAULT_EVENT_COLOR
    """Marker colour, ``#rrggbb`` or ``#rgb`` in any case, stored as lowercase
    ``#rrggbb``. Anything else (a CSS name such as ``"red"``, ``rgb()``) raises
    ``EventValidationError`` (a ``ValueError``) at construction."""

    dash: str = DEFAULT_EVENT_DASH
    """Plotly line dash style — one of ``solid``, ``dot``, ``dash``, ``longdash``, ``dashdot``, ``longdashdot``."""  # noqa: E501 — long field docstring

    id: int = 0
    """Unique integer id assigned by MetricCollector on creation (or loaded from SQLite)."""

    end_timestamp: datetime | None = None
    """End timestamp for span events (naive means UTC); ``None`` for instantaneous events."""

    def __post_init__(self) -> None:
        self.timestamp = as_utc(self.timestamp)
        self.end_timestamp = as_utc(self.end_timestamp)
        # Every construction is a write boundary: the collector's add_event,
        # a revert and any direct caller get the same colour/dash contract.
        # Refused as EventValidationError, still a ValueError for library
        # callers, so every HTTP route's existing 422 path answers it.
        try:
            self.color = normalize_event_color(self.color)
            check_event_dash(self.dash)
        except ValueError as err:
            raise EventValidationError(str(err)) from err

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict for the dashboard API."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "label": self.label,
            "source": self.source,
            "color": self.color,
            "dash": self.dash,
            "end_timestamp": self.end_timestamp.isoformat() if self.end_timestamp else None,
        }
