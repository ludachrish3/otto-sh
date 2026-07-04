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

from otto.host.session import LocalSession, SessionManager, ShellSession
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
        assert result.value == "hello world"
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
        assert "command not found" in result.value

    @pytest.mark.asyncio
    async def test_empty_output_command(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("cd /tmp")
        await feed_task

        assert result.value == ""
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

        assert result.value == "line1\nline2\nline3"
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_prompt_noise_before_begin_marker_stripped(self, session: MockSession):
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"$ {session._begin_marker}\nhello\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("echo hello")
        await feed_task

        assert result.value == "hello"

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
        assert result.value == "ok"
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
        assert "timed out" in result.value

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
        assert "hello_otto" in result.value
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
        assert result.value.strip() == "/tmp"

    @pytest.mark.asyncio
    async def test_env_var_persists(self, local_session: LocalSession):
        await local_session.run_cmd("export OTTO_SESSION_TEST=abc123")
        result = await local_session.run_cmd("echo $OTTO_SESSION_TEST")
        assert "abc123" in result.value

    @pytest.mark.asyncio
    async def test_multiline_output(self, local_session: LocalSession):
        result = await local_session.run_cmd("echo line1; echo line2; echo line3")
        assert result.status == Status.Success
        lines = result.value.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_timeout_recovery(self, local_session: LocalSession):
        result = await local_session.run_cmd("sleep 999", timeout=0.1)
        assert result.status == Status.Error
        assert "timed out" in result.value

        # Session should recover
        result = await local_session.run_cmd("echo recovered")
        assert result.status == Status.Success
        assert "recovered" in result.value

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
        """CommandResult.value still contains the complete output."""
        logged: list[str] = []
        session._on_output = logged.append

        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\none\ntwo\n{session._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await session.run_cmd("test")
        await feed_task

        assert result.value == "one\ntwo"
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
        assert result.value == "hello"

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

        assert result.value == "file.txt"
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

    conn = MagicMock(spec=["credentials"])
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

    conn = MagicMock(spec=["credentials"])
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._seed_user(s)
    assert s.current_user == "alice"


def test_session_manager_set_current_user_updates_default_session():
    from otto.host.session import SessionManager

    conn = MagicMock(spec=["credentials"])
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._session = s
    mgr._set_current_user("root")
    assert s.current_user == "root"
    assert mgr.current_user == "root"


def test_session_manager_accepts_creds_arg():
    from otto.host.login_proxy import Cred
    from otto.host.session import SessionManager

    mgr = SessionManager(name="h", creds=[Cred(login="root", password="pw")], host_id="h")
    assert mgr._creds == [Cred(login="root", password="pw")]
    assert mgr._host_id == "h"


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
    from otto.host.login_proxy import Cred
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
        creds=[Cred(login="root", password="rootpw")],
        host_id="n",
    )
    await hs.switch_user("root")
    assert shell.current_user == "root"
    sent = [c.args[0] for c in shell.send.await_args_list]
    assert "su root\n" in sent
    assert "rootpw\n" in sent


@pytest.mark.asyncio
async def test_host_session_as_user_restores_previous():
    from otto.host.login_proxy import Cred
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
        creds=[Cred(login="root", password="rootpw")],
        host_id="n",
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
    from otto.host.login_proxy import Cred
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
        creds=[Cred(login="root", password="rootpw")],
        host_id="mon",
    )
    await hs.switch_user("root")
    assert named_shell.current_user == "root"  # named session elevated
    assert default_shell.current_user == "alice"  # the other session untouched


@pytest.mark.asyncio
async def test_host_session_as_user_nested_restores_each_level():
    """Nested as_user blocks restore the prior user at each level."""
    from otto.host.login_proxy import Cred
    from otto.host.session import HostSession

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession(
        "mon",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        creds=[Cred(login="bob", password="pw"), Cred(login="root", password="pw")],
        host_id="mon",
    )
    async with hs.as_user("bob"):
        assert shell.current_user == "bob"
        async with hs.as_user("root"):
            assert shell.current_user == "root"
        assert shell.current_user == "bob"  # inner block restored to bob
    assert shell.current_user == "alice"  # outer block restored to alice


