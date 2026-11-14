"""A session reads one long line in work proportional to its length.

The local shell session once read its stdout one byte at a time and searched
the whole text read so far after every byte: a 100 kB line with no newline
cost 100 000 searches over ~5 * 10**9 characters, and its read never yielded
to the event loop while bytes were buffered, so the command's timeout fired
or not depending on when the pipe ran dry. Work is measured here in searches
and characters searched, never in seconds.
"""

import asyncio
import re
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telnetlib3.stream_reader import TelnetReader

from otto.host.output_codec import (
    MAX_SEPARATOR_LEN,
    READ_CHUNK,
    PatternReader,
    bounded_pattern,
)
from otto.host.session import LocalSession, SshSession, TelnetSession
from otto.host.telnet import ReadGate
from tests.unit.host._telnet_fakes import stream_client

MARK = "__LONGLINE_DONE__"


class _CountingPattern:
    """A pattern that counts its searches and the characters each one is given."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self._pattern = pattern
        self.pattern = pattern.pattern
        self.searches = 0
        self.chars_searched = 0

    def search(self, text: str, pos: int = 0) -> re.Match[str] | None:
        self.searches += 1
        self.chars_searched += len(text) - pos
        return self._pattern.search(text, pos)


def _counting() -> _CountingPattern:
    """otto's framing shape (a marker or a newline), recorded as bounded as a session's is."""
    counting = _CountingPattern(re.compile(re.escape(MARK) + r"|\n"))
    bounded_pattern(cast("re.Pattern[str]", counting))
    return counting


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [10_000, 100_000])
async def test_pattern_reader_searches_once_per_chunk(size: int) -> None:
    stream = asyncio.StreamReader(limit=2 * size)
    stream.feed_data(b"x" * size + MARK.encode())
    stream.feed_eof()

    async def read_chunk() -> bytes:
        return await stream.read(READ_CHUNK)

    reader = PatternReader(read_chunk)
    pattern = _counting()
    matched = await reader.read_until(cast("re.Pattern[str]", pattern))

    assert matched == "x" * size + MARK
    chunks = -(-(size + len(MARK)) // READ_CHUNK)
    # One search before the first read, then one per chunk.
    assert pattern.searches == chunks + 1
    # Linear here: at most two chunks, so at most twice the line is searched.
    assert pattern.chars_searched <= 2 * (size + len(MARK))


def _fed(chunks: list[bytes]) -> PatternReader:
    """A reader over a stream that hands out *chunks* one per read, then EOF."""
    pending = list(chunks)

    async def read_chunk() -> bytes:
        return pending.pop(0) if pending else b""

    return PatternReader(read_chunk)


async def _read_through_reader(chunks: list[bytes], pattern: re.Pattern[str]) -> "str | None":
    return await _fed(chunks).read_until(pattern)


async def _read_through_ssh_session(chunks: list[bytes], pattern: re.Pattern[str]) -> str:
    session = SshSession(_ssh_connection(_Fed(chunks)))
    await session.open_transport()
    return await session.read_transport_until(pattern=pattern)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(_read_through_reader, id="reader"),
        pytest.param(_read_through_ssh_session, id="ssh-session"),
    ],
)
async def test_small_chunks_search_each_character_a_bounded_number_of_times(read) -> None:
    """100 kB in 16-byte chunks: the old full rescan searched ~313 M characters.

    The ssh session reads through the same reader, so otto's own pattern over
    one long line costs the same linear work there."""
    line = b"x" * 100_000 + MARK.encode()
    chunks = [line[i : i + 16] for i in range(0, len(line), 16)]
    pattern = _counting()

    matched = await read(chunks, cast("re.Pattern[str]", pattern))

    assert matched == line.decode()
    assert pattern.searches == len(chunks) + 1
    assert pattern.chars_searched <= 2 * (len(line) + MAX_SEPARATOR_LEN * len(chunks))


@pytest.mark.asyncio
async def test_a_match_straddling_a_chunk_boundary_is_found_and_the_rest_kept() -> None:
    reader = _fed([b"x" * 1000 + b"__LONG", b"LINE_DONE__ rest\n"])

    assert await reader.read_until(re.compile(MARK)) == "x" * 1000 + MARK
    assert await reader.read_until(re.compile("\n")) == " rest\n"


@pytest.mark.asyncio
async def test_a_character_split_across_chunks_is_matched_whole() -> None:
    reader = _fed([b"caf\xc3", b"\xa9!"])

    assert await reader.read_until(re.compile("\N{LATIN SMALL LETTER E WITH ACUTE}!")) == (
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}!"
    )


@pytest.mark.asyncio
async def test_a_match_in_the_last_chunk_before_end_of_stream() -> None:
    reader = _fed([b"a" * 500, b"b" * 500 + MARK.encode() + b"tail"])

    assert await reader.read_until(re.compile(MARK)) == "a" * 500 + "b" * 500 + MARK
    assert await reader.read_until(re.compile(MARK)) is None
    assert reader.unmatched == "tail"


@pytest.mark.asyncio
async def test_the_match_is_the_leftmost_within_what_has_arrived() -> None:
    """A greedy expect matches as soon as it can, on the text read so far: the
    ``b`` that arrives later is the next read's."""
    reader = _fed([b"xaab", b"b aab"])

    assert await reader.read_until(re.compile("a+b+")) == "xaab"
    assert await reader.read_until(re.compile("a+b+")) == "b aab"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [10_000, 100_000])
