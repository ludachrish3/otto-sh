"""Unit tests for TelnetSession._write byte-level chunking.

An embedded RTOS shell behind a slow/UART-backed telnet link (e.g. a Zephyr
QEMU `-serial telnet:` bridge) overruns its console RX FIFO when a multi-KB
command line — `llext load_hex <hex>` — arrives as one burst, dropping
characters and destroying otto's command framing. ``write_chunk_size`` /
``write_chunk_delay`` pace the write so the device keeps up.
"""

import pytest

from otto.host.session import TelnetSession


class _FakeWriter:
    """Records every telnetlib3 ``write`` call (bytes), like a StreamWriter."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def is_closing(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_write_unchunked_by_default() -> None:
    """Default (write_chunk_size=0) sends the whole payload in one write."""
    w = _FakeWriter()
    sess = TelnetSession(reader=None, writer=w)
    await sess._write("x" * 200)
    assert w.writes == [b"x" * 200]


@pytest.mark.asyncio
async def test_write_splits_into_chunks() -> None:
    """A payload larger than the chunk size is split into <=chunk-size writes."""
    w = _FakeWriter()
    sess = TelnetSession(reader=None, writer=w, write_chunk_size=64, write_chunk_delay=0.0)
    await sess._write("x" * 200)
    assert [len(c) for c in w.writes] == [64, 64, 64, 8]
    assert b"".join(w.writes) == b"x" * 200


@pytest.mark.asyncio
async def test_write_smaller_than_chunk_is_single_write() -> None:
    """A payload at or below the chunk size is one write."""
    w = _FakeWriter()
    sess = TelnetSession(reader=None, writer=w, write_chunk_size=64, write_chunk_delay=0.0)
    await sess._write("abc")
    assert w.writes == [b"abc"]


@pytest.mark.asyncio
async def test_chunking_preserves_crlf_to_cr_substitution() -> None:
    """Normalization (CRLF/LF -> CR) still applies, and the reassembled chunks
    match the normalized payload across chunk boundaries.
    """
    w = _FakeWriter()
    sess = TelnetSession(reader=None, writer=w, write_chunk_size=4, write_chunk_delay=0.0)
    await sess._write("ab\r\ncd\nef")
    assert b"".join(w.writes) == b"ab\rcd\ref"
