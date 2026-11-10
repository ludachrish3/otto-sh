"""Unit tests for :class:`otto.host.telnet.TelnetClient`.

Focused on the bits that are tricky to exercise end-to-end: NAWS framing,
which has to bypass telnetlib3's default IAC-escaping on outbound writes,
the ``interactive`` switch on :meth:`TelnetClient.connect` that
controls whether ``DONT ECHO`` is negotiated, and the client's half of a
release: it takes its stream before its first await, releases it through
:func:`otto.host.telnet.release_telnet_stream`, and makes a second closer
wait for the first.
"""

import asyncio
import contextlib
import struct
from unittest.mock import MagicMock

import pytest
from telnetlib3.telopt import IAC, NAWS, SB, SE

from otto.host import telnet as telnet_mod
from otto.host.telnet import ReadGateClosed, TelnetClient
from otto.utils import wait_for_async
from tests._fixtures.loopback_servers import closed_port
from tests.unit.host._telnet_fakes import RecordingReader, SilentTelnetPeer, fake_stream


@pytest.fixture
def client() -> TelnetClient:
    c = TelnetClient(host="10.0.0.1", user="u", password="p")
    # _send_naws() only touches self.writer — install a mock that mirrors
    # the telnetlib3 TelnetWriter surface area we care about.
    c.writer = MagicMock()
    return c


class TestSendNaws:
    """Regression tests for the NAWS framing bug.

    The bug: ``_send_naws`` used ``writer.write(frame)``, but telnetlib3's
    ``TelnetWriter.write`` escapes every 0xFF byte in its argument to
    ``IAC IAC`` because it assumes the buffer is user payload. That
    doubled the command framing bytes of the NAWS subnegotiation
    (``IAC SB ... IAC SE``), producing garbage that the remote telnetd
    couldn't parse as a command and passed through to the shell.

    The correct API is ``TelnetWriter.send_iac`` which writes raw
    command bytes with no transformation.
    """

    def test_uses_send_iac_not_write(self, client: TelnetClient):
        """``_send_naws`` must NOT route through ``writer.write`` — that
        path re-escapes the framing IAC bytes and corrupts the command."""
        client._send_naws(80, 24)
        client.writer.write.assert_not_called()
        client.writer.send_iac.assert_called_once()

    def test_frame_bytes_are_exact_rfc1073(self, client: TelnetClient):
        """The frame handed to the writer must be literal
        ``IAC SB NAWS <cols16> <rows16> IAC SE`` with no extra escaping
        of the command framing bytes."""
        client._send_naws(80, 24)
        frame = client.writer.send_iac.call_args.args[0]
        assert frame == IAC + SB + NAWS + struct.pack(">HH", 80, 24) + IAC + SE

    def test_frame_starts_with_single_iac_sb(self, client: TelnetClient):
        """The regression symptom: the old code wrote the frame through
        ``write`` which escaped the leading ``IAC`` to ``IAC IAC``,
        producing ``FF FF FA ...`` instead of ``FF FA ...``. Assert the
        leading bytes are not doubled."""
        client._send_naws(80, 24)
        frame = client.writer.send_iac.call_args.args[0]
        assert frame[:2] == IAC + SB
        assert frame[:3] != IAC + IAC + SB

    def test_frame_ends_with_single_iac_se(self, client: TelnetClient):
        """Same symptom on the tail: trailing ``IAC SE`` must not have
        been escaped to ``IAC IAC SE``."""
        client._send_naws(80, 24)
        frame = client.writer.send_iac.call_args.args[0]
        assert frame[-2:] == IAC + SE
        assert frame[-3:] != IAC + IAC + SE

    def test_literal_ff_in_payload_is_doubled(self, client: TelnetClient):
        """NAWS payload must still escape literal 0xFF bytes per RFC 1073,
        because the remote parser cannot otherwise distinguish them from
        the IAC SE terminator. cols=0xFF00 has a 0xFF high byte; assert
        it appears doubled in the payload region."""
        client._send_naws(0xFF00, 24)
        frame = client.writer.send_iac.call_args.args[0]
        # Strip the outer IAC SB NAWS ... IAC SE framing and check the
        # middle bytes directly. The payload slice is everything between
        # the leading IAC SB NAWS (3 bytes) and the trailing IAC SE (2).
        payload = frame[3:-2]
        # cols high byte 0xFF should appear as IAC IAC (doubled).
        assert payload.startswith(IAC + IAC)

    def test_missing_writer_is_noop(self):
        """Degrade gracefully if called before connect/after close."""
        c = TelnetClient(host="10.0.0.1", user="u", password="p")
        assert c.writer is None
        c._send_naws(80, 24)  # must not raise

    def test_negative_dimensions_clamped_to_zero(self, client: TelnetClient):
        """Guard the ``struct.pack`` against negative-dimension crashes
        from weird terminal-size reports."""
        client._send_naws(-1, -5)
        frame = client.writer.send_iac.call_args.args[0]
        payload = frame[3:-2]
        assert payload == b"\x00\x00\x00\x00"


