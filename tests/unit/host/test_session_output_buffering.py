"""ShellSession output emission: bash streams live per-line; Zephyr streams
with its tail held back; a buffered frame emits exactly parse_output() once,
with no prompt/retval scaffolding.
"""

import asyncio
import re

import pytest
import pytest_asyncio

from otto.host.command_frame import BashFrame, ZephyrFrame, ZephyrSerialFrame
from otto.host.session import ShellSession
from otto.utils import wait_for_async
from tests.unit.host._session_feed import FeedAfterWriteMixin, handshake_reply


class FrameMockSession(FeedAfterWriteMixin, ShellSession):
    """In-memory ShellSession that records what reaches the output sink."""

    def __init__(self, command_frame=None) -> None:
        super().__init__(command_frame=command_frame)
        self._out_reader: asyncio.StreamReader | None = None
        self.written: list[str] = []
        self.emitted: list[str] = []
        self._on_output = self.emitted.append
        self.answer_on_write: str | None = None
        """Output fed the moment the next command is written, then cleared.

        A test whose command runs under a short timeout sets this instead of
        feeding from its own task: the output is there before the session
        reads, so no step of the test sits inside the command's timeout.
        """

    async def open_transport(self) -> None:
        self._out_reader = asyncio.StreamReader()

    async def write_transport(self, data: str) -> None:
        self.written.append(data)
        answer, self.answer_on_write = self.answer_on_write, None
        if answer is not None:
            self.feed(answer)

    async def _take_unread(self) -> str:
        return ""

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        assert self._out_reader is not None
        buf = ""
        while True:
            chunk = await self._out_reader.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(buf.encode(), None)
            buf += chunk.decode()
            if pattern.search(buf):
                return buf

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    def feed(self, data: str) -> None:
        assert self._out_reader is not None
        self._out_reader.feed_data(data.encode())


async def _init(s: FrameMockSession) -> None:
    await s.open_transport()
    feed_task = asyncio.create_task(s.feed_after_write(handshake_reply(s._frame, s._ready_marker)))
    task = asyncio.create_task(s._ensure_initialized())
    await task
    await feed_task
    s.written.clear()


@pytest_asyncio.fixture
async def bash_session() -> FrameMockSession:
    s = FrameMockSession()  # default BashFrame, streams_output_live=True
    await _init(s)
    return s


class _BufferedZephyrFrame(ZephyrFrame):
    """Zephyr's parse, declared buffered: a frame that does not stream its output."""

    streams_output_live = False


@pytest_asyncio.fixture
async def zephyr_session() -> FrameMockSession:
    s = FrameMockSession(command_frame=ZephyrFrame())
    await _init(s)
    return s


@pytest.mark.asyncio
async def test_bash_streams_each_line_live(bash_session: FrameMockSession):
    s = bash_session

    feed_task = asyncio.create_task(
        s.feed_after_write(f"{s._begin_marker}\nline1\nline2\n{s._end_marker_prefix}0__\n")
    )
    result = await s.run_cmd(cmd="seq 1 2")
    await feed_task

    assert result.value == "line1\nline2"
    assert s.emitted == ["line1", "line2"]  # live frames emit each line as it arrives


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [_BufferedZephyrFrame, ZephyrFrame], ids=["buffered", "live"])
async def test_a_zephyr_parse_logs_its_parsed_output_once(frame: type[ZephyrFrame]):
    s = FrameMockSession(command_frame=frame())
    await _init(s)

    # Real Zephyr shell shape: a prompt after each executed line, plus the
    # standalone `retval` integer — all scaffolding parse_output discards.
    feed_task = asyncio.create_task(
        s.feed_after_write(
            f"\r\n{s._begin_marker}: command not found\r\n~$ "
            f"\r\nUnloaded extension cov_ext\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        )
    )
    result = await s.run_cmd(cmd="llext unload cov_ext")
    await feed_task

    assert result.value == "Unloaded extension cov_ext"
    # Exactly one emit, equal to parsed output — no prompts, no `0`, no blanks.
    assert s.emitted == ["Unloaded extension cov_ext"]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [_BufferedZephyrFrame, ZephyrFrame], ids=["buffered", "live"])
