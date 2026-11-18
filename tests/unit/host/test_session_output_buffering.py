"""ShellSession output emission: bash streams live per-line; a buffered frame
(Zephyr) emits exactly parse_output() once, with no prompt/retval scaffolding.
"""

import asyncio
import re

import pytest
import pytest_asyncio

from otto.host.command_frame import ZephyrFrame
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

    async def open_transport(self) -> None:
        self._out_reader = asyncio.StreamReader()

    async def write_transport(self, data: str) -> None:
        self.written.append(data)

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
async def test_zephyr_buffers_and_emits_parsed_output_once(zephyr_session: FrameMockSession):
    s = zephyr_session

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
async def test_zephyr_no_output_emits_nothing(zephyr_session: FrameMockSession):
    s = zephyr_session

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
    s = _UnreadKeepingSession(command_frame=ZephyrFrame())
    await _init(s)

    async def no_stop(*, restore=None, held=None) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    run = asyncio.create_task(s.run_cmd(cmd="long", timeout=0.3))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    s.feed(f"\r\n{s._begin_marker}: command not found\r\n~$ \r\n{printed}")
    result = await run
    assert result.timed_out
    shown = result.value.partition("\n")[2]
    assert shown.startswith("one\ntwo")
    assert s.emitted == logged


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["bash", "zephyr"])
async def test_a_command_that_never_ran_logs_no_output_on_any_frame(frame: str):
    from otto.host.session import NEVER_RAN

    s = FrameMockSession(command_frame=ZephyrFrame() if frame == "zephyr" else None)
    await _init(s)
    end = (
        f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        if frame == "zephyr"
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
    s = _UnreadKeepingSession(command_frame=ZephyrFrame())
    await _init(s)

    def failing(line: str) -> None:
        raise _SinkError(line)

    async def no_stop(*, restore=None, held=None) -> None:
        return None

    s._on_output = failing
    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    run = asyncio.create_task(s.run_cmd(cmd="long", timeout=0.3))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    s.feed(f"\r\n{s._begin_marker}: command not found\r\n~$ \r\none\r\n")
    result = await run
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

    async def no_stop(*, restore=None, held=None) -> None:
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

    async def no_stop(*, restore=None, held=None) -> None:
        return None

    monkeypatch.setattr(s, "_stop_under_bound", no_stop)
    result = await s.run_cmd(cmd="long", timeout=0.3)
    assert result.timed_out
    assert s.emitted == ["out"]
