"""How otto decodes the bytes a host's command prints into text.

One decision for every reader of command output, so no two routes can decode
the same bytes differently: UTF-8, with a byte that is not valid UTF-8 replaced
(U+FFFD) instead of raised. The contract it serves is
:ref:`command-output-contract`.

A leaf on purpose: it imports only the standard library, so a light module
such as :mod:`otto.host.console` can use it without loading the session layer.
"""

import asyncio
import codecs
import functools
import re
from collections.abc import Awaitable, Callable

OUTPUT_ENCODING = "utf-8"
"""The encoding otto decodes a command's output bytes with.

The readers that turn a command's output bytes into text use it: local exec;
the ssh exec channel and ssh shell session, which pass it to asyncssh; and,
through :func:`output_decoder`, the telnet and local shell sessions and the
interactive bridge; and the console login client's line reads.
"""

OUTPUT_ERRORS = "replace"
"""How a byte that is not valid :data:`OUTPUT_ENCODING` is decoded: replaced
with U+FFFD, never raised (:ref:`command-output-contract`). asyncssh's own
default is ``strict``, which turns such a byte into a ``ProtocolError``, so a
channel that reads output passes this explicitly."""


def output_decoder() -> codecs.IncrementalDecoder:
    """Return a fresh decoder for one output stream that arrives in pieces.

    A multi-byte character can be split across two reads (a one-byte read
    always splits it). Decoding each read on its own turns the halves into
    U+FFFD; this decoder holds an incomplete tail until the next read
    completes it. Hold one per stream, for the stream's life.
    """
    return codecs.getincrementaldecoder(OUTPUT_ENCODING)(errors=OUTPUT_ERRORS)


def held_bytes(decoder: codecs.IncrementalDecoder) -> bytes:
    """Return the incomplete character *decoder* is holding, as bytes.

    A stream handed on from a decoding reader to a byte-level one carries these
    after the decoded text, so the character is completed downstream instead of
    being lost.
    """
    held, _flag = decoder.getstate()
    return held


@functools.lru_cache(maxsize=64)
def _continuing(held: bytes) -> re.Pattern[bytes]:
    r"""Return a byte pattern for the next byte after *held*, an incomplete character.

    It matches that byte when it continues the character, and otherwise matches
    the empty string in front of it, so a byte-pattern reader takes a
    continuation and leaves any other byte unread. Which bytes continue *held*
    is the codec's answer (complete, or still a valid incomplete prefix), not a
    table kept here. The second alternative is the lookahead ``(?=[\s\S])``,
    not a bare empty one, so that it matches only once a byte has arrived: on
    an empty buffer the read waits for that byte instead of matching nothing
    at once and flushing a character whose continuation is still in transit.
    """
    allowed = []
    for value in range(256):
        candidate = held + bytes([value])
        try:
            candidate.decode(OUTPUT_ENCODING)
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                continue
        allowed.append(b"\\x%02x" % value)
    continuation = b"[" + b"".join(allowed) + b"]|" if allowed else b""
    return re.compile(rb"\A(?:" + continuation + rb"(?=[\s\S]))")


async def finish_character(
    decoder: codecs.IncrementalDecoder,
    read_until: Callable[[re.Pattern[bytes]], Awaitable[bytes]],
) -> str:
    """Finish the character a read left split, and return its text.

    A reader that stops where a *byte* pattern matched can stop inside a
    multi-byte character, so the decoded text lacks the end of the match. This
    reads the rest of that character with *read_until*, a byte-pattern read
    with :meth:`asyncio.StreamReader.readuntil` semantics (telnetlib3's
    ``readuntil_pattern``): it consumes through the match and raises
    :exc:`asyncio.IncompleteReadError` at end of stream.

    It takes only bytes that continue the character, one per read, at most
    three. A byte that cannot continue it is left unread for the stream's next
    read, and the incomplete character shows as U+FFFD; so does one cut off by
    end of stream. It waits for those bytes to arrive, so a peer that sends a
    character's first byte and then falls silent holds the read until the
    caller's timeout, which every read of a session has.
    """
    text = ""
    while held := held_bytes(decoder):
        try:
            byte = await read_until(_continuing(held))
        except asyncio.IncompleteReadError:
            byte = b""
        if not byte:
            return text + decoder.decode(b"", final=True)
        text += decoder.decode(byte)
    return text
