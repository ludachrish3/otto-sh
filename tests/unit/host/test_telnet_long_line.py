"""A telnet or console session returns a line longer than telnetlib3's 64 KiB limit.

telnetlib3's ``readuntil_pattern`` raises ``LimitOverrunError`` once the
bytes before a match outgrow the reader's limit. The session raises the
limit for that read and asks again, so the line is searched whole, in its
true context (an anchor or a lookbehind never matches at a cut), the match
is the one an unlimited reader finds, and every other reader of the stream
still sees what follows it.
Asserted on values and on the absence of the exception, never on time.
"""

import asyncio
import re

import pytest
from telnetlib3.stream_reader import TelnetReader

from otto.host.output_codec import MAX_SEPARATOR_LEN, bounded_pattern, is_bounded
from otto.host.session import TelnetSession
from tests.unit.host._telnet_fakes import stream_client

END = b"__OTTO_0123_END__0__"
PATTERN = re.compile(r"__OTTO_0123_END__\d+__|\n")
LIMIT = 2**16  # telnetlib3's default reader limit


def _session(reader: TelnetReader) -> TelnetSession:
    writer = object()
    return TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))


@pytest.mark.asyncio
async def test_a_100_kb_line_with_no_newline_is_returned_whole() -> None:
    reader = TelnetReader()
    for i in range(0, 100_000, 4096):
        reader.feed_data(b"x" * min(4096, 100_000 - i))
    reader.feed_data(END + b"\r\n")

    got = await _session(reader).read_transport_until(pattern=PATTERN)

    assert got == "x" * 100_000 + END.decode()


@pytest.mark.asyncio
async def test_bytes_after_the_match_stay_in_the_stream_for_a_direct_reader() -> None:
    reader = TelnetReader()
    reader.feed_data(b"x" * 100_000 + END + b"after the marker")

    await _session(reader).read_transport_until(pattern=PATTERN)

    assert await reader.read(4096) == b"after the marker"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [LIMIT - len(END) - 1, LIMIT - len(END), LIMIT, LIMIT + 1])
async def test_a_line_at_the_limit_boundary_is_returned_whole(size: int) -> None:
    reader = TelnetReader()
    reader.feed_data(b"x" * size + END + b"rest")

    got = await _session(reader).read_transport_until(pattern=PATTERN)

    assert got == "x" * size + END.decode()
    assert await reader.read(4096) == b"rest"


def test_the_limit_constant_is_the_readers_own() -> None:
    assert TelnetReader()._limit == LIMIT  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_line_fed_while_the_read_waits_is_returned_whole_and_stops_at_the_match() -> None:
    reader = TelnetReader()
    read = asyncio.create_task(_session(reader).read_transport_until(pattern=PATTERN))
    await asyncio.sleep(0)  # the read is parked on an empty buffer

    # Each piece alone stays under the limit; the unmatched buffer outgrows it
    # while the read is waiting, so the overrun carries no match.
    for _ in range(5):
        reader.feed_data(b"x" * 30_000)
        await asyncio.sleep(0)
    reader.feed_data(END + b"after the marker")
    got = await read

    assert got == "x" * 150_000 + END.decode()
    assert await reader.read(4096) == b"after the marker"


def _straddling(straddle: bytes) -> bytes:
    """A long line whose *straddle* sits across ``match_end - MAX_SEPARATOR_LEN``.

    Where the read once cut the line; kept as a regression case for any
    bounded resume.
    """
    match_end = 100_000
    cut = match_end - MAX_SEPARATOR_LEN
    head = b"x" * (cut - 1)  # the straddle's first byte is the one before the cut
    tail = b"y" * (match_end - len(END) - len(head) - len(straddle))
    return head + straddle + tail + END


