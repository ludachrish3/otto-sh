"""
Unit tests for ZephyrSession — the Zephyr RTOS shell framing seam.

These use a MockZephyrSession backed by in-memory streams (mirroring the
MockSession double in test_session.py). The simulated shell output models the
*real* Zephyr telnet shell, verified live on Zephyr 3.7.2: input is **not**
echoed, and the shell prints its prompt after every executed line.
"""

import asyncio
import re

import pytest
import pytest_asyncio

from otto.host.zephyr import ZephyrSession
from otto.utils import Status


class MockZephyrSession(ZephyrSession):
    """ZephyrSession backed by in-memory asyncio streams for testing.

    Keeps ZephyrSession's framing/parsing seam; only the I/O primitives are
    swapped for in-memory equivalents.
    """

    def __init__(self) -> None:
        super().__init__(reader=None, writer=None)
        self._out_reader: asyncio.StreamReader | None = None
        self.written: list[str] = []

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
        """Feed data into the session's stdout (simulates shell output)."""
        assert self._out_reader is not None
        self._out_reader.feed_data(data.encode())

    def feed_eof(self) -> None:
        assert self._out_reader is not None
        self._out_reader.feed_eof()

    def shell_response(self, output: str, retcode: int, prompt: str = '~$ ') -> str:
        """Build raw shell output for a framed command, in the real format.

        Verified live on Zephyr 3.7.2: input is not echoed; the shell prints
        ``\\r\\n<result>\\r\\n<prompt>`` for each of the four framed lines (the
        two rejected markers, the command, and ``retval``).
        """
        if output:
            body = ''.join(f'{ln}\r\n' for ln in output.split('\n'))
        else:
            body = ''
        return (
            f'\r\n{self._begin_marker}: command not found\r\n{prompt}'
            f'\r\n{body}{prompt}'
            f'\r\n{retcode}\r\n{prompt}'
            f'\r\n{self._end_marker_prefix}: command not found\r\n{prompt}'
        )


@pytest_asyncio.fixture
async def session() -> MockZephyrSession:
    """Create and initialize a MockZephyrSession (readiness handshake done)."""
    s = MockZephyrSession()
    await s._open()

    async def init_handshake():
        await s._ensure_initialized()

    task = asyncio.create_task(init_handshake())
    await asyncio.sleep(0.01)
    # The shell rejects the unknown READY token and echoes it in the error line.
    s.feed(f"\r\n{s._ready_marker}: command not found\r\n~$ ")
    await task
    s.written.clear()
    return s


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

class TestFraming:

    def test_frame_command_is_four_cr_separated_lines(self):
        s = MockZephyrSession()
        framed = s._frame_command('kernel version')
        assert framed == (
            f"{s._begin_marker}\r"
            f"kernel version\r"
            f"retval\r"
            f"{s._end_marker_prefix}\r"
        )

    def test_handshake_command_is_bare_marker(self):
        s = MockZephyrSession()
        assert s._handshake_command() == f"{s._ready_marker}\n"

    def test_recover_command_is_bare_marker(self):
        s = MockZephyrSession()
        assert s._recover_command() == f"{s._recover_marker}\n"

    def test_end_pattern_has_no_retcode_group(self):
        """The Zephyr END marker carries no exit code (retval reports it)."""
        s = MockZephyrSession()
        assert s._end_pattern.pattern == rf"__OTTO_{s._session_id}_END__"

    def test_marks_begin_matches_command_not_found_line(self):
        """Zephyr rejects BEGIN as `<token>: command not found` — substring."""
        s = MockZephyrSession()
        assert s._marks_begin(f"{s._begin_marker}: command not found\r\n")
        assert not s._marks_begin("some other line\r\n")

    def test_init_timeout_is_generous(self):
        """The QEMU telnet shell needs warm-up; the handshake ceiling is raised."""
        assert MockZephyrSession._init_timeout >= 10.0

    @pytest.mark.asyncio
    async def test_run_cmd_writes_framed_command(self, session: MockZephyrSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('3.7.2', 0))

        asyncio.create_task(simulate())
        await session.run_cmd('kernel version')
        assert session.written[0] == session._frame_command('kernel version')


