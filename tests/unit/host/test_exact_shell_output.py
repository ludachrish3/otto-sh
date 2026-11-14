"""The shell route returns a command's exact output: one trailing newline removed, nothing else.

``BashFrame.parse_output`` used to skip every newline after BEGIN, strip every
trailing newline and delete every ``\\r`` (#713). Now the session undoes only
what the terminal added (the ``onlcr`` fold, when the handshake measured
``onlcr``) and the frame drops only BEGIN's own line ending and the output's
last newline.

Each case in :data:`otto.testing.EXACT_OUTPUT_CASES` is given as the bytes the
PROGRAM writes. The tests
deliver them as each terminal mapping would, through a session whose
handshake measured that mapping, and assert the exact ``value``.
"""

import asyncio

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers
from otto.testing import EXACT_OUTPUT_CASES, OutputCase
from tests.unit.host.test_session import MockSession
from tests.unit.host.test_terminal_mapping import identity_reply, lossy_reply, onlcr_reply

M = SessionMarkers.for_session(session_id="cafef00d")


def _onlcr(text: str) -> str:
    """What a tty with ``onlcr`` sends for *text*: every LF preceded by a CR."""
    return text.replace("\n", "\r\n")


class TestParseOutput:
    """The frame alone, on a buffer with nothing to fold (a pipe, or after the session's fold)."""

    @pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
    def test_exact(self, case: OutputCase):
        buf = f"{M.begin}\n{case.text}{M.end_prefix}{case.retcode}__\n"
        assert BashFrame().parse_output(buf, cmd=case.command, m=M) == case.value
        assert BashFrame().extract_retcode(buf, m=M) == case.retcode

    @pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
    def test_the_echoed_command_is_still_skipped(self, case: OutputCase):
        echoed = BashFrame().frame(case.command, m=M)
        buf = f"{echoed}{M.begin}\n{case.text}{M.end_prefix}{case.retcode}__\n"
        assert BashFrame().parse_output(buf, cmd=case.command, m=M) == case.value


async def _measured(reply) -> MockSession:
    s = MockSession()
    await s.open_transport()
    feed = asyncio.create_task(s.feed_after_write(reply(s.markers.ready)))
    await s._ensure_initialized()
    await feed
    s.written.clear()
    return s


async def _run(s: MockSession, cmd: str, raw: str):
    feed = asyncio.create_task(s.feed_after_write(raw))
    result = await s.run_cmd(cmd=cmd)
    await feed
    return result


class TestAnUnfoldedCrlfBuffer:
    """An unknown mapping leaves CRLF in the buffer; the frame trims symmetrically."""

    def test_begins_crlf_means_the_trailing_crlf_is_the_one_removed(self):
        buf = f"{M.begin}\r\na\r\nb\r\n{M.end_prefix}0__\r\n"
        assert BashFrame().parse_output(buf, cmd="cmd", m=M) == "a\r\nb"

    def test_a_trailing_lone_lf_is_not_trimmed_after_a_crlf_begin(self):
        # BEGIN said this stream's line ending is CRLF, so a bare LF at the end is data.
        buf = f"{M.begin}\r\na\n{M.end_prefix}0__\r\n"
        assert BashFrame().parse_output(buf, cmd="cmd", m=M) == "a\n"

    def test_a_program_cr_at_the_end_survives_on_an_lf_stream(self):
        buf = f"{M.begin}\nprogress\r{M.end_prefix}0__\n"
        assert BashFrame().parse_output(buf, cmd="cmd", m=M) == "progress\r"


class TestThroughTheSession:
    @pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
    @pytest.mark.asyncio
    async def test_identity(self, case: OutputCase):
        s = await _measured(identity_reply)
        m = s.markers
        raw = f"{m.begin}\n{case.text}{m.end_prefix}{case.retcode}__\n"
        result = await _run(s, case.command, raw)
        assert result.value == case.value
        assert result.retcode == case.retcode

    @pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
    @pytest.mark.asyncio
    async def test_onlcr(self, case: OutputCase):
        s = await _measured(onlcr_reply)
        assert s.terminal_mapping == "onlcr"
        m = s.markers
        raw = _onlcr(f"{m.begin}\n{case.text}{m.end_prefix}{case.retcode}__\n")
        result = await _run(s, case.command, raw)
        assert result.value == case.value
        assert result.retcode == case.retcode

    @pytest.mark.asyncio
    async def test_an_unknown_mapping_leaves_the_bytes_as_they_arrived(self):
        s = await _measured(lossy_reply)
        m = s.markers
        result = await _run(s, "printf 'a\\nb\\n'", f"{m.begin}\r\na\r\nb\r\n{m.end_prefix}0__\r\n")
        # Nothing folded: the program's own line ending between a and b stays
        # as it arrived. BEGIN's CRLF and the one trailing CRLF are the frame's
        # to drop, symmetrically, so no stray CR is left at the end.
        assert result.value == "a\r\nb"
