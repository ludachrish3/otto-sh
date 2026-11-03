"""The one release of a telnet-framed stream, and the read gate it closes first.

A single-client console (Zephyr ``shell_telnet``, ser2net) wedges when its
client goes away while it still has output queued, so the release drains the
line to quiet before it closes, then waits until otto's socket is really
released before it drops the transport from the emergency registry. Every way
out that is not graceful (a deadline, an exception, a cancellation) goes
through one abort path, and the transport stays registered until that abort
is observed.

Tests whose name ends in ``real_writer`` run a real telnetlib3 client over a
connected socket pair; the rest use plain fakes.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from telnetlib3.client import TelnetClient as TelnetlibClient
from telnetlib3.stream_reader import TelnetReader
from telnetlib3.stream_writer import TelnetWriter

from otto.host import telnet
from otto.host.telnet import (
    CONSOLE_QUIESCE_QUIET_WINDOW,
    ReadGate,
    ReadGateClosed,
    _abort_and_unregister_soon,
    _live_console_transports,
    _register_console_transport,
    abort_console_transports,
    release_telnet_stream,
)
from otto.utils import wait_for_async
from tests.unit.host._telnet_fakes import (
    RecordingReader,
    RecordingTransport,
    RecordingWriter,
    SilentTelnetPeer,
    fake_stream,
    loopback_socket_pair,
)
from tests.unit.host.conftest import StalledTransport


@pytest.fixture(autouse=True)
def _empty_registry() -> Iterator[None]:
    """Start and end every test with an empty emergency registry."""
    _live_console_transports.clear()
    yield
    _live_console_transports.clear()


@pytest.fixture
def abort_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Record every ``abort_telnet_stream`` call, still running the real one."""
    calls: list[tuple[Any, Any]] = []
    real = telnet.abort_telnet_stream

    def spy(writer: Any, transport: Any) -> None:
        calls.append((writer, transport))
        real(writer, transport)

    monkeypatch.setattr(telnet, "abort_telnet_stream", spy)
    return calls


class RealStream:
    """A real telnetlib3 client stream over a socket pair, and its peer's end."""

    def __init__(self) -> None:
        self.reader: Any = None
        self.writer: Any = None
        self.transport: Any = None
        self.sock: Any = None
        self.peer_reader: asyncio.StreamReader | None = None
        self.peer_writer: asyncio.StreamWriter | None = None
        self.peer_task: asyncio.Task[None] | None = None

    def chatter_forever(self, every: float = 0.05) -> None:
        """Have the peer write a line every *every* seconds until the test ends."""

        async def chatter() -> None:
            assert self.peer_writer is not None
            while True:
                self.peer_writer.write(b"tick\r\n")
                await asyncio.sleep(every)

        self.peer_task = asyncio.create_task(chatter())


@contextlib.asynccontextmanager
async def real_stream() -> AsyncIterator[RealStream]:
    """Open a bytes-mode telnetlib3 client on one end of a loopback socket pair."""
    loop = asyncio.get_running_loop()
    ours, theirs = loopback_socket_pair()
    stream = RealStream()
    transport, protocol = await loop.create_connection(
        lambda: TelnetlibClient(encoding=False, connect_minwait=0, connect_maxwait=0.05),
        sock=ours,
    )
    await protocol._waiter_connected
    stream.reader, stream.writer, stream.transport = protocol.reader, protocol.writer, transport
    stream.sock = transport.get_extra_info("socket")
    stream.peer_reader, stream.peer_writer = await asyncio.open_connection(sock=theirs)
    _register_console_transport(transport)
    try:
        yield stream
    finally:
        if stream.peer_task is not None:
            stream.peer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream.peer_task
        transport.abort()
        stream.peer_writer.close()
        with contextlib.suppress(Exception):
            await stream.peer_writer.wait_closed()
        await asyncio.sleep(0)


class ChattyReader:
    """A reader whose line never goes quiet."""

    def __init__(self, every: float = 0.01) -> None:
        self.every = every

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(self.every)
        return b"tick\r\n"


