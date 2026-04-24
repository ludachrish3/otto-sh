"""
Unit tests for ShellSession — sentinel wrapping, output parsing,
expect handling, and timeout recovery.

These tests use a concrete MockSession subclass that reads/writes
to in-memory asyncio streams, avoiding any real SSH or telnet connections.
"""

import asyncio
import re
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from otto.host.session import LocalSession, ShellSession, Expect
from otto.utils import CommandStatus, Status


class MockSession(ShellSession):
    """Concrete ShellSession backed by in-memory asyncio streams for testing."""

    def __init__(self) -> None:
        super().__init__()
        # Streams simulating the shell's stdin/stdout
        self._in_reader: asyncio.StreamReader | None = None
        self._in_writer: asyncio.StreamWriter | None = None
        self._out_reader: asyncio.StreamReader | None = None
        self._out_writer: asyncio.StreamWriter | None = None
        # Captures everything written to stdin (for assertions)
        self.written: list[str] = []

    async def _open(self) -> None:
        # Create paired streams: what the session writes to "stdin" can be read
        # by the test, and what the test writes to "stdout" can be read by the session.
        self._out_reader = asyncio.StreamReader()
        # No real writer needed — we feed data directly into the StreamReader

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
            m = pattern.search(buf)
            if m:
                return buf

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    def feed(self, data: str) -> None:
        """Feed data into the session's stdout (simulates shell output)."""
        assert self._out_reader is not None
        self._out_reader.feed_data(data.encode())

    def feed_eof(self) -> None:
        """Signal EOF on the session's stdout (simulates shell death)."""
        assert self._out_reader is not None
        self._out_reader.feed_eof()


@pytest_asyncio.fixture
async def session() -> MockSession:
    """Create and initialize a MockSession."""
    s = MockSession()
    await s._open()

    async def init_handshake():
        await s._ensure_initialized()

    task = asyncio.create_task(init_handshake())
    await asyncio.sleep(0.01)
    s.feed(s._ready_marker + "\n")
    await task
    s.written.clear()
    return s


# ---------------------------------------------------------------------------
# Basic run_cmd
# ---------------------------------------------------------------------------

