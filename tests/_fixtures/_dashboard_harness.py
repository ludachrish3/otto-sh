"""DashboardHarness — MonitorServer on a background thread for dashboard tests.

The server (and every collector coroutine) runs on the thread's own event
loop; test-side helpers marshal onto it with ``run_coroutine_threadsafe`` so
the collector is only ever touched from one loop.
"""

import asyncio
import gc
import threading
import time
from collections.abc import Coroutine
from typing import Any, Generic, Literal, TypeVar

from otto.models import LabSnapshot, MonitorExport
from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer
from otto.monitor.session import SessionFrame

C = TypeVar("C", bound=MetricCollector)
T = TypeVar("T")

_STARTUP_TIMEOUT = 15.0


class DashboardHarness(Generic[C]):
    """Serve *collector*'s dashboard on ``127.0.0.1:<ephemeral>``.

    The ``mode``/``document``/``source_name``/``frame``/``lab`` keywords pass
    straight through to :class:`~otto.monitor.server.MonitorServer` (defaults
    match its own: an unadorned live server) — kept explicit and typed here,
    rather than a generic ``**kwargs``, so ``ty`` still catches a caller
    passing the wrong shape.
    """

    def __init__(
        self,
        collector: C,
        *,
        mode: Literal["live", "review"] = "live",
        document: MonitorExport | None = None,
        source_name: str | None = None,
        frame: SessionFrame | None = None,
        lab: LabSnapshot | None = None,
    ) -> None:
        self.collector: C = collector
        self.server = MonitorServer(
            collector,
            host="127.0.0.1",
            port=0,
            mode=mode,
            document=document,
            source_name=source_name,
            frame=frame,
            lab=lab,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Base URL (valid once start() has returned)."""
        return self.server.url

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "DashboardHarness[C]":
        self._thread = threading.Thread(target=self._serve, name="dashboard-harness", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError(f"MonitorServer did not start within {_STARTUP_TIMEOUT}s")
            time.sleep(0.02)
        return self

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.server.serve())
        finally:
            # `server.serve()` returning doesn't guarantee uvicorn's own
            # background lifespan task (LifespanOn.main(), started via
            # loop.create_task() inside uvicorn) has reached a *terminal*
            # state yet -- shutdown_event.set() is its last statement, which
            # unblocks our await but needs one more loop iteration to mark
            # the task itself done. asyncio.run() drains exactly this case
            # for free; a hand-rolled new_event_loop()/close() does not. Skip
            # this and the still-"pending" task gets closed out from under it,
            # so Python's GC finalizes it against an already-closed loop at
            # some arbitrary *later* point -- raising "Event loop is closed"
            # that pytest's unraisableexception hook then attributes to
            # whatever unrelated test happens to be running at GC time.
            pending = list(asyncio.all_tasks(loop=self._loop))
            if pending:
                for task in pending:
                    task.cancel()
                results = self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
                for task, result in zip(pending, results, strict=True):
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        self._loop.call_exception_handler(
                            {
                                "message": "dashboard harness: task failed during teardown",
                                "exception": result,
                                "task": task,
                            }
                        )
            self._reap_orphaned_transports()
            self._loop.close()

    def _reap_orphaned_transports(self) -> None:
        """Abort any live server connection whose transport would outlive the loop.

        ``force_stop()`` aborts a one-shot snapshot of uvicorn's
        ``server_state.connections``, but asyncio defers ``connection_made``
        (where uvicorn *registers* a connection) onto the loop via
        ``call_soon``. So a socket accepted right as we shut down can finish
        registering *after* force_stop's abort snapshot — even after
        ``serve()`` returns — and is then never aborted. Its still-open
        ``_SelectorSocketTransport`` survives ``loop.close()`` and fires a
        ``ResourceWarning`` from ``__del__`` at GC time, which pytest's
        ``[unraisable]`` hook (with ``filterwarnings=error``, e.g. under
        ``OTTO_DETECT_ASYNCIO_LEAKS=1`` in ``make release``) escalates into a
        teardown ``ExceptionGroup`` — seen on Python 3.14, whose finalizer
        surfaces the warning. No snapshot inside ``force_stop`` can close this
        window (registration timing is unbounded), so the hand-rolled loop
        owner reaps here instead: settle pending ``connection_made``, abort
        every live selector transport bound to *this* loop, and flush the
        socket-close callbacks before closing. ``asyncio.run()`` runs extra
        turns that let registration settle but never actively closes live
        transports, so production only escapes this by exiting the process.
        """
        # _SelectorTransport is the concrete base of the read/write selector
        # transports; there is no public per-loop transport registry, so scan
        # the heap (as tests/conftest.py's leak detector does).
        from asyncio.selector_events import _SelectorTransport

        loop = self._loop
        assert loop is not None
        for _ in range(10):  # loop-until-clean: a late connection_made may add one mid-reap
            loop.run_until_complete(asyncio.sleep(0))
            live = [
                obj
                for obj in gc.get_objects()
                if isinstance(obj, _SelectorTransport)
                and getattr(obj, "_loop", None) is loop
                and getattr(obj, "_sock", None) is not None
                and not obj.is_closing()
            ]
            if not live:
                break
            for transport in live:
                transport.abort()
        # Final turn so the last abort()'s _call_connection_lost closes its socket.
        loop.run_until_complete(asyncio.sleep(0))

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* on the server's loop and return its result."""
        if self._loop is None:
            raise RuntimeError("harness not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)

    def stop(self) -> None:
        """Signal shutdown and join the server thread (idempotent).

        Delegates to ``MonitorServer.force_stop()``, which skips waiting for
        open SSE connections to drain and aborts their transports so a live
        EventSource on the browser side sees the connection die promptly
        (see that method's docstring for the full h11/force_exit rationale).
        """
        if self._thread is None:
            return
        self.server.force_stop()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("dashboard harness thread did not exit within 10s")
        self._thread = None