@pytest.mark.asyncio
async def test_host_session_as_user_undo_via_ordering_observable():
    """HostSession path (its own separate undo loop): a proxy with a CUSTOM

    undo makes the ``via`` handed to each reverse hop observable. Undo #1
    (mysql) must see via=admin and undo #2 (admin) must see via=root, each
    carrying the FULL via cred (password intact). Guards the ``applied[-i-2]``
    reverse index + full-cred lookup in ``HostSession.as_user``, which the only
    built-in proxy (``su``, no custom undo) can never exercise.
    """
    from otto.host.login_proxy import Cred, register_login_proxy
    from otto.host.session import HostSession

    captured: list[tuple[str, str, str | None]] = []

    async def fake_fn(io, ctx):
        await io.send(f"become {ctx.target.login}\n")

    async def fake_undo(io, ctx):
        captured.append((ctx.target.login, ctx.via.login, ctx.via.password))
        await io.send("leave\n")

    register_login_proxy("task6-fake-undo-session", fake_fn, undo=fake_undo, overwrite=True)

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "root"
    shell.expect.return_value = "Password:"
    hs = HostSession(
        "mon",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        creds=[
            Cred(login="root", password="rootpw"),
            Cred(login="admin", password="adminpw", proxy="task6-fake-undo-session", via="root"),
            Cred(login="mysql", password="mysqlpw", proxy="task6-fake-undo-session", via="admin"),
        ],
        host_id="mon",
    )

    async with hs.as_user("mysql"):
        assert captured == []  # nothing undone until the block exits

    assert captured == [
        ("mysql", "admin", "adminpw"),  # undo #1: reverse-innermost, via = admin cred
        ("admin", "root", "rootpw"),  # undo #2: via = root cred (the prior user)
    ]


# ---------------------------------------------------------------------------
# Login-proxy hop replay at session establishment (Task 7)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from otto.host.login_proxy import Cred, LoginProxyError, register_login_proxy
from otto.logger.mode import LogMode


