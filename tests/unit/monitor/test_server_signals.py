"""MonitorServer signal ownership: uvicorn must not displace lifecycle handlers.

Chaos plan 3. ``uvicorn.Server.serve()`` wraps the whole run in
``capture_signals()`` — raw ``signal.signal(SIGINT/SIGTERM, ...)`` for the
entire serve window — which would displace the per-loop handlers
``run_command`` installs and bypass otto's two-stage interrupt policy
(status line, teardown deadline, force hooks). otto owns interrupt policy:
the server subclass neuters the capture, and shutdown is driven by
cancellation, translated here into uvicorn's graceful drain.

No real signal handlers are installed by these tests (root-conftest guard);
the displacement check is differential on ``signal.getsignal``.
"""

import asyncio
import signal

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer


async def _started_server() -> "tuple[MonitorServer, asyncio.Task[None]]":
    server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
    task = asyncio.get_running_loop().create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surface the startup failure instead of hanging
        await asyncio.sleep(0.01)
    return server, task


@pytest.mark.asyncio
async def test_serve_does_not_displace_signal_dispositions() -> None:
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    _server, task = await _started_server()
    try:
        assert signal.getsignal(signal.SIGINT) is before_int
        assert signal.getsignal(signal.SIGTERM) is before_term
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cancellation_drains_uvicorn_and_reraises() -> None:
    server, task = await _started_server()
    port = server._port  # rebound to the real ephemeral port during startup
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The listener is really gone — a fresh connect must be refused. The
    # drain inside serve() awaited uvicorn's shutdown, so no lingering
    # server task exists to trip filterwarnings=error at loop close.
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)