class TestLogin:
    """``login()`` no longer drains to silence.

    The fixed ~1 s silence-drain was removed: when no ``prompt`` is
    configured, ``login()`` returns as soon as the password is written and
    the session's marker handshake becomes the readiness check. A configured
    ``prompt`` remains an opt-in fast path that ``login()`` waits for.
    """

    @staticmethod
    def _login_client(prompt: str | None = None) -> TelnetClient:
        c = TelnetClient(host="10.0.0.1", user="u", password="p", prompt=prompt)
        c.reader = asyncio.StreamReader()
        c.writer = MagicMock()
        return c

    @pytest.mark.asyncio
    async def test_no_prompt_returns_without_fixed_wait(self):
        """With ``prompt=None``, login returns right after the password —
        no silence-drain, so a tight timeout must not trip."""
        c = self._login_client(prompt=None)
        # login: prompt, then password: prompt — both end in the ':' delimiter.
        c.reader.feed_data(b"login:Password:")

        await asyncio.wait_for(c.login(), timeout=0.5)

        writes = b"".join(call.args[0] for call in c.writer.write.call_args_list)
        assert b"u\r\n" in writes
        assert b"p\r\n" in writes

    @pytest.mark.asyncio
    async def test_prompt_fast_path_waits_for_prompt(self):
        """With ``prompt`` set, login blocks until the prompt string appears."""
        c = self._login_client(prompt="ready> ")
        c.reader.feed_data(b"login:Password:")

        login_task = asyncio.create_task(c.login())
        # Not done yet — the prompt hasn't arrived.
        await asyncio.sleep(0.05)
        assert not login_task.done()

        c.reader.feed_data(b"banner text\nready> ")
        await asyncio.wait_for(login_task, timeout=0.5)


class TestConsoleTransportRegistry:
    @pytest.fixture(autouse=True)
    def _clear_registry(self):
        telnet_mod._live_console_transports.clear()
        yield
        telnet_mod._live_console_transports.clear()

    def test_abort_aborts_registered_transport_and_clears(self):
        t = MagicMock()
        telnet_mod._register_console_transport(t)
        n = telnet_mod.abort_console_transports()
        t.abort.assert_called_once_with()
        assert n == 1
        assert len(telnet_mod._live_console_transports) == 0

    def test_register_none_is_noop(self):
        telnet_mod._register_console_transport(None)
        assert len(telnet_mod._live_console_transports) == 0

    def test_unregister_removes_transport(self):
        t = MagicMock()
        telnet_mod._register_console_transport(t)
        telnet_mod._unregister_console_transport(t)
        assert telnet_mod.abort_console_transports() == 0
        t.abort.assert_not_called()

    def test_abort_is_defensive_against_one_bad_transport(self):
        good, bad = MagicMock(), MagicMock()
        bad.abort.side_effect = RuntimeError("already closed")
        telnet_mod._register_console_transport(bad)
        telnet_mod._register_console_transport(good)
        telnet_mod.abort_console_transports()  # must not raise
        good.abort.assert_called_once_with()
        assert len(telnet_mod._live_console_transports) == 0


# ---------------------------------------------------------------------------
# Take, then release
# ---------------------------------------------------------------------------
#
# A client's close takes its stream before its first await, then releases it
# through the one release (``release_telnet_stream``): drain to quiet, close,
# observe the socket's release. A second closer waits for the first.