def record_unregister(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    real = telnet._unregister_console_transport

    def recording(transport: Any) -> None:
        events.append("unregister")
        real(transport)

    monkeypatch.setattr(telnet, "_unregister_console_transport", recording)


def stalled_stream(transport: StalledTransport) -> tuple[Any, TelnetWriter]:
    """A real ``TelnetWriter`` over *transport*, with a reader whose line is quiet."""
    writer = TelnetWriter(transport, None, client=True)
    _register_console_transport(transport)
    return RecordingReader([]), writer


@pytest.mark.asyncio
async def test_release_single_client_drains_then_closes_then_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    record_unregister(monkeypatch, events)
    chunks = [b"one", b"two", b"three"]
    reader, writer, transport = fake_stream(
        events, RecordingReader(events, chunks, gap=0.05, then="quiet")
    )

    await release_telnet_stream(reader, writer, transport, single_client=True)

    assert events == ["read:one", "read:two", "read:three", "close", "unregister"]
    assert reader.chunks == []
    assert transport.aborts == 0
    assert transport not in _live_console_transports


@pytest.mark.asyncio
async def test_release_non_single_client_skips_wait_and_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    record_unregister(monkeypatch, events)
    reader, writer, transport = fake_stream(events, RecordingReader(events, [b"queued"]))
    gate = ReadGate()

    await release_telnet_stream(reader, writer, transport, single_client=False, reads_idle=gate)

    assert reader.calls == 0
    assert gate.closed is False
    assert writer.closes == 1
    assert events == ["close", "unregister"]
    assert transport not in _live_console_transports


@pytest.mark.asyncio
async def test_release_returns_only_after_fd_released_real_writer(
    abort_spy: list[tuple[Any, Any]],
) -> None:
    async with real_stream() as s:
        sock = s.transport.get_extra_info("socket")
        assert sock.fileno() != -1

        await release_telnet_stream(s.reader, s.writer, s.transport, single_client=True)

        assert sock.fileno() == -1
        assert s.transport not in _live_console_transports
        assert abort_spy == []


@pytest.mark.asyncio
async def test_peer_never_quiet_aborts_at_deadline_real_writer(
    abort_spy: list[tuple[Any, Any]],
) -> None:
    loop = asyncio.get_running_loop()
    async with real_stream() as s:
        s.chatter_forever(0.05)
        start = loop.time()

        await release_telnet_stream(
            s.reader, s.writer, s.transport, single_client=True, deadline=start + 0.5
        )

        assert loop.time() - start < 0.5 + 0.3
        assert len(abort_spy) == 1
        assert s.sock.fileno() == -1
        assert s.transport not in _live_console_transports


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["quiet", "eof"])
async def test_quiet_window_or_eof_is_what_permits_graceful_path(
    ending: str, abort_spy: list[tuple[Any, Any]]
) -> None:
    loop = asyncio.get_running_loop()
    events: list[str] = []
    reader, writer, transport = fake_stream(events, RecordingReader(events, then=ending))
    start = loop.time()

    await release_telnet_stream(reader, writer, transport, single_client=True)

    elapsed = loop.time() - start
    assert abort_spy == []
    assert writer.closes == 1
    assert transport not in _live_console_transports
    if ending == "quiet":
        assert elapsed >= CONSOLE_QUIESCE_QUIET_WINDOW
    else:
        assert elapsed < CONSOLE_QUIESCE_QUIET_WINDOW


@pytest.mark.asyncio
async def test_blocked_writes_abort_at_deadline_real_writer(
    stalled_transport: Callable[..., StalledTransport],
) -> None:
    loop = asyncio.get_running_loop()
    transport = stalled_transport(release_after_turns=1)
    reader, writer = stalled_stream(transport)

    release = asyncio.create_task(
        release_telnet_stream(
            reader, writer, transport, single_client=True, deadline=loop.time() + 0.5
        )
    )
    await asyncio.sleep(0.35)  # past the quiet window, waiting on the flush
    assert transport.closing
    assert transport.aborts == 0
    assert transport in _live_console_transports

    await release

    assert transport.aborts >= 1
    assert transport.sock.fileno() == -1
    assert transport not in _live_console_transports


