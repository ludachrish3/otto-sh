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
            self._loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* on the server's loop and return its result."""
        if self._loop is None:
            raise RuntimeError("harness not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)

    def stop(self) -> None:
        """Signal shutdown and join the server thread (idempotent)."""
        if self._thread is None:
            return
        self.server.stop()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("dashboard harness thread did not exit within 10s")
        self._thread = None