async def test_zephyr_no_output_emits_nothing(frame: type[ZephyrFrame]):
    s = FrameMockSession(command_frame=frame())
    await _init(s)

    feed_task = asyncio.create_task(
        s.feed_after_write(
            f"\r\n{s._begin_marker}: command not found\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        )
    )
    result = await s.run_cmd(cmd="fs mount fat /RAM:")
    await feed_task

    assert result.value == ""
    assert s.emitted == []


@pytest.mark.asyncio
async def test_on_output_argument_overrides_default_sink(bash_session: FrameMockSession):
    s = bash_session
    sink: list[str] = []

    feed_task = asyncio.create_task(
        s.feed_after_write(f"{s._begin_marker}\nhi\n{s._end_marker_prefix}0__\n")
    )
    await s.run_cmd(cmd="echo hi", on_output=sink.append)
    await feed_task

    assert sink == ["hi"]
    assert s.emitted == []  # the per-command sink replaced the default


from types import SimpleNamespace

from otto.host.session import TelnetSession
from tests.unit.host._telnet_fakes import stream_client


class TestWriteProgress:
    @pytest.mark.asyncio
    async def test_telnet_write_reports_progress_per_chunk(self):
        writes: list[bytes] = []
        writer = SimpleNamespace(write=writes.append)
        s = TelnetSession(
            reader=None, writer=writer, client=stream_client(None, writer), write_chunk_size=4
        )
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s.write_transport(data="0123456789")  # 10 bytes, chunk 4 -> 3 writes

        assert b"".join(writes) == b"0123456789"
        assert progress == [(4, 10), (8, 10), (10, 10)]

    @pytest.mark.asyncio
    async def test_telnet_single_write_reports_once_at_completion(self):
        writer = SimpleNamespace(write=lambda b: None)
        s = TelnetSession(
            reader=None, writer=writer, client=stream_client(None, writer), write_chunk_size=0
        )  # unchunked
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s.write_transport(data="abcd")

        assert progress == [(4, 4)]

    @pytest.mark.asyncio
    async def test_run_cmd_scopes_write_progress_to_framed_write(self, zephyr_session):
        # write_progress is set only for the framed command write, then cleared.
        s = zephyr_session
        seen: list[object] = []
        orig_write = s.write_transport

        async def _record_write(data):
            if s._begin_marker in data:  # the framed command write
                seen.append(s._write_progress)
            await orig_write(data)

        s.write_transport = _record_write

        def cb(done: int, total: int) -> None:
            return None

        feed_task = asyncio.create_task(
            s.feed_after_write(
                f"\r\n{s._begin_marker}: command not found\r\n~$ "
                f"\r\nok\r\n~$ \r\n0\r\n~$ "
                f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
            )
        )
        await s.run_cmd(cmd="noop", write_progress=cb)
        await feed_task

        assert seen == [cb]  # set during the framed write
        assert s._write_progress is None  # cleared afterward


class _UnreadKeepingSession(FrameMockSession):
    """A FrameMockSession whose cancelled read leaves what it held for ``_take_unread``."""

    def __init__(self, command_frame=None) -> None:
        super().__init__(command_frame=command_frame)
        self._held = ""

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        assert self._out_reader is not None
        while not pattern.search(self._held):
            chunk = await self._out_reader.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(self._held.encode(), None)
            self._held += chunk.decode()
        data, self._held = self._held, ""
        return data

    async def _take_unread(self) -> str:
        held, self._held = self._held, ""
        return held


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("printed", "logged"),
    [
        pytest.param("one\r\ntwo\r\nunfini", ["one", "two"], id="mid-line"),
        pytest.param("one\r\ntwo\r\n", ["one", "two"], id="at-a-line-end"),
    ],
)
async def test_a_timed_out_buffered_frame_logs_the_complete_lines_of_its_value(
    monkeypatch: pytest.MonkeyPatch, printed: str, logged: list[str]
):
    s = _UnreadKeepingSession(command_frame=_BufferedZephyrFrame())
    await _init(s)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    s.answer_on_write = f"\r\n{s._begin_marker}: command not found\r\n~$ \r\n{printed}"
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out
    shown = result.value.partition("\n")[2]
    assert shown.startswith("one\ntwo")
    assert s.emitted == logged


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["bash", "zephyr", "buffered"])
async def test_a_command_that_never_ran_logs_no_output_on_any_frame(frame: str):
    from otto.host.session import NEVER_RAN

    frames = {"bash": None, "zephyr": ZephyrFrame(), "buffered": _BufferedZephyrFrame()}
    s = FrameMockSession(command_frame=frames[frame])
    await _init(s)
    end = (
        f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        if frame != "bash"
        else f"{s._end_marker_prefix}0__\n"
    )
    feed_task = asyncio.create_task(s.feed_after_write(end))
    result = await s.run_cmd(cmd="never")
    await feed_task
    assert result.value == NEVER_RAN
    assert s.emitted == []