async def test_local_session_reads_a_long_line_in_chunks(size: int) -> None:
    session = LocalSession()
    await session.open_transport()
    try:
        await session.write_transport(
            data=f"head -c {size} /dev/zero | tr '\\0' x; echo {MARK}\n",
        )
        pattern = _counting()
        matched = await asyncio.wait_for(
            session.read_transport_until(pattern=cast("re.Pattern[str]", pattern)),
            timeout=30,
        )
    finally:
        await session.close()

    assert matched == "x" * size + MARK
    # A pipe hands over at least one writer's buffer (512 bytes or more) per
    # read; a reader that searched per byte made `size` searches.
    assert pattern.searches <= size // 512 + 2


# One match type everywhere: every pattern, otto's or a caller's, matches as
# one ``re.search`` over all the output not yet matched would, on every
# transport's reader (bounded_pattern states it). Each case is fed whole and
# split at every boundary listed, through the reader itself and through every
# session adapter that reads a stream, and every split must answer as the
# whole text does. *otto* marks the cases that fit bounded_pattern's
# contract (a short match with no context further back): they run a second
# time recorded as otto's own, which must change only the work, never the
# match.
_PAST_64K = b"x" * 70_000
_CALLER_CASES = [
    pytest.param(
        b"BEGIN" + b"x" * 300, b"END", r"BEGINx{300}END", 0, False, id="longer-than-the-window"
    ),
    pytest.param(
        b"A" + b"x" * 300, b"END", r"A(?=x{300}END)", 0, False, id="lookahead-past-the-window"
    ),
    pytest.param(
        b"y" * 400 + b"\n" + b"x" * 300,
        b"PROMPT",
        r"(?<=\n)x{300}PROMPT",
        0,
        False,
        id="lookbehind",
    ),
    pytest.param(b"z" * 600, b"PROMPT", r"^PROMPT", re.MULTILINE, False, id="anchor-never"),
    pytest.param(
        b"z" * 600 + b"\n", b"PROMPT", r"^PROMPT", re.MULTILINE, False, id="line-anchor-after-600"
    ),
    pytest.param(b"PRO", b"MPT" + b"x" * 300, r"\APROMPT", 0, False, id="text-anchor-at-start"),
    pytest.param(b"z" * 600, b"PROMPT", r"\APROMPT", 0, False, id="text-anchor-never"),
    pytest.param(
        b"x" * 1000 + b"__OTTO_E", b"ND__0__", r"__OTTO_END__\d+__", 0, True, id="straddling"
    ),
    pytest.param(
        b"q" * 500 + b"pass", b"WORD: ", r"password:\s", re.IGNORECASE, True, id="compiled-flags"
    ),
    pytest.param(_PAST_64K, b"__OTTO_END__0__", r"__OTTO_END__\d+__", 0, True, id="past-64KiB"),
    pytest.param(
        _PAST_64K + b"\n", b"PROMPT", r"(?<=\n)PROMPT", 0, False, id="past-64KiB-lookbehind"
    ),
    pytest.param(
        b"q" * 500 + b"pass",
        b"word: ",
        "pass word : \\s  # the prompt, spaced out",
        re.VERBOSE,
        False,
        id="verbose-trailing-comment",
    ),
    pytest.param(b"q" * 500 + b"pass", b"WORD: ", r"(?i)password:\s", 0, False, id="inline-i"),
    pytest.param(b"A\n", b"\nB", r"A.*B", re.DOTALL, False, id="dotall"),
]

