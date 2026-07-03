"""DashboardHarness — MonitorServer on a background thread for dashboard tests.

The server (and every collector coroutine) runs on the thread's own event
loop; test-side helpers marshal onto it with ``run_coroutine_threadsafe`` so
the collector is only ever touched from one loop.
"""

import asyncio
import threading
import time
from collections.abc import Coroutine
from typing import Any, Generic, TypeVar

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer

C = TypeVar("C", bound=MetricCollector)
T = TypeVar("T")

_STARTUP_TIMEOUT = 15.0


class DashboardHarness(Generic[C]):
    """Serve *collector*'s dashboard on ``127.0.0.1:<ephemeral>``."""

    def __init__(self, collector: C) -> None:
        self.collector: C = collector
        self.server = MonitorServer(collector, host="127.0.0.1", port=0)
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
            pending = asyncio.all_tasks(loop=self._loop)
            if pending:
                for task in pending:
                    task.cancel()
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* on the server's loop and return its result."""
        if self._loop is None:
            raise RuntimeError("harness not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)

    def run_export(self) -> str:
        """Serialize the collector to its --file JSON on the server loop."""

        async def _export() -> str:
            return self.collector.to_json()

        return self.run(_export())

    def stop(self) -> None:
        """Signal shutdown and join the server thread (idempotent).

        Sets uvicorn's force_exit so shutdown does not wait for open SSE
        connections to drain — dashboard pages (and streaming test clients)
        hold /api/stream open indefinitely, which would otherwise stall
        graceful shutdown until keepalive timeouts fire.

        force_exit only skips *waiting* for those connections; it does not
        close them at the OS level (h11's shutdown path merely flips
        keep_alive for a streaming response that never completes). A live
        EventSource on the browser side would then never observe an error —
        so before signaling shutdown, forcibly abort every open transport on
        the server's loop. transport.abort() closes with an RST, which makes
        the browser's EventSource.onerror fire promptly.
        """
        if self._thread is None:
            return
        uv_server = self.server._server
        if uv_server is not None and self._loop is not None:
            state = uv_server.server_state

            def _abort_connections() -> None:
                for conn in list(state.connections):
                    transport = getattr(conn, "transport", None)
                    if transport is not None:
                        transport.abort()

            self._loop.call_soon_threadsafe(_abort_connections)
            uv_server.force_exit = True
        self.server.stop()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("dashboard harness thread did not exit within 10s")
        self._thread = None