class _SinkError(Exception):
    """The host log's sink failed on a line of output."""


@pytest.mark.asyncio
async def test_a_timed_out_buffered_frame_returns_its_result_when_the_sink_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """A sink that fails on the timed-out lines costs those lines, never the timed-out result."""
    s = _UnreadKeepingSession(command_frame=_BufferedZephyrFrame())
    await _init(s)

    def failing(line: str) -> None:
        raise _SinkError(line)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    s._on_output = failing
    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    s.answer_on_write = f"\r\n{s._begin_marker}: command not found\r\n~$ \r\none\r\n"
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out


class _UnreadAtTimeoutSession(FrameMockSession):
    """A live frame's read: the BEGIN line, then a wait with lines it has not handed over.

    What a reader holds past its last match when the budget runs out: the
    timeout takes it with ``_take_unread``.
    """

    def __init__(self, unread: str) -> None:
        super().__init__()
        self._unread = unread
        self._begun = False

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        if not self._initialized or self._begun:
            if self._begun:
                await asyncio.Event().wait()
            return await super().read_transport_until(pattern)
        self._begun = True
        return f"{self._begin_marker}\r\n"

    async def _take_unread(self) -> str:
        unread, self._unread = self._unread, ""
        return unread


@pytest.mark.asyncio
async def test_a_timed_out_live_frame_logs_the_complete_lines_it_had_not_read(
    monkeypatch: pytest.MonkeyPatch,
):
    from otto.host.terminal_mapping import ONLCR, Measurement

    s = _UnreadAtTimeoutSession("one\r\ntwo\r\nunfini")
    await _init(s)
    s._terminal = Measurement(ONLCR, calibrated=True)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out
    assert result.value.partition("\n")[2] == "one\ntwo\nunfini"
    assert s.emitted == ["one", "two"]


@pytest.mark.asyncio
async def test_a_timed_out_live_frame_logs_nothing_from_the_end_record_on(
    monkeypatch: pytest.MonkeyPatch,
):
    """The END record, and what the shell printed after it, are never the command's lines."""
    from otto.host.terminal_mapping import ONLCR, Measurement

    s = _UnreadAtTimeoutSession("")
    s._unread = f"out\r\n{s._end_marker_prefix}0__\r\n~$ more\r\n"
    await _init(s)
    s._terminal = Measurement(ONLCR, calibrated=True)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out
    assert s.emitted == ["out"]


# ---------------------------------------------------------------------------
# Zephyr streams: measured console transcripts replayed chunk by chunk
# ---------------------------------------------------------------------------

import ast
import logging

from pydantic import SecretStr

from otto.host.host import CredentialExpect
from otto.logger.relay import log_lines
from otto.result import CommandResult
from otto.testing import EXACT_OUTPUT_CASES, OutputCase
from otto.utils import Status
from tests._fixtures import shell_transcripts
from tests._fixtures.paths import TESTS_ROOT, ensure_custom_hosts_on_path
from tests.conformance._log import records_containing

ensure_custom_hosts_on_path()

from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

_LIVE_LOG = TESTS_ROOT / "measurements" / "zephyr_live_log"
_SESSION_ID = re.compile(r"__OTTO_[0-9a-f]+_")
_TAIL = re.compile(r"~\$|^-?\d+$|retCode")


class _ChunkedSession(FrameMockSession):
    """A FrameMockSession that reads in whole chunks, and says when it waits for more."""

    def __init__(self, command_frame=None) -> None:
        super().__init__(command_frame=command_frame)
        self._unread = ""
        self.starved = asyncio.Event()

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        assert self._out_reader is not None
        while (match := pattern.search(self._unread)) is None:
            self.starved.set()
            chunk = await self._out_reader.read(65536)
            if not chunk:
                raise asyncio.IncompleteReadError(self._unread.encode(), None)
            self._unread += chunk.decode()
        self.starved.clear()
        data, self._unread = self._unread[: match.end()], self._unread[match.end() :]
        return data


def _help_chunks(name: str, s: ShellSession) -> list[str]:
    """The wire chunks of the transcript's ``help`` command, in order, in *s*'s markers."""
    prefix = s._begin_marker.removesuffix("BEGIN__")
    chunks: list[str] = []
    inside = False
    for row in (_LIVE_LOG / name).read_text().splitlines():
        if "MARK 'begin help'" in row:
            inside = True
        elif "MARK 'end help'" in row:
            break
        elif inside and " telnet-feed " in row:
            raw = ast.literal_eval(row.split(" telnet-feed ", 1)[1]).decode()
            chunks.append(_SESSION_ID.sub(prefix, raw))
    return chunks