class TestRunCmd:

    @pytest.mark.asyncio
    async def test_basic_command_output_and_retcode(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"hello world\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello world")

        assert result.command == "echo hello world"
        assert result.output == "hello world"
        assert result.status == Status.Success
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_nonzero_retcode_returns_failed(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"command not found\n"
                f"{session._end_marker_prefix}127__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("badcmd")

        assert result.status == Status.Failed
        assert result.retcode == 127
        assert "command not found" in result.output

    @pytest.mark.asyncio
    async def test_empty_output_command(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("cd /tmp")

        assert result.output == ""
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_multiline_output(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"line1\n"
                f"line2\n"
                f"line3\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("seq 1 3")

        assert result.output == "line1\nline2\nline3"
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_prompt_noise_before_begin_marker_stripped(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"$ {session._begin_marker}\n"
                f"hello\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")

        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_sentinel_wrapping_sent_to_stdin(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        await session.run_cmd("ls")

        # Verify the command was sentinel-wrapped
        cmd_write = session.written[0]
        assert session._begin_marker in cmd_write
        assert "ls" in cmd_write
        assert session._end_marker_prefix in cmd_write
        assert "$?__" in cmd_write


# ---------------------------------------------------------------------------
# Expects
# ---------------------------------------------------------------------------

class TestExpects:

    @pytest.mark.asyncio
    async def test_expect_auto_responds(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"Password:"
            )
            # Wait for the response to be sent
            await asyncio.sleep(0.02)
            session.feed(
                f"\ninstalled ok\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[(r"Password:", "secret\n")],
        )

        assert result.status == Status.Success
        assert "secret\n" in session.written  # response was sent

    @pytest.mark.asyncio
    async def test_multiple_expects(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\nPassword:")
            await asyncio.sleep(0.02)
            session.feed(f"\n[Y/n]")
            await asyncio.sleep(0.02)
            session.feed(f"\ndone\n{session._end_marker_prefix}0__\n")

        asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[
                (r"Password:", "secret\n"),
                (r"\[Y/n\]", "Y\n"),
            ],
        )

        assert result.status == Status.Success
        assert "secret\n" in session.written
        assert "Y\n" in session.written

    @pytest.mark.asyncio
    async def test_unused_expect_pattern_ignored(self, session: MockSession):
        """If an expect pattern never appears, the sentinel matches normally."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"ok\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd(
            "echo ok",
            expects=[(r"Password:", "secret\n")],
        )

        assert result.status == Status.Success
        assert result.output == "ok"
        # The password response should NOT have been sent
        assert "secret\n" not in session.written

    @pytest.mark.asyncio
    async def test_compiled_regex_expect(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\npassword for admin:")
            await asyncio.sleep(0.02)
            session.feed(f"\nok\n{session._end_marker_prefix}0__\n")

        asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo ls",
            expects=[(re.compile(r"password for \w+:"), "pw\n")],
        )

        assert result.status == Status.Success
        assert "pw\n" in session.written


# ---------------------------------------------------------------------------
# Timeout and recovery
# ---------------------------------------------------------------------------

class TestTimeout:

    @pytest.mark.asyncio
    async def test_timeout_returns_error_status(self, session: MockSession):
        # Don't feed any output — command will hang
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n")
            # Never feed the END marker — simulates a hung command

        asyncio.create_task(simulate())

        # After timeout, recovery sends Ctrl+C + recovery sentinel
        # We need to feed the recovery marker
        async def feed_recovery():
            await asyncio.sleep(0.15)
            session.feed(f"{session._recover_marker}\n")

        asyncio.create_task(feed_recovery())

        result = await session.run_cmd("sleep 999", timeout=0.1)

        assert result.status == Status.Error
        assert result.retcode == -1
        assert "timed out" in result.output

    @pytest.mark.asyncio
    async def test_session_stays_alive_after_recovered_timeout(self, session: MockSession):
        # Simulate timeout + successful recovery
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n")
            # Hang...

        asyncio.create_task(simulate())

        async def feed_recovery():
            await asyncio.sleep(0.15)
            session.feed(f"{session._recover_marker}\n")

        asyncio.create_task(feed_recovery())

        await session.run_cmd("sleep 999", timeout=0.1)

        # Session should still be alive
        assert session.alive

    @pytest.mark.asyncio
    async def test_session_dies_if_recovery_fails(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n")
            # Never feed anything — recovery will also time out

        asyncio.create_task(simulate())

        # Patch _RECOVERY_TIMEOUT to something very short for the test
        import otto.host.session as session_mod
        original = session_mod._RECOVERY_TIMEOUT
        session_mod._RECOVERY_TIMEOUT = 0.1
        try:
            result = await session.run_cmd("sleep 999", timeout=0.1)
        finally:
            session_mod._RECOVERY_TIMEOUT = original

        assert result.status == Status.Error
        assert not session.alive


# ---------------------------------------------------------------------------
# Send / Expect (raw)
# ---------------------------------------------------------------------------

class TestSendExpect:

    @pytest.mark.asyncio
    async def test_send_writes_to_stdin(self, session: MockSession):
        await session.send("hello\n")
        assert "hello\n" in session.written

    @pytest.mark.asyncio
    async def test_expect_returns_matched_data(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed("Welcome to Python 3.10\n>>> ")

        asyncio.create_task(simulate())
        data = await session.expect(r">>> ")
        assert ">>> " in data
        assert "Welcome to Python" in data

    @pytest.mark.asyncio
    async def test_expect_timeout_raises(self, session: MockSession):
        # Don't feed any data — expect will timeout
        with pytest.raises(asyncio.TimeoutError):
            await session.expect(r">>> ", timeout=0.1)

    @pytest.mark.asyncio
    async def test_expect_eof_marks_session_dead(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed_eof()

        asyncio.create_task(simulate())
        with pytest.raises(asyncio.IncompleteReadError):
            await session.expect(r">>> ", timeout=1.0)

        assert not session.alive


# ---------------------------------------------------------------------------
# Session initialization
# ---------------------------------------------------------------------------

class MockSessionInit:

    @pytest.mark.asyncio
    async def test_init_sends_stty_and_ready_marker(self):
        s = MockSession()
        await s._open()

        async def init():
            await s._ensure_initialized()

        task = asyncio.create_task(init())
        await asyncio.sleep(0.01)

        # Verify stty -echo and ready marker were sent
        assert len(s.written) == 1
        assert "stty -echo" in s.written[0]
        assert s._ready_marker in s.written[0]

        # Feed the ready marker response
        s.feed(s._ready_marker + "\n")
        await task

        assert s.alive
        assert s._initialized

    @pytest.mark.asyncio
    async def test_init_is_idempotent(self, session: MockSession):
        session.written.clear()
        await session._ensure_initialized()
        # Should not send any additional init commands
        assert len(session.written) == 0


# ---------------------------------------------------------------------------
# Session death and run_cmd error handling
# ---------------------------------------------------------------------------

class MockSessionDeath:

    @pytest.mark.asyncio
    async def test_eof_during_run_cmd_returns_error(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed_eof()

        asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")

        assert result.status == Status.Error
        assert not session.alive

    @pytest.mark.asyncio
    async def test_dead_session_raises_on_send(self, session: MockSession):
        session._alive = False
        with pytest.raises(RuntimeError, match="not alive"):
            await session.send("hello\n")

    @pytest.mark.asyncio
    async def test_dead_session_raises_on_expect(self, session: MockSession):
        session._alive = False
        with pytest.raises(RuntimeError, match="not alive"):
            await session.expect(r">>>")


# ---------------------------------------------------------------------------
# LocalSession — real bash subprocess
# ---------------------------------------------------------------------------

class TestLocalSession:

    @pytest_asyncio.fixture
    async def local_session(self):
        s = LocalSession()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_run_echo_command(self, local_session: LocalSession):
        result = await local_session.run_cmd("echo hello_otto")
        assert result.status == Status.Success
        assert "hello_otto" in result.output
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self, local_session: LocalSession):
        result = await local_session.run_cmd("false")
        assert result.status == Status.Failed
        assert result.retcode == 1

    @pytest.mark.asyncio
    async def test_state_persists_between_commands(self, local_session: LocalSession):
        await local_session.run_cmd("cd /tmp")
        result = await local_session.run_cmd("pwd")
        assert result.status == Status.Success
        assert result.output.strip() == "/tmp"

    @pytest.mark.asyncio
    async def test_env_var_persists(self, local_session: LocalSession):
        await local_session.run_cmd("export OTTO_SESSION_TEST=abc123")
        result = await local_session.run_cmd("echo $OTTO_SESSION_TEST")
        assert "abc123" in result.output

    @pytest.mark.asyncio
    async def test_multiline_output(self, local_session: LocalSession):
        result = await local_session.run_cmd("echo line1; echo line2; echo line3")
        assert result.status == Status.Success
        lines = result.output.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_timeout_recovery(self, local_session: LocalSession):
        result = await local_session.run_cmd("sleep 999", timeout=0.5)
        assert result.status == Status.Error
        assert "timed out" in result.output

        # Session should recover
        result = await local_session.run_cmd("echo recovered")
        assert result.status == Status.Success
        assert "recovered" in result.output

    @pytest.mark.asyncio
    async def test_send_and_expect(self, local_session: LocalSession):
        await local_session.send("echo otto_marker\n")
        output = await local_session.expect(r"otto_marker", timeout=5.0)
        assert "otto_marker" in output

    @pytest.mark.asyncio
    async def test_close_terminates_process(self, local_session: LocalSession):
        await local_session.run_cmd("echo init")
        assert local_session.alive
        await local_session.close()
        assert not local_session.alive

    @pytest.mark.asyncio
    async def test_streaming_callback_receives_lines(self, local_session: LocalSession):
        """_on_output callback is invoked with each line as it arrives."""
        logged: list[str] = []
        local_session._on_output = logged.append
        result = await local_session.run_cmd("echo line1; echo line2; echo line3")
        assert result.status == Status.Success
        assert logged == ["line1", "line2", "line3"]


# ---------------------------------------------------------------------------
# Command logging — incremental output streaming via _on_output
# ---------------------------------------------------------------------------

class TestCommandLogging:

    @pytest.mark.asyncio
    async def test_callback_called_per_line(self, session: MockSession):
        """_on_output is invoked once per output line."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"alpha\n"
                f"bravo\n"
                f"charlie\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("seq 3")

        assert result.status == Status.Success
        assert logged == ["alpha", "bravo", "charlie"]

    @pytest.mark.asyncio
    async def test_sentinels_filtered(self, session: MockSession):
        """BEGIN/END sentinel markers never appear in _on_output calls."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"content\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        await session.run_cmd("echo content")

        assert logged == ["content"]
        for line in logged:
            assert "__OTTO_" not in line

    @pytest.mark.asyncio
    async def test_empty_output_no_callback(self, session: MockSession):
        """Commands with no output produce zero _on_output calls."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("cd /tmp")

        assert result.status == Status.Success
        assert logged == []

    @pytest.mark.asyncio
    async def test_with_expects(self, session: MockSession):
        """_on_output works alongside expect auto-responses."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\nPassword:")
            await asyncio.sleep(0.02)
            session.feed(f"\ninstalled ok\n{session._end_marker_prefix}0__\n")

        asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[(r"Password:", "secret\n")],
        )

        assert result.status == Status.Success
        assert "installed ok" in logged
        # Expect prompt text should NOT appear in logged output
        for line in logged:
            assert "Password:" not in line

    @pytest.mark.asyncio
    async def test_partial_output_before_timeout(self, session: MockSession):
        """Lines received before a timeout are still delivered to _on_output."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"early line\n"
            )
            # Never send end sentinel — command will time out

        asyncio.create_task(simulate())
        result = await session.run_cmd("long_running_cmd", timeout=0.3)

        assert result.status == Status.Error
        assert "early line" in logged

    @pytest.mark.asyncio
    async def test_final_output_unchanged(self, session: MockSession):
        """CommandStatus.output still contains the complete output."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"one\n"
                f"two\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("test")

        assert result.output == "one\ntwo"
        assert logged == ["one", "two"]

    @pytest.mark.asyncio
    async def test_default_noop_callback(self, session: MockSession):
        """run_cmd works correctly with the default no-op _on_output."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\n"
                f"hello\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")

        assert result.status == Status.Success
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_echoed_command_filtered(self, session: MockSession):
        """Shell echo of the wrapped command is not passed to _on_output."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            # Simulate shell echoing the wrapped command before the BEGIN marker
            session.feed(
                f'echo "{session._begin_marker}"; ls; echo "{session._end_marker_prefix}$?__"\n'
                f"{session._begin_marker}\n"
                f"file.txt\n"
                f"{session._end_marker_prefix}0__\n"
            )

        asyncio.create_task(simulate())
        result = await session.run_cmd("ls")

        assert result.output == "file.txt"
        assert logged == ["file.txt"]
