"""Telnet stream doubles shared by the release, NVT, interact and session tests.

A fake reader, writer and socketless transport that record what the release
did to them, a real ``TelnetClient`` holding given streams (every
``TelnetSession`` closes through a client), and a loopback TCP pair for tests
that run a real telnetlib3 client.
"""

import asyncio
import contextlib
import socket
from typing import Any
from unittest.mock import patch

from typing_extensions import Self

from otto.host import console as console_mod
from otto.host.console import ConsoleClient
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.telnet import TelnetClient, _register_console_transport


def loopback_socket_pair() -> tuple[socket.socket, socket.socket]:
    """A connected TCP socket pair on 127.0.0.1.

    ``socket.socketpair()`` gives AF_UNIX sockets, whose empty peer name
    breaks telnetlib3's protocol ``repr`` in its ``connection_made`` log line,
    so the connection never completes. A loopback pair is the same thing over
    the family telnetlib3 expects.
    """
    with socket.create_server(("127.0.0.1", 0)) as server:
        ours = socket.create_connection(server.getsockname())
        theirs, _ = server.accept()
    return ours, theirs


class RecordingReader:
    """A reader that returns scripted chunks, then *then*: ``"quiet"`` or ``"eof"``."""

    def __init__(
        self,
        events: list[str],
        chunks: list[bytes] | None = None,
        *,
        gap: float = 0.0,
        then: str = "quiet",
    ) -> None:
        self.events = events
        self.chunks = list(chunks or [])
        self.gap = gap
        self.then = then
        self.calls = 0

    async def read(self, n: int) -> bytes:
        self.calls += 1
        if self.chunks:
            await asyncio.sleep(self.gap)
            data = self.chunks.pop(0)
            self.events.append(f"read:{data.decode()}")
            return data
        if self.then == "eof":
            return b""
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable")


class RecordingWriter:
    """A writer that records its close and closes its transport."""

    def __init__(self, events: list[str], transport: "RecordingTransport") -> None:
        self.events = events
        self.transport = transport
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self.events.append("close")
        self.transport.closing = True

    def is_closing(self) -> bool:
        return self.transport.closing


class RecordingTransport:
    """A socketless transport: released once closing with an empty buffer."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closing = False
        self.aborts = 0

    def get_write_buffer_size(self) -> int:
        return 0

    def is_closing(self) -> bool:
        return self.closing

    def abort(self) -> None:
        self.aborts += 1
        self.closing = True
        self.events.append("abort")

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def fake_stream(
    events: list[str], reader: Any = None
) -> tuple[Any, RecordingWriter, RecordingTransport]:
    """A registered fake stream: *reader* (or a quiet one), a recording writer and transport."""
    transport = RecordingTransport(events)
    writer = RecordingWriter(events, transport)
    _register_console_transport(transport)
    return reader if reader is not None else RecordingReader(events), writer, transport


def stream_client(reader: Any, writer: Any, *, single_client: bool = False) -> TelnetClient:
    """A real ``TelnetClient`` holding *reader* and *writer* as if it had connected.

    The client a test's ``TelnetSession`` is built on: the session reads
    through this client's read gate and closes through its ``close()``, so the
    streams and the gate are the ones the test drives. *single_client* is the
    client's ``single_client_console``, which decides whether its release
    drains the line first.
    """
    client = TelnetClient(
        host="127.0.0.1",
        user="",
        password=None,
        options=TelnetOptions(login=False, single_client_console=single_client),
    )
    if reader is not None or writer is not None:
        client._install(reader, writer)  # as connect() installs a stream
    return client


async def opened_console(reader: Any, writer: Any, *, logout: bool = False) -> ConsoleClient:
    """A real ``ConsoleClient`` that opened *reader* and *writer* through its own ``open()``.

    The dial is the only thing faked: ``open_telnet_connection`` hands back
    the given streams, so the client installs them, its fresh read gate and
    its pending release exactly as a real open does. No login, no echo
    negotiation and no settle read, so nothing reads the stream before the
    test does.
    """
    client = ConsoleClient(
        host="localhost",
        port=54321,
        user="u",
        password=None,
        options=ConsoleOptions(server="s", port=4001, login=False, logout=logout, settle=0),
    )

    async def dial(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        return reader, writer

    with patch.object(console_mod, "open_telnet_connection", dial):
        await client.open(interactive=True)
    return client


class SilentTelnetPeer:
    """A loopback telnet server that sends a banner and never answers negotiation.

    So a client opened against it stays in telnetlib3's negotiation until its
    ``connect_minwait`` passes. ``drained`` is the caller's record of what its
    side read; ``drained_at_eof`` is a copy of it taken when this peer sees
    the client's EOF, so a test can assert what was read before the close.
    """

    def __init__(self, banner: list[bytes] | None = None, *, gap: float = 0.02) -> None:
        self.banner = list(banner or [])
        self.gap = gap
        self.drained: list[bytes] = []
        self.drained_at_eof: bytes | None = None
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            for chunk in self.banner:
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(self.gap)
            while await reader.read(4096):
                pass
            self.drained_at_eof = b"".join(self.drained)
        except ConnectionError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
