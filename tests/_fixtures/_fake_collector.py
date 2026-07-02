"""FakeCollector — scripted stand-in for live collection in dashboard tests.

Subclasses the real :class:`~otto.monitor.collector.MetricCollector` so the
store, chart map, event CRUD, SSE publish, and JSON export paths under test
are the production ones — only the "poll a host" step is replaced by
:meth:`FakeCollector.push`.
"""

from datetime import datetime, timezone
from typing import Any

from typing_extensions import override

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import DEFAULT_PARSERS, MetricDataPoint

# Friendly chart name → DEFAULT_PARSERS key (the dict key IS the shell command).
CHART_COMMANDS: dict[str, str] = {
    "cpu": "top -d 0.5 -bn2",
    "memory": "free -b",
    "disk": "df -h",
    "load": "cat /proc/loadavg",
}


class FakeCollector(MetricCollector):
    """A MetricCollector that never talks to hosts: tests push points directly."""

    def __init__(self, *, force_live: bool = True) -> None:
        # hosts=[] builds zero targets, so the base class leaves the parser
        # and view catalogs empty. Install the production DEFAULT_PARSERS
        # catalog so push() resolves commands and /api/meta serves the real
        # tabs/metrics, exactly as a live single-host collector would.
        super().__init__(hosts=[])
        self._parsers = dict(DEFAULT_PARSERS)
        self._views = list(DEFAULT_PARSERS.values())
        self._force_live = force_live

    @override
    def get_meta(self) -> dict[str, Any]:
        """Production meta, with ``live`` forced (hosts=[] would report historical)."""
        meta = super().get_meta()
        meta["live"] = self._force_live
        return meta

    async def push(
        self,
        host: str,
        label: str,
        value: float,
        *,
        chart: str = "cpu",
        meta: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Record one point exactly as a live tick would (store + SSE publish)."""
        view = self._parsers[CHART_COMMANDS[chart]]
        await self._record_point(
            host,
            ts or datetime.now(tz=timezone.utc),
            label,
            MetricDataPoint(value=value, meta=meta),
            view,
        )