@pytest.fixture
def _empty_registry():
    telnet_mod._live_console_transports.clear()
    yield
    telnet_mod._live_console_transports.clear()


def _opener(monkeypatch, streams: list, seen: list | None = None) -> None:
    """Patch the opener to hand out the next of *streams*, recording its kwargs in *seen*."""

    async def fake_open(host, **kwargs):
        if seen is not None:
            seen.append(kwargs)
        reader, writer, _transport = streams.pop(0)
        return reader, writer

    monkeypatch.setattr(telnet_mod, "open_telnet_connection", fake_open)


def _client(*, single_client: bool = True) -> TelnetClient:
    from otto.host.options import TelnetOptions

    return TelnetClient(
        host="h",
        user="u",
        password="p",
        options=TelnetOptions(login=False, single_client_console=single_client),
    )


async def _connected(monkeypatch, events: list[str], reader=None, *, single_client: bool = True):
    """A client connected (no login, no ECHO negotiation) to a registered fake stream."""
    stream = fake_stream(events, reader)
    _opener(monkeypatch, [stream])
    client = _client(single_client=single_client)
    await client.connect(interactive=True)
    return client, stream


def _blocking_release(monkeypatch, gate: asyncio.Event, calls: list) -> None:
    """Patch the release so it records its call, then waits on *gate*."""

    async def release(reader, writer, transport, **kwargs):
        calls.append((reader, writer, transport, kwargs))
        await gate.wait()

    monkeypatch.setattr(telnet_mod, "release_telnet_stream", release)


