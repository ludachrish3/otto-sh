"""Every release of a single-client console runs one sequence, from every site (#386).

Zephyr's ``shell_telnet`` wedges when otto closes the line while the guest
still has output queued, so every release drains the line to quiet first.
The release has one home (:func:`otto.host.telnet.release_telnet_stream`),
reached through each client's ``close()``. These are the cross-site guards:

- the call-point guard: one behavioural stream, driven through every async
  release site, records the same sequence from each. A site that skips the
  drain, closes the writer itself or unregisters before the socket is
  released records a different one;
- a default session and its manager closing the one cached client: one
  drain, the second closer waits, the hop closes last;
- a command cancelled mid-output, then ``host.close()``: the drain reads the
  rest before the close, and a fresh connection follows;
- the bounded sweep of four hosts whose consoles never go quiet;
- ``asyncio.run`` shutdown cancelling a release that has started.

The release itself is never stubbed: a stub would pass for any site that
merely calls it.
"""

import asyncio
import contextlib
import re
from typing import Any

import pytest
from typing_extensions import Self, override

from otto import invocation as inv
from otto.host import ZephyrHost
from otto.host import telnet as telnet_mod
from otto.host.connections import ConnectionManager
from otto.host.element import Element
from otto.host.options import TelnetOptions
from otto.host.session import SessionManager, TelnetSession
from otto.host.telnet import TelnetClient
from otto.logger.mode import LogMode
from otto.utils import wait_for_async
from tests._fixtures.registry import registered_ids
from tests.unit.host._telnet_fakes import (
    RecordingReader,
    RecordingWriter,
    opened_console,
    stream_client,
)

_TAIL = [b"tail-1", b"tail-2"]
"""What the guest still has queued when the release starts."""

_SEQUENCE = ["read:tail-1", "read:tail-2", "close", "fd_released", "unregister"]
"""The one release: drain the queued output, close, see the socket released, unregister."""


class _RecordingRegistry(set[Any]):
    """The emergency registry, recording each transport it drops."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    @override
    def discard(self, item: Any) -> None:
        if item in self:
            self.events.append("unregister")
        super().discard(item)


@pytest.fixture(autouse=True)
def events(monkeypatch) -> list[str]:
    """One event log, with a fresh emergency registry recording into it.

    Every test gets its own registry, so what one leaves registered never
    reaches another, nor the process's real one.
    """
    log: list[str] = []
    monkeypatch.setattr(telnet_mod, "_live_console_transports", _RecordingRegistry(log))
    return log


class _Socket:
    """The socket under an :class:`_ObservedTransport`: open until the transport releases it."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fd = 7

    def fileno(self) -> int:
        return self.fd

    def release(self) -> None:
        if self.fd != -1:
            self.fd = -1
            self.events.append("fd_released")


class _ObservedTransport:
    """A transport whose socket closes a loop turn after it does, as asyncio's does.

    Closing (by the writer, or an abort) only schedules the socket's release,
    so a release that unregisters before it has seen ``fileno() == -1``
    records ``unregister`` before ``fd_released``.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.sock = _Socket(events)
        self.closing = False
        self.aborts = 0

    def _schedule_release(self) -> None:
        if not self.closing:
            self.closing = True
            asyncio.get_running_loop().call_soon(self.sock.release)

    def close(self) -> None:
        self._schedule_release()

    def abort(self) -> None:
        self.aborts += 1
        self.events.append("abort")
        self._schedule_release()

    def is_closing(self) -> bool:
        return self.closing

    def get_write_buffer_size(self) -> int:
        return 0

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self.sock if name == "socket" else default


class _Writer(RecordingWriter):
    """A recording writer whose close closes its transport, as telnetlib3's does."""

    def close(self) -> None:
        self.closes += 1
        self.events.append("close")
        self.transport.close()


def _stream(events: list[str], reader: Any = None) -> tuple[Any, _Writer, _ObservedTransport]:
    """A registered stream whose guest still has :data:`_TAIL` queued, then goes quiet."""
    transport = _ObservedTransport(events)
    writer = _Writer(events, transport)  # type: ignore[arg-type]
    telnet_mod._register_console_transport(transport)
    return reader if reader is not None else RecordingReader(events, _TAIL), writer, transport


def _telnet_manager(client: TelnetClient | None = None) -> ConnectionManager:
    """A real telnet-term manager whose cached client is *client*."""
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="telnet", name="box", host_id="box")
    mgr._telnet_conn = client
    return mgr


