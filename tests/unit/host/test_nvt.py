"""Telnet NVT decoding (RFC 854): ``CR NUL`` is a bare CR, decoded before any pattern sees it.

A telnet server sends a bare carriage return as ``CR NUL``; nothing in
telnetlib3 decodes it, so the NUL used to reach ``CommandResult.value``
(``printf 'a\\rb'`` came back as ``'a\\x00b'`` once the CR was deleted, #713).
The decoder runs where telnetlib3 feeds in-band bytes to the reader, so the
inbound BINARY state it consults is the state AT THAT STREAM POSITION, and
every read on the stream (the session's pattern reads, the console client's
classification reads, the quiesce drain) sees decoded bytes.
"""

import asyncio
import re
from unittest.mock import MagicMock

import pytest

from otto.host.nvt import NvtDecoder, nvt_client_factory


class TestNvtDecoder:
    def test_a_bare_cr_arrives_as_cr_nul_and_decodes_to_cr(self):
        assert NvtDecoder().decode(b"a\r\x00b", binary=False) == b"a\rb"

    def test_cr_lf_is_left_alone(self):
        assert NvtDecoder().decode(b"a\r\nb\r\n", binary=False) == b"a\r\nb\r\n"

    def test_a_nul_not_after_a_cr_is_data(self):
        assert NvtDecoder().decode(b"a\x00b", binary=False) == b"a\x00b"

    def test_only_the_nul_right_after_a_cr_is_dropped(self):
        assert NvtDecoder().decode(b"\r\x00\x00\r\r\x00", binary=False) == b"\r\x00\r\r"

    def test_cr_nul_split_across_two_reads(self):
        d = NvtDecoder()
        # The CR is passed on at once (no latency for a stream that pauses on
        # a CR); what is carried is the fact that the last byte was a CR.
        assert d.decode(b"a\r", binary=False) == b"a\r"
        assert d.decode(b"\x00b", binary=False) == b"b"

    def test_cr_lf_split_across_two_reads(self):
        d = NvtDecoder()
        assert d.decode(b"a\r", binary=False) + d.decode(b"\nb", binary=False) == b"a\r\nb"

    def test_a_consumed_nul_does_not_carry(self):
        d = NvtDecoder()
        assert d.decode(b"a\r\x00", binary=False) == b"a\r"
        assert d.decode(b"\x00", binary=False) == b"\x00"

    def test_binary_on_leaves_every_byte(self):
        assert NvtDecoder().decode(b"a\r\x00b", binary=True) == b"a\r\x00b"

    def test_binary_switched_on_between_cr_and_nul(self):
        """A mode change takes effect at its stream position: the NUL after it is data."""
        d = NvtDecoder()
        assert d.decode(b"a\r", binary=False) == b"a\r"
        assert d.decode(b"\x00", binary=True) == b"\x00"

    def test_binary_switched_off_again(self):
        d = NvtDecoder()
        assert d.decode(b"x\r", binary=True) == b"x\r"
        # The CR before the switch was binary data, so it does not pair with
        # this NUL; after the switch, decoding resumes.
        assert d.decode(b"\x00y\r\x00", binary=False) == b"\x00y\r"


def _client(client_class: "type | None" = None):
    """A client of *client_class* (default: the NVT class), connected to a fake transport."""
    from telnetlib3.client import TelnetClient

    client = (client_class or nvt_client_factory(TelnetClient))(encoding=False)
    # No negotiation: connection_made schedules it on the loop, and these
    # tests feed the protocol by hand.
    client.begin_negotiation = lambda: None
    transport = MagicMock()
    transport.is_closing.return_value = False
    client.connection_made(transport)
    return client


def _receive(client, data: bytes) -> None:
    """Feed *data* the way telnetlib3's protocol does: IAC commands interpreted in-stream."""
    from telnetlib3._base import _process_data_chunk

    _process_data_chunk(data, client.writer, client.reader, None, print)


class TestTheTelnetReaderDecodes:
    @pytest.mark.asyncio
    async def test_a_pattern_read_sees_decoded_bytes(self):
        client = _client()
        _receive(client, b"a\r\x00b\r\nEND")
        got = await client.reader.readuntil_pattern(re.compile(b"END"))
        assert got == b"a\rb\r\nEND"
        client.connection_lost(None)

    @pytest.mark.asyncio
    async def test_cr_nul_split_across_two_packets(self):
        client = _client()
        _receive(client, b"a\r")
        _receive(client, b"\x00b")
        assert await client.reader.read(10) == b"a\rb"
        client.connection_lost(None)

    @pytest.mark.asyncio
    async def test_binary_negotiated_mid_stream_is_honoured_at_its_position(self):
        from telnetlib3.telopt import BINARY, IAC, WILL, WONT

        client = _client()
        _receive(
            client, b"a\r\x00" + IAC + WILL + BINARY + b"b\r\x00" + IAC + WONT + BINARY + b"c\r\x00"
        )
        await asyncio.sleep(0)
        assert await client.reader.read(64) == b"a\rb\r\x00c\r"
        client.connection_lost(None)


