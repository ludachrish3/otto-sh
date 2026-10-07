"""``MonitorHandle`` — the per-test metrics monitor the ``monitor`` fixture hands out.

The ``monitor`` fixture (defined on ``otto.suite.pytest_plugin.OttoFixturesPlugin``)
hands each test, class or plain function, a fresh ``MonitorHandle``: the
``start``/``stop``/``event``/``results``/``events`` calls and their state live
on the handle, never on the test instance, and the fixture stops the monitor
for the test automatically at teardown if it was ever started.
"""

import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from otto.host.remote_host import RemoteHost
    from otto.monitor.collector import MetricCollector, MonitorTarget
    from otto.monitor.events import MonitorEvent
    from otto.monitor.parsers import MetricParser
    from otto.monitor.session import MonitorSession
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
        self._session: "MonitorSession | None" = None
        self._task: "asyncio.Task[None] | None" = None

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has run and not yet been followed by a completed :meth:`stop`.

        Its own state, not derived from whether a collector object exists:
        :meth:`results`/:meth:`events` keep reading the stopped collector's
        data after :meth:`stop` completes, so "a collector is present" can no
        longer stand in for "still running". A :meth:`start` whose archive
        fails to open still leaves this ``True`` — deliberately: the
        fixture's teardown reads it to decide whether to call :meth:`stop`
        and clean up the partially started state.
        """
        return self._started

    async def start(
        self,
        hosts: "list[RemoteHost] | None" = None,
        *,
        targets: "list[MonitorTarget] | None" = None,
        interval: "timedelta | float" = timedelta(seconds=5),
        parsers: "list[MetricParser] | None" = None,
        db_path: "str | None" = None,
    ) -> None:
        """Start sampling *hosts* until :meth:`stop` (or the test's teardown).

        Must be awaited: ``await monitor.start(hosts=[host])``. Collection
        only, with no dashboard: archive with *db_path* and review it with
        ``otto monitor <file>.db``, or watch live with ``otto monitor --live``.
        The hosts stay the test's: :meth:`stop` closes the archive, never their
        connections.

        Args:
            hosts: Hosts to sample (Unix hosts over a shell, hosts with an
                ``snmp`` block over SNMP). Exactly one of *hosts* and *targets*.
            targets: Per-host targets, to give different hosts different parsers.
            interval: Sampling interval (timedelta or seconds), at least 1 s.
            parsers: Shell parsers for every Unix host in *hosts*.
            db_path: SQLite archive path; ``None`` keeps data in memory.

        Raises:
            MonitorInputError: neither or both of hosts/targets, an interval
                below the floor, or a host otto cannot sample.
            RuntimeError: already started; call :meth:`stop` first.
        """
        from otto.config.fleet import get_lab
        from otto.monitor.session import MonitorSession

        if self._started:
            raise RuntimeError("monitor already started: call stop() before starting it again")
        session = MonitorSession.build(
            hosts,
            targets=targets,
            parsers=parsers,
            interval=interval,
            db_path=db_path,
            declared=get_lab().links,
            owns_hosts=False,
        )
        self._session = session
        # True from here, not after open(): a failing open() must still leave
        # the fixture's teardown something to finish.
        self._started = True
        await session.open()
        self._task = session.spawn()

    async def stop(self) -> None:
        """Stop sampling and close the archive. Idempotent.

        The collected data stays readable through :meth:`results` and
        :meth:`events` until the next :meth:`start`.
        """
        if not self._started:
            return
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._session is not None:
            await self._session.finish()
        self._started = False

    def _active_collector(self) -> "MetricCollector | None":
        """Return this handle's own collector while started, else the running session's.

        The session-wide collector lives on the active ``OttoPlugin`` under
        ``otto test --monitor`` (``OttoPlugin._otto_session_monitor``); a
        handle built with ``plugin=None`` (no ``OttoPlugin`` active, e.g. a
        bare unit test) simply has nothing to fall back to. Checked via
        :attr:`started`, not whether a session exists: the
        collector object outlives a completed :meth:`stop` (so
        :meth:`results`/:meth:`events` keep working), so its mere presence no
        longer means "mine to write into right now".
        """
        if self._started and self._session is not None:
            return self._session.collector
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
        not discarded, only the running collection is torn down. A
        following :meth:`start` builds a fresh collector, replacing it.
        """
        if self._session is None:
            return {}
        return {
            key: [(pt.ts, pt.value) for pt in pts]
            for key, pts in self._session.collector.get_series().items()
        }

    def events(self) -> "list[MonitorEvent]":
        """Return all recorded events. Empty list if never started.

        Still readable after :meth:`stop` — see :meth:`results`.
        """
        if self._session is None:
            return []
        return self._session.collector.get_events()