async def _default_session(mgr: ConnectionManager) -> Any:
    """The default session a real :class:`SessionManager` builds on *mgr*'s cached client."""
    return await SessionManager(connections=mgr, name="box")._open_transport_session(named=False)


async def _turns(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# --- The sites. Each takes a fresh stream and releases it its own way. ---


async def _telnet_client_close(reader: Any, writer: Any) -> None:
    await stream_client(reader, writer, single_client=True).close()


async def _console_client_close(reader: Any, writer: Any) -> None:
    await (await opened_console(reader, writer)).close()


async def _shared_session_close(reader: Any, writer: Any) -> None:
    session = await _default_session(
        _telnet_manager(stream_client(reader, writer, single_client=True))
    )
    await session.close()


async def _recovering_session_close(reader: Any, writer: Any) -> None:
    session = await _default_session(
        _telnet_manager(stream_client(reader, writer, single_client=True))
    )
    session._needs_recovery = True
    await session.close()


async def _manager_close(reader: Any, writer: Any) -> None:
    await _telnet_manager(stream_client(reader, writer, single_client=True)).close()


async def _failed_init(reader: Any, writer: Any) -> None:
    client = stream_client(reader, writer, single_client=True)
    session = TelnetSession(reader, writer, client=client)
    with pytest.raises(ConnectionError):
        await session._fail_init()


_SITES = {
    "TelnetClient.close": _telnet_client_close,
    "ConsoleClient.close": _console_client_close,
    "TelnetSession.close (shared client)": _shared_session_close,
    "TelnetSession.close (needs recovery)": _recovering_session_close,
    "ConnectionManager.close": _manager_close,
    "_fail_init": _failed_init,
}


@pytest.mark.asyncio
async def test_every_async_release_site_runs_the_same_sequence(events):
    """The call-point guard: every async release site records the one release sequence.

    Each site gets the same stream: a guest with output still queued, a
    socket that closes a loop turn after its transport, and a registry
    entry. Each records drain, close, socket released, unregister, in that
    order, and nothing else: no abort, no second close.
    """
    recorded: dict[str, list[str]] = {}
    for name, site in _SITES.items():
        events.clear()
        reader, writer, transport = _stream(events)
        await site(reader, writer)
        await _turns()  # what a site left scheduled lands in its own record
        if transport in telnet_mod._live_console_transports:
            events.append("still registered")
        recorded[name] = list(events)
    assert recorded == dict.fromkeys(_SITES, _SEQUENCE)


# --- A default session and its manager close the one cached client. ---


class _ChattyReader:
    """A guest that keeps talking until *quiet* is set; a second concurrent read fails.

    The failure is telnetlib3's own ("already waiting"), so two drains of
    one stream at once cannot pass unnoticed.
    """

    def __init__(self, quiet: asyncio.Event) -> None:
        self.quiet = quiet
        self.started = asyncio.Event()
        self.reads = 0
        self._pending = False

    async def read(self, n: int) -> bytes:
        if self._pending:
            raise RuntimeError("read() called while another coroutine is already waiting")
        self._pending = True
        try:
            self.reads += 1
            self.started.set()
            if self.quiet.is_set():
                await asyncio.get_running_loop().create_future()
            await asyncio.sleep(0.01)
            return b"tick\r\n"
        finally:
            self._pending = False


class _Hop:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("hop")


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["session", "manager"])
async def test_default_session_and_manager_close_drain_once_hop_last(events, first):
    """The normal teardown of every Zephyr bed host, with the real session and manager.

    The default session is built by a real :class:`SessionManager` on the
    manager's cached client. Both close it at once, while the guest is still
    talking: one drain runs, the second closer waits for it, and the hop
    closes only after the stream is released, once.
    """
    quiet = asyncio.Event()
    reader = _ChattyReader(quiet)
    _, writer, transport = _stream(events, reader)
    mgr = _telnet_manager(stream_client(reader, writer, single_client=True))
    mgr._hop = _Hop(events)
    session = await _default_session(mgr)

    closers = [session.close(), mgr.close()]
    if first == "manager":
        closers.reverse()
    tasks = [asyncio.ensure_future(c) for c in closers]
    try:
        await reader.started.wait()
        await asyncio.sleep(0.1)  # the drain is reading a guest that is still talking
        assert reader.reads > 1
        assert not any(task.done() for task in tasks), "a closer returned under a running drain"
        assert events == [], "the stream was closed, or the hop torn down, under a running drain"

        quiet.set()
        await asyncio.gather(*tasks)
    finally:
        # An assertion above must not leave the closers pending.
        quiet.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert events == ["close", "fd_released", "unregister", "hop"]
    assert writer.closes == 1
    assert transport.aborts == 0