# The cases that also run as a caller's ``expects`` pattern inside the
# session's combined pattern (_build_combined_pattern), read as run_cmd reads:
# one line at a time until the caller's part matched. A case that needs a
# match across a line ending is left out, since the combined pattern stops each
# read at one (see ``dotall`` above, and the combined pattern's own tests).
_COMBINED_IDS = {"compiled-flags", "verbose-trailing-comment", "inline-i", "line-anchor-after-600"}

# What follows every case's output: read back after the match, it shows that
# nothing past the match was consumed.
_REST_END = b"|REST-END|"


def _splits(first: bytes, second: bytes) -> list[list[bytes]]:
    data = first + second + _REST_END
    cuts = [1, 5, len(first) - 1, len(first), len(first) + 1, len(data) - 1]
    # Seven-byte pieces for the short cases; a long one in about 64 pieces,
    # so a caller's pattern (searched whole after each) stays cheap to test.
    step = max(7, len(data) // 64)
    return (
        [[data]]
        + [[data[:c], data[c:]] for c in cuts if 0 < c < len(data)]
        + [[data[i : i + step] for i in range(0, len(data), step)]]
    )


class _Fed:
    """The stream a case's chunks arrive on: one chunk per read, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._pending = list(chunks)

    async def read_remote(self) -> bytes:
        return self._pending.pop(0) if self._pending else b""


async def _via_reader(fed: _Fed):
    reader = PatternReader(fed.read_remote)

    class _Adapter:
        async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
            return await reader.read_transport_until(pattern)

    return _Adapter()


async def _bridge_session(fed: _Fed):
    from otto.host.interact import _BridgeShellSession

    async def write_remote(_data: bytes) -> None:
        return None

    return _BridgeShellSession(write_remote, fed.read_remote, newline=b"\n", command_frame=None)


async def _bridge_proxy_io(fed: _Fed):
    from otto.host.interact import _BridgeProxyIO

    async def write_remote(_data: bytes) -> None:
        return None

    io = _BridgeProxyIO(write_remote, fed.read_remote, newline=b"\n")

    class _Adapter:
        async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
            return await io.reader.read_transport_until(pattern)

    return _Adapter()


async def _local_session(fed: _Fed):
    session = LocalSession()
    session._reader = PatternReader(
        fed.read_remote
    )  # the reader open_transport builds, over a fake stream
    session._process = object()  # read_transport_until asserts a process; nothing is spawned
    return session


def _ssh_connection(fed: _Fed) -> SimpleNamespace:
    """An asyncssh connection whose one channel's stdout hands out *fed*'s chunks."""

    async def read(_n: int) -> bytes:
        return await fed.read_remote()

    process = SimpleNamespace(stdout=SimpleNamespace(read=read), close=lambda: None)
    return SimpleNamespace(create_process=AsyncMock(return_value=process))


async def _ssh_session(fed: _Fed):
    session = SshSession(_ssh_connection(fed))
    await session.open_transport()  # the real wiring: the channel's stdout read in chunks
    return session


def _telnet_reader_session(fed: _Fed, *, console: bool):
    """A TelnetSession over a real telnetlib3 reader, fed a chunk whenever its read waits."""
    reader = TelnetReader()
    writer = object()
    if console:

        class _Console:
            logged_in = False
            read_gate = ReadGate()

        session = TelnetSession(reader=reader, writer=writer, console_client=_Console())
    else:
        session = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))

    class _Adapter:
        async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
            read = asyncio.ensure_future(session.read_transport_until(pattern=pattern))
            while True:
                for _ in range(3):  # let the read search what has arrived
                    await asyncio.sleep(0)
                if read.done():
                    return read.result()
                chunk = await fed.read_remote()
                if chunk:
                    reader.feed_data(chunk)
                else:
                    reader.feed_eof()
                    return await read

    return _Adapter()


async def _telnet_session(fed: _Fed):
    return _telnet_reader_session(fed, console=False)


async def _console_session(fed: _Fed):
    return _telnet_reader_session(fed, console=True)


_READERS = [
    pytest.param(_via_reader, id="reader"),
    pytest.param(_local_session, id="local-session"),
    pytest.param(_bridge_session, id="bridge-session"),
    pytest.param(_bridge_proxy_io, id="bridge-proxy-io"),
    pytest.param(_ssh_session, id="ssh-session"),
    pytest.param(_telnet_session, id="telnet-session"),
    pytest.param(_console_session, id="console-session"),
]


async def _read_twice(build, chunks: list[bytes], pattern: re.Pattern[str]):
    """The match, then what a second read finds through the end marker."""
    adapter = await build(_Fed(chunks))
    try:
        matched = await adapter.read_transport_until(pattern=pattern)
    except asyncio.IncompleteReadError:
        return None, None
    rest = await adapter.read_transport_until(pattern=re.compile(re.escape(_REST_END.decode())))
    return matched, rest


# Every case as a caller's pattern; the cases that fit the contract as otto's too.
_OWNED_CASES = [
    pytest.param(*case.values[:4], owner, id=f"{case.id}-{owner}")
    for case in _CALLER_CASES
    for owner in ("caller", "otto")
    if owner == "caller" or case.values[4]
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "second", "regex", "flags", "owner"), _OWNED_CASES)
@pytest.mark.parametrize("build", _READERS)
async def test_every_pattern_matches_as_one_search_of_the_whole_text_on_every_reader(
    first: bytes, second: bytes, regex: str, flags: int, owner: str, build
) -> None:
    if owner == "otto":
        # A pattern object of its own (re caches compiled patterns), so
        # recording it never marks the caller run's pattern as otto's.
        pattern = bounded_pattern(re.compile(f"(?:{regex})", flags))
    else:
        pattern = re.compile(regex, flags)
    text = (first + second + _REST_END).decode()
    whole = re.search(regex, text, flags)
    want = (None, None) if whole is None else (text[: whole.end()], text[whole.end() :])
    for chunks in _splits(first, second):
        assert await _read_twice(build, chunks, pattern) == want, [len(c) for c in chunks]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "regex", "flags"),
    [pytest.param(*c.values[:4], id=c.id) for c in _CALLER_CASES if c.id in _COMBINED_IDS],
)
@pytest.mark.parametrize("build", _READERS)
async def test_a_callers_pattern_in_the_combined_pattern_matches_as_alone_on_every_reader(
    first: bytes, second: bytes, regex: str, flags: int, build
) -> None:
    """Its flags, compiled or inline, travel with it: the read ends where it alone matches."""
    caller = re.compile(regex, flags)
    combined = LocalSession()._build_combined_pattern([(caller, "answer\n")])
    text = (first + second + _REST_END).decode()
    alone = caller.search(text)
    assert alone is not None
    want = (text[: alone.end()], text[alone.end() :])
    for chunks in _splits(first, second):
        adapter = await build(_Fed(chunks))
        read = ""
        while True:
            data = await adapter.read_transport_until(pattern=combined)
            read += data
            found = combined.search(data)
            if found is not None and found.lastgroup == "expect_0":
                break
        rest = await adapter.read_transport_until(pattern=re.compile(re.escape(_REST_END.decode())))
        assert (read, rest) == want, [len(c) for c in chunks]


def _parts_matched(combined: re.Pattern[str], text: str) -> list[str]:
    """Every named part of *combined* that matches somewhere in *text*, leftmost first."""
    return [m.lastgroup or "" for m in combined.finditer(text)]


def test_each_callers_flags_stay_its_own_in_the_combined_pattern() -> None:
    session = LocalSession()
    sentinel = f"{session._markers.end_prefix}0__"
    combined = session._build_combined_pattern(
        [(re.compile("abc", re.IGNORECASE), "1\n"), (re.compile("x.y", re.DOTALL), "2\n")]
    )
    assert _parts_matched(combined, "ABC") == ["expect_0"]
    assert _parts_matched(combined, "x\ny") == ["expect_1"]
    # Neither lends its flag to the other, nor to otto's own parts.
    assert _parts_matched(combined, "X\nY") == ["newline"]
    assert _parts_matched(combined, "x\nabc") == ["newline", "expect_0"]
    # Not ``== []``: the marker carries a random hex id, which now and then
    # holds ``abc``, and a caller pattern is free to match there. What must
    # never happen is otto's own part matching the lowercased marker.
    assert "sentinel" not in _parts_matched(combined, sentinel.lower())
    assert _parts_matched(combined, sentinel) == ["sentinel"]


@pytest.mark.parametrize(
    "caller",
    [
        pytest.param(r"(?i)abc", id="leading-inline-flag"),
        pytest.param(re.compile(r"(?i)abc"), id="leading-inline-flag-compiled"),
        pytest.param(re.compile(r"^abc$", re.MULTILINE), id="multiline"),
        pytest.param(re.compile("a b c  # trailing comment", re.VERBOSE), id="verbose-comment"),
        pytest.param(re.compile(r"(?x) a b c  # comment"), id="inline-verbose"),
    ],
)
def test_a_callers_pattern_matches_in_the_combined_pattern_as_alone(caller) -> None:
    session = LocalSession()
    sentinel = f"{session._markers.end_prefix}0__"
    combined = session._build_combined_pattern([(caller, "1\n")])
    alone = re.compile(caller) if isinstance(caller, str) else caller
    for text in ["ABC", "abc", "q abc q", "a b c", sentinel.lower()]:
        found = alone.search(text)
        part = combined.search(text)
        got = part.group() if part is not None and part.lastgroup == "expect_0" else None
        assert got == (found.group() if found is not None else None), text
    # otto's own parts are untouched: a comment cannot swallow them, a flag cannot widen them.
    assert _parts_matched(combined, sentinel) == ["sentinel"]
    # Not ``== []``: the marker carries a random hex id, which now and then
    # holds ``abc``, and a caller pattern is free to match there. What must
    # never happen is otto's own part matching the lowercased marker.
    assert "sentinel" not in _parts_matched(combined, sentinel.lower())


@pytest.mark.parametrize(
    "caller",
    [
        pytest.param("(?x) (?i)a", id="space-between-flags-under-x"),
        pytest.param("(?x)\n(?i)a", id="newline-between-flags-under-x"),
        pytest.param("(?x)#c\n(?i)a", id="comment-line-between-flags-under-x"),
        pytest.param("(?x)(?#c)(?i)a", id="inline-comment-between-flags"),
        pytest.param("(?#c)(?i)a", id="inline-comment-before-the-flag"),
    ],
)
def test_leading_flags_python_accepts_past_comments_and_verbose_space_are_scoped(caller) -> None:
    """Python reads the start past ``(?#...)`` and, once ``x`` is on, past whitespace and comments.

    Each compiles alone, so each must join the combined pattern and match as alone.
    """
    alone = re.compile(caller)
    session = LocalSession()
    combined = session._build_combined_pattern([(caller, "1\n")])
    for text in ["A", "a", "qaq", "b"]:
        found = alone.search(text)
        part = combined.search(text)
        got = part.group() if part is not None and part.lastgroup == "expect_0" else None
        assert got == (found.group() if found is not None else None), text


@pytest.mark.parametrize(
    "caller",
    [
        pytest.param(re.compile("(?i) (?m)a", re.VERBOSE), id="space-between-flags-compiled-x"),
        pytest.param(
            re.compile("(?i)#c\n(?m)a", re.VERBOSE), id="comment-between-flags-compiled-x"
        ),
        pytest.param(re.compile(" (?i)a", re.VERBOSE), id="space-before-the-flag-compiled-x"),
        pytest.param(re.compile("# c\n(?i)a", re.VERBOSE), id="comment-before-the-flag-compiled-x"),
        pytest.param(re.compile("(?x)# c (?i)", re.VERBOSE), id="unterminated-comment-at-the-end"),
        pytest.param(re.compile("(?x)(?i)a# c (?i)"), id="unterminated-comment-after-pattern"),
    ],
)
def test_a_pattern_compiled_verbose_scopes_its_leading_flags_past_filler(caller) -> None:
    """``re.X`` makes the source verbose without an inline ``(?x)``, so filler before or
    between leading flags is skipped; each such pattern compiles alone and must match as alone."""
    session = LocalSession()
    combined = session._build_combined_pattern([(caller, "1\n")])
    for text in ["A", "a", "qaq", "b", ""]:
        found = caller.search(text)
        part = combined.search(text)
        got = part.group() if part is not None and part.lastgroup == "expect_0" else None
        assert got == (found.group() if found is not None else None), text


def test_leading_whitespace_without_verbose_still_ends_the_start() -> None:
    """`` (?x)a``: the space is a literal, so the flag is past the start and is refused."""
    with pytest.raises(ValueError, match=r"' \(\?x\)a'"):
        LocalSession()._build_combined_pattern([(" (?x)a", "1\n")])


def test_a_global_inline_flag_past_a_callers_start_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match=r"'abc\(\?i\)d'"):
        LocalSession()._build_combined_pattern([("abc(?i)d", "1\n")])


def test_only_the_framing_pattern_without_a_callers_part_is_bounded() -> None:
    from otto.host.output_codec import is_bounded

    session = LocalSession()
    assert is_bounded(session._build_combined_pattern(None))
    assert not is_bounded(session._build_combined_pattern([("BEGINx{300}END", "y\n")]))