@pytest.mark.usefixtures("_empty_registry")
class TestTakeThenRelease:
    @pytest.mark.asyncio
    async def test_close_takes_before_first_await(self, monkeypatch):
        """Driven by hand up to its first suspension: the stream is already taken there.

        ``coro.send(None)`` runs ``close()`` synchronously until it first
        yields to the loop. Any await that suspends before the take would
        stop it there with the stream still installed.
        """
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        parked = asyncio.get_running_loop().create_future()
        entered: list[bool] = []

        async def release(reader, writer, transport, **kwargs):
            entered.append(True)
            await parked

        monkeypatch.setattr(telnet_mod, "release_telnet_stream", release)
        coro = client.close()
        try:
            coro.send(None)
            assert client.reader is None
            assert client.writer is None
            assert entered == [True]
        finally:
            coro.close()
        assert client._released.is_set()

    @pytest.mark.asyncio
    async def test_close_releases_through_the_one_release(self, monkeypatch):
        """The taken stream, its socket as the take saw it, the single-client flag and the gate."""
        events: list[str] = []
        client, (reader, writer, transport) = await _connected(monkeypatch, events)
        gate = client._read_gate
        calls: list = []
        released = asyncio.Event()
        released.set()
        _blocking_release(monkeypatch, released, calls)
        await client.close()
        assert calls == [
            (reader, writer, transport, {"single_client": True, "reads_idle": gate, "sock": None})
        ]

    @pytest.mark.asyncio
    async def test_close_drains_then_closes_then_unregisters(self, monkeypatch):
        """The old close was ``writer.close(); transport.abort()``, under queued output.

        That abort is what wedges a Zephyr console. Now the queued output is
        read first, then the writer is closed, and nothing is aborted.
        """
        events: list[str] = []
        client, (_, _, transport) = await _connected(
            monkeypatch, events, RecordingReader(events, [b"tail"])
        )
        assert transport in telnet_mod._live_console_transports
        await client.close()
        assert events == ["read:tail", "close"]
        assert transport.aborts == 0
        assert transport not in telnet_mod._live_console_transports
        assert client._released.is_set()

    @pytest.mark.asyncio
    async def test_plain_telnet_close_does_not_drain(self, monkeypatch):
        events: list[str] = []
        client, (_, _, transport) = await _connected(
            monkeypatch, events, RecordingReader(events, [b"tail"]), single_client=False
        )
        await client.close()
        assert events == ["close"]
        assert transport.aborts == 0

    @pytest.mark.asyncio
    async def test_second_close_waits_for_first(self, monkeypatch):
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        gate = asyncio.Event()
        calls: list = []
        _blocking_release(monkeypatch, gate, calls)

        first = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        second = asyncio.create_task(client.close())
        await asyncio.sleep(0.1)
        assert not first.done()
        assert not second.done(), "a second closer returned while the first was still releasing"

        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1.0)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cancelled_second_closer_leaves_the_first_releasing(self, monkeypatch):
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        gate = asyncio.Event()
        _blocking_release(monkeypatch, gate, [])

        first = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        second = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not first.done()
        gate.set()
        await asyncio.wait_for(first, 1.0)
        assert client._released.is_set()

    @pytest.mark.asyncio
    async def test_cancelled_release_still_sets_released(self, monkeypatch):
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        _blocking_release(monkeypatch, asyncio.Event(), [])

        first = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        assert not client._released.is_set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert client._released.is_set()
        await asyncio.wait_for(client.close(), 0.05)

    @pytest.mark.asyncio
    async def test_close_on_never_connected_client_returns_at_once(self):
        await asyncio.wait_for(_client().close(), 0.05)

    @pytest.mark.asyncio
    async def test_abort_is_sync_and_idempotent(self, monkeypatch):
        events: list[str] = []
        client, (_, writer, transport) = await _connected(monkeypatch, events)
        aborts: list = []
        real = telnet_mod.abort_telnet_stream

        def spy(w, t):
            aborts.append((w, t))
            real(w, t)

        monkeypatch.setattr(telnet_mod, "abort_telnet_stream", spy)
        assert not client._released.is_set()

        client.abort()
        client.abort()

        assert aborts == [(writer, transport)]
        assert client._released.is_set()
        assert client.reader is None
        assert client.writer is None
        # Unregistered only once the release is observed, a loop turn later.
        assert transport in telnet_mod._live_console_transports
        await asyncio.sleep(0)
        assert transport not in telnet_mod._live_console_transports
        await asyncio.wait_for(client.close(), 0.05)

    def test_abort_without_a_running_loop_leaves_it_to_the_sweep(self):
        events: list[str] = []
        reader, writer, transport = fake_stream(events)
        client = _client()
        client.reader, client.writer = reader, writer
        client.abort()
        assert transport.aborts == 1
        assert client._released.is_set()
        assert transport in telnet_mod._live_console_transports

    @pytest.mark.asyncio
    async def test_alive_false_once_taken_and_reconnect_works(self, monkeypatch):
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        assert client.alive is True
        gate = asyncio.Event()
        _blocking_release(monkeypatch, gate, [])

        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        assert client.alive is False
        gate.set()
        await closing

        old_gate = client._read_gate
        _opener(monkeypatch, [fake_stream(events)])
        await client.connect(interactive=True)
        assert client.alive is True
        assert client._read_gate is not old_gate
        assert not client._read_gate.closed
        assert not client._released.is_set()

    @pytest.mark.asyncio
    async def test_reconnect_during_an_in_flight_release_waits_for_it(self, monkeypatch):
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        gate = asyncio.Event()
        _blocking_release(monkeypatch, gate, [])
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)

        seen: list[dict] = []
        _opener(monkeypatch, [fake_stream(events)], seen)
        reconnecting = asyncio.create_task(client.connect(interactive=True))
        await asyncio.sleep(0.05)
        assert seen == [], "the new stream dialled while the old one was still being released"
        assert not reconnecting.done()

        gate.set()
        await asyncio.wait_for(asyncio.gather(closing, reconnecting), 1.0)
        assert len(seen) == 1
        assert client.alive is True
        assert not client._released.is_set(), "the old release's end was taken for the new stream's"

    @pytest.mark.asyncio
    async def test_an_old_release_ending_leaves_a_newer_stream_unreleased(self, monkeypatch):
        """``_released`` belongs to the installed stream, never to the one whose release ended."""
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        gate = asyncio.Event()
        _blocking_release(monkeypatch, gate, [])
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)

        # A newer stream installed under the release, as connect() installs one.
        client.reader, client.writer, _ = fake_stream(events)
        client._read_gate = telnet_mod.ReadGate()
        client._released.clear()

        gate.set()
        await asyncio.wait_for(closing, 1.0)
        assert not client._released.is_set()
        assert not client._read_gate.closed