# --- A command cancelled mid-output, then the host closed. ---


_TOKEN = re.compile(r"__OTTO_\w+?__")


class _ZephyrPeer:
    """A loopback Zephyr-like shell: rejects otto's marker tokens, runs ``retval`` and ``spew``.

    ``spew`` sends a burst of output for *burst* seconds. ``eof_after_burst``
    records, when the client's EOF arrives, whether the burst had finished,
    so a test can tell a close after quiet from a close mid-output. A second
    client while one is attached is refused, as ``shell_telnet`` refuses it.
    """

    def __init__(self, *, burst: float) -> None:
        self.burst = burst
        self.spew_started = asyncio.Event()
        self.burst_done = False
        self.eof_after_burst: bool | None = None
        self.accepted = 0
        self.refused = 0
        self.attached = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        if self.attached:
            self.refused += 1
            writer.close()
            return
        self.attached += 1
        self.accepted += 1
        lines: asyncio.Queue[str] = asyncio.Queue()
        shell = asyncio.ensure_future(self._shell(lines, writer))
        self._tasks.add(shell)
        try:
            pending = ""
            while data := await reader.read(4096):
                pending += data.decode("latin-1")
                *complete, pending = re.split(r"[\r\n]", pending)
                for line in complete:
                    lines.put_nowait(line)
            self.eof_after_burst = self.burst_done
        except ConnectionError:
            pass
        finally:
            shell.cancel()
            await asyncio.gather(shell, return_exceptions=True)
            self.attached -= 1
            writer.close()

    async def _shell(self, lines: "asyncio.Queue[str]", writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                line = await lines.get()
                if token := _TOKEN.search(line):
                    writer.write(f"\r\n{token.group()}: command not found\r\nuart:~$ ".encode())
                elif line.endswith("retval"):
                    writer.write(b"\r\n0\r\nuart:~$ ")
                elif line.endswith("spew"):
                    await self._spew(writer)
                elif line.strip():
                    writer.write(f"\r\n{line.strip()}\r\nuart:~$ ".encode())
                await writer.drain()

    async def _spew(self, writer: asyncio.StreamWriter) -> None:
        async def _lines_forever() -> None:
            count = 0
            while True:
                count += 1
                writer.write(f"spew line {count}\r\n".encode())
                await writer.drain()
                await asyncio.sleep(0.01)

        self.spew_started.set()
        # Bound the burst with wait_for rather than a hand-rolled deadline
        # loop (architecture gate G6, no-handrolled-deadline-poll).
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_lines_forever(), timeout=self.burst)
        writer.write(b"\r\nuart:~$ ")
        self.burst_done = True


def _zephyr(port: int, name: str = "zephyr37_fat") -> ZephyrHost:
    """A Zephyr host on a loopback peer; the family forces single-client and no login."""
    return ZephyrHost(
        ip="127.0.0.1",
        element=Element(name=name),
        log=LogMode.QUIET,
        telnet_options=TelnetOptions(
            port=port,
            echo_negotiation_timeout=0.05,
            extra={"connect_minwait": 0, "connect_maxwait": 0.05},
        ),
    )


@pytest.mark.asyncio
async def test_cancel_then_close_drains_and_reconnects(monkeypatch):
    """A command cancelled mid-output, then ``host.close()``: drained before close, then redialled.

    The peer is still sending when the cancellation lands. The close reads
    every byte it sends before it closes the line (the peer's EOF arrives
    after its burst, and nothing is left unread on otto's side), and a
    fresh ``ConnectionManager.telnet()`` then connects to the single-client
    peer.
    """
    async with _ZephyrPeer(burst=0.4) as peer:
        host = _zephyr(peer.port)
        try:
            assert (await host.run("hello")).is_ok
            client = host.connections._telnet_conn
            assert client is not None
            reader, writer = client.reader, client.writer
            at_close: list[tuple[bool, int]] = []
            real_close = writer.close

            def close() -> None:
                # ``_buffer`` is telnetlib3's private read buffer: test
                # instrumentation coupled to the pinned telnetlib3 version.
                at_close.append((peer.burst_done, len(reader._buffer)))
                real_close()

            monkeypatch.setattr(writer, "close", close)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(host.run("spew"), 0.05)
            assert peer.spew_started.is_set(), "the command was cancelled before its output began"
            assert not peer.burst_done, "the command was cancelled after its output ended"
            await host.close()

            # telnetlib3 buffers what arrives; an empty buffer at the close
            # means the drain read every byte the guest sent.
            assert at_close == [(True, 0)]
            await wait_for_async(
                lambda: peer.attached == 0,
                1.0,
                interval=0.01,
                on_timeout="the peer never saw the close's EOF",
            )
            assert peer.eof_after_burst is True

            fresh = await host.connections.telnet()
            assert fresh.alive
            assert fresh is not client
            assert (peer.accepted, peer.refused) == (2, 0)
        finally:
            await host.close()