@pytest.mark.asyncio
async def test_release_never_observed_stays_registered(
    stalled_transport: Callable[..., StalledTransport],
) -> None:
    loop = asyncio.get_running_loop()
    transport = stalled_transport(release_after_turns=None)
    reader, writer = stalled_stream(transport)
    start = loop.time()

    await release_telnet_stream(reader, writer, transport, single_client=True, deadline=start + 0.3)

    assert loop.time() - start >= 0.3 + telnet._ABORT_GRACE
    assert transport.aborts == 1
    assert transport.sock.fileno() != -1
    assert transport in _live_console_transports


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["gate", "drain", "flush"])
async def test_cancel_during_gate_wait_drain_and_flush_aborts_then_reraises(
    phase: str,
    abort_spy: list[tuple[Any, Any]],
    stalled_transport: Callable[..., StalledTransport],
) -> None:
    async with contextlib.AsyncExitStack() as stack:
        gate = ReadGate()
        if phase == "flush":
            transport = stalled_transport(release_after_turns=1)
            reader, writer = stalled_stream(transport)
            sock: Any = transport.sock
            settle = CONSOLE_QUIESCE_QUIET_WINDOW + 0.1  # drained, now flushing
        else:
            s = await stack.enter_async_context(real_stream())
            reader, writer, transport, sock = s.reader, s.writer, s.transport, s.sock
            if phase == "drain":
                s.chatter_forever(0.02)
            else:
                stack.enter_context(gate.reading())  # a read that never finishes
            settle = 0.1

        release = asyncio.create_task(
            release_telnet_stream(reader, writer, transport, single_client=True, reads_idle=gate)
        )
        await asyncio.sleep(settle)
        assert abort_spy == []
        release.cancel()

        with pytest.raises(asyncio.CancelledError):
            await release

        assert len(abort_spy) == 1
        assert sock.fileno() == -1
        assert transport not in _live_console_transports


@pytest.mark.asyncio
async def test_repeated_cancel_during_grace_leaves_transport_registered(
    monkeypatch: pytest.MonkeyPatch,
    stalled_transport: Callable[..., StalledTransport],
) -> None:
    loop = asyncio.get_running_loop()
    # The release lands inside the abort's grace, after the second cancel: a
    # grace that swallowed that cancel would see it, unregister and return
    # 0.25 s late. The grace is widened so a loaded machine has room on both
    # sides of the second cancel.
    monkeypatch.setattr(telnet, "_ABORT_GRACE", 0.5)
    transport = stalled_transport(release_after_seconds=0.3)
    reader, writer = stalled_stream(transport)
    release = asyncio.create_task(
        release_telnet_stream(reader, writer, transport, single_client=True)
    )
    await asyncio.sleep(0.05)  # draining
    release.cancel()
    await asyncio.sleep(0.05)  # inside the abort's grace, well before the release lands
    assert transport.aborts == 1
    assert transport.sock.fileno() != -1
    second_cancel = loop.time()
    release.cancel()

    with pytest.raises(asyncio.CancelledError):
        await release

    assert loop.time() - second_cancel < 0.1
    assert transport in _live_console_transports
    assert abort_console_transports() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["pending_empty_buffer", "buffered_unmatched", "wakeup_before_resume", "never_finishes"]
)
async def test_read_gate_cases(case: str) -> None:
    loop = asyncio.get_running_loop()
    gate = ReadGate()
    reader = TelnetReader()
    order: list[str] = []

    async def read() -> None:
        with gate.reading():
            if case == "buffered_unmatched":
                await reader.readuntil(b"$ ")
            else:
                await reader.read(4096)
        order.append("left")

    if case == "buffered_unmatched":
        reader.feed_data(b"partial output")
    reading = asyncio.create_task(read())
    await asyncio.sleep(0.01)
    assert gate.in_flight == 1

    if case == "never_finishes":
        start = loop.time()
        assert await gate.wait_idle(loop.time() + 0.2) is False
        assert loop.time() - start < 0.4
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reading
        return

    async def wait() -> None:
        assert await gate.wait_idle(loop.time() + 1.0) is True
        order.append("idle")

    if case == "wakeup_before_resume":
        reader.feed_data(b"x")  # the reader's wakeup is set; its task has not resumed
        await wait()
    else:
        waiting = asyncio.create_task(wait())
        await asyncio.sleep(0.05)
        assert not waiting.done()
        reader.feed_data(b"$ " if case == "buffered_unmatched" else b"x")
        await waiting
    await reading

    assert order == ["left", "idle"]


@pytest.mark.asyncio
async def test_read_gate_rejects_entry_after_close() -> None:
    loop = asyncio.get_running_loop()
    gate = ReadGate()

    assert await gate.wait_idle(loop.time() + 0.1) is True
    assert gate.closed is True
    with pytest.raises(ReadGateClosed), gate.reading():
        pass
    assert gate.in_flight == 0
    assert await gate.wait_idle(loop.time() + 0.1) is True


