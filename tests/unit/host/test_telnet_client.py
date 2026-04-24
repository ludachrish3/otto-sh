"""Unit tests for :class:`otto.host.telnet.TelnetClient`.

Focused on the bits that are tricky to exercise end-to-end: NAWS framing,
which has to bypass telnetlib3's default IAC-escaping on outbound writes,
and the ``interactive`` switch on :meth:`TelnetClient.connect` that
controls whether ``DONT ECHO`` is negotiated.
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest
from telnetlib3.telopt import IAC, NAWS, SB, SE

from otto.host.telnet import TelnetClient


@pytest.fixture
def client() -> TelnetClient:
    c = TelnetClient(host='10.0.0.1', user='u', password='p')
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
        assert frame == IAC + SB + NAWS + struct.pack('>HH', 80, 24) + IAC + SE

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
        c = TelnetClient(host='10.0.0.1', user='u', password='p')
        assert c.writer is None
        c._send_naws(80, 24)  # must not raise

    def test_negative_dimensions_clamped_to_zero(self, client: TelnetClient):
        """Guard the ``struct.pack`` against negative-dimension crashes
        from weird terminal-size reports."""
        client._send_naws(-1, -5)
        frame = client.writer.send_iac.call_args.args[0]
        payload = frame[3:-2]
        assert payload == b'\x00\x00\x00\x00'
