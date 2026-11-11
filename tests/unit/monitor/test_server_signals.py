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
import contextlib
import logging
import signal
import time

import pytest

from otto.lifecycle import _CommandRun, run_command
from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer


async def _started_server() -> "tuple[MonitorServer, asyncio.Task[None]]":
    server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
    task = asyncio.get_running_loop().create_task(server.serve())
    await server.wait_started()
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
    assert server._server is not None
    listeners = list(server._server.servers)
    sockets = [sock for listener in listeners for sock in listener.sockets]
    assert sockets, "a started server listens on at least one socket"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The listener is really gone, judged on the server's own sockets: once
    # released, the ephemeral port belongs to whoever asks next (a sibling
    # xdist worker included), so dialing it proves nothing. The drain inside
    # serve() awaited uvicorn's shutdown, so no lingering server task exists
    # to trip filterwarnings=error at loop close.
    assert not any(listener.is_serving() for listener in listeners)
    assert [sock.fileno() for sock in sockets] == [-1] * len(sockets)


# --- #784: one SIGINT with a dashboard stream open drains, never forces ------
#
# sse_starlette learns about a shutdown only through uvicorn's signal handlers,
# which _LifecycleOwnedServer removes on purpose. An open /api/stream therefore
# parked in its queue and held uvicorn's graceful drain until the teardown
# deadline forced past it, logging the lifespan and ASGI CancelledErrors at
# ERROR. The server must end its own streams when the drain begins.


async def _open_stream(port: int, key: str) -> "tuple[asyncio.StreamReader, asyncio.StreamWriter]":
    """Open a raw ``GET /api/stream`` and read until the priming comment arrives."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET /api/stream?key={key} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    received = b""
    while b"connected" not in received:
        chunk = await reader.read(4096)
        assert chunk, f"the stream closed before it was primed: {received!r}"
        received += chunk
    return reader, writer


async def _read_to_eof(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bytes:
    """Read until the server closes the connection; a reset raises out of here."""
    received = b""
    try:
        while chunk := await reader.read(4096):
            received += chunk
    finally:
        writer.close()
    return received


def _error_records(caplog: pytest.LogCaptureFixture) -> "list[str]":
    return [f"{r.name}: {r.getMessage()}" for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.serial_timing
@pytest.mark.timeout(15)
def test_one_sigint_with_open_streams_drains_well_inside_the_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = _CommandRun(teardown_deadline=5.0, install_handlers=False)
    holder: "dict[str, MonitorServer]" = {}
    eofs: "list[bytes]" = []
    started: "list[float]" = []

    async def peer(server: MonitorServer, primed: asyncio.Event) -> None:
        await server.wait_started()
        reader, writer = await _open_stream(server._port, server.key)
        primed.set()
        eofs.append(await _read_to_eof(reader, writer))

    async def interrupt_once_primed(events: "list[asyncio.Event]") -> None:
        for event in events:
            await event.wait()
        started.append(time.monotonic())
        ctrl._on_signal(signal.SIGINT)

    async def body() -> None:
        server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
        holder["server"] = server
        primed = [asyncio.Event(), asyncio.Event()]
        peers = [asyncio.ensure_future(peer(server, event)) for event in primed]
        trigger = asyncio.ensure_future(interrupt_once_primed(primed))
        try:
            await server.serve()
        finally:
            await asyncio.gather(trigger, *peers)

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    elapsed = time.monotonic() - started[0]

    assert exc_info.value.code == 130
    assert ctrl.forced is False, "the open streams held the drain to the teardown deadline"
    assert elapsed < 2.5, f"teardown took {elapsed:.2f}s after the signal"
    assert len(eofs) == 2, "both stream clients saw the connection close cleanly"
    assert _error_records(caplog) == []
    server = holder["server"]
    assert server._server is not None
    assert not server._server.lifespan.shutdown_failed  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_stop_twice_ends_an_open_stream_within_a_bounded_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, task = await _started_server()
    try:
        reader, writer = await _open_stream(server._port, server.key)
        eof = asyncio.ensure_future(_read_to_eof(reader, writer))
        server.stop()
        server.stop()
        await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        await asyncio.wait_for(eof, timeout=10.0)
        assert _error_records(caplog) == []
    finally:
        server.force_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_stop_after_the_server_finished_and_its_loop_closed_is_harmless() -> None:
    async def serve_and_stop() -> MonitorServer:
        server, task = await _started_server()
        server.stop()
        await task
        return server

    server = asyncio.run(serve_and_stop())
    # serve() leaves _loop and _server set; the loop they name is now closed.
    assert server._loop is not None
    assert server._loop.is_closed()
    assert server._server is not None
    server.stop()
    server.stop()
    # force_stop schedules onto the same loop through the same guard.
    server.force_stop()


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_a_fresh_server_reuses_the_collector_after_a_drain() -> None:
    collector = MetricCollector(hosts=[])

    async def start() -> "tuple[MonitorServer, asyncio.Task[None]]":
        server = MonitorServer(collector, host="127.0.0.1", port=0)
        task = asyncio.get_running_loop().create_task(server.serve())
        await server.wait_started()
        return server, task

    first, first_task = await start()
    try:
        reader, writer = await _open_stream(first._port, first.key)
        eof = asyncio.ensure_future(_read_to_eof(reader, writer))
        first.stop()
        await asyncio.wait_for(asyncio.shield(first_task), timeout=10.0)
        await asyncio.wait_for(eof, timeout=10.0)
    finally:
        first.force_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task

    second, second_task = await start()
    try:
        reader, writer = await _open_stream(second._port, second.key)
        await collector.add_event(label="boot")
        received = b""
        while b"data:" not in received:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            assert chunk, "the second server's stream ended instead of carrying the event"
            received += chunk
        eof = asyncio.ensure_future(_read_to_eof(reader, writer))
        second.stop()
        await asyncio.wait_for(asyncio.shield(second_task), timeout=10.0)
        await asyncio.wait_for(eof, timeout=10.0)
    finally:
        second.force_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await second_task