class _EchoWriter:
    """A writer whose ECHO agreement is a future the test settles."""

    def __init__(self) -> None:
        self.agreed: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.iacs: list[tuple[bytes, bytes]] = []

    def iac(self, cmd: bytes, opt: bytes) -> None:
        self.iacs.append((cmd, opt))

    async def wait_for(self, **kwargs: Any) -> Any:
        return await self.agreed


@pytest.mark.asyncio
async def test_negotiate_no_echo_never_drops_a_cancel_in_the_turn_it_agrees() -> None:
    """On 3.10/3.11 ``asyncio.wait_for`` returns the result and loses this cancel."""
    writer = _EchoWriter()
    negotiating = asyncio.create_task(telnet.negotiate_no_echo(writer, 5.0))
    await asyncio.sleep(0.01)
    writer.agreed.set_result(True)
    negotiating.cancel()  # in the same loop turn the agreement lands
    with pytest.raises(asyncio.CancelledError):
        await negotiating


@pytest.mark.asyncio
async def test_negotiate_no_echo_times_out_quietly_and_reraises_its_own_error() -> None:
    writer = _EchoWriter()
    await asyncio.wait_for(telnet.negotiate_no_echo(writer, 0.02), 1.0)  # proceeds anyway
    assert writer.agreed.cancelled()  # nothing left pending on the stream

    failing = _EchoWriter()
    failing.agreed.set_exception(ConnectionResetError("peer went away"))
    with pytest.raises(ConnectionResetError):
        await telnet.negotiate_no_echo(failing, 1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 30.0], ids=["no_timeout", "timeout"])
async def test_await_within_re_cancels_an_inner_await_that_swallows_the_first_cancel(
    timeout: float | None,
) -> None:
    """The caller's cancel propagates at once even when the inner await drops one.

    telnetlib3's own ``asyncio.wait_for`` on Python 3.10 and 3.11 can swallow
    the first cancellation and carry on into a long wait (its negotiation, up
    to ``connect_maxwait``). One cancel of the inner await would leave the
    caller's cancellation waiting that out.
    """
    loop = asyncio.get_running_loop()
    inner: dict[str, asyncio.Task[Any]] = {}
    started = asyncio.Event()

    async def swallows_one_cancel() -> None:
        task = asyncio.current_task()
        assert task is not None
        inner["task"] = task
        started.set()
        # Dropped, as 3.10/3.11 ``wait_for`` does in the turn its inner await finishes.
        with contextlib.suppress(asyncio.CancelledError):
            await loop.create_future()
        await asyncio.sleep(30)

    outer = asyncio.create_task(telnet._await_within(swallows_one_cancel(), timeout))
    await started.wait()
    await asyncio.sleep(0)
    outer.cancel()
    cancelled_at = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(outer, 5.0)

    assert loop.time() - cancelled_at < 1.0
    assert inner["task"].done()


@pytest.mark.asyncio
async def test_await_within_timeout_re_cancels_an_inner_await_that_swallows_the_first_cancel() -> (
    None
):
    """A timeout ends the wait promptly too, with the swallowing inner await finished."""
    loop = asyncio.get_running_loop()
    inner: dict[str, asyncio.Task[Any]] = {}

    async def swallows_one_cancel() -> None:
        task = asyncio.current_task()
        assert task is not None
        inner["task"] = task
        with contextlib.suppress(asyncio.CancelledError):
            await loop.create_future()
        await asyncio.sleep(30)

    started = loop.time()
    outcome = await asyncio.wait_for(telnet._await_within(swallows_one_cancel(), 0.05), 5.0)

    assert outcome is None
    assert loop.time() - started < 1.0
    assert inner["task"].done()


def test_read_gate_close_is_sync_idempotent_and_spares_a_read_in_flight() -> None:
    gate = ReadGate()
    with gate.reading():
        gate.close()
        gate.close()
        assert gate.closed is True
        assert gate.in_flight == 1  # the read that entered first keeps going
    assert gate.in_flight == 0
    with pytest.raises(ReadGateClosed), gate.reading():
        pass


@pytest.mark.asyncio
async def test_emergency_sweep_during_blocked_drain_aborts() -> None:
    loop = asyncio.get_running_loop()
    events: list[str] = []
    reader, writer, transport = fake_stream(events, ChattyReader())
    start = loop.time()
    release = asyncio.create_task(
        release_telnet_stream(reader, writer, transport, single_client=True, deadline=start + 0.3)
    )
    await asyncio.sleep(0.1)  # draining a line that never goes quiet

    assert abort_console_transports() == 1
    assert transport.aborts == 1

    await release

    assert loop.time() - start < 0.3 + 0.3
    assert transport not in _live_console_transports