class _ImmediateSession(ShellSession):
    """A ``ShellSession`` whose handshake succeeds instantly.

    Mirrors ``_AliveStubSession`` in test_session_logging.py — no async
    orchestration is needed to drive it past readiness, so these tests can
    call ``_ensure_session``/``open_session`` directly and assert on the
    raw writes a login-proxy hop produces. ``expect()`` only returns a
    canned response (``expect_response``) rather than driving a real
    read loop — most hops here send a password directly (log=NEVER)
    without waiting on a prompt, since that interplay is already covered
    by the built-in ``su`` proxy's own tests (test_login_proxy.py) and
    Task 6's switch_user/as_user tests; the one test that does exercise
    the built-in ``su`` proxy's ``expect()`` call supplies a response.
    """

    def __init__(self, expect_response: str | None = None) -> None:
        super().__init__()
        self.writes: list[str] = []
        self.closed = False
        self._expect_response = expect_response

    async def _open(self) -> None: ...

    async def _write(self, data: str) -> None:
        self.writes.append(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        # Resync-aware: otto.host.login_proxy.run_proxy/run_undo now end every
        # hop with a post-transition "echo <marker>"/expect(marker) resync —
        # answer that transparently (most hops here never call expect()
        # themselves, so without this every hop replay would raise below).
        if self.writes and self.writes[-1].startswith("echo __OTTO_LP_SYNC_"):
            marker = self.writes[-1].removeprefix("echo ").rstrip("\n")
            return f"\n{marker}\n"
        if self._expect_response is not None:
            return self._expect_response
        raise AssertionError("this fake does not support expect(); pass expect_response=...")

    async def close(self) -> None:
        self.closed = True
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True


def _proxy_connections(
    hops: list[Cred],
    login_target: str = "mysql",
    credentials: tuple[str, str | None] = ("admin", "adminpw"),
) -> SimpleNamespace:
    """Minimal connections fake exposing exactly what ``_apply_login_proxy`` reads."""
    return SimpleNamespace(credentials=credentials, login_target=login_target, proxy_hops=hops)


async def _task7_hop_with_password(io, ctx) -> None:
    """Fake registered proxy: send + a redacted password line — no expect() needed."""
    await io.send(f"su {ctx.target.login}\n")
    if ctx.target.password is not None:
        await io.send(ctx.target.password + "\n", log=LogMode.NEVER)


def _without_resync_writes(writes: list[str]) -> list[str]:
    """Drop the engine's post-transition "echo <marker>" resync probes.

    ``run_proxy``/``run_undo`` now end every hop with a resync (see
    ``otto.host.login_proxy._resync_shell``) — filter its noise out before
    asserting on the exact write sequence a test cares about.
    """
    return [w for w in writes if not w.startswith("echo __OTTO_LP_SYNC_")]


class TestLoginProxyAtSessionEstablishment:
    """Task 7: ``_apply_login_proxy`` replay in both session-establishment paths."""

    @pytest.mark.asyncio
    async def test_ensure_session_applies_proxy_hop_default_session(self):
        """Default session: a passwordless hop via the built-in su proxy replays + stamps user."""
        hop = Cred(login="mysql", proxy="su", via="admin")
        conn = _proxy_connections([hop])
        built: list[_ImmediateSession] = []

        def factory() -> _ImmediateSession:
            s = _ImmediateSession()
            built.append(s)
            return s

        mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")
        await mgr._ensure_session()

        assert mgr._session is built[0]
        assert mgr._session.current_user == "mysql"
        assert "su mysql\n" in built[0].writes
        assert len(built) == 1  # a proxy success must not trigger a rebuild

    @pytest.mark.asyncio
    async def test_ensure_session_no_hops_stamps_login_target_only(self):
        """No proxy_hops -> plain direct login: existing behavior intact, no proxy sends."""
        conn = _proxy_connections([], login_target="alice", credentials=("alice", "pw"))
        built: list[_ImmediateSession] = []

        def factory() -> _ImmediateSession:
            s = _ImmediateSession()
            built.append(s)
            return s

        mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")
        await mgr._ensure_session()

        assert mgr._session.current_user == "alice"
        assert built[0].writes == []

    @pytest.mark.asyncio
    async def test_ensure_session_password_hop_redacted_in_log(self):
        """A hop's password send arrives with log=NEVER — never reaches the command-log sink."""
        register_login_proxy("task7-hop-pw", _task7_hop_with_password, overwrite=True)
        hop = Cred(login="mysql", password="mysqlpw", proxy="task7-hop-pw", via="admin")
        conn = _proxy_connections([hop])
        logged_cmds: list[tuple[str, LogMode]] = []

        mgr = SessionManager(
            connections=conn,
            name="h",
            session_factory=_ImmediateSession,
            log_command=lambda cmd, mode: logged_cmds.append((cmd, mode)),
            host_id="h",
        )
        await mgr._ensure_session()

        assert mgr._session.current_user == "mysql"
        assert _without_resync_writes(mgr._session.writes) == ["su mysql\n", "mysqlpw\n"]
        assert ("su mysql", LogMode.NORMAL) in logged_cmds
        assert not any(cmd == "mysqlpw" for cmd, _ in logged_cmds)

    @pytest.mark.asyncio
    async def test_ensure_session_builtin_su_hop_drives_expect(self):
        """The built-in su proxy's expect()-driven password prompt, end to end.

        Covers ``_SessionProxyIO.expect()`` (the redirect-based hop-with-password
        test above uses a fake proxy that never calls ``expect()``): asserts the
        matched prompt is logged via ``_log_output`` and the password itself
        still never reaches ``_log_command``.
        """
        hop = Cred(login="mysql", password="mysqlpw", proxy="su", via="admin")
        conn = _proxy_connections([hop])
        logged_cmds: list[tuple[str, LogMode]] = []
        logged_out: list[tuple[str, LogMode]] = []

        def factory() -> _ImmediateSession:
            return _ImmediateSession(expect_response="Password: ")

        mgr = SessionManager(
            connections=conn,
            name="h",
            session_factory=factory,
            log_command=lambda cmd, mode: logged_cmds.append((cmd, mode)),
            log_output=lambda out, mode: logged_out.append((out, mode)),
            host_id="h",
        )
        await mgr._ensure_session()

        assert mgr._session.current_user == "mysql"
        assert _without_resync_writes(mgr._session.writes) == ["su mysql\n", "mysqlpw\n"]
        assert ("su mysql", LogMode.NORMAL) in logged_cmds
        assert not any(cmd == "mysqlpw" for cmd, _ in logged_cmds)
        assert ("Password: ", LogMode.NORMAL) in logged_out

    @pytest.mark.asyncio
    async def test_ensure_session_multi_hop_via_chain_ordering(self):
        """Each hop after the first receives the PREVIOUS hop's login as `via`.

        Also pins that `via` is the FULL cred (password intact), resolved from
        ``self._creds`` via ``cred_for`` — a regression back to a bare
        ``Cred(login=via_login)`` would capture ``None`` for the via password
        and fail. Mirrors Task 6's undo-ordering observability test.
        """
        captured: list[tuple[str, str, str | None]] = []

        async def _record_via(io, ctx) -> None:
            captured.append((ctx.target.login, ctx.via.login, ctx.via.password))
            await io.send(f"become {ctx.target.login}\n")

        register_login_proxy("task7-record-via", _record_via, overwrite=True)
        hops = [
            Cred(login="admin", password="adminpw", proxy="task7-record-via", via="root"),
            Cred(login="mysql", password="mysqlpw", proxy="task7-record-via", via="admin"),
        ]
        conn = _proxy_connections(hops, login_target="mysql", credentials=("root", "rootpw"))
        # SessionManager._creds is the list _apply_login_proxy resolves `via`
        # against (the full cred, incl. password) — the same list HostSession
        # elevation uses. The via accounts must carry passwords here so a bare
        # Cred(login=...) regression is observable.
        mgr = SessionManager(
            connections=conn,
            name="h",
            session_factory=_ImmediateSession,
            host_id="h",
            creds=[
                Cred(login="root", password="rootpw"),
                *hops,
            ],
        )
        await mgr._ensure_session()

        assert captured == [
            ("admin", "root", "rootpw"),  # hop #1: via = full root cred (password intact)
            ("mysql", "admin", "adminpw"),  # hop #2: via = full admin cred (password intact)
        ]
        assert mgr._session.current_user == "mysql"

    @pytest.mark.asyncio
    async def test_ensure_session_failed_hop_raises_and_tears_down(self):
        """A hop whose proxy raises tears the session down; the failure is not retried."""

        async def _boom(io, ctx) -> None:
            raise RuntimeError("boom")

        register_login_proxy("task7-boom", _boom, overwrite=True)
        hop = Cred(login="mysql", proxy="task7-boom", via="admin")
        conn = _proxy_connections([hop])
        built: list[_ImmediateSession] = []

        def factory() -> _ImmediateSession:
            s = _ImmediateSession()
            built.append(s)
            return s

        mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")

        with pytest.raises(LoginProxyError):
            await mgr._ensure_session()

        assert mgr._session is None
        assert len(built) == 1  # the one-retry was NOT consumed by a proxy failure
        assert built[0].closed  # torn down, not left dangling

    @pytest.mark.asyncio
    async def test_open_session_applies_proxy_hop(self):
        """Named session (open_session): same hop replay + current_user stamp."""
        hop = Cred(login="mysql", proxy="su", via="admin")
        conn = _proxy_connections([hop])
        built: list[_ImmediateSession] = []

        def factory() -> _ImmediateSession:
            s = _ImmediateSession()
            built.append(s)
            return s

        mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")
        hs = await mgr.open_session("mon")

        assert hs.current_user == "mysql"
        assert "su mysql\n" in built[0].writes

    @pytest.mark.asyncio
    async def test_open_session_failed_hop_leaves_no_named_session(self):
        """A failed hop tears the named session down and leaves no dict entry."""

        async def _boom(io, ctx) -> None:
            raise RuntimeError("boom")

        register_login_proxy("task7-boom-named", _boom, overwrite=True)
        hop = Cred(login="mysql", proxy="task7-boom-named", via="admin")
        conn = _proxy_connections([hop])
        built: list[_ImmediateSession] = []

        def factory() -> _ImmediateSession:
            s = _ImmediateSession()
            built.append(s)
            return s

        mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")

        with pytest.raises(LoginProxyError):
            await mgr.open_session("mon")

        assert "mon" not in mgr._named_sessions
        assert built[0].closed

    @pytest.mark.asyncio
    async def test_apply_login_proxy_tolerates_connections_without_proxy_hops(self):
        """A minimal connections fake with no proxy_hops/login_target attrs is a no-op."""
        mgr = SessionManager(connections=SimpleNamespace(), name="h")
        s = _ImmediateSession()
        s.current_user = "alice"
        await mgr._apply_login_proxy(s)
        assert s.current_user == "alice"  # untouched
        assert s.writes == []


# ---------------------------------------------------------------------------
# Proxied oneshot routing (Task 8)
# ---------------------------------------------------------------------------

from otto.result import CommandResult


class _StubOneshotSession(ShellSession):
    """Immediate-handshake session with a canned, call-recording ``run_cmd``.

    Mirrors ``_AliveStubSession`` in test_session_logging.py — these tests
    only care about *which path* ``oneshot()`` takes (raw exec factory vs.
    pooled named session), not real transport/frame parsing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_cmd_calls: list[str] = []
        self.writes: list[str] = []

    async def _open(self) -> None: ...

    async def _write(self, data: str) -> None:
        self.writes.append(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        # Resync-aware (see _ImmediateSession): the proxied-oneshot tests
        # below use a passwordless built-in `su` hop, so the only expect()
        # call this stub ever sees is the engine's post-transition resync.
        if self.writes and self.writes[-1].startswith("echo __OTTO_LP_SYNC_"):
            marker = self.writes[-1].removeprefix("echo ").rstrip("\n")
            return f"\n{marker}\n"
        raise AssertionError("stub does not read")

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True

    async def run_cmd(
        self,
        cmd: str,
        expects=None,
        timeout: float | None = None,
        on_output=None,
        redact: bool = False,
        write_progress=None,
    ) -> CommandResult:
        self.run_cmd_calls.append(cmd)
        return CommandResult(status=Status.Success, value="OUT", command=cmd, retcode=0)


class TestOneshotProxyRouting:
    """Task 8: ``oneshot()`` must route through the proxied pool — not a raw
    SSH exec channel — whenever the login is proxied (non-empty ``proxy_hops``).

    Both raw-exec fast paths (the ``_oneshot_factory`` callable, and the
    inline ``ssh_conn.create_process`` in the ``case "ssh":`` branch)
    authenticate as the resolved DIRECT cred and cannot replay proxy hops —
    so a proxied oneshot on either fast path would silently run as the
    via-user instead of the target. Only the pooled named-session path
    (``_acquire_oneshot_session`` -> ``open_session``) replays hops, via
    ``_apply_login_proxy`` (Task 7).
    """

    @pytest.mark.asyncio
    async def test_no_hops_uses_factory_fast_path(self):
        """Existing fast path preserved: no proxy_hops -> factory IS called."""
        factory_calls: list[str] = []

        async def fake_factory(cmd: str, timeout: float | None) -> CommandResult:
            factory_calls.append(cmd)
            return CommandResult(status=Status.Success, value="factory", command=cmd, retcode=0)

        conn = _proxy_connections([], login_target="alice", credentials=("alice", "alicepw"))
        mgr = SessionManager(
            connections=conn,
            session_factory=_StubOneshotSession,
            oneshot_factory=fake_factory,
            host_id="h",
        )

        result = await mgr.oneshot("id")

        assert factory_calls == ["id"]
        assert result.value == "factory"
        assert mgr._oneshot_pool == []  # pool never touched

    @pytest.mark.asyncio
    async def test_hops_present_skips_factory_uses_pool(self):
        """A proxied login (non-empty proxy_hops) bypasses the factory entirely."""
        factory_calls: list[str] = []

        async def fake_factory(cmd: str, timeout: float | None) -> CommandResult:
            factory_calls.append(cmd)
            return CommandResult(status=Status.Success, value="factory", command=cmd, retcode=0)

        hop = Cred(login="mysql", proxy="su", via="admin")
        conn = _proxy_connections([hop], login_target="mysql", credentials=("admin", "adminpw"))
        mgr = SessionManager(
            connections=conn,
            session_factory=_StubOneshotSession,
            oneshot_factory=fake_factory,
            host_id="h",
        )

        result = await mgr.oneshot("id")

        assert factory_calls == []  # the raw exec factory must NOT run
        assert result.value == "OUT"
        # the pooled session actually ran the command and ended up stamped
        # as the proxied target, not the direct-auth via-user
        assert len(mgr._oneshot_pool) == 1
        pooled = mgr._oneshot_pool[0]
        assert pooled.current_user == "mysql"
        assert pooled._session.run_cmd_calls == ["id"]

    @pytest.mark.asyncio
    async def test_ssh_term_with_hops_skips_inline_create_process(self):
        """Even with ``term='ssh'`` configured, hops route through the pool —
        never the inline ``ssh_conn.create_process`` exec channel (which
        can't replay proxy hops).
        """
        conn = SimpleNamespace(
            credentials=("admin", "adminpw"),
            login_target="mysql",
            proxy_hops=[Cred(login="mysql", proxy="su", via="admin")],
            term="ssh",
            ssh=AsyncMock(),
        )
        mgr = SessionManager(
            connections=conn,
            session_factory=_StubOneshotSession,
            host_id="h",
        )

        result = await mgr.oneshot("id")

        conn.ssh.assert_not_awaited()  # the raw exec channel must NOT be opened
        assert result.value == "OUT"
        assert mgr._oneshot_pool[0].current_user == "mysql"