class TestLoginReadGate:
    @staticmethod
    def _login_client() -> TelnetClient:
        c = TelnetClient(host="10.0.0.1", user="u", password="p")
        c.reader = asyncio.StreamReader()
        c.writer = MagicMock()
        return c

    @pytest.mark.asyncio
    async def test_login_read_rejected_after_gate_closed(self):
        c = self._login_client()
        c.reader.feed_data(b"login:Password:")
        assert await c._read_gate.wait_idle(asyncio.get_running_loop().time())
        with pytest.raises(ReadGateClosed):
            await c.login()
        c.writer.write.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_empty_registry")
    @pytest.mark.parametrize("taker", ["abort", "close"])
    async def test_login_read_rejected_after_take(self, monkeypatch, taker):
        """The take closes the gate: a later read fails as a dropped line, not on a None reader."""
        events: list[str] = []
        client, _ = await _connected(monkeypatch, events)
        if taker == "abort":
            client.abort()
        else:
            _blocking_release(monkeypatch, asyncio.Event(), [])
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0)
        assert client._read_gate.closed
        with pytest.raises(ReadGateClosed):
            await client.login()
        if taker == "close":
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing

    @pytest.mark.asyncio
    async def test_a_take_mid_login_surfaces_as_a_lost_connection(self):
        """The read in flight finishes on its own stream; the next write is refused.

        Never an ``AttributeError`` from a write or read on the taken
        (None) attributes, and no credential typed into a stream that is
        being released: the console login refuses the same write the same way.
        """
        c = self._login_client()
        reader, writer = c.reader, c.writer
        login = asyncio.create_task(c.login())
        await asyncio.sleep(0.01)
        assert c._read_gate.in_flight == 1
        c.abort()  # takes the stream while login waits for its first prompt
        assert c.reader is None
        assert c.writer is None

        reader.feed_data(b"login:")  # the read in flight completes on its own stream
        with pytest.raises(ReadGateClosed):
            await asyncio.wait_for(login, 0.5)
        writer.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_read_counts_as_in_flight(self):
        c = self._login_client()
        login = asyncio.create_task(c.login())
        await asyncio.sleep(0.01)
        assert c._read_gate.in_flight == 1
        c.reader.feed_data(b"login:Password:")
        await asyncio.wait_for(login, 0.5)
        assert c._read_gate.in_flight == 0


@pytest.mark.usefixtures("_empty_registry")
class TestOpenerSeam:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("single_client", [True, False])
    async def test_client_passes_single_client_to_opener(self, monkeypatch, single_client):
        """Registration lives in the opener's ``connection_made`` now, never in the client."""
        fake_writer = MagicMock()
        fake_writer.transport = MagicMock()
        seen: list[dict] = []
        _opener(monkeypatch, [(MagicMock(), fake_writer, fake_writer.transport)], seen)
        c = _client(single_client=single_client)
        await c.connect(interactive=True)
        assert seen[0]["single_client"] is single_client
        assert fake_writer.transport not in telnet_mod._live_console_transports


# ---------------------------------------------------------------------------
# A failed open leaves nothing to release
# ---------------------------------------------------------------------------
#
# The opener cleans up a cancelled or failed open itself, before anything is
# installed on the client, so ``_released`` stays set and the caller's
# failure cleanup returns at once.

NEVER_NEGOTIATES = {"connect_minwait": 30, "connect_maxwait": 30}
BANNER = [b"*** Booting Zephyr OS ***\r\n", b"uart:~$ "]


def _cancel_in_establishment(monkeypatch) -> None:
    """Cancel the opening task from ``connection_made``, as a caller timeout there would."""
    loop = asyncio.get_running_loop()
    real_create_connection = loop.create_connection

    async def cancelled_after_connection_made(factory, *args, **kwargs):
        setup = asyncio.current_task()
        assert setup is not None

        def protocol():
            built = factory()
            made = built.connection_made

            def connection_made(transport):
                made(transport)
                setup.cancel()

            built.connection_made = connection_made
            return built

        return await real_create_connection(protocol, *args, **kwargs)

    monkeypatch.setattr(loop, "create_connection", cancelled_after_connection_made)