class SameTurnReader:
    """A reader whose first read completes only when the test says so; then quiet."""

    def __init__(self) -> None:
        self.first: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self.started = asyncio.Event()
        self.calls = 0

    async def read(self, n: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            return await self.first
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_cancel_in_the_turn_a_drain_read_completes_propagates(
    abort_spy: list[tuple[Any, Any]],
) -> None:
    events: list[str] = []
    reader, writer, transport = fake_stream(events, SameTurnReader())
    release = asyncio.create_task(
        release_telnet_stream(reader, writer, transport, single_client=True)
    )
    await reader.started.wait()

    # Same loop turn: the drain's read completes and the release is cancelled.
    reader.first.set_result(b"late output")
    release.cancel()

    with pytest.raises(asyncio.CancelledError):
        await release
    assert len(abort_spy) == 1
    assert writer.closes == 1  # only the abort's close, never a graceful one


class UnanswerableTransport(RecordingTransport):
    """A transport that fails to answer one of the release's questions."""

    def __init__(self, events: list[str], failing: str) -> None:
        super().__init__(events)
        self.failing = failing

    def get_write_buffer_size(self) -> int:
        if self.failing == "buffer_size":
            raise OSError("buffer size unavailable")
        return 0

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if self.failing == "extra_info":
            raise OSError("extra info unavailable")
        return default


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing",
    ["buffer_size", "extra_info"],
    ids=["buffer-unanswered-aborts", "socket-unanswered-aborts"],
)
async def test_release_never_raises_when_the_transport_cannot_answer(failing: str) -> None:
    """Neither failure raises, and both end the conservative way.

    A failed buffer query means the release is never observed. A failed
    socket lookup is not evidence that there is no socket: the fd may still
    be open, so the release is never observed either. Both abort, and the
    emergency sweep keeps the transport.
    """
    loop = asyncio.get_running_loop()
    events: list[str] = []
    transport = UnanswerableTransport(events, failing)
    writer = RecordingWriter(events, transport)
    _register_console_transport(transport)

    await release_telnet_stream(
        RecordingReader(events), writer, transport, single_client=False, deadline=loop.time() + 0.1
    )

    assert transport.aborts == 1
    assert transport in _live_console_transports


@pytest.mark.asyncio
async def test_abort_and_unregister_soon_unregisters_after_one_turn_real_writer() -> None:
    async with real_stream() as s:
        _abort_and_unregister_soon(s.writer, s.transport, s.sock)

        assert s.transport in _live_console_transports
        await asyncio.sleep(0)

        assert s.sock.fileno() == -1
        assert s.transport not in _live_console_transports


def test_abort_and_unregister_soon_without_running_loop_stays_registered() -> None:
    events: list[str] = []
    _, writer, transport = fake_stream(events)

    _abort_and_unregister_soon(writer, transport, None)

    assert transport.aborts == 1
    assert transport in _live_console_transports


def test_abort_telnet_stream_never_raises() -> None:
    class Broken:
        def close(self) -> None:
            raise OSError("close failed")

        def abort(self) -> None:
            raise OSError("abort failed")

    telnet.abort_telnet_stream(Broken(), Broken())
    telnet.abort_telnet_stream(None, None)


# The opener: a cancellation inside telnetlib3's ``open_connection``, before
# otto holds the writer, still releases what the factory built.

BANNER = [b"*** Booting Zephyr OS ***\r\n", b"uart:~$ ", b"\r\nuart:~$ "]
NEVER_NEGOTIATES: dict[str, Any] = {"encoding": False, "connect_minwait": 30, "connect_maxwait": 30}


def record_drain(monkeypatch: pytest.MonkeyPatch, peer: SilentTelnetPeer) -> None:
    """Record every byte the release's drain reads into ``peer.drained``."""
    real = telnet._read_within

    async def recording(reader: Any, timeout: float) -> bytes | None:
        data = await real(reader, timeout)
        if data:
            peer.drained.append(data)
        return data

    monkeypatch.setattr(telnet, "_read_within", recording)


