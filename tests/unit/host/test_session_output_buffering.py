"""ShellSession output emission: bash streams live per-line; a buffered frame
(Zephyr) emits exactly parse_output() once, with no prompt/retval scaffolding.
"""

import asyncio
import re

import pytest
import pytest_asyncio

from otto.host.command_frame import ZephyrFrame
from otto.host.session import ShellSession


class FrameMockSession(ShellSession):
    """In-memory ShellSession that records what reaches the output sink."""

    def __init__(self, command_frame=None) -> None:
        super().__init__(command_frame=command_frame)
        self._out_reader: asyncio.StreamReader | None = None
        self.written: list[str] = []
        self.emitted: list[str] = []
        self._on_output = self.emitted.append

    async def _open(self) -> None:
        self._out_reader = asyncio.StreamReader()

    async def _write(self, data: str) -> None:
        self.written.append(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
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
    await s._open()
    task = asyncio.create_task(s._ensure_initialized())
    await asyncio.sleep(0.01)
    s.feed(s._ready_marker + "\n")
    await task
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

    async def simulate():
        await asyncio.sleep(0.01)
        s.feed(f"{s._begin_marker}\nline1\nline2\n{s._end_marker_prefix}0__\n")

    feed_task = asyncio.create_task(simulate())
    result = await s.run_cmd("seq 1 2")
    await feed_task

    assert result.output == "line1\nline2"
    assert s.emitted == ["line1", "line2"]  # live frames emit each line as it arrives


@pytest.mark.asyncio
async def test_zephyr_buffers_and_emits_parsed_output_once(zephyr_session: FrameMockSession):
    s = zephyr_session

    async def simulate():
        await asyncio.sleep(0.01)
        # Real Zephyr shell shape: a prompt after each executed line, plus the
        # standalone `retval` integer — all scaffolding parse_output discards.
        s.feed(
            f"\r\n{s._begin_marker}: command not found\r\n~$ "
            f"\r\nUnloaded extension cov_ext\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        )

    feed_task = asyncio.create_task(simulate())
    result = await s.run_cmd("llext unload cov_ext")
    await feed_task

    assert result.output == "Unloaded extension cov_ext"
    # Exactly one emit, equal to parsed output — no prompts, no `0`, no blanks.
    assert s.emitted == ["Unloaded extension cov_ext"]


@pytest.mark.asyncio
async def test_zephyr_no_output_emits_nothing(zephyr_session: FrameMockSession):
    s = zephyr_session

    async def simulate():
        await asyncio.sleep(0.01)
        s.feed(
            f"\r\n{s._begin_marker}: command not found\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
        )

    feed_task = asyncio.create_task(simulate())
    result = await s.run_cmd("fs mount fat /RAM:")
    await feed_task

    assert result.output == ""
    assert s.emitted == []


@pytest.mark.asyncio
async def test_on_output_argument_overrides_default_sink(bash_session: FrameMockSession):
    s = bash_session
    sink: list[str] = []

    async def simulate():
        await asyncio.sleep(0.01)
        s.feed(f"{s._begin_marker}\nhi\n{s._end_marker_prefix}0__\n")

    feed_task = asyncio.create_task(simulate())
    await s.run_cmd("echo hi", on_output=sink.append)
    await feed_task

    assert sink == ["hi"]
    assert s.emitted == []  # the per-command sink replaced the default


from types import SimpleNamespace

from otto.host.session import TelnetSession


class TestWriteProgress:
    @pytest.mark.asyncio
    async def test_telnet_write_reports_progress_per_chunk(self):
        writes: list[bytes] = []
        writer = SimpleNamespace(write=writes.append)
        s = TelnetSession(reader=None, writer=writer, write_chunk_size=4)
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s._write("0123456789")  # 10 bytes, chunk 4 -> 3 writes

        assert b"".join(writes) == b"0123456789"
        assert progress == [(4, 10), (8, 10), (10, 10)]

    @pytest.mark.asyncio
    async def test_telnet_single_write_reports_once_at_completion(self):
        writer = SimpleNamespace(write=lambda b: None)
        s = TelnetSession(reader=None, writer=writer, write_chunk_size=0)  # unchunked
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s._write("abcd")

        assert progress == [(4, 4)]

    @pytest.mark.asyncio
    async def test_run_cmd_scopes_write_progress_to_framed_write(self, zephyr_session):
        # write_progress is set only for the framed command write, then cleared.
        s = zephyr_session
        seen: list[object] = []
        orig_write = s._write

        async def _record_write(data):
            if s._begin_marker in data:  # the framed command write
                seen.append(s._write_progress)
            await orig_write(data)

        s._write = _record_write
        cb = lambda done, total: None

        async def simulate():
            await asyncio.sleep(0.01)
            s.feed(
                f"\r\n{s._begin_marker}: command not found\r\n~$ "
                f"\r\nok\r\n~$ \r\n0\r\n~$ "
                f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
            )

        feed_task = asyncio.create_task(simulate())
        await s.run_cmd("noop", write_progress=cb)
        await feed_task

        assert seen == [cb]  # set during the framed write
        assert s._write_progress is None  # cleared afterward