async def _open_through(path: str, options) -> None:
    from otto.host.connections import ConnectionManager
    from otto.host.login_proxy import Cred
    from otto.host.session import SessionManager

    cm = ConnectionManager(
        ip="127.0.0.1",
        creds=[Cred(login="u", password="p")],
        term="telnet",
        name="h1",
        host_id="h1",
        telnet_options=options,
    )
    if path == "manager":
        await cm.telnet()
    else:
        await SessionManager(connections=cm, name="h1")._open_transport_session(named=True)


@pytest.mark.usefixtures("_empty_registry")
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["manager", "named_session"])
@pytest.mark.parametrize("case", ["refused", "cancel_establishment", "cancel_negotiation"])
async def test_close_after_failed_open_is_bounded(monkeypatch, case, path):
    """A failed or cancelled ``connect()`` leaves a client whose close returns at once.

    Driven through both callers' own failure cleanup, which closes the
    client: ``ConnectionManager.telnet()`` and a named session's dedicated
    client.
    """
    from otto.host.options import TelnetOptions

    loop = asyncio.get_running_loop()
    closes: list[tuple[TelnetClient, float]] = []
    real_close = TelnetClient.close

    async def timed_close(self):
        started = loop.time()
        await real_close(self)
        closes.append((self, loop.time() - started))

    monkeypatch.setattr(TelnetClient, "close", timed_close)

    async with SilentTelnetPeer(BANNER if case == "cancel_negotiation" else None) as peer:
        with contextlib.ExitStack() as stack:  # the refused port stays held through the dial
            port = stack.enter_context(closed_port()) if case == "refused" else peer.port
            options = TelnetOptions(port=port, single_client_console=True, extra=NEVER_NEGOTIATES)
            if case == "cancel_establishment":
                _cancel_in_establishment(monkeypatch)
            opening = asyncio.create_task(_open_through(path, options))
            if case == "cancel_negotiation":
                await wait_for_async(
                    lambda: bool(telnet_mod._live_console_transports) or opening.done(),
                    2.0,
                    interval=0.01,
                    on_timeout="the open never registered its transport",
                )
                await asyncio.sleep(0.1)  # the banner has arrived; negotiation never completes
                opening.cancel()
            expected = OSError if case == "refused" else asyncio.CancelledError
            with pytest.raises(expected):
                await opening

    # The caller's own failure cleanup closed the client, and returned at once.
    ((client, took),) = closes
    assert took < 0.5
    assert client.reader is None
    assert client.writer is None
    assert client._released.is_set()
    await asyncio.wait_for(real_close(client), 0.5)
    assert not telnet_mod._live_console_transports


class TestNawsSize:
    """#785: NAWS reports the real terminal, never an injected ``COLUMNS``."""

    def test_the_resize_fanout_reads_the_terminal(self, monkeypatch):
        from tests._fixtures.terminals import fake_terminals

        sent: list[tuple[int, int]] = []
        recorder = MagicMock()
        recorder._send_naws = lambda c, r: sent.append((c, r))
        monkeypatch.setattr(telnet_mod, "_naws_subscribers", {recorder})
        monkeypatch.setenv("COLUMNS", "200")
        fake_terminals(monkeypatch, {0: (100, 30)})
        telnet_mod._sigwinch_fanout()
        assert sent == [(100, 30)]

    @pytest.mark.asyncio
    async def test_an_unreadable_terminal_sends_the_configured_size(self, monkeypatch):
        from otto.host.options import TelnetOptions
        from tests._fixtures.terminals import fake_terminals

        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(TelnetClient, "_send_naws", lambda self, c, r: sent.append((c, r)))
        monkeypatch.setattr(telnet_mod, "_install_sigwinch_handler", lambda: None)
        monkeypatch.setattr(telnet_mod, "_naws_subscribers", set())
        monkeypatch.setattr(telnet_mod.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setenv("COLUMNS", "200")
        _opener(monkeypatch, [fake_stream([])])
        client = TelnetClient(
            host="h",
            user="u",
            password="p",
            options=TelnetOptions(login=False, auto_window_resize=True, cols=400, rows=24),
        )
        fake_terminals(monkeypatch, {0: OSError(5, "Input/output error")})
        await client.connect(interactive=True)
        assert sent[0] == (400, 24)