async def registered_transport(opener: "asyncio.Task[Any]") -> Any:
    """Wait until *opener* has registered its transport, and return it."""
    await wait_for_async(
        lambda: bool(_live_console_transports) or opener.done(),
        2.0,
        interval=0.01,
        on_timeout="the opener never registered its transport",
    )
    assert not opener.done()
    (transport,) = _live_console_transports
    return transport


async def peer_saw_eof(peer: SilentTelnetPeer) -> None:
    await wait_for_async(
        lambda: peer.drained_at_eof is not None,
        2.0,
        interval=0.01,
        on_timeout="the peer never saw the client's EOF",
    )


@pytest.mark.asyncio
async def test_cancel_during_establishment_aborts_releases_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    real_create_connection = loop.create_connection
    seen: dict[str, Any] = {}

    async def cancelled_after_connection_made(factory: Any, *args: Any, **kwargs: Any) -> Any:
        setup = asyncio.current_task()
        assert setup is not None

        def protocol() -> Any:
            built = factory()
            made = built.connection_made

            def connection_made(transport: Any) -> None:
                made(transport)
                seen["transport"] = transport
                seen["sock"] = transport.get_extra_info("socket")
                seen["registered"] = transport in _live_console_transports
                setup.cancel()  # the setup waiter is cancelled; asyncio closes the transport

            built.connection_made = connection_made
            return built

        return await real_create_connection(protocol, *args, **kwargs)

    monkeypatch.setattr(loop, "create_connection", cancelled_after_connection_made)
    async with SilentTelnetPeer() as peer:
        with pytest.raises(asyncio.CancelledError):
            await telnet.open_telnet_connection(
                "127.0.0.1", port=peer.port, single_client=True, **NEVER_NEGOTIATES
            )

    assert seen["registered"] is True
    assert seen["transport"].is_closing()
    assert seen["sock"].fileno() == -1
    assert not _live_console_transports


@pytest.mark.asyncio
async def test_cancel_during_negotiation_drains_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SilentTelnetPeer(BANNER) as peer:
        record_drain(monkeypatch, peer)
        opener = asyncio.create_task(
            telnet.open_telnet_connection(
                "127.0.0.1", port=peer.port, single_client=True, **NEVER_NEGOTIATES
            )
        )
        transport = await registered_transport(opener)
        sock = transport.get_extra_info("socket")
        await asyncio.sleep(0.1)  # the banner has arrived; negotiation never completes
        opener.cancel()

        with pytest.raises(asyncio.CancelledError):
            await opener
        await peer_saw_eof(peer)

    assert peer.drained_at_eof == b"".join(BANNER)
    assert sock.fileno() == -1
    assert not _live_console_transports


