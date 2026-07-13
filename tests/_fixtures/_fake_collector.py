"""FakeCollector — scripted stand-in for live collection in dashboard tests.

Subclasses the real :class:`~otto.monitor.collector.MetricCollector` so the
store, chart map, event CRUD, SSE publish, and JSON export paths under test
are the production ones — only the "poll a host" step is replaced by
:meth:`FakeCollector.push`.
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from typing_extensions import override

from otto.models.monitor import MonitorMeta
from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import LogEvent, MetricDataPoint, MetricParser, default_catalog

# Friendly chart name → DEFAULT_PARSERS key (the dict key IS the shell command).
CHART_COMMANDS: dict[str, str] = {
    "cpu": "top -d 0.5 -bn2",
    "memory": "free -b",
    "disk": "df -h",
    "load": "cat /proc/loadavg",
}


class FakeCollector(MetricCollector):
    """A MetricCollector that never talks to hosts: tests push points directly.

    ``hosts=[]`` builds zero targets, so the base class installs the
    production DEFAULT_PARSERS catalog itself (a targetless collector still
    declares its parser/view catalog — see MetricCollector.__init__) — push()
    resolves commands and get_meta_model() serves the real tabs/metrics,
    exactly as a live single-host collector would.
    """

    def __init__(
        self,
        *,
        force_live: bool = True,
        extra_parsers: "Sequence[MetricParser] | None" = None,
        interval: float | None = None,
    ) -> None:
        parsers = [*default_catalog().values(), *extra_parsers] if extra_parsers else None
        super().__init__(hosts=[], parsers=parsers)
        self._force_live = force_live
        if interval is not None:
            # Stand in for what MetricCollector.run() stamps as its very first
            # statement (collector.py) before a real live run's collection loop
            # starts — a scripted collector that only ever calls push() never
            # reaches that line, so get_meta_model().interval (and so
            # session.meta.interval on the wire) stays None *permanently*
            # (test_meta_interval_is_none_before_run pins exactly that as the
            # DEFAULT — hence this stays opt-in, not a new default). Live
            # dashboard specs (Plan 5b Task 13) need a real interval: the
            # OverviewPage liveness clock only ticks when
            # `session.meta.interval != null`, and unreachable-host dimming
            # (data/health.ts) resolves cadence from the same field — without
            # this, both stay permanently unresolvable ("unknown") under a
            # FakeCollector-backed live server, no matter how many points are
            # pushed.
            self._global_interval = interval

    @override
    def get_meta_model(self) -> MonitorMeta:
        """Production meta, with ``live`` forced (hosts=[] would report historical)."""
        model = super().get_meta_model()
        model.live = self._force_live
        return model

    async def push_log_events(
        self, host: str, *, tab: str, rows: "list[tuple[datetime, dict[str, str]]]"
    ) -> None:
        """Record a batch of log-event rows exactly as a live tick would (ring + one SSE frame)."""
        await self._record_log_events(
            host, tab, [LogEvent(ts=ts, fields=fields) for ts, fields in rows]
        )

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
