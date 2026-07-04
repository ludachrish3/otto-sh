"""Counter->rate conversion shared by shell parsers and the SNMP path.

Monotonic counters (network bytes, disk sectors, SNMP Counter32) become
per-second rates here. One rule for both channels: a negative delta means the
counter reset (device reboot) or wrapped — return ``None``, re-baseline, and
emit no point for that tick. Rates divide by *actual elapsed time* between
samples, never the nominal interval, so they are correct at any cadence and
across missed ticks.
"""

from datetime import datetime


def compute_rate(prev_value: float, cur_value: float, dt: float) -> float | None:
    """Per-second rate over one sample interval, or ``None`` when undefined.

    ``None`` on a non-positive ``dt`` (clock anomaly / duplicate tick) or a
    negative delta (counter reset or wrap — reboots are common on test beds,
    wraps are rare, and wrap-compensation would turn every reboot into one
    absurd spike; losing one tick is the better trade).
    """
    if dt <= 0:
        return None
    delta = cur_value - prev_value
    if delta < 0:
        return None
    return delta / dt


class RateTracker:
    """Per-key previous-sample state for counter->rate conversion.

    Shell rate parsers hold one as instance state (parser instances are
    per-target deep copies, so state never leaks across hosts); the SNMP path
    holds one per :class:`~otto.monitor.snmp.SnmpSource`.
    """

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, datetime]] = {}

    def update(self, key: str, value: float, ts: datetime) -> float | None:
        """Record ``(value, ts)`` for ``key`` and return the rate since the previous sample.

        Returns ``None`` on first sighting / reset (see :func:`compute_rate`).
        """
        prev = self._prev.get(key)
        self._prev[key] = (value, ts)
        if prev is None:
            return None
        prev_value, prev_ts = prev
        return compute_rate(prev_value, value, (ts - prev_ts).total_seconds())

    def prune(self, active: set[str]) -> None:
        """Drop state for keys not in ``active`` (e.g. a vanished interface)."""
        for key in list(self._prev):
            if key not in active:
                del self._prev[key]