# --- The bounded sweep of hosts whose consoles never go quiet. ---


class _NeverQuietPeer:
    """A loopback console that talks forever to every client it accepts."""

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            while True:
                writer.write(b"uart: tick tick\r\n")
                await writer.drain()
                await asyncio.sleep(0.01)
        except ConnectionError:
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_bounded_teardown_cancels_four_never_quiet_hosts(monkeypatch):
    """Four single-client hosts that never go quiet, in one layer: the sweep's deadline bounds them.

    Each release would drain for its full budget, longer than the sweep's
    deadline, so the deadline's cancellation ends them, and each release's
    cancellation arm aborts its stream. Every socket is then released, or
    its transport is still registered for the emergency sweep, and no close
    is left running.
    """
    # The sweep's deadline must cut the releases short, not their own budget.
    assert telnet_mod.CONSOLE_QUIESCE_BUDGET > 1.0
    loop = asyncio.get_running_loop()
    draining: list[None] = []
    all_draining = asyncio.Event()
    real_drain = telnet_mod._drain_to_quiet

    # A counting pass-through around the real drain: test instrumentation,
    # coupled to the private name ``_drain_to_quiet``.
    async def drain(reader: Any, deadline: float) -> bool:
        draining.append(None)
        if len(draining) == 4:
            all_draining.set()
        return await real_drain(reader, deadline)

    monkeypatch.setattr(telnet_mod, "_drain_to_quiet", drain)
    captured: list[tuple[Any, Any]] = []
    async with _NeverQuietPeer() as peer:
        try:
            hosts = [_zephyr(peer.port, f"zephyr_{n}") for n in range(4)]
            for host in hosts:
                client = await host.connections.telnet()
                transport = client.writer.transport
                captured.append((transport, transport.get_extra_info("socket")))
                assert transport in telnet_mod._live_console_transports
            assert sorted(registered_ids(loop)) == sorted(h.id for h in hosts)

            started = loop.time()
            sweep = asyncio.ensure_future(inv.sweep_unheld(loop, label="t", deadline=1.0))
            entered = asyncio.ensure_future(all_draining.wait())
            await asyncio.wait({sweep, entered}, return_when=asyncio.FIRST_COMPLETED)
            assert entered.done(), "the sweep ended before all four releases drained"
            assert not sweep.done()
            await sweep
            elapsed = loop.time() - started

            assert elapsed < 1.0 + 0.5, f"the sweep took {elapsed:.2f} s"
            for transport, sock in captured:
                assert sock.fileno() == -1 or transport in telnet_mod._live_console_transports
            # Here each cancelled release saw its abort land: all four were released.
            assert [sock.fileno() for _, sock in captured] == [-1] * 4
            assert not telnet_mod._live_console_transports
            outstanding = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and task not in peer.tasks
            ]
            assert outstanding == []
        finally:
            for transport, _ in captured:
                transport.abort()  # a no-op on one already released


# --- asyncio.run shutdown cancels a release that has started. ---


def test_asyncio_run_shutdown_aborts_started_release(stalled_transport):
    """A release under way when ``asyncio.run`` returns is aborted in its cancellation arm.

    The close's drain never ends (the guest never goes quiet). The main
    coroutine waits until the drain has started, then returns, and
    ``asyncio.run`` cancels the close. A close cancelled before it ever
    started is outside this claim (#828).
    """
    transport = stalled_transport()
    outcome: dict[str, Any] = {}

    async def main() -> None:
        quiet = asyncio.Event()  # never set: the guest talks forever
        reader = _ChattyReader(quiet)
        writer = RecordingWriter([], transport)  # type: ignore[arg-type]
        telnet_mod._register_console_transport(transport)
        client = stream_client(reader, writer, single_client=True)
        outcome["close"] = asyncio.ensure_future(client.close())
        await reader.started.wait()
        outcome["aborts_before_shutdown"] = transport.aborts

    asyncio.run(main())
    assert outcome["aborts_before_shutdown"] == 0
    assert outcome["close"].cancelled()
    assert transport.aborts == 1
    assert transport.sock.fileno() == -1
    assert transport not in telnet_mod._live_console_transports
