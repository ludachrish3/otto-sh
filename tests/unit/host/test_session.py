"""
Unit tests for ShellSession — sentinel wrapping, output parsing,
expect handling, and timeout recovery.

These tests use a concrete MockSession subclass that reads/writes
to in-memory asyncio streams, avoiding any real SSH or telnet connections.
"""

import asyncio
import os
import re
import signal
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from otto.host.session import LocalSession, ShellSession
from otto.utils import Status


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
            session.feed(f"{session._begin_marker}\nhello world\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello world")
        await feed_task

        assert result.command == "echo hello world"
        assert result.output == "hello world"
        assert result.status == Status.Success
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_nonzero_retcode_returns_failed(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\ncommand not found\n{session._end_marker_prefix}127__\n"
            )

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("badcmd")
        await feed_task

        assert result.status == Status.Failed
        assert result.retcode == 127
        assert "command not found" in result.output

    @pytest.mark.asyncio
    async def test_empty_output_command(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("cd /tmp")
        await feed_task

        assert result.output == ""
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_multiline_output(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(
                f"{session._begin_marker}\nline1\nline2\nline3\n{session._end_marker_prefix}0__\n"
            )

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("seq 1 3")
        await feed_task

        assert result.output == "line1\nline2\nline3"
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_prompt_noise_before_begin_marker_stripped(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"$ {session._begin_marker}\nhello\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")
        await feed_task

        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_sentinel_wrapping_sent_to_stdin(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        await session.run_cmd("ls")
        await feed_task

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
            session.feed(f"{session._begin_marker}\nPassword:")
            # Wait for the response to be sent
            await asyncio.sleep(0.02)
            session.feed(f"\ninstalled ok\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[(r"Password:", "secret\n")],
        )
        await feed_task

        assert result.status == Status.Success
        assert "secret\n" in session.written  # response was sent

    @pytest.mark.asyncio
    async def test_multiple_expects(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\nPassword:")
            await asyncio.sleep(0.02)
            session.feed("\n[Y/n]")
            await asyncio.sleep(0.02)
            session.feed(f"\ndone\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[
                (r"Password:", "secret\n"),
                (r"\[Y/n\]", "Y\n"),
            ],
        )
        await feed_task

        assert result.status == Status.Success
        assert "secret\n" in session.written
        assert "Y\n" in session.written

    @pytest.mark.asyncio
    async def test_unused_expect_pattern_ignored(self, session: MockSession):
        """If an expect pattern never appears, the sentinel matches normally."""

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\nok\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd(
            "echo ok",
            expects=[(r"Password:", "secret\n")],
        )
        await feed_task

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

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo ls",
            expects=[(re.compile(r"password for \w+:"), "pw\n")],
        )
        await feed_task

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

        simulate_task = asyncio.create_task(simulate())

        # After timeout, recovery sends Ctrl+C + recovery sentinel
        # We need to feed the recovery marker
        async def feed_recovery():
            await asyncio.sleep(0.15)
            session.feed(f"{session._recover_marker}\n")

        recovery_task = asyncio.create_task(feed_recovery())

        result = await session.run_cmd("sleep 999", timeout=0.1)
        await simulate_task
        await recovery_task

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

        simulate_task = asyncio.create_task(simulate())

        async def feed_recovery():
            await asyncio.sleep(0.15)
            session.feed(f"{session._recover_marker}\n")

        recovery_task = asyncio.create_task(feed_recovery())

        await session.run_cmd("sleep 999", timeout=0.1)
        await simulate_task
        await recovery_task

        # Session should still be alive
        assert session.alive

    @pytest.mark.asyncio
    async def test_session_dies_if_recovery_fails(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n")
            # Never feed anything — recovery will also time out

        feed_task = asyncio.create_task(simulate())

        # Patch _RECOVERY_TIMEOUT to something very short for the test
        import otto.host.session as session_mod

        original = session_mod._RECOVERY_TIMEOUT
        session_mod._RECOVERY_TIMEOUT = 0.1
        try:
            result = await session.run_cmd("sleep 999", timeout=0.1)
        finally:
            session_mod._RECOVERY_TIMEOUT = original
        await feed_task

        assert result.status == Status.Error
        assert not session.alive


# ---------------------------------------------------------------------------
# Send / Expect (raw)  # noqa: ERA001 — section divider comment
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

        feed_task = asyncio.create_task(simulate())
        data = await session.expect(r">>> ")
        await feed_task
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

        feed_task = asyncio.create_task(simulate())
        with pytest.raises(asyncio.IncompleteReadError):
            await session.expect(r">>> ", timeout=1.0)
        await feed_task

        assert not session.alive


# ---------------------------------------------------------------------------
# Session initialization
# ---------------------------------------------------------------------------


def test_shell_session_current_user_defaults_empty():
    """A freshly constructed shell session has no tracked user yet."""
    s = MockSession()
    assert s.current_user == ""


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

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")
        await feed_task

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
        result = await local_session.run_cmd("sleep 999", timeout=0.1)
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

    def test_signal_children_tolerates_child_exiting_mid_scan(self):
        """A child that exits between the /proc scan and os.kill must not crash recovery.

        Reproduces the TOCTOU race behind the flaky
        ``test_slow_command_times_out_and_session_recovers``: _signal_children reads
        ``/proc/<pid>/stat`` to find children, then signals each with os.kill. If a
        child exits in that window, os.kill raises ProcessLookupError (ESRCH, errno 3).
        That is a sibling of FileNotFoundError (ENOENT, errno 2), not a subclass, so the
        existing handler did not catch it and recovery blew up. A vanishing child is
        exactly what we are trying to signal, so it must be swallowed silently.
        """
        # Guarantee at least one matching child so the scan reaches the os.kill call;
        # without it the loop would no-op and the test would pass vacuously.
        child = subprocess.Popen(["sleep", "30"])
        try:
            with patch("os.kill", side_effect=ProcessLookupError(3, "No such process")):
                # Must not raise — the race is benign and gets swallowed.
                LocalSession._signal_children(os.getpid(), signal.SIGINT)
        finally:
            child.terminate()
            child.wait()


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
                f"{session._begin_marker}\nalpha\nbravo\ncharlie\n{session._end_marker_prefix}0__\n"
            )

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("seq 3")
        await feed_task

        assert result.status == Status.Success
        assert logged == ["alpha", "bravo", "charlie"]

    @pytest.mark.asyncio
    async def test_sentinels_filtered(self, session: MockSession):
        """BEGIN/END sentinel markers never appear in _on_output calls."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\ncontent\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        await session.run_cmd("echo content")
        await feed_task

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
            session.feed(f"{session._begin_marker}\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("cd /tmp")
        await feed_task

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

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd(
            "sudo apt install nginx",
            expects=[(r"Password:", "secret\n")],
        )
        await feed_task

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
            session.feed(f"{session._begin_marker}\nearly line\n")
            # Never send end sentinel — command will time out

        feed_task = asyncio.create_task(simulate())
        # Patch the recovery timeout to a small value so the post-timeout
        # ``_recover_session`` call doesn't block this test for the full
        # 5-second recovery window (no recovery sentinel ever arrives).
        import otto.host.session as session_mod

        original = session_mod._RECOVERY_TIMEOUT
        session_mod._RECOVERY_TIMEOUT = 0.05
        try:
            result = await session.run_cmd("long_running_cmd", timeout=0.1)
        finally:
            session_mod._RECOVERY_TIMEOUT = original
        await feed_task

        assert result.status == Status.Error
        assert "early line" in logged

    @pytest.mark.asyncio
    async def test_final_output_unchanged(self, session: MockSession):
        """CommandStatus.output still contains the complete output."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\none\ntwo\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("test")
        await feed_task

        assert result.output == "one\ntwo"
        assert logged == ["one", "two"]

    @pytest.mark.asyncio
    async def test_default_noop_callback(self, session: MockSession):
        """run_cmd works correctly with the default no-op _on_output."""

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\nhello\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")
        await feed_task

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

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("ls")
        await feed_task

        assert result.output == "file.txt"
        assert logged == ["file.txt"]


# ---------------------------------------------------------------------------
# Marker handshake timeout (_ensure_initialized)
# ---------------------------------------------------------------------------


class TestEnsureInitializedTimeout:
    """The post-open marker handshake must be bounded.

    A failed telnet login leaves the device in its login-prompt loop — no
    shell spawns, so the READY marker never appears. Without a bound the
    handshake hangs forever; with one it must surface a clear error.
    """

    @pytest.mark.asyncio
    async def test_missing_marker_raises_clear_error(self):
        """Marker never arrives -> ConnectionError, not an indefinite hang."""
        s = MockSession()
        s._init_timeout = 0.05  # shrink so the test is fast
        await s._open()
        # Never feed the READY marker — simulates a stuck login prompt.

        with pytest.raises(ConnectionError, match="never became ready"):
            await s._ensure_initialized()

        assert s.alive is False
        assert s._initialized is False

    @pytest.mark.asyncio
    async def test_eof_during_handshake_raises_clear_error(self):
        """Peer EOF mid-handshake also surfaces as a clear error."""
        s = MockSession()
        s._init_timeout = 5.0  # EOF fires first; timeout shouldn't be reached
        await s._open()

        async def drop():
            await asyncio.sleep(0.01)
            s.feed_eof()

        drop_task = asyncio.create_task(drop())
        with pytest.raises(ConnectionError, match="never became ready"):
            await s._ensure_initialized()
        await drop_task

        assert s.alive is False


def test_session_manager_current_user_falls_back_to_login():
    from otto.host.session import SessionManager

    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    assert mgr.current_user == "alice"  # no default session built yet


def test_session_manager_current_user_empty_without_connections():
    from otto.host.session import SessionManager

    mgr = SessionManager(name="local")  # connections=None (e.g. LocalHost)
    assert mgr.current_user == ""


def test_session_manager_current_user_tolerates_connections_without_credentials():
    """Seeding runs _login_user on every session build, so a connection
    manager that exposes no ``credentials`` (minimal test fakes, loginless
    transports) must fall back to '' rather than raise."""
    import types

    from otto.host.session import SessionManager

    mgr = SessionManager(connections=types.SimpleNamespace(), name="h")
    assert mgr._login_user() == ""
    assert mgr.current_user == ""
    s = MockSession()
    mgr._seed_user(s)  # must not raise
    assert s.current_user == ""


def test_session_manager_seed_user_stamps_login_user():
    from otto.host.session import SessionManager

    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._seed_user(s)
    assert s.current_user == "alice"


def test_session_manager_set_current_user_updates_default_session():
    from otto.host.session import SessionManager

    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._session = s
    mgr._set_current_user("root")
    assert s.current_user == "root"
    assert mgr.current_user == "root"


def test_session_manager_accepts_user_password_arg():
    from otto.host.session import SessionManager

    mgr = SessionManager(name="h", user_password=lambda u: "pw")
    assert mgr._user_password("anyone") == "pw"


@pytest.mark.asyncio
async def test_host_session_current_user_delegates_to_shell():
    from otto.host.session import HostSession

    shell = MockSession()
    shell.current_user = "alice"
    hs = HostSession("n", shell, lambda *_: None, lambda *_: None, lambda _: None)
    assert hs.current_user == "alice"


@pytest.mark.asyncio
async def test_host_session_switch_user_without_resolver_raises():
    from otto.host.session import HostSession

    shell = MockSession()
    hs = HostSession("n", shell, lambda *_: None, lambda *_: None, lambda _: None)
    with pytest.raises(NotImplementedError):
        await hs.switch_user("root")


@pytest.mark.asyncio
async def test_host_session_switch_user_elevates_and_stamps():
    from otto.host.session import HostSession

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession(
        "n",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        user_password=lambda u: "rootpw",
    )
    await hs.switch_user("root")
    assert shell.current_user == "root"
    sent = [c.args[0] for c in shell.send.await_args_list]
    assert "su root\n" in sent
    assert "rootpw\n" in sent


@pytest.mark.asyncio
async def test_host_session_as_user_restores_previous():
    from otto.host.session import HostSession

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession(
        "n",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        user_password=lambda u: "rootpw",
    )
    async with hs.as_user("root"):
        assert shell.current_user == "root"
    assert shell.current_user == "alice"


@pytest.mark.asyncio
async def test_default_session_seeds_current_user_from_login():
    """The default session is stamped with the login user at build time."""
    import types

    from otto.host.session import SessionManager

    conn = types.SimpleNamespace(credentials=("alice", "pw"))
    built: list[MockSession] = []

    def factory() -> MockSession:
        s = MockSession()
        built.append(s)
        return s

    mgr = SessionManager(connections=conn, name="h", session_factory=factory)
    task = asyncio.create_task(mgr.send("hello\n"))  # triggers default-session build
    await asyncio.sleep(0.01)
    built[0].feed(built[0]._ready_marker + "\n")
    await task
    assert mgr._session is built[0]
    assert mgr._session.current_user == "alice"
    assert mgr.current_user == "alice"


@pytest.mark.asyncio
async def test_open_session_seeds_named_session_current_user_from_login():
    """A freshly opened named session is stamped with the login user."""
    import types

    from otto.host.session import SessionManager

    conn = types.SimpleNamespace(credentials=("alice", "pw"))
    built: list[MockSession] = []

    def factory() -> MockSession:
        s = MockSession()
        built.append(s)
        return s

    mgr = SessionManager(connections=conn, name="h", session_factory=factory)
    task = asyncio.create_task(mgr.open_session("mon"))
    await asyncio.sleep(0.01)
    built[0].feed(built[0]._ready_marker + "\n")
    hs = await task
    assert hs.current_user == "alice"


@pytest.mark.asyncio
async def test_named_session_elevation_does_not_touch_default():
    """Elevating a named session leaves another (default) session untouched."""
    from otto.host.session import HostSession

    default_shell = MockSession()
    default_shell.current_user = "alice"
    named_shell = AsyncMock(spec=ShellSession)
    named_shell.current_user = "alice"
    named_shell.expect.return_value = "Password:"
    hs = HostSession(
        "mon",
        named_shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        user_password=lambda u: "rootpw",
    )
    await hs.switch_user("root")
    assert named_shell.current_user == "root"  # named session elevated
    assert default_shell.current_user == "alice"  # the other session untouched


@pytest.mark.asyncio
async def test_host_session_as_user_nested_restores_each_level():
    """Nested as_user blocks restore the prior user at each level."""
    from otto.host.session import HostSession

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession(
        "mon", shell, lambda *_: None, lambda *_: None, lambda _: None, user_password=lambda u: "pw"
    )
    async with hs.as_user("bob"):
        assert shell.current_user == "bob"
        async with hs.as_user("root"):
            assert shell.current_user == "root"
        assert shell.current_user == "bob"  # inner block restored to bob
    assert shell.current_user == "alice"  # outer block restored to alice