@pytest.mark.asyncio
@pytest.mark.parametrize("straddle", ["é".encode(), b"\r\x00"], ids=["utf8-pair", "cr-nul"])
async def test_a_character_split_by_the_cut_decodes_as_a_single_read_would(straddle: bytes) -> None:
    data = _straddling(straddle) + b"rest"
    assert len(data) - 4 - MAX_SEPARATOR_LEN == data.index(straddle) + 1  # split in two by the cut

    cut_reader = TelnetReader()
    cut_reader.feed_data(data)
    got = await _session(cut_reader).read_transport_until(pattern=PATTERN)

    whole_reader = TelnetReader(limit=2**22)  # never overruns
    whole_reader.feed_data(data)
    want = await _session(whole_reader).read_transport_until(pattern=PATTERN)

    assert got == want
    assert got.endswith(END.decode())
    assert await cut_reader.read(4096) == b"rest"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [1, 5, len(END) // 2, len(END) - 1])
async def test_the_marker_straddling_the_reader_limit_is_returned_whole(offset: int) -> None:
    # The marker starts *offset* bytes before the limit and ends past it.
    reader = TelnetReader()
    reader.feed_data(b"x" * (LIMIT - offset) + END + b"rest")

    got = await _session(reader).read_transport_until(pattern=PATTERN)

    assert got == "x" * (LIMIT - offset) + END.decode()
    assert await reader.read(4096) == b"rest"


@pytest.mark.asyncio
async def test_end_of_stream_in_a_200_kb_line_carries_all_of_it() -> None:
    reader = TelnetReader()
    reader.feed_data(b"x" * 200_000)
    reader.feed_eof()

    with pytest.raises(asyncio.IncompleteReadError) as caught:
        await _session(reader).read_transport_until(pattern=PATTERN)

    assert caught.value.partial.decode() == "x" * 200_000


async def _read(data: bytes, pattern: "re.Pattern[str]", *, eof: bool = False) -> str:
    reader = TelnetReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return await _session(reader).read_transport_until(pattern=pattern)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [0, 1, MAX_SEPARATOR_LEN - len(b"PASSWORD:"), 4096])
async def test_an_anchored_pattern_never_matches_at_a_cut_inside_a_long_line(after: int) -> None:
    """70,000 ``x`` then ``PASSWORD:`` has no ``^PASSWORD:``: the line does not start there.

    With *after* bytes following it, a read that took all but the last
    :data:`MAX_SEPARATOR_LEN` bytes and searched the rest as a new string saw
    ``PASSWORD:`` at its start, and returned. Nothing matches, so the read
    ends only at end of stream, carrying every byte.
    """
    data = b"x" * 70_000 + b"PASSWORD:" + b"y" * after
    for pattern in [r"^PASSWORD:", r"\APASSWORD:", r"(?<![x])PASSWORD:"]:
        with pytest.raises(asyncio.IncompleteReadError) as caught:
            await _read(data, re.compile(pattern, re.MULTILINE), eof=True)
        assert caught.value.partial == data, pattern


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [LIMIT - 300, LIMIT - 5, LIMIT, LIMIT + 5, 70_000, 200_000])
async def test_an_anchored_or_lookbehind_pattern_matches_where_one_whole_search_would(
    size: int,
) -> None:
    """Around the overrun the line is searched in its true context, so real matches are found."""
    data = b"x" * size + b"\nPASSWORD:" + b"rest"
    want = "x" * size + "\nPASSWORD:"
    for pattern in [r"^PASSWORD:", r"(?<=\n)PASSWORD:", r"(?<=x{300}\n)PASSWORD:"]:
        reader = TelnetReader()
        reader.feed_data(data)
        got = await _session(reader).read_transport_until(pattern=re.compile(pattern, re.MULTILINE))
        assert got == want, pattern
        assert await reader.read(4096) == b"rest", pattern


@pytest.mark.asyncio
async def test_the_readers_limit_is_restored_after_a_long_line() -> None:
    reader = TelnetReader()
    reader.feed_data(b"x" * 200_000 + END + b"rest")
    await _session(reader).read_transport_until(pattern=PATTERN)
    assert reader._limit == LIMIT  # pyright: ignore[reportPrivateUsage]


