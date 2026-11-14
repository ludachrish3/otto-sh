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
import weakref
from collections.abc import Awaitable, Callable

OUTPUT_ENCODING = "utf-8"
"""The encoding otto decodes a command's output bytes with.

The readers that turn a command's output bytes into text use it: local exec;
the ssh exec channel, which passes it to asyncssh; and, through
:func:`output_decoder`, the ssh, telnet and local shell sessions and the
interactive bridge; and the console login client's line reads.
"""

READ_CHUNK = 2**16
"""How many bytes a local reader asks its pipe for at a time.

Local exec's line reader and the local shell session's
:class:`PatternReader` both read this much, so a long line arrives in a few
reads, not one read per byte.
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


MAX_SEPARATOR_LEN = 256
"""How far back, in characters, a chunked read of a BOUNDED pattern looks into text it searched.

The window of the optimisation :func:`bounded_pattern` states the contract of.
"""


_BOUNDED_PATTERNS: "weakref.WeakSet[object]" = weakref.WeakSet()


def bounded_pattern(pattern: "re.Pattern[str]") -> "re.Pattern[str]":
    """Return *pattern*, recorded as one otto built whose context fits :data:`MAX_SEPARATOR_LEN`.

    **Every pattern otto waits for, its own or a caller's, on every
    transport, matches exactly as one** :meth:`re.Pattern.search` **over all
    the output not yet matched would**: the same match, and nothing after it
    consumed. That is otto's one match type.

    Recording a pattern here changes only how much work the reader does to
    find that match, never which match it finds: a reader of a chunked
    stream may resume each search :data:`MAX_SEPARATOR_LEN` characters
    before where the last one ended, instead of from the start of the
    unmatched text, so a long line costs work in proportion to its length,
    not its length times its number of chunks. The result is identical
    because of what a recorded pattern must be: made wholly of otto's own
    parts (a session's end marker, a newline), every match at most
    :data:`MAX_SEPARATOR_LEN` characters long, with no anchor or lookaround
    reaching further back. Such a match lies wholly inside the resumed
    window, and no search before it could have matched. A pattern that
    includes a caller's part is never passed here.

    The one way a reader learns that a pattern may be searched so: the code
    that BUILDS the pattern states it, here, and nothing inspects a
    pattern's source.
    """
    _BOUNDED_PATTERNS.add(pattern)
    return pattern


def is_bounded(pattern: "re.Pattern[str]") -> bool:
    """Whether *pattern* was recorded by :func:`bounded_pattern`."""
    return pattern in _BOUNDED_PATTERNS


class PatternReader:
    """What a session's reader has read from a byte stream and not yet matched.

    The one reader a session uses to wait for a pattern on a stream it reads
    in chunks: the ssh and local shell sessions and both interactive-bridge
    adapters. It reads whatever *read_remote* returns (a chunk, of any size)
    and decodes it with one held :func:`output_decoder`. The match is the one
    :func:`bounded_pattern` states, whatever its chunk sizes.

    A match is consumed (like :meth:`asyncio.StreamReader.readuntil`): a later
    read only sees what came after it, and what was read past the final match
    is handed on by :meth:`take_residual` when automation ends.
    """

    def __init__(self, read_remote: Callable[[], Awaitable[bytes]]) -> None:
        self._read_remote = read_remote
        # The unmatched text is ``"".join(self._head) + self._tail``: the tail
        # holds what the next search can still reach (the window and what
        # arrived since), the head what lies before it. Kept apart so a long
        # line is copied once, not once per chunk.
        self._head: list[str] = []
        self._tail = ""
        # Chunks can split a multi-byte character; the held decoder joins it.
        self._decoder = output_decoder()

    @property
    def unmatched(self) -> str:
        """The text read and not yet matched."""
        return "".join(self._head) + self._tail

    async def read_until(self, pattern: re.Pattern[str]) -> str | None:
        """Return the text through *pattern*'s first match, consuming it.

        ``None`` at end of stream, with the decoder flushed into
        :attr:`unmatched`, as every output reader does.
        """
        if self._head:
            self._tail = self.unmatched
            self._head = []
        windowed = is_bounded(pattern)
        start = 0
        while True:
            match = pattern.search(self._tail, start)
            if match is not None:
                matched = "".join(self._head) + self._tail[: match.end()]
                self._head = []
                self._tail = self._tail[match.end() :]
                return matched
            # One character more than the window stays in the tail, so the
            # next search starts past the tail's first character: ``^`` and a
            # lookbehind see the real character before the window.
            # A caller's pattern keeps the whole text and is searched from its
            # start again: its context is not otto's to bound.
            if windowed:
                keep = MAX_SEPARATOR_LEN + 1
                if len(self._tail) > keep:
                    self._head.append(self._tail[:-keep])
                    self._tail = self._tail[-keep:]
                start = max(0, len(self._tail) - MAX_SEPARATOR_LEN)
            chunk = await self._read_remote()
            if not chunk:
                self._tail += self._decoder.decode(b"", final=True)
                return None
            self._tail += self._decoder.decode(chunk)

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        """:meth:`read_until`, raising at end of stream as a session read does.

        The session hook's contract
        (:meth:`otto.host.session.ShellSession.read_transport_until`): end of
        stream raises :exc:`asyncio.IncompleteReadError` carrying the
        unmatched text.
        """
        matched = await self.read_until(pattern)
        if matched is None:
            raise asyncio.IncompleteReadError(self.unmatched.encode(OUTPUT_ENCODING), None)
        return matched

    def take_unmatched(self) -> str:
        """Return and clear the text read and not yet matched, keeping the decoder.

        A character the last read left split stays in the decoder, so the
        next read completes it. After a command's timeout that next read is
        the recovery's, whose text is dropped: the taken text then ends one
        character short, with no replacement character.
        """
        text = self.unmatched
        self._head = []
        self._tail = ""
        return text

    def take_residual(self) -> bytes:
        """Return and clear whatever was read past the last match, as bytes.

        The bytes of a character the last read left split are still in the
        decoder, not the text: they follow it, so the raw pump's next bytes
        complete the character instead of arriving without their start.
        """
        residual = self.unmatched.encode(OUTPUT_ENCODING) + held_bytes(self._decoder)
        self._head = []
        self._tail = ""
        self._decoder = output_decoder()
        return residual