async def _fed(s: _ChunkedSession, chunk: str) -> None:
    """Feed *chunk*, then wait until the session has read all of it and waits for more."""
    s.starved.clear()
    s.feed(chunk)
    await asyncio.wait_for(s.starved.wait(), 5)


_REPLAYS = [
    pytest.param(
        "zephyr27_fat-help.txt",
        ZephyrInlineRetcodeFrame,
        "  device    :Device commands",
        id="zephyr-2.7",
    ),
    pytest.param(
        "zephyr37_fat-help.txt",
        ZephyrFrame,
        "\x1b[17Cwidth change to ensure correct text display.",
        id="zephyr-3.7",
    ),
    pytest.param(
        "zephyr44_lfs-help.txt",
        ZephyrFrame,
        "\x1b[17CRead memory at address with optional width:",
        id="zephyr-4.4",
    ),
    pytest.param(
        "zephyr37_fat-help-no-final-newline.synthetic.txt",
        ZephyrFrame,
        "\x1b[17Cwidth change to ensure correct text display.",
        id="synthetic-zephyr-3.7-no-final-newline",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "frame", "last_before_end"), _REPLAYS)
async def test_a_zephyr_command_logs_each_line_before_its_end_arrives(
    name: str, frame: type[ZephyrFrame], last_before_end: str
):
    s = _ChunkedSession(command_frame=frame())
    await _init(s)
    logged: list[str] = []
    chunks = _help_chunks(name, s)
    *before, last = chunks
    assert s._end_marker_prefix in last
    assert not any(s._end_marker_prefix in chunk for chunk in before)
    today = s._frame.parse_output("".join(chunks), cmd="help", m=s._markers)

    run = asyncio.create_task(s.run_cmd(cmd="help", on_output=logged.append))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    for chunk in before:
        await _fed(s, chunk)
    logged_before_end = list(logged)
    s.feed(last)
    result = await run

    assert result.value == today
    assert result.retcode == 0
    assert result.status == Status.Success
    assert logged == log_lines(result.value)
    # Every line released before END is the value's, in order, and the
    # newest held back are not yet: they may have been the shell's tail.
    assert logged_before_end == logged[: len(logged_before_end)]
    assert logged_before_end[-1] == last_before_end
    assert not [line for line in logged if _TAIL.search(line) or "\x1b[1;3" in line]


@pytest.mark.asyncio
async def test_a_synthetic_zephyr_line_with_no_final_newline_is_logged_without_the_prompt():
    s = _ChunkedSession(command_frame=ZephyrFrame())
    await _init(s)
    logged: list[str] = []
    feed_task = asyncio.create_task(
        s.feed_after_write(*_help_chunks("zephyr37_fat-help-no-final-newline.synthetic.txt", s))
    )
    result = await s.run_cmd(cmd="help", on_output=logged.append)
    await feed_task
    assert result.value.endswith("  version      : Show kernel version")
    assert logged[-1] == "  version      : Show kernel version"
    assert logged == log_lines(result.value)


_PROMPT = "\x1b[1;32m~$ \x1b[m"


_SHOWN_PROMPT = "\x1b[1;32m~$ "  # the prompt as the value shows it: its level reset removed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("printed", "logged"),
    [
        pytest.param(
            "l1\r\nl2\r\nl3\r\nl4\r\nl5\r\nunfini",
            ["l1", "l2", "l3", "l4", "l5"],
            id="mid-output",
        ),
        pytest.param(
            f"out\r\n{_PROMPT}\r\n0\r\n{_PROMPT}\r\n",
            ["out", _SHOWN_PROMPT, "0", _SHOWN_PROMPT],
            id="whole-tail-before-end",
        ),
        pytest.param("", [], id="before-any-output"),
    ],
)
async def test_a_timed_out_zephyr_command_logs_the_complete_lines_of_its_value(
    monkeypatch: pytest.MonkeyPatch, printed: str, logged: list[str]
):
    """Once each, in order: the held lines too, and whatever tail the timed-out value shows."""
    s = _UnreadKeepingSession(command_frame=ZephyrFrame())
    await _init(s)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    s.answer_on_write = f"\r\n{s._begin_marker}: command not found\r\n{_PROMPT}\r\n{printed}"
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out
    shown = result.value.partition("\n")[2]
    complete = log_lines(shown)[: -1 if printed.endswith("unfini") else None]
    assert s.emitted == complete == logged