class _CountingReader(TelnetReader):
    """A real :class:`TelnetReader` that counts the bytes its pattern search is handed.

    telnetlib3's ``readuntil_pattern`` runs ``pattern.search(self._buffer)`` on
    entry and again after every wake of ``_wait_for_data``; the buffer's length
    at each of those points is exactly the work handed to the search, counted
    without touching the search itself (a ``re.Pattern`` cannot be wrapped:
    telnetlib3 refuses anything else).
    """

    searched = 0

    async def readuntil_pattern(self, pattern: "re.Pattern[bytes]") -> bytes:
        self.searched += len(self._buffer)  # pyright: ignore[reportPrivateUsage]
        return await super().readuntil_pattern(pattern)

    async def _wait_for_data(self, func_name: str) -> None:
        await super()._wait_for_data(func_name)  # pyright: ignore[reportPrivateUsage]
        self.searched += len(self._buffer)  # pyright: ignore[reportPrivateUsage]


# A distinct source, so ``re.compile``'s cache never hands this module's PATTERN
# (a caller's pattern) the bounded record.
OWN_PATTERN = bounded_pattern(re.compile(r"(?:__OTTO_0123_END__\d+__)|\n"))


async def _searched_while_fed(size: int, pattern: "re.Pattern[str]") -> int:
    """Feed *size* bytes of one line in 4 KiB pieces while the read waits; count the search work."""
    reader = _CountingReader()
    read = asyncio.create_task(_session(reader).read_transport_until(pattern=pattern))
    await asyncio.sleep(0)
    for _ in range(size // 4096):
        reader.feed_data(b"x" * 4096)
        await asyncio.sleep(0)
    reader.feed_data(END + b"rest")
    got = await read
    assert got == "x" * (size // 4096 * 4096) + END.decode()
    assert await reader.read(4096) == b"rest"
    assert reader._limit == LIMIT  # pyright: ignore[reportPrivateUsage]
    return reader.searched


@pytest.mark.asyncio
async def test_otto_s_own_pattern_over_a_long_line_costs_search_work_linear_in_its_length() -> None:
    """Doubling the line doubles the bytes handed to the search; it does not quadruple them.

    Measured in OPERATIONS, never in time: the bytes telnetlib3 handed to the
    pattern's ``search``. A raised limit would make every feed rescan the whole
    line so far, and the ratio would be about four.
    """
    one = await _searched_while_fed(1 << 20, OWN_PATTERN)
    two = await _searched_while_fed(2 << 20, OWN_PATTERN)
    assert two <= 2.1 * one, (one, two)
    # Each feed is searched over at most the limit plus the kept tail and the piece.
    assert two <= ((2 << 20) // 4096 + 1) * (LIMIT + MAX_SEPARATOR_LEN + 4096 + len(END) + 4), two


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [LIMIT - 5, LIMIT + 5, 200_000])
async def test_a_caller_s_anchored_pattern_still_matches_where_an_unlimited_search_would(
    size: int,
) -> None:
    """The bounded path is otto's alone: a caller's anchor or lookbehind keeps its whole line."""
    data = b"x" * size + b"\nPASSWORD:" + b"rest"
    for source in [r"^PASSWORD:", r"(?<=x{300}\n)PASSWORD:"]:
        pattern = re.compile(source, re.MULTILINE)
        assert not is_bounded(pattern)
        reader = TelnetReader()
        reader.feed_data(data)
        got = await _session(reader).read_transport_until(pattern=pattern)
        whole = TelnetReader(limit=2**22)  # never overruns
        whole.feed_data(data)
        want = await _session(whole).read_transport_until(pattern=pattern)
        assert got == want == "x" * size + "\nPASSWORD:", source
        assert await reader.read(4096) == b"rest", source


def test_the_buffer_the_timeout_take_reads_is_the_readers_own() -> None:
    """``TelnetSession._take_unread`` reads telnetlib3's private ``_buffer`` length."""
    reader = TelnetReader()
    reader.feed_data(b"held")
    assert bytes(reader._buffer) == b"held"  # pyright: ignore[reportPrivateUsage]
