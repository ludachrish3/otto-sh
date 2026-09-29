"""``MonitorHandle`` — the per-test metrics monitor the ``monitor`` fixture hands out.

The ``monitor`` fixture (``otto.suite.pytest_plugin.OttoFixturesPlugin.monitor``)
hands each test, class or plain function, a fresh ``MonitorHandle``: the
``start``/``stop``/``event``/``results``/``events`` calls and their state live
on the handle, never on the test instance, and the fixture stops the monitor
for the test automatically at teardown if it was ever started.
"""

import asyncio
import contextlib
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from otto.host.unix_host import UnixHost
    from otto.monitor.collector import MetricCollector, MonitorTarget
    from otto.monitor.db import MetricDB
    from otto.monitor.events import MonitorEvent
    from otto.monitor.parsers import MetricParser
    from otto.monitor.server import MonitorServer
    from otto.suite.plugin import OttoPlugin

logger = getLogger(__name__)


class MonitorHandle:
    """Per-test metrics monitor handed out by the ``monitor`` fixture.

    Construct one with the active :class:`~otto.suite.plugin.OttoPlugin`
    instance, or ``None`` when no ``--monitor`` session is running (the plugin
    is looked up through ``request.config.stash`` — see the fixture). Nothing
    otto-specific lives on the test instance any more; all per-test monitor
    state lives here instead.
    """

    def __init__(self, plugin: "OttoPlugin | None") -> None:
        self._plugin = plugin
        self._started = False
        self._monitor_collector: "MetricCollector | None" = None
        self._monitor_server: "MonitorServer | None" = None
        self._monitor_task: "asyncio.Task[None] | None" = None
        self._monitor_db: "MetricDB | None" = None

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has run and not yet been followed by a completed :meth:`stop`.

        Its own state, not derived from whether a collector object exists:
        :meth:`results`/:meth:`events` keep reading the stopped collector's
        data after :meth:`stop` completes, so "a collector is present" can no
        longer stand in for "still running". A :meth:`start` that raises
        AFTER building the collector (e.g. ``wait_started()`` failing) still
        leaves this ``True`` — deliberately: the fixture's teardown reads it
        to decide whether to call :meth:`stop` and clean up the partially
        started state.
        """
        return self._started

    async def start(
        self,
        hosts: "list[UnixHost] | None" = None,
        interval: "timedelta | float" = timedelta(seconds=5),
        parsers: "list[MetricParser] | None" = None,
        port: int = 0,
        bind: str = "127.0.0.1",
        db_path: "str | None" = None,
        targets: "list[MonitorTarget] | None" = None,
    ) -> str:
        """
        Start metric collection from all hosts and launch the web dashboard.

        Must be called with ``await``::

            url = await monitor.start(hosts=[host])

        All hosts are polled simultaneously on each tick via asyncio.gather().
        Series keys in results are ``"hostname/metric_label"``.

        Args:
            hosts: The UnixHosts to monitor. Ignored when *targets* is provided.
            interval: How often to poll the hosts. timedelta or float (seconds).
            parsers: Custom metric parsers applied to all hosts. Ignored when *targets* is provided.
            port: TCP port for the dashboard web server (0 = auto-assign).
            bind: Address to bind to. Use '0.0.0.0' for access from other machines.
            db_path: Path for SQLite persistence. If None, data is in-memory only.
            targets: Per-host MonitorTarget objects. When provided, *hosts* and *parsers*
                are ignored. Use this to assign different parsers to different hosts.

        Returns:
            Dashboard URL, keyed with the per-run access key, e.g.
            'http://127.0.0.1:8080/?key=abc123...'.
        """
        from otto.models import validate_interval
        from otto.monitor.collector import MetricCollector
        from otto.monitor.export import build_session_metric_db
        from otto.monitor.server import MonitorServer
        from otto.monitor.session import new_frame, snapshot_lab

        if targets is None and hosts is None:
            raise ValueError("Provide either hosts or targets")

        if isinstance(interval, (int, float)):
            interval = timedelta(seconds=float(interval))
        validate_interval(interval.total_seconds())

        # Real session identity + lab snapshot (spec 2026-07-12), built
        # unconditionally: MonitorServer stamps collector.session_id from
        # `frame` and (live mode) needs both `frame`/`lab` to serve
        # /api/monitor_sessions, regardless of whether this run also
        # persists to a --db archive. No suite/run name is threaded through
        # today, so `label`/`note` stay None (honest, not a placeholder) —
        # same as otto.suite.plugin's --monitor --db path.
        snapshot_hosts = (
            [target.host for target in targets] if targets is not None else list(hosts or [])
        )
        frame = new_frame(label=None, note=None)
        lab = snapshot_lab(snapshot_hosts, declared=[])

        monitor_db = None
        if db_path is not None:
            # See otto.monitor.export.build_session_metric_db for why a
            # throwaway meta_collector (never run, never handed this
            # MetricDB), built the SAME way (targets, or hosts+parsers) as
            # the real collector below, is unavoidable here. chart_map is
            # NOT built here either: it accumulates as points arrive, so the
            # collector writes it into the session row itself
            # (MetricDB.write_chart_map).
            meta_collector = (
                MetricCollector(targets=targets)
                if targets is not None
                else MetricCollector(hosts=hosts, parsers=parsers)
            )
            monitor_db = build_session_metric_db(
                db_path, frame, lab, meta_collector, interval=interval.total_seconds()
            )
        self._monitor_db = monitor_db

        if targets is not None:
            self._monitor_collector = MetricCollector(
                targets=targets,
                db=monitor_db,
            )
        else:
            self._monitor_collector = MetricCollector(
                hosts=hosts,
                parsers=parsers,
                db=monitor_db,
            )
        # Set as soon as the collector exists, not at the end of this method:
        # a later failure in this same call (e.g. wait_started() below) must
        # still leave `started` True, so the fixture's teardown knows to call
        # stop() and clean up whatever got built.
        self._started = True
        self._monitor_server = MonitorServer(
            self._monitor_collector,
            host=bind,
            port=port,
            frame=frame,
            lab=lab,
        )

        collector = self._monitor_collector
        server = self._monitor_server

        # Open the session archive BEFORE spawning the collector task: the
        # spawn happens inside _run() (itself a task), so collector.
        # spawn_collection() there would put the open back in cancellable
        # task context — a prompt stop() could cancel it mid-schema (the
        # #136/#137/#142-#144 flake wave). Awaiting here also surfaces a
        # locked/unsupported --db as a loud error at start. run() enforces
        # the ordering with a RuntimeError, so dropping this await fails
        # immediately instead of racing.
        await collector.init_db()

        async def _run() -> None:
            task = asyncio.create_task(collector.run(interval))
            try:
                await server.serve()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._monitor_task = asyncio.create_task(_run())

        # Wait until the server is ready to accept connections — or inherit
        # its startup failure (bad TLS pair, bound port) instead of hanging on
        # a flag nothing will ever flip (gate G7).
        try:
            await self._monitor_server.wait_started()
        except BaseException:
            # Reap the failed serve task before re-raising: left in
            # _monitor_task, a later stop() would await it and surface the
            # same failure a second time, and an unawaited dead task fires
            # "exception was never retrieved" at GC.
            task, self._monitor_task = self._monitor_task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

        url = self._monitor_server.url
        # SECURITY: `url` carries the per-run ?key=<token> access credential, so
        # log only the keyless origin — `serve()` already prints the full keyed
        # URL straight to the terminal. Writing the key here would persist a live
        # credential to the suite run's console.log / verbose.log.
        logger.info(f"Monitor dashboard: {self._monitor_server.origin}")
        return url

    async def stop(self) -> None:
        """Stop metric collection and shut down the dashboard server.

        Must be called with ``await``::

            await monitor.stop()

        Idempotent: safe to call again (or call after the handle was never
        started at all) once it has already stopped everything — guarded by
        :attr:`started` rather than by object identity, since the collector
        itself is deliberately kept (see below).

        Does NOT discard the collector: :meth:`results`/:meth:`events` keep
        reading its recorded data after this returns — only :meth:`start`
        replaces it, building a fresh collector for the next run.
        """
        if not self._started:
            return
        if self._monitor_server is not None:
            self._monitor_server.stop()
        if self._monitor_task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._monitor_task, timeout=10)
            self._monitor_task = None
        if self._monitor_db is not None:
            # Stamp this session's end BEFORE closing the DB connection below
            # (finalize() is a no-op once closed) — an unstamped end is the
            # producer's deliberate crash marker (MetricDB.finalize /
            # otto.monitor.export._fallback_end), so a clean stop() must not
            # leave every archived suite run looking crashed.
            await self._monitor_db.finalize(datetime.now(tz=timezone.utc))
        if self._monitor_collector is not None:
            await self._monitor_collector.close_db()
        self._monitor_server = None
        self._monitor_db = None
        self._started = False

    def _active_collector(self) -> "MetricCollector | None":
        """Return this handle's own collector while started, else the running session's.

        The session-wide collector lives on the active ``OttoPlugin`` under
        ``otto test --monitor`` (``OttoPlugin._otto_session_monitor``); a
        handle built with ``plugin=None`` (no ``OttoPlugin`` active, e.g. a
        bare unit test) simply has nothing to fall back to. Checked via
        :attr:`started`, not ``self._monitor_collector is not None``: the
        collector object outlives a completed :meth:`stop` (so
        :meth:`results`/:meth:`events` keep working), so its mere presence no
        longer means "mine to write into right now".
        """
        if self._started:
            return self._monitor_collector
        return self._plugin.session_monitor_collector if self._plugin is not None else None

    def event(
        self,
        label: str,
        color: str = "#888888",
        dash: str = "dash",
    ) -> Coroutine[Any, Any, None]:
        """
        Record a labeled event on the live dashboard at the current time.

        Has no effect if monitoring is not active. Honors this handle's own
        collector while :attr:`started` (i.e. between :meth:`start` and the
        matching :meth:`stop`), then falls back to the session-wide collector
        started by ``otto test --monitor``.

        Validates through the same seam as every other marking surface
        (:class:`~otto.models.monitor.EventCreateBody`) — a blank label, a
        non-``#rrggbb`` color, or an unknown dash style raises
        ``pydantic.ValidationError`` (a ``ValueError`` subclass) synchronously,
        at the call site, before the collector is ever touched. Call sites
        still ``await`` the result (``await monitor.event(...)``) — this is a
        plain (non-``async``) method only so that raise happens immediately
        even if the caller forgets to await it, rather than silently
        vanishing into an unawaited coroutine.
        """
        # Same rules as every other marking surface (one seam):
        # constructing the body IS the validation — pydantic.ValidationError
        # (a ValueError) surfaces bad input at the call site instead of
        # persisting a style the dashboard cannot render.
        from ..models.monitor import EventCreateBody

        EventCreateBody(label=label, color=color, dash=dash)
        return self._record_event(label, color, dash)

    async def _record_event(self, label: str, color: str, dash: str) -> None:
        """Do the actual write; split out so validation above stays synchronous."""
        collector = self._active_collector()
        if collector is not None:
            await collector.add_event(
                label=label,
                color=color,
                dash=dash,
                source="user_code",
            )

    def results(self) -> "dict[str, list[tuple[datetime, float]]]":
        """Return collected metric series. Empty dict if never started.

        Still readable after :meth:`stop`: the collector's recorded data is
        not discarded, only the running collection/server is torn down. A
        following :meth:`start` builds a fresh collector, replacing it.
        """
        if self._monitor_collector is None:
            return {}
        return {
            key: [(pt.ts, pt.value) for pt in pts]
            for key, pts in self._monitor_collector.get_series().items()
        }

    def events(self) -> "list[MonitorEvent]":
        """Return all recorded events. Empty list if never started.

        Still readable after :meth:`stop` — see :meth:`results`.
        """
        if self._monitor_collector is None:
            return []
        return self._monitor_collector.get_events()