class TestTheConnectionSeamInstallsIt:
    @pytest.mark.asyncio
    async def test_open_telnet_connection_passes_an_nvt_client_factory(self, monkeypatch):
        import telnetlib3

        from otto.host import telnet as telnet_mod

        seen: dict = {}

        async def fake_open(*args, **kwargs):
            seen.update(kwargs)
            return MagicMock(), MagicMock()

        monkeypatch.setattr(telnetlib3, "open_connection", fake_open)
        await telnet_mod.open_telnet_connection("h", port=23, encoding=False)
        await _assert_decodes(seen["client_factory"])

    @pytest.mark.asyncio
    async def test_a_caller_supplied_client_class_is_kept_and_decoded(self, monkeypatch):
        import telnetlib3
        from telnetlib3.client import TelnetClient

        from otto.host import telnet as telnet_mod

        class Mine(TelnetClient):
            pass

        seen: dict = {}

        async def fake_open(*args, **kwargs):
            seen.update(kwargs)
            return MagicMock(), MagicMock()

        monkeypatch.setattr(telnetlib3, "open_connection", fake_open)
        await telnet_mod.open_telnet_connection("h", port=23, client_factory=Mine)
        factory = seen["client_factory"]
        assert isinstance(_client(factory), Mine)
        await _assert_decodes(factory)

    @pytest.mark.asyncio
    async def test_nvt_composition_preserved_with_registration(self, monkeypatch):
        import telnetlib3
        from telnetlib3.client import TelnetClient

        from otto.host import telnet as telnet_mod

        class Mine(TelnetClient):
            pass

        seen: dict = {}

        async def fake_open(*args, **kwargs):
            seen.update(kwargs)
            return MagicMock(), MagicMock()

        monkeypatch.setattr(telnetlib3, "open_connection", fake_open)
        # A registry of the test's own, so nothing it registers reaches the process's.
        registry: set = set()
        monkeypatch.setattr(telnet_mod, "_live_console_transports", registry)
        await telnet_mod.open_telnet_connection(
            "h", port=23, client_factory=Mine, single_client=True
        )
        client = _client(seen["client_factory"])
        # Captured now: telnetlib3's connection_lost clears ``_transport``.
        transport = client._transport
        try:
            assert isinstance(client, nvt_client_factory(Mine))
            assert transport in registry
            _receive(client, b"a\r\x00b")
            assert await client.reader.read(10) == b"a\rb"
        finally:
            client.connection_lost(None)
            telnet_mod._unregister_console_transport(transport)
        assert registry == set()

    @pytest.mark.asyncio
    async def test_single_client_kw_not_forwarded_to_telnetlib3(self, monkeypatch):
        import telnetlib3

        from otto.host import telnet as telnet_mod

        seen: dict = {}

        async def fake_open(*args, **kwargs):
            seen.update(kwargs)
            return MagicMock(), MagicMock()

        monkeypatch.setattr(telnetlib3, "open_connection", fake_open)
        await telnet_mod.open_telnet_connection("h", port=23, single_client=True)
        assert "single_client" not in seen
        assert "client_factory" in seen

    @pytest.mark.asyncio
    async def test_registration_happens_in_connection_made(self):
        from otto.host import telnet as telnet_mod
        from otto.utils import wait_for_async
        from tests.unit.host._telnet_fakes import SilentTelnetPeer

        registry = telnet_mod._live_console_transports
        before = set(registry)
        async with SilentTelnetPeer() as peer:
            opener = asyncio.create_task(
                telnet_mod.open_telnet_connection(
                    "127.0.0.1",
                    port=peer.port,
                    single_client=True,
                    encoding=False,
                    connect_minwait=30,
                    connect_maxwait=30,
                )
            )
            try:
                await wait_for_async(
                    lambda: bool(registry - before) or opener.done(),
                    2.0,
                    interval=0.01,
                    on_timeout="the transport was never registered",
                )
                # Registered while telnetlib3 is still negotiating with a peer
                # that never answers: from connection_made, not after the open.
                assert not opener.done()
                (transport,) = registry - before
                assert not transport.is_closing()
            finally:
                opener.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await opener
        assert registry == before


async def _assert_decodes(client_class: type) -> None:
    """A connection of *client_class* reads ``CR NUL`` back as a bare CR."""
    client = _client(client_class)
    _receive(client, b"a\r\x00b")
    assert await client.reader.read(10) == b"a\rb"
    client.connection_lost(None)


@pytest.mark.asyncio
async def test_a_non_class_client_factory_warns_that_decoding_is_lost(monkeypatch, caplog):
    import logging

    import telnetlib3

    from otto.host import telnet as telnet_mod

    async def fake_open(*args, **kwargs):
        return MagicMock(), MagicMock()

    monkeypatch.setattr(telnetlib3, "open_connection", fake_open)
    with caplog.at_level(logging.WARNING, logger="otto.host.telnet"):
        await telnet_mod.open_telnet_connection("h", port=23, client_factory=lambda **_k: None)
    assert any(
        r.levelno == logging.WARNING and "NVT decoding" in r.getMessage() for r in caplog.records
    )
