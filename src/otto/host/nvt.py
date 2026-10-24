"""Telnet NVT decoding for every telnet stream otto reads: ``CR NUL`` becomes a bare ``CR``.

RFC 854 makes a telnet sender transmit a bare carriage return as
``CR NUL`` (and a newline as ``CR LF``) unless inbound BINARY (RFC 856) is
in force. telnetlib3 interprets IAC commands but leaves that pairing alone,
so without this layer the NUL reaches whatever reads the stream: the shell
session's output, the console client's prompt classification, a pattern a
caller is waiting for.

The decoder sits where telnetlib3's protocol feeds in-band bytes to its
reader (its ``feed_data``), not around the reads. That is the
one place where the BINARY state is known *at the bytes' stream position*:
the protocol interprets an ``IAC WILL BINARY`` between two feeds, so a feed
after it already sees the new state. Every reader method then works on
decoded bytes, which is what "before any pattern is searched" requires.

otto never negotiates BINARY itself; this only honours a server that does.
"""

import functools
from collections.abc import Callable
from typing import Any

__all__ = ["NvtDecoder", "nvt_client_factory"]

_CR = 0x0D
_NUL = 0x00


class NvtDecoder:
    """Streaming ``CR NUL`` → ``CR`` decoder that carries a trailing CR across reads.

    A CR is passed on at once, so a stream that pauses on one (a progress
    line) is never held back; what carries to the next chunk is the fact that
    the last byte was an undecoded CR, so a NUL opening that chunk is the
    second half of the pair and is dropped.
    """

    def __init__(self) -> None:
        self._after_cr = False

    def decode(self, data: bytes, *, binary: bool) -> bytes:
        """Return *data* decoded; *binary* is whether inbound BINARY is in force for it."""
        if binary:
            # Binary data has no NVT pairing, and a CR in it pairs with nothing after.
            self._after_cr = False
            return data
        if not data:
            return data
        if self._after_cr and data[0] == _NUL:
            data = data[1:]
        # The pair is consumed whole, so a CR that ends the chunk is the only
        # one still waiting for its second byte.
        self._after_cr = bool(data) and data[-1] == _CR
        return data.replace(b"\r\x00", b"\r")


def _decoding_reader(base: Any) -> type:
    """Return a subclass of the telnetlib3 reader *base* whose ``feed_data`` decodes first."""

    class NvtReader(base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.nvt = NvtDecoder()
            # Wired to the writer's ``inbinary`` once the client has both.
            self.inbinary: Callable[[], bool] = lambda: False

        def feed_data(self, data: bytes) -> None:
            super().feed_data(self.nvt.decode(data, binary=self.inbinary()))

    NvtReader.__name__ = NvtReader.__qualname__ = f"Nvt{base.__name__}"
    return NvtReader


def _client_base(base: "type | None") -> Any:
    """Return *base*, or the client class :func:`telnetlib3.open_connection` would pick.

    ``Any`` because the class is only known at connect time, and a class
    statement needs a class object, which this always returns.
    """
    from telnetlib3 import accessories
    from telnetlib3.client import TelnetClient, TelnetTerminalClient

    if base is not None:
        return base
    return TelnetTerminalClient if accessories.is_a_tty() else TelnetClient


def nvt_client_factory(base: "type | None" = None) -> type:
    """Return a telnetlib3 client class like *base* whose reader decodes NVT ``CR NUL``.

    *base* defaults to what :func:`telnetlib3.open_connection` itself would
    pick (the terminal client when stdin is a tty), so the only difference
    from the default is the decoding. The default is resolved on every call,
    as ``open_connection`` resolves it, and the class is cached per base, so
    every connection of one kind shares one class.
    """
    return _nvt_client_class(_client_base(base))


@functools.cache
def _nvt_client_class(parent: Any) -> type:
    """Build (once per *parent*) the decoding subclass :func:`nvt_client_factory` returns."""
    from telnetlib3.stream_reader import TelnetReader, TelnetReaderUnicode

    class NvtClient(parent):
        _reader_factory = _decoding_reader(TelnetReader)
        _reader_factory_encoding = _decoding_reader(TelnetReaderUnicode)

        def connection_made(self, transport: Any) -> None:
            super().connection_made(transport)
            writer = self.writer
            if isinstance(getattr(self.reader, "nvt", None), NvtDecoder):
                self.reader.inbinary = lambda: bool(writer.inbinary)

    NvtClient.__name__ = NvtClient.__qualname__ = f"Nvt{parent.__name__}"
    return NvtClient