@pytest.mark.asyncio
async def test_cancel_during_negotiation_releases_with_the_socket_captured_at_connection_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed open's release observes the socket the opener captured, never a new lookup.

    A lookup made at release time answers unknown here, and an unknown socket
    never counts as released: a release that looked again would wait out the
    budget, abort, and leave the transport registered.
    """
    monkeypatch.setattr(telnet, "CONSOLE_QUIESCE_BUDGET", 0.6)
    async with SilentTelnetPeer(BANNER) as peer:
        opener = asyncio.create_task(
            telnet.open_telnet_connection(
                "127.0.0.1", port=peer.port, single_client=True, **NEVER_NEGOTIATES
            )
        )
        transport = await registered_transport(opener)
        sock = transport.get_extra_info("socket")
        await asyncio.sleep(0.1)  # the banner has arrived; negotiation never completes
        monkeypatch.setattr(telnet, "_transport_socket", lambda _t: telnet._SOCKET_UNKNOWN)
        aborts: list[Any] = []
        real_abort = telnet.abort_telnet_stream
        monkeypatch.setattr(
            telnet,
            "abort_telnet_stream",
            lambda w, t: (aborts.append(t), real_abort(w, t)),
        )
        opener.cancel()

        with pytest.raises(asyncio.CancelledError):
            await opener
        await peer_saw_eof(peer)

    assert aborts == []  # released gracefully, never aborted at the deadline
    assert sock.fileno() == -1
    assert not _live_console_transports


@pytest.mark.asyncio
async def test_callable_factory_registers_early_and_cleans_up_on_cancel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def make_client(**kwargs: Any) -> TelnetlibClient:
        return TelnetlibClient(**kwargs)

    async with SilentTelnetPeer(BANNER) as peer:
        record_drain(monkeypatch, peer)
        with caplog.at_level(logging.WARNING, logger="otto.host.telnet"):
            opener = asyncio.create_task(
                telnet.open_telnet_connection(
                    "127.0.0.1",
                    port=peer.port,
                    client_factory=make_client,
                    single_client=True,
                    **NEVER_NEGOTIATES,
                )
            )
            transport = await registered_transport(opener)
        sock = transport.get_extra_info("socket")
        await asyncio.sleep(0.1)
        opener.cancel()

        with pytest.raises(asyncio.CancelledError):
            await opener
        await peer_saw_eof(peer)

    assert any("NVT decoding" in r.getMessage() for r in caplog.records)
    assert peer.drained_at_eof == b"".join(BANNER)
    assert sock.fileno() == -1
    assert not _live_console_transports


@pytest.mark.asyncio
async def test_negotiation_timer_never_fires_into_a_cancelled_open() -> None:
    """telnetlib3's negotiation timer is stopped before the drain of a cancelled open.

    The timer settles the opener's connected future, which the cancellation
    has already cancelled. Firing during the drain, it would raise
    ``InvalidStateError`` into the loop's exception handler. Short real
    waits put both of its firings (at the minimum and the maximum wait)
    inside a drain kept alive by a chatty peer.
    """
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        async with SilentTelnetPeer([b"tick\r\n"] * 20, gap=0.05) as peer:
            opener = asyncio.create_task(
                telnet.open_telnet_connection(
                    "127.0.0.1",
                    port=peer.port,
                    single_client=True,
                    encoding=False,
                    connect_minwait=0.3,
                    connect_maxwait=0.6,
                )
            )
            transport = await registered_transport(opener)
            sock = transport.get_extra_info("socket")
            await asyncio.sleep(0.1)
            opener.cancel()
            started = loop.time()

            with pytest.raises(asyncio.CancelledError):
                await opener
            await peer_saw_eof(peer)
    finally:
        loop.set_exception_handler(previous)

    assert loop.time() - started > 0.6  # the drain outlived both timer firings
    assert errors == []
    assert sock.fileno() == -1
    assert not _live_console_transports


@pytest.mark.asyncio
@pytest.mark.parametrize("stopper", ["stop_negotiation", "connection_lost"])
async def test_negotiation_timers_live_in_tasks_differential(stopper: str) -> None:
    """Pins the private telnetlib3 coupling :func:`telnet._stop_negotiation` relies on.

    A client protocol built through the opener's factory path keeps its
    pending negotiation timer handles in ``_tasks``, and its own
    ``connection_lost`` cancels them. ``_stop_negotiation`` does the same
    thing, so both stoppers must cancel the same handles. If a telnetlib3
    upgrade moves the timers out of ``_tasks``, this fails loudly instead of
    ``_stop_negotiation`` silently doing nothing.
    """
    from otto.host.nvt import nvt_client_factory

    loop = asyncio.get_running_loop()
    record = telnet._OpenRecord()
    factory = telnet._owned_factory(nvt_client_factory(None), single_client=False, record=record)
    ours, theirs = loopback_socket_pair()
    transport: Any = None
    try:
        transport, protocol = await loop.create_connection(
            lambda: factory(**NEVER_NEGOTIATES), sock=ours
        )
        assert record.protocol is protocol
        await wait_for_async(
            lambda: any(isinstance(h, asyncio.TimerHandle) for h in protocol._tasks),
            2.0,
            interval=0.01,
            on_timeout="telnetlib3 kept no negotiation timer in _tasks",
        )
        handles = list(protocol._tasks)
        assert handles
        assert all(isinstance(h, asyncio.Handle) for h in handles)
        assert not any(h.cancelled() for h in handles)
        assert not protocol._waiter_connected.done()  # negotiation is still pending

        if stopper == "stop_negotiation":
            telnet._stop_negotiation(protocol)
        else:
            transport.abort()
            await wait_for_async(
                lambda: transport.get_extra_info("socket").fileno() == -1,
                2.0,
                interval=0.01,
                on_timeout="connection_lost never ran",
            )
        assert all(h.cancelled() for h in handles)
    finally:
        if transport is None:
            ours.close()
        elif not transport.is_closing():
            transport.abort()
        await asyncio.sleep(0)
        theirs.close()


class _PartialInit(TelnetlibClient):
    """A client whose ``connection_made`` fails after the transport arrived."""

    def connection_made(self, transport: Any) -> None:
        self._transport = transport
        raise RuntimeError("client setup failed after the transport arrived")


def _fake_open_connection(transport: StalledTransport, *, establishing: bool) -> Any:
    """A ``telnetlib3.open_connection`` that connects *transport*, then waits to be cancelled.

    Like asyncio, it carries on when the protocol's ``connection_made``
    raises. With *establishing*, a cancellation closes the transport first,
    as asyncio does when its setup waiter is cancelled.
    """

    async def fake_open(*args: Any, client_factory: Any, **kwargs: Any) -> Any:
        protocol = client_factory(**NEVER_NEGOTIATES)
        protocol.begin_negotiation = lambda: None
        with contextlib.suppress(RuntimeError):
            protocol.connection_made(transport)
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            if establishing:
                transport.close()
            raise

    return fake_open


OPENER_CASES = {
    # case: the window the cancellation lands in
    "partial_init_then_cancel": "negotiation, no reader or writer: abort",
    "negotiation_drain_then_flush_deadline": "negotiation: drain, close, abort at the deadline",
    "observed_release": "establishment: abort, release observed",
    "grace_expires": "establishment: abort, release never observed",
    "cancel_repeated_in_grace": "establishment: abort, second cancel in the grace",
}
NEGOTIATION_CASES = {"partial_init_then_cancel", "negotiation_drain_then_flush_deadline"}


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_kind", ["class", "callable"])
@pytest.mark.parametrize("case", list(OPENER_CASES), ids=list(OPENER_CASES))
async def test_opener_lifecycle(
    factory_kind: str,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    stalled_transport: Callable[..., StalledTransport],
) -> None:
    """A cancelled open releases its stream by window; see ``OPENER_CASES``.

    In the establishment cases asyncio has already closed the transport, so
    it is aborted. In the negotiation cases the transport is live: one with
    a reader and writer is drained, closed, and aborted when its blocked
    flush outlives the (shortened) budget; one whose setup failed is aborted.
    """
    import telnetlib3

    loop = asyncio.get_running_loop()
    if case == "negotiation_drain_then_flush_deadline":
        monkeypatch.setattr(telnet, "CONSOLE_QUIESCE_BUDGET", 0.4)
    if case == "cancel_repeated_in_grace":
        # A grace that waited through the second cancel would see the release
        # land at 0.3 s and unregister; the widened grace leaves room for that.
        monkeypatch.setattr(telnet, "_ABORT_GRACE", 0.5)
        transport = stalled_transport(release_after_seconds=0.3)
    elif case == "grace_expires":
        transport = stalled_transport(release_after_turns=None)
    else:
        transport = stalled_transport(release_after_turns=1)
    base = _PartialInit if case == "partial_init_then_cancel" else TelnetlibClient
    factory: Any = base if factory_kind == "class" else functools.partial(base)
    monkeypatch.setattr(
        telnetlib3,
        "open_connection",
        _fake_open_connection(transport, establishing=case not in NEGOTIATION_CASES),
    )
    in_grace = asyncio.Event()
    real_await = telnet._await_local_release

    async def entering_grace(*args: Any, **kwargs: Any) -> bool:
        in_grace.set()
        return await real_await(*args, **kwargs)

    monkeypatch.setattr(telnet, "_await_local_release", entering_grace)

    opener = asyncio.create_task(
        telnet.open_telnet_connection("h", port=23, client_factory=factory, single_client=True)
    )
    assert await registered_transport(opener) is transport
    opener.cancel()
    cancelled_at = loop.time()
    second_cancel = 0.0
    if case == "cancel_repeated_in_grace":
        await wait_for_async(
            in_grace.is_set, 2.0, interval=0.005, on_timeout="the abort's grace never began"
        )
        second_cancel = loop.time()
        opener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opener

    assert in_grace.is_set()  # the real abort-and-observe ran
    assert transport.aborts == 1
    if case == "cancel_repeated_in_grace":
        assert loop.time() - second_cancel < 0.05
        assert transport.sock.fileno() != -1
        assert transport in _live_console_transports
    elif case == "grace_expires":
        assert transport.sock.fileno() != -1
        assert transport in _live_console_transports
    else:
        assert transport.sock.fileno() == -1
        assert transport not in _live_console_transports
    if case == "negotiation_drain_then_flush_deadline":
        # It drained to quiet and waited out the budget before the abort.
        assert loop.time() - cancelled_at >= 0.4
