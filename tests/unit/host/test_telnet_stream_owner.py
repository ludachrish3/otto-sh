"""Both telnet-framed clients own their stream through one core (#386).

A :class:`~otto.host.telnet.TelnetClient` and a
:class:`~otto.host.console.ConsoleClient` install, take, release and abort a
stream the same way, so every scenario here runs against both: a second
closer waits for the first, a reconnect waits for the release under way and
keeps its own pending release, a cancelled release still ends, and a sync
abort is idempotent and unregisters only once the release is observed.

The release itself is blocked or stubbed in the ownership scenarios: what
they pin is who holds the stream, when a closer returns and what a reconnect
waits for. The release sequence has its own guards
(``test_telnet_release.py``, ``test_telnet_release_sites.py``).

The last scenario cancels an open in the turn its TCP setup completes, with
the real telnetlib3 opener and a real socket: neither client may publish the
stream.
"""

import asyncio
from typing import Any

import pytest

from otto.host import console as console_mod
from otto.host import telnet as telnet_mod
from otto.host.console import ConsoleClient
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.telnet import TelnetClient
from tests.unit.host._telnet_fakes import SilentTelnetPeer, fake_stream

_KINDS = ["telnet", "console"]


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> set[Any]:
    """A fresh emergency registry per test, so nothing reaches the process's real one."""
    fresh: set[Any] = set()
    monkeypatch.setattr(telnet_mod, "_live_console_transports", fresh)
    return fresh