_CREDENTIAL = CredentialExpect(
    pattern=r"Password: ", response=SecretStr("s3cret-answer"), user="u", host="h"
)


class _HoldingBashFrame(BashFrame):
    """Bash, holding its two newest lines until END: a frame whose hold exceeds its tail."""

    live_hold_lines = 2


async def _run_with_credential(
    body: str, caplog: pytest.LogCaptureFixture
) -> "tuple[CommandResult, list[str]]":
    caplog.set_level(logging.DEBUG, logger="otto")
    s = _ChunkedSession(command_frame=_HoldingBashFrame())
    await _init(s)
    logged: list[str] = []
    transcript = f"{s._begin_marker}\n{body}{s._end_marker_prefix}0__\n"
    feed_task = asyncio.create_task(s.feed_after_write(*transcript.splitlines(keepends=True)))
    result = await s.run_cmd(cmd="login", expects=[_CREDENTIAL], on_output=logged.append)
    await feed_task
    assert "s3cret-answer" not in "\n".join(r.getMessage() for r in caplog.records)
    return result, logged


@pytest.mark.asyncio
async def test_a_line_cut_to_nothing_by_a_credential_keeps_its_place_in_a_held_log(
    caplog: pytest.LogCaptureFixture,
):
    """A and B are released live; the cut line is never logged and B is not logged twice."""
    result, logged = await _run_with_credential("A\nPassword: \nB\nC\nD\nE\n", caplog)
    assert log_lines(result.value) == ["A", "Password: ", "B", "C", "D", "E"]
    assert logged == ["A", "B", "C", "D", "E"]


@pytest.mark.asyncio
async def test_a_held_line_a_credential_was_cut_from_is_logged_as_cut(
    caplog: pytest.LogCaptureFixture,
):
    """The cut line is still held at END: it is logged as cut, never from the value's copy."""
    result, logged = await _run_with_credential("A\nB\nPassword: yes\n", caplog)
    assert log_lines(result.value) == ["A", "B", "Password: yes"]
    assert logged == ["A", "B", "yes"]


_WHOLE_LINE_CREDENTIAL = CredentialExpect(
    pattern=r"Password: \n", response=SecretStr("s3cret-answer"), user="u", host="h"
)


async def _run_held(
    body: str, credential: CredentialExpect, *, end: str = "\n"
) -> "tuple[CommandResult, list[str], list[str]]":
    """Run *body* on a hold-2 frame answering *credential*: result, logged lines, secret records."""
    s = _ChunkedSession(command_frame=_HoldingBashFrame())
    await _init(s)
    logged: list[str] = []
    transcript = f"{s._begin_marker}\n{body}{s._end_marker_prefix}0__{end}"
    with records_containing("s3cret-answer") as secret:
        feed_task = asyncio.create_task(s.feed_after_write(*_credential_chunks(transcript)))
        result = await s.run_cmd(cmd="login", expects=[credential], on_output=logged.append)
        await feed_task
    return result, logged, secret


def _credential_chunks(transcript: str) -> list[str]:
    """*transcript* as wire chunks: each line, the prompt arriving on its own."""
    return [part for part in re.split(r"(?<=\n)|(?<=Password: )", transcript) if part]


@pytest.mark.asyncio
async def test_a_credential_line_consumed_with_its_newline_keeps_its_place_in_a_held_log():
    """The match takes the whole prompt line: the five lines after it are each logged once."""
    result, logged, secret = await _run_held(
        "A\nPassword: \nB\nC\nD\nE\nF\n", _WHOLE_LINE_CREDENTIAL
    )
    assert log_lines(result.value) == ["A", "Password: ", "B", "C", "D", "E", "F"]
    assert logged == ["A", "B", "C", "D", "E", "F"]
    assert secret == []


@pytest.mark.asyncio
async def test_a_credential_cut_from_the_unfinished_last_line_of_a_held_log_stays_cut():
    """``A``, ``B``, the prompt, then ``yes`` and END, no newline: the prompt is never logged."""
    result, logged, secret = await _run_held("A\nB\nPassword: yes", _CREDENTIAL)
    assert result.value.endswith("Password: yes")
    assert logged == ["A", "B", "yes"]
    assert secret == []


