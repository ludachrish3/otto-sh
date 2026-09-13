"""A single-client console must be read to silence before it is released (#324).

The wedge these guard against: a Zephyr ``shell_telnet`` backend whose ``send``
fails because the connection was released while it still had queued output
takes a restart path that leaves it dead for the guest's life. otto's part is
to let the console finish talking — go quiet — before the connection is
released, on the handshake-failure teardown path (``_fail_init``), where the
transport is still live. Quiet (not FIN-vs-RST) is what makes the release safe,
and quiet is observable through a hop, so this holds on the tunneled path too.

These drive the real ``TelnetSession`` over a loopback peer that behaves like
the guest: it keeps sending for a while, never sends the readiness marker, and
(via a concurrent reader that times the client's EOF) records whether otto held
the console open until the burst finished. EOF is a reliable close signal on
loopback where a write into a just-reset socket is not, so the contract is
asserted on "did otto wait for quiet", not on a racy peer-side send failure.
"""

import asyncio
import time
from contextlib import suppress

import pytest
from typing_extensions import Self  # `typing.Self` is 3.11+; otto's floor is 3.10

from otto.host.command_frame import ZephyrFrame
from otto.host.options import TelnetOptions
from otto.host.session import TelnetSession
from otto.host.telnet import TelnetClient


class _GuestLikePeer:
    """Loopback server: burst for ``burst`` s (never the marker), and time the client's EOF.

    ``eof_after_burst`` is True iff the client was still attached when the burst
    finished — i.e. otto held the console open until it went quiet, instead of
    releasing it mid-send (the shape that wedges a Zephyr console, #324).
    """

    def __init__(self, *, burst: float) -> None:
        self.burst = burst
        self.eof_after_burst: bool | None = None
        self.burst_done = False
        self.connections = 0
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in self._handlers:
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._handlers.add(asyncio.current_task())  # type: ignore[arg-type]

        async def _watch_eof() -> None:
            # First empty read is the client's FIN/close. Record whether the
            # burst had already finished by then.
            while await reader.read(4096):
                pass
            self.eof_after_burst = self.burst_done

        async def _burst_forever() -> None:
            while True:
                with suppress(ConnectionError, OSError):
                    writer.write(b"uart: tick tick\r\n")  # never the READY marker
                    await writer.drain()
                await asyncio.sleep(0.01)

        watcher = asyncio.ensure_future(_watch_eof())
        self._handlers.add(watcher)
        try:
            # Bound the burst with wait_for rather than a hand-rolled deadline
            # loop (architecture gate G6, no-handrolled-deadline-poll).
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_burst_forever(), timeout=self.burst)
            self.burst_done = True
        finally:
            writer.close()


def _client(port: int, *, single: bool = True) -> TelnetClient:
    return TelnetClient(
        host="127.0.0.1",
        user="",
        password="",
        options=TelnetOptions(
            login=False, port=port, echo_negotiation_timeout=0.05, single_client_console=single
        ),
    )


@pytest.mark.asyncio
async def test_failed_handshake_holds_the_console_open_until_it_goes_quiet() -> None:
    """_fail_init must read the console to silence before releasing the connection."""
    # Burst outlasts the readiness ceiling: the handshake fails at 0.2 s while the
    # peer is still sending, the production shape of a fast failure.
    async with _GuestLikePeer(burst=0.6) as peer:
        client = _client(peer.port)
        await client.connect()
        session = TelnetSession(
            client.reader,
            client.writer,
            _owned_client=client,
            command_frame=ZephyrFrame(),
            init_timeout=0.2,
        )
        with pytest.raises(ConnectionError):
            await session._ensure_initialized()  # never matches the marker → _fail_init
        await asyncio.sleep(0.05)  # let the peer's EOF watcher settle
        assert peer.eof_after_burst is True, (
            "otto released the console before it went quiet — the connection closed while the "
            "guest was still sending, the shape that wedges a Zephyr console (#324)"
        )


@pytest.mark.asyncio
async def test_quiesce_is_bounded_when_the_console_never_goes_quiet(monkeypatch) -> None:
    """A console that never stops talking must not turn a fast failure into a hang."""
    from otto.host import session as session_mod

    monkeypatch.setattr(session_mod, "_CONSOLE_QUIESCE_BUDGET", 0.3)
    async with _GuestLikePeer(burst=10.0) as peer:
        client = _client(peer.port)
        await client.connect()
        session = TelnetSession(
            client.reader,
            client.writer,
            _owned_client=client,
            command_frame=ZephyrFrame(),
            init_timeout=0.1,
        )
        started = time.monotonic()
        with pytest.raises(ConnectionError):
            await session._ensure_initialized()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"teardown took {elapsed:.2f}s against a console that never quiets"


@pytest.mark.asyncio
async def test_non_console_telnet_does_not_quiesce(monkeypatch) -> None:
    """A plain (multi-client) telnet shell must not pay the quiesce budget."""
    from otto.host import session as session_mod

    calls: list[str] = []
    real = session_mod.TelnetSession._quiesce

    async def spy(self):
        calls.append("quiesce")
        return await real(self)

    monkeypatch.setattr(session_mod.TelnetSession, "_quiesce", spy)
    async with _GuestLikePeer(burst=0.3) as peer:
        client = _client(peer.port, single=False)
        await client.connect()
        session = TelnetSession(
            client.reader, client.writer, _owned_client=client, init_timeout=0.15
        )  # default frame: single_client_console is False
        with pytest.raises(ConnectionError):
            await session._ensure_initialized()
    assert calls == [], "quiesce ran for a non-single-client console"