class _Dials:
    """The opener both clients dial through, handing out *streams* in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, streams: list[tuple[Any, Any]]) -> None:
        self.streams = list(streams)
        self.dialled = 0

        async def dial(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            self.dialled += 1
            return self.streams.pop(0)

        monkeypatch.setattr(telnet_mod, "open_telnet_connection", dial)
        monkeypatch.setattr(console_mod, "open_telnet_connection", dial)


def _fresh(events: list[str]) -> tuple[Any, Any]:
    reader, writer, _ = fake_stream(events)
    return reader, writer


def _client(kind: str, **options: Any) -> Any:
    """A single-client telnet client, or a console client, that logs nothing in or out."""
    if kind == "telnet":
        return TelnetClient(
            host="127.0.0.1",
            user="",
            password=None,
            options=TelnetOptions(login=False, single_client_console=True, **options),
        )
    port = options.pop("port", 54321)
    return ConsoleClient(
        host="127.0.0.1",
        port=port,
        user="u",
        password=None,
        options=ConsoleOptions(
            server="s", port=4001, login=False, logout=False, settle=0, **options
        ),
    )


async def _dial(client: Any) -> None:
    """Connect without login, echo negotiation or settle reads: the dial alone."""
    if isinstance(client, ConsoleClient):
        await client.open(interactive=True)
    else:
        await client.connect(interactive=True)


async def _turns(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _block_release(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, calls: list[Any]) -> None:
    """Both clients' release records the stream it was given, then waits on *gate*."""

    async def release(reader: Any, writer: Any, transport: Any, **kwargs: Any) -> None:
        calls.append(writer)
        await gate.wait()

    monkeypatch.setattr(telnet_mod, "release_telnet_stream", release)
    monkeypatch.setattr(console_mod, "release_telnet_stream", release)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_a_second_closer_waits_for_the_first_release(kind, monkeypatch):
    events: list[str] = []
    _Dials(monkeypatch, [_fresh(events)])
    client = _client(kind)
    await _dial(client)
    gate = asyncio.Event()
    calls: list[Any] = []
    _block_release(monkeypatch, gate, calls)

    first = asyncio.ensure_future(client.close())
    await _turns()
    second = asyncio.ensure_future(client.close())
    await _turns()
    assert client.reader is None
    assert client.writer is None
    assert client.read_gate.closed
    assert not first.done()
    assert not second.done(), "a second closer returned while the first was still releasing"

    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1.0)
    assert len(calls) == 1
    assert client._released.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_a_reconnect_waits_for_the_release_and_keeps_its_own(kind, monkeypatch):
    """A dial during a release waits for it; that release's end is never the new stream's."""
    events: list[str] = []
    dials = _Dials(monkeypatch, [_fresh(events), _fresh(events)])
    client = _client(kind)
    await _dial(client)
    gate = asyncio.Event()
    _block_release(monkeypatch, gate, [])

    closing = asyncio.ensure_future(client.close())
    await _turns()
    reconnecting = asyncio.ensure_future(_dial(client))
    await _turns()
    assert dials.dialled == 1, "the new stream dialled while the old one was still being released"

    gate.set()
    await asyncio.wait_for(asyncio.gather(closing, reconnecting), 1.0)
    assert dials.dialled == 2
    assert client.alive
    assert not client.read_gate.closed
    assert not client._released.is_set(), "the old release's end was taken for the new stream's"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_a_cancelled_release_still_ends_and_frees_later_closers(kind, monkeypatch):
    events: list[str] = []
    _Dials(monkeypatch, [_fresh(events)])
    client = _client(kind)
    await _dial(client)
    _block_release(monkeypatch, asyncio.Event(), [])

    first = asyncio.ensure_future(client.close())
    await _turns()
    second = asyncio.ensure_future(client.close())
    await _turns()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not first.done(), "a cancelled second closer stopped the first one's release"

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert client._released.is_set()
    await asyncio.wait_for(client.close(), 0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_abort_is_sync_idempotent_and_unregisters_once_observed(kind, monkeypatch, registry):
    events: list[str] = []
    reader, writer, transport = fake_stream(events)
    _Dials(monkeypatch, [(reader, writer)])
    client = _client(kind)
    await _dial(client)

    client.abort()
    client.abort()

    assert transport.aborts == 1
    assert client.reader is None
    assert client.writer is None
    assert client.read_gate.closed
    assert client._released.is_set()
    assert transport in registry, "unregistered before its release was observed"
    await asyncio.sleep(0)
    assert transport not in registry
    await asyncio.wait_for(client.close(), 0.05)


# --- A cancellation that reaches the opener after its TCP setup finished. ---


@pytest.mark.asyncio
@pytest.mark.parametrize("negotiation_pending", [False, True], ids=["negotiated", "pending"])
@pytest.mark.parametrize("kind", _KINDS)
async def test_cancel_after_tcp_setup_publishes_no_client(
    kind, negotiation_pending, monkeypatch, registry
):
    """The open is cancelled in the turn telnetlib3's own TCP setup completes.

    On Python 3.10 and 3.11 telnetlib3's ``asyncio.wait_for`` around that
    setup returns its result and drops a cancellation delivered then. Without
    otto's own cancellation boundary the open would succeed and install a
    live stream on a client whose caller asked it to stop. With
    ``negotiated``, negotiation is marked done before the setup returns, so
    nothing after it suspends. With ``pending`` it is left pending, so
    telnetlib3 carries on into its negotiation wait (``connect_maxwait``,
    far beyond the test) after dropping the cancel; the cancel must still
    reach the caller promptly.
    """
    loop = asyncio.get_running_loop()
    real_create_connection = loop.create_connection
    opening: dict[str, Any] = {}

    async def setup_then_cancel(factory: Any, *args: Any, **kwargs: Any) -> Any:
        transport, protocol = await real_create_connection(factory, *args, **kwargs)
        opening["transport"] = transport
        opening["sock"] = transport.get_extra_info("socket")
        if not negotiation_pending:
            protocol._waiter_connected.set_result(None)
        loop.call_soon(opening["task"].cancel)
        return transport, protocol

    monkeypatch.setattr(loop, "create_connection", setup_then_cancel)
    # A TCP-connect timeout, so telnetlib3 wraps its setup in ``wait_for``;
    # negotiation timers far beyond the test.
    negotiation = {"connect_minwait": 30, "connect_maxwait": 30}
    async with SilentTelnetPeer() as peer:
        if kind == "telnet":
            client = _client(kind, port=peer.port, connect_timeout=5.0, extra=negotiation)
        else:
            client = _client(kind, port=peer.port, extra=negotiation)
        opening["task"] = asyncio.ensure_future(_dial(client))
        started = loop.time()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(opening["task"], 10.0)
        finally:
            client.abort()  # a published stream must not outlive the test
        elapsed = loop.time() - started

    assert client.reader is None
    assert client.writer is None
    assert client._released.is_set()
    # The opener released what it built: the fd is closed and unregistered.
    assert opening["sock"].fileno() == -1
    assert opening["transport"] not in registry
    # The release's drain to quiet, never telnetlib3's negotiation wait.
    assert elapsed < 2.0