class _AnswerBlockingSession(_UnreadKeepingSession):
    """A session whose write of a credential's answer never returns."""

    async def write_transport(self, data: str) -> None:
        if isinstance(data, SecretStr):
            await asyncio.Event().wait()
        await super().write_transport(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [_HoldingBashFrame, BashFrame], ids=["hold-2", "stock-bash"])
async def test_a_timeout_while_a_held_log_types_a_credential_logs_each_line_once_never_the_prompt(
    monkeypatch: pytest.MonkeyPatch, frame: type
):
    """The read the answer was for completes a line: it is cut, and nothing is logged twice.

    On a stock bash frame too (no hold): the read's lines are logged before
    the answer is typed.
    """
    s = _AnswerBlockingSession(command_frame=frame())
    await _init(s)

    async def no_stop(*, restore=None, held=None, disposable=False) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    logged: list[str] = []
    with records_containing("s3cret-answer") as secret:
        s.answer_on_write = f"{s._begin_marker}\nA\nB\nC\nPassword: \n"
        result = await s.run_cmd(
            cmd="login",
            expects=[_WHOLE_LINE_CREDENTIAL],
            on_output=logged.append,
            timeout=0.3,
        )
    assert result.timed_out
    assert logged == ["A", "B", "C"]
    assert secret == []


_ZEPHYR_FRAMES = {
    "zephyr": (ZephyrFrame, "zephyr_transcript"),
    "zephyr-serial": (ZephyrSerialFrame, "zephyr_serial_transcript"),
    "zephyr-inline": (ZephyrInlineRetcodeFrame, "zephyr_inline_transcript"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", sorted(_ZEPHYR_FRAMES))
@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
async def test_every_zephyr_frame_logs_the_lines_of_its_value(frame: str, case: OutputCase):
    """Each exact-output case, read line by line: the log is the lines of ``value``."""
    cls, build = _ZEPHYR_FRAMES[frame]
    s = _ChunkedSession(command_frame=cls())
    await _init(s)
    transcript = getattr(shell_transcripts, build)(case, s._markers)
    logged: list[str] = []
    feed_task = asyncio.create_task(s.feed_after_write(*transcript.splitlines(keepends=True)))
    result = await s.run_cmd(cmd=case.command, on_output=logged.append)
    await feed_task
    assert result.value == s._frame.parse_output(transcript, cmd=case.command, m=s._markers)
    assert result.retcode == case.retcode
    assert logged == log_lines(result.value), f"value={result.value!r}"


class _UnheldZephyrFrame(ZephyrFrame):
    """Zephyr, buffered and holding no line back: a logging choice, never a parse one."""

    streams_output_live = False
    live_hold_lines = 0


class _OverHeldZephyrFrame(ZephyrFrame):
    """Zephyr, holding one line more than its tail."""

    live_hold_lines = 4


@pytest.mark.parametrize("cls", [_UnheldZephyrFrame, _OverHeldZephyrFrame])
@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
def test_a_zephyr_frame_s_hold_never_changes_its_value(cls: type, case: OutputCase):
    """The parse's tail is its own: a subclass's hold leaves every value the stock frame's."""
    stock = ZephyrFrame()
    markers = FrameMockSession(command_frame=stock)._markers
    transcript = shell_transcripts.zephyr_transcript(case, markers)
    expected = stock.parse_output(transcript, cmd=case.command, m=markers)
    assert cls().parse_output(transcript, cmd=case.command, m=markers) == expected


def test_a_zephyr_frame_that_streams_with_a_hold_below_its_parse_tail_is_refused():
    """Its prompt and retval lines would be logged live: the declaration is refused where made."""
    with pytest.raises(TypeError, match=r"streams_output_live.*live_hold_lines.*3") as raised:

        class _Leaky(ZephyrFrame):
            live_hold_lines = 0

    assert "streams_output_live = False" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
async def test_a_zephyr_frame_with_streaming_off_logs_its_value_s_lines_at_end(case: OutputCase):
    s = _ChunkedSession(command_frame=_UnheldZephyrFrame())
    await _init(s)
    transcript = shell_transcripts.zephyr_transcript(case, s._markers)
    logged: list[str] = []
    feed_task = asyncio.create_task(s.feed_after_write(*transcript.splitlines(keepends=True)))
    result = await s.run_cmd(cmd=case.command, on_output=logged.append)
    await feed_task
    assert result.value == ZephyrFrame().parse_output(transcript, cmd=case.command, m=s._markers)
    assert logged == log_lines(result.value)