# ---------------------------------------------------------------------------
# Output and return codes
# ---------------------------------------------------------------------------

class TestRunCmd:

    @pytest.mark.asyncio
    async def test_success(self, session: MockZephyrSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('Zephyr version 3.7.2', 0))

        asyncio.create_task(simulate())
        result = await session.run_cmd('kernel version')

        assert result.output == 'Zephyr version 3.7.2'
        assert result.retcode == 0
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_negative_retcode_is_failure(self, session: MockZephyrSession):
        """Zephyr return codes are signed errno-style values."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('usage: ...', -22))

        asyncio.create_task(simulate())
        result = await session.run_cmd('device off bad')

        assert result.retcode == -22
        assert result.status == Status.Failed

    @pytest.mark.asyncio
    async def test_unknown_command_retcode(self, session: MockZephyrSession):
        """An unknown command yields the shell's -8 (-ENOEXEC)."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('bogus: command not found', -8))

        asyncio.create_task(simulate())
        result = await session.run_cmd('bogus')

        assert result.retcode == -8
        assert result.status == Status.Failed
        assert result.output == 'bogus: command not found'

    @pytest.mark.asyncio
    async def test_empty_output(self, session: MockZephyrSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('', 0))

        asyncio.create_task(simulate())
        result = await session.run_cmd('kernel reboot cold')

        assert result.output == ''
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_multiline_output(self, session: MockZephyrSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response(
                'devices:\n- uart@3f8 (READY)\n- eth0 (READY)', 0))

        asyncio.create_task(simulate())
        result = await session.run_cmd('device list')

        assert result.output == 'devices:\n- uart@3f8 (READY)\n- eth0 (READY)'
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_integer_in_output_not_mistaken_for_retcode(
        self, session: MockZephyrSession,
    ):
        """A bare integer in command output must not be read as the retcode."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response('123456', 0))

        asyncio.create_task(simulate())
        result = await session.run_cmd('kernel uptime')

        assert result.output == '123456'
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_custom_prompt_handled_positionally(self, session: MockZephyrSession):
        """A non-default prompt is stripped just the same — parsing is positional,
        it never reads the prompt text."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response(
                'Zephyr version 3.7.2', 0, prompt='zephyr-board:/$ '))

        asyncio.create_task(simulate())
        result = await session.run_cmd('kernel version')

        assert result.output == 'Zephyr version 3.7.2'
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_ansi_colour_codes_are_stripped(self, session: MockZephyrSession):
        """The Zephyr shell colours its prompt; ANSI escapes must not leak into
        the parsed output."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(session.shell_response(
                'Zephyr version 3.7.2', 0, prompt='\x1b[1;32m~$ \x1b[m'))

        asyncio.create_task(simulate())
        result = await session.run_cmd('kernel version')

        assert result.output == 'Zephyr version 3.7.2'
        assert result.retcode == 0


# ---------------------------------------------------------------------------
# Expect handling (shared engine, exercised over Zephyr framing)
# ---------------------------------------------------------------------------

class TestExpect:

    @pytest.mark.asyncio
    async def test_expect_response_is_sent(self, session: MockZephyrSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"\r\n{session._begin_marker}: command not found\r\n~$ ")
            session.feed("\r\nconfirm? ")
            await asyncio.sleep(0.01)
            session.feed(
                "\r\ndone\r\n~$ "
                "\r\n0\r\n~$ "
                f"\r\n{session._end_marker_prefix}: command not found\r\n~$ "
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd('risky', expects=[('confirm', 'y\r')])

        assert 'y\r' in session.written
        assert result.retcode == 0
