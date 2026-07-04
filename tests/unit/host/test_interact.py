"""Unit tests for the interactive session bridge module.

These tests exercise the pure helpers (``_strip_ansi``, ``_LineBuffer``)
directly, and the two pump coroutines plus the shared ``_run_bridge``
via protocol-agnostic fake write/read callables. The asyncssh and
telnetlib3 back-ends are not touched — the bridge is protocol-free by
design, so there is no need to fake either library here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import interact
from otto.host.interact import (
    _ESCAPE_BYTE,
    _BridgeProxyIO,
    _LineBuffer,
    _pump_remote_to_stdout,
    _pump_stdin_to_remote,
    _replay_proxy_hops,
    _run_bridge,
    _SessionLogFile,
    _strip_ansi,
)
from otto.host.login_proxy import (
    LOGIN_PROXIES,
    Cred,
    LoginProxyError,
    _resync_shell,
    register_login_proxy,
    run_proxy,
)
from otto.logger.mode import LogMode

# ---------------------------------------------------------------------------
# _strip_ansi
# ---------------------------------------------------------------------------


class TestStripAnsi:
    def test_csi_sequences_removed(self):
        assert _strip_ansi(b"\x1b[31mred\x1b[0m") == b"red"

    def test_osc_sequence_bel_terminated(self):
        assert _strip_ansi(b"before\x1b]0;title\x07after") == b"beforeafter"

    def test_osc_sequence_st_terminated(self):
        assert _strip_ansi(b"a\x1b]0;title\x1b\\b") == b"ab"

    def test_plain_text_unchanged(self):
        assert _strip_ansi(b"plain text\n") == b"plain text\n"

    def test_two_byte_escape_removed(self):
        # ESC followed by a char in [@-_] is stripped. The trailing char must
        # also be in [@-_] (or be EOF) for the tail to stop matching — here
        # `B` (0x42) qualifies, so only ESC+M is consumed.
        assert _strip_ansi(b"a\x1bMBC") == b"aBC"


# ---------------------------------------------------------------------------
# _LineBuffer
# ---------------------------------------------------------------------------


class TestLineBuffer:
    def test_emits_on_newline(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b"hello\nworld")
        assert out == ["hello"]

    def test_flush_emits_residual(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b"partial")
        buf.flush()
        assert out == ["partial"]

    def test_multiple_lines_in_one_feed(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b"a\nb\nc\n")
        assert out == ["a", "b", "c"]

    def test_strips_ansi_and_carriage_return(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b"\x1b[1mbold\x1b[0m\r\n")
        assert out == ["bold"]

    def test_empty_lines_are_skipped(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b"\n\n")
        assert out == []

    def test_flush_is_idempotent_when_empty(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.flush()
        buf.flush()
        assert out == []


# ---------------------------------------------------------------------------
# _SessionLogFile
# ---------------------------------------------------------------------------


class TestSessionLogFile:
    def test_write_line_emits_preamble_to_file(self, tmp_path: Path):
        log = _SessionLogFile(tmp_path / "session.log", host_name="router1")
        log.write_line("hello world")
        log.close()
        content = (tmp_path / "session.log").read_text()
        assert "hello world" in content
        assert "@router1 > |" in content

    def test_write_marker_uses_bookend_preamble(self, tmp_path: Path):
        log = _SessionLogFile(tmp_path / "session.log", host_name="router1")
        log.write_marker("Entering interactive session")
        log.close()
        content = (tmp_path / "session.log").read_text()
        assert "Entering interactive session" in content
        assert "@router1   |" in content

    def test_unopenable_path_degrades_silently(self, tmp_path: Path):
        log = _SessionLogFile(tmp_path / "nonexistent-dir" / "session.log", host_name="h")
        log.write_line("should not raise")
        log.write_marker("should not raise")
        log.close()


# ---------------------------------------------------------------------------
# _pump_stdin_to_remote — escape-byte handling
# ---------------------------------------------------------------------------


class TestStdinPump:
    @pytest.mark.asyncio
    async def test_forwards_plain_chunks(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b"hello")
        await queue.put(b" world")
        await queue.put(None)  # EOF

        received: list[bytes] = []

        async def write(data: bytes) -> None:
            received.append(data)

        await _pump_stdin_to_remote(queue, write)
        assert received == [b"hello", b" world"]

    @pytest.mark.asyncio
    async def test_escape_byte_stops_pump(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b"abc" + bytes([_ESCAPE_BYTE]) + b"def")

        received: list[bytes] = []

        async def write(data: bytes) -> None:
            received.append(data)

        await _pump_stdin_to_remote(queue, write)
        # Pre-escape bytes are forwarded; escape + post-escape are dropped.
        assert received == [b"abc"]

    @pytest.mark.asyncio
    async def test_escape_byte_at_start_forwards_nothing(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(bytes([_ESCAPE_BYTE]) + b"ignored")

        received: list[bytes] = []

        async def write(data: bytes) -> None:
            received.append(data)

        await _pump_stdin_to_remote(queue, write)
        assert received == []


# ---------------------------------------------------------------------------
# _pump_remote_to_stdout — terminal fidelity + line logging
# ---------------------------------------------------------------------------


class TestRemotePump:
    @pytest.mark.asyncio
    async def test_writes_raw_bytes_to_fd1_and_buffers_lines(self):
        # Scripted remote output across two reads, then EOF.
        chunks = [b"hello\n", b"world\n", b""]

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, "write") as mock_write:
            await _pump_remote_to_stdout(read, buf)

        # Raw bytes reached fd 1 unchanged.
        written = b"".join(call.args[1] for call in mock_write.call_args_list)
        assert written == b"hello\nworld\n"
        # Lines captured.
        assert logged == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_flushes_residual_on_eof(self):
        chunks = [b"trailing no newline", b""]

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, "write"):
            await _pump_remote_to_stdout(read, buf)

        assert logged == ["trailing no newline"]

    @pytest.mark.asyncio
    async def test_broken_pipe_exits_cleanly(self):
        chunks = [b"hello\n"]  # no follow-up read; should exit on BrokenPipeError first

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, "write", side_effect=BrokenPipeError()):
            await _pump_remote_to_stdout(read, buf)

        # Residual should still be flushed even on broken pipe.
        assert logged == ["hello"]


# ---------------------------------------------------------------------------
# _run_bridge — integration of the pumps with a fake protocol
# ---------------------------------------------------------------------------


class TestRunBridge:
    @pytest.mark.asyncio
    async def test_remote_eof_ends_session_and_logs(self):
        # Fake remote side: one line then EOF.
        remote_chunks = [b"welcome\n", b""]

        async def read_remote() -> bytes:
            return remote_chunks.pop(0)

        sent: list[bytes] = []

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        logged: list[str] = []

        def on_line(line: str) -> None:
            logged.append(line)

        installed: list[bool] = []

        def install_sigwinch():
            installed.append(True)
            return lambda: installed.append(False)

        # Prevent the real stdin reader thread from touching fd 0 in CI.
        with (
            patch.object(interact, "_spawn_stdin_reader") as mock_reader,
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            # Fake reader future that never produces stdin input.
            mock_reader.return_value = asyncio.get_running_loop().create_future()
            mock_reader.return_value.set_result(None)

            await _run_bridge(
                write_remote=write_remote,
                read_remote=read_remote,
                install_sigwinch=install_sigwinch,
                on_output_line=on_line,
            )

        assert logged == ["welcome"]


# ---------------------------------------------------------------------------
# UnixHost._interact dispatch — verifies CLI path reaches the right runner
# ---------------------------------------------------------------------------


class TestUnixHostInteractDispatch:
    @pytest.mark.asyncio
    async def test_ssh_dispatch_uses_cached_connection(self):
        from otto.host.unix_host import UnixHost

        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[Cred(login="u", password="p")],
            term="ssh",
            log=LogMode.QUIET,
        )

        fake_conn = object()
        host._connections.ssh = AsyncMock(return_value=fake_conn)  # type: ignore[method-assign]

        with patch("otto.host.unix_host.run_ssh_login", new=AsyncMock()) as mock_ssh_login:
            await host._interact()

        mock_ssh_login.assert_awaited_once()
        call_kwargs = mock_ssh_login.await_args.kwargs
        assert call_kwargs["conn"] is fake_conn
        assert call_kwargs["host_name"] == host.name
        await host.close()

    @pytest.mark.asyncio
    async def test_telnet_dispatch_builds_dedicated_interactive_client(self):
        from otto.host.unix_host import UnixHost

        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )

        # Avoid the real TelnetClient; patch it at the import site.
        fake_client = AsyncMock()
        fake_client.connect = AsyncMock()
        fake_client.close = AsyncMock()
        with (
            patch("otto.host.unix_host.TelnetClient", return_value=fake_client) as mock_cls,
            patch("otto.host.unix_host.run_telnet_login", new=AsyncMock()) as mock_login,
        ):
            await host._interact()

        # A fresh client was constructed with auto_window_resize=True.
        construct_kwargs = mock_cls.call_args.kwargs
        assert construct_kwargs["options"].auto_window_resize is True
        assert construct_kwargs["user"] == "u"
        assert construct_kwargs["password"] == "p"

        fake_client.connect.assert_awaited_once_with(interactive=True)
        mock_login.assert_awaited_once()
        fake_client.close.assert_awaited_once()
        await host.close()


# ---------------------------------------------------------------------------
# Task 9: UnixHost._interact(as_user=...) — resolve_chain + direct-cred guard
# ---------------------------------------------------------------------------


class TestUnixHostInteractAsUser:
    @pytest.mark.asyncio
    async def test_ssh_as_user_passes_resolved_hops_and_via_login(self):
        from otto.host.unix_host import UnixHost

        # login_target defaults to the first cred ("admin"); --as-user mysql
        # resolves through admin via a single su hop.
        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[
                Cred(login="admin", password="hunter2"),
                Cred(login="mysql", password="sqlpw", proxy="su", via="admin"),
            ],
            term="ssh",
            log=LogMode.QUIET,
        )
        fake_conn = object()
        host._connections.ssh = AsyncMock(return_value=fake_conn)  # type: ignore[method-assign]

        with patch("otto.host.unix_host.run_ssh_login", new=AsyncMock()) as mock_ssh_login:
            await host._interact(as_user="mysql")

        mock_ssh_login.assert_awaited_once()
        kwargs = mock_ssh_login.await_args.kwargs
        assert kwargs["conn"] is fake_conn
        assert kwargs["proxy_hops"] == [
            Cred(login="mysql", password="sqlpw", proxy="su", via="admin")
        ]
        assert kwargs["via_login"] == "admin"
        assert kwargs["host_id"] == host.name
        await host.close()

    @pytest.mark.asyncio
    async def test_ssh_as_user_mismatched_direct_login_raises(self):
        """--as-user resolving to a DIFFERENT direct login than the one the
        cached connection already authenticated as is out of scope — the
        connection can't be re-authenticated mid-session, so this must raise
        a clear LoginProxyError rather than silently proxying as the wrong
        account."""
        from otto.host.unix_host import UnixHost

        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[
                Cred(login="other", password="op"),
                Cred(login="admin", password="ap"),
            ],
            user="other",  # login_target -> "other"; connection authenticates as "other"
            term="ssh",
            log=LogMode.QUIET,
        )
        host._connections.ssh = AsyncMock(return_value=object())  # type: ignore[method-assign]

        with (
            patch("otto.host.unix_host.run_ssh_login", new=AsyncMock()) as mock_ssh_login,
            pytest.raises(LoginProxyError, match=r"other.*admin"),
        ):
            await host._interact(as_user="admin")

        mock_ssh_login.assert_not_awaited()
        await host.close()

    @pytest.mark.asyncio
    async def test_telnet_as_user_passes_resolved_hops_and_via_login(self):
        from otto.host.unix_host import UnixHost

        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[
                Cred(login="admin", password="hunter2"),
                Cred(login="mysql", password="sqlpw", proxy="su", via="admin"),
            ],
            term="telnet",
            log=LogMode.QUIET,
        )
        fake_client = AsyncMock()
        fake_client.connect = AsyncMock()
        fake_client.close = AsyncMock()
        with (
            patch("otto.host.unix_host.TelnetClient", return_value=fake_client),
            patch("otto.host.unix_host.run_telnet_login", new=AsyncMock()) as mock_login,
        ):
            await host._interact(as_user="mysql")

        mock_login.assert_awaited_once()
        kwargs = mock_login.await_args.kwargs
        assert kwargs["proxy_hops"] == [
            Cred(login="mysql", password="sqlpw", proxy="su", via="admin")
        ]
        assert kwargs["via_login"] == "admin"
        assert kwargs["host_id"] == host.name
        fake_client.close.assert_awaited_once()
        await host.close()

    @pytest.mark.asyncio
    async def test_telnet_as_user_mismatched_direct_login_raises_before_connecting(self):
        from otto.host.unix_host import UnixHost

        host = UnixHost(
            ip="10.0.0.1",
            element="router",
            creds=[
                Cred(login="other", password="op"),
                Cred(login="admin", password="ap"),
            ],
            user="other",
            term="telnet",
            log=LogMode.QUIET,
        )

        with (
            patch("otto.host.unix_host.TelnetClient") as mock_cls,
            patch("otto.host.unix_host.run_telnet_login", new=AsyncMock()) as mock_login,
            pytest.raises(LoginProxyError, match=r"other.*admin"),
        ):
            await host._interact(as_user="admin")

        # Failed before ever building a dedicated telnet client/connection.
        mock_cls.assert_not_called()
        mock_login.assert_not_awaited()
        await host.close()


# ---------------------------------------------------------------------------
# run_ssh_login — command= branch (container docker exec login)
# ---------------------------------------------------------------------------


def _fake_process() -> MagicMock:
    """Return a minimal fake SSHClientProcess sufficient to drive run_ssh_login."""
    proc = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.change_terminal_size = MagicMock()
    proc.close = MagicMock()
    return proc


def _make_fake_asyncssh() -> MagicMock:
    """Return a MagicMock that satisfies asyncssh attribute lookups in run_ssh_login."""
    fake = MagicMock()
    # asyncssh.STDOUT is used in process_kwargs; any value is fine for the mock.
    fake.STDOUT = MagicMock()
    # asyncssh.misc.ConnectionLost is referenced in the read_remote closure;
    # must be an exception class so it can appear in an except clause.
    fake.misc.ConnectionLost = type("ConnectionLost", (Exception,), {})
    return fake


class TestRunSshLoginCommandBranch:
    """Cover the ``command=`` branch of ``run_ssh_login`` (lines ~433-435)."""

    @pytest.mark.asyncio
    async def test_run_ssh_login_passes_command_to_create_process(self):
        """When command= is given, create_process receives it as a kwarg."""
        proc = _fake_process()
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=proc)

        fake_asyncssh = _make_fake_asyncssh()

        with (
            patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
            patch.object(interact, "_run_bridge", new=AsyncMock()) as bridge,
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            await interact.run_ssh_login(
                conn=conn,
                host_name="h",
                command="docker exec -it abc /bin/sh",
            )

        conn.create_process.assert_awaited_once()
        kwargs = conn.create_process.await_args.kwargs
        assert kwargs.get("command") == "docker exec -it abc /bin/sh"
        bridge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_ssh_login_no_command_omits_command_kwarg(self):
        """When command= is absent, create_process does NOT receive a command kwarg.

        This companion test proves the assertion above is branch-specific —
        the ``command`` key only enters ``process_kwargs`` on the taken branch.
        """
        proc = _fake_process()
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=proc)

        fake_asyncssh = _make_fake_asyncssh()

        with (
            patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
            patch.object(interact, "_run_bridge", new=AsyncMock()) as bridge,
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            await interact.run_ssh_login(conn=conn, host_name="h")

        conn.create_process.assert_awaited_once()
        kwargs = conn.create_process.await_args.kwargs
        assert "command" not in kwargs
        bridge.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 9: _BridgeProxyIO — send() newline translation, expect() buffering
# ---------------------------------------------------------------------------


class TestBridgeProxyIOSend:
    @pytest.mark.asyncio
    async def test_ssh_newline_keeps_lf(self):
        sent: list[bytes] = []

        async def write(data: bytes) -> None:
            sent.append(data)

        async def read() -> bytes:
            return b""

        io = _BridgeProxyIO(write, read, newline=b"\n")
        await io.send("su mysql\n")
        assert sent == [b"su mysql\n"]

    @pytest.mark.asyncio
    async def test_telnet_newline_translates_lf_to_cr(self):
        sent: list[bytes] = []

        async def write(data: bytes) -> None:
            sent.append(data)

        async def read() -> bytes:
            return b""

        io = _BridgeProxyIO(write, read, newline=b"\r")
        await io.send("su mysql\n")
        assert sent == [b"su mysql\r"]

    @pytest.mark.asyncio
    async def test_no_trailing_newline_is_unchanged(self):
        sent: list[bytes] = []

        async def write(data: bytes) -> None:
            sent.append(data)

        async def read() -> bytes:
            return b""

        io = _BridgeProxyIO(write, read, newline=b"\r")
        await io.send("no newline here")
        assert sent == [b"no newline here"]

    @pytest.mark.asyncio
    async def test_send_never_logs_regardless_of_log_arg(self):
        # `log` exists only to satisfy the ProxyIO protocol — this adapter has
        # no sink to leak a password to either way. Passing NEVER (as a
        # password hop does) must not raise or change behavior.
        sent: list[bytes] = []

        async def write(data: bytes) -> None:
            sent.append(data)

        async def read() -> bytes:
            return b""

        io = _BridgeProxyIO(write, read, newline=b"\n")
        await io.send("hunter2\n", log=LogMode.NEVER)
        assert sent == [b"hunter2\n"]


class TestBridgeProxyIOExpect:
    @pytest.mark.asyncio
    async def test_accumulates_across_reads_until_match(self):
        chunks = [b"foo", b"bar", b"Password:"]

        async def read() -> bytes:
            return chunks.pop(0)

        async def write(data: bytes) -> None:
            pass

        io = _BridgeProxyIO(write, read, newline=b"\n")
        out = await io.expect(r"[Pp]assword:")
        assert out == "foobarPassword:"

    @pytest.mark.asyncio
    async def test_str_pattern_is_compiled(self):
        async def read() -> bytes:
            return b"login: "

        async def write(data: bytes) -> None:
            pass

        io = _BridgeProxyIO(write, read, newline=b"\n")
        out = await io.expect("login:")
        assert out == "login:"

    @pytest.mark.asyncio
    async def test_times_out_when_pattern_never_arrives(self):
        async def read() -> bytes:
            # Blocks well past the short timeout below; asyncio.wait_for
            # cancels it rather than the test actually waiting this long.
            await asyncio.sleep(10)
            return b""  # pragma: no cover — unreachable, cancelled first

        async def write(data: bytes) -> None:
            pass

        io = _BridgeProxyIO(write, read, newline=b"\n")
        with pytest.raises(asyncio.TimeoutError):
            await io.expect(r"[Pp]assword:", timeout=0.05)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_remote_eof(self):
        async def read() -> bytes:
            return b""

        async def write(data: bytes) -> None:
            pass

        io = _BridgeProxyIO(write, read, newline=b"\n")
        with pytest.raises(ConnectionError):
            await io.expect(r"[Pp]assword:", timeout=1.0)

    @pytest.mark.asyncio
    async def test_consumes_matched_bytes_so_next_expect_reads_fresh(self):
        """A second expect() on the SAME instance for the same pattern must NOT
        re-match the first call's already-consumed bytes — it has to read fresh
        input. Otherwise a 2+-hop replay would fire hop 2's password blind
        against hop 1's leftover prompt. Proven by counting read_remote calls:
        the second expect must trigger at least one additional read."""
        read_calls = 0
        # Read 1 satisfies expect #1; read 2 (only reached if the buffer was
        # correctly trimmed) satisfies expect #2. A leftover-buffer bug would
        # let expect #2 return without ever reaching read 2.
        chunks = [b"first Password:", b"second Password:"]

        async def read() -> bytes:
            nonlocal read_calls
            read_calls += 1
            if not chunks:
                # If expect #2 (incorrectly) consumed nothing and looped for a
                # 3rd read, surface it as EOF rather than hanging.
                return b""
            return chunks.pop(0)

        async def write(data: bytes) -> None:
            pass

        io = _BridgeProxyIO(write, read, newline=b"\n")

        first = await io.expect(r"[Pp]assword:")
        assert first == "first Password:"
        assert read_calls == 1

        second = await io.expect(r"[Pp]assword:")
        # The second match came from the SECOND read, not the stale buffer.
        assert second == "second Password:"
        assert read_calls == 2


# ---------------------------------------------------------------------------
# Task 9: replaying the built-in `su` hop over _BridgeProxyIO via run_proxy
#
# ``run_proxy``/``run_undo`` now end every hop with a post-transition
# "echo <marker>"/expect(marker) resync (see
# ``otto.host.login_proxy._resync_shell``) — a real, bed-confirmed fix for a
# su/sudo/exit tty-flush race. ``_BridgeProxyIO.expect()`` does REAL regex
# matching against accumulated ``read_remote()`` chunks (unlike the simpler
# record-and-replay fakes elsewhere), so a fake `read_remote` that doesn't
# recognize the resync's echo probe and answer it would time out / hit EOF —
# breaking every test below. ``_is_resync_probe``/``_resync_reply`` make the
# fakes resync-aware; ``_drop_resync_probes`` keeps sent-sequence assertions
# meaningful by filtering the probe's own noise back out.
# ---------------------------------------------------------------------------

_RESYNC_ECHO_PREFIX = b"echo __OTTO_LP_SYNC_"


def _is_resync_probe(write: bytes) -> bool:
    """Whether *write* is the login-proxy engine's post-transition resync probe."""
    return write.startswith(_RESYNC_ECHO_PREFIX)


def _resync_reply(write: bytes) -> bytes:
    """Build a read_remote() reply that satisfies expect() for the marker in *write*."""
    marker = write.decode("utf-8").removeprefix("echo ").rstrip("\r\n")
    return f"\n{marker}\n".encode()


def _drop_resync_probes(writes: list[bytes]) -> list[bytes]:
    """Filter the resync's own echo probes out of a `sent`/write log."""
    return [w for w in writes if not _is_resync_probe(w)]


class TestReplaySuHopOverBridge:
    @pytest.mark.asyncio
    async def test_sends_su_command_then_password_on_prompt(self):
        sent: list[bytes] = []
        chunks = [b"Password:"]

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            if sent and _is_resync_probe(sent[-1]):
                return _resync_reply(sent[-1])
            return chunks.pop(0) if chunks else b""

        io = _BridgeProxyIO(write_remote, read_remote, newline=b"\n")
        hop = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")
        await run_proxy(io, hop, via=Cred(login="admin"), host_id="h1")

        assert _drop_resync_probes(sent) == [b"su mysql\n", b"sqlpw\n"]

    @pytest.mark.asyncio
    async def test_timeout_surfaces_as_login_proxy_error_with_context(self):
        """A hop whose prompt never arrives times out inside `expect`; `run_proxy`
        wraps that into a `LoginProxyError` naming the host, login, and proxy."""

        async def slow_proxy(io: object, ctx: object) -> None:
            await io.expect("this-never-arrives", timeout=0.05)  # type: ignore[attr-defined]

        register_login_proxy("slow-test-proxy", slow_proxy)
        try:

            async def write_remote(data: bytes) -> None:
                pass

            async def read_remote() -> bytes:
                await asyncio.sleep(10)
                return b""  # pragma: no cover — unreachable, cancelled first

            io = _BridgeProxyIO(write_remote, read_remote, newline=b"\n")
            hop = Cred(login="mysql", proxy="slow-test-proxy")
            with pytest.raises(LoginProxyError, match=r"h1.*mysql.*slow-test-proxy"):
                await run_proxy(io, hop, via=Cred(login="admin"), host_id="h1")
        finally:
            LOGIN_PROXIES.unregister("slow-test-proxy")


# ---------------------------------------------------------------------------
# Task 9: _replay_proxy_hops — via-chain sourcing across multiple hops
# ---------------------------------------------------------------------------


class TestReplayProxyHops:
    @pytest.mark.asyncio
    async def test_noop_when_no_hops(self):
        sent: list[bytes] = []

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            return b""

        await _replay_proxy_hops(
            write_remote=write_remote,
            read_remote=read_remote,
            newline=b"\n",
            proxy_hops=[],
            via_login="root",
            host_id="h1",
        )
        assert sent == []

    @pytest.mark.asyncio
    async def test_multi_hop_sends_each_hop_in_order(self):
        sent: list[bytes] = []
        replies = [b"Password:", b"Password:"]

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            if sent and _is_resync_probe(sent[-1]):
                return _resync_reply(sent[-1])
            return replies.pop(0) if replies else b""

        hops = [
            Cred(login="mysql", password="pw1", proxy="su", via="admin"),
            Cred(login="app", password="pw2", proxy="su", via="mysql"),
        ]
        await _replay_proxy_hops(
            write_remote=write_remote,
            read_remote=read_remote,
            newline=b"\n",
            proxy_hops=hops,
            via_login="admin",
            host_id="h1",
        )
        assert _drop_resync_probes(sent) == [b"su mysql\n", b"pw1\n", b"su app\n", b"pw2\n"]

    @pytest.mark.asyncio
    async def test_each_hop_waits_for_its_own_fresh_prompt(self):
        """Regression: the shared _BridgeProxyIO must consume matched bytes so
        hop 2 waits for a NEWLY read prompt instead of re-matching hop 1's
        leftover "Password:". Proven by counting read_remote calls — a 2-hop
        chain must read the prompt twice (once per hop) for its password, plus
        once more per hop for the engine's post-transition resync (Task: resync
        fix) — 4 reads total. A leftover buffer would let a later expect()
        re-match stale bytes instead of reading fresh, undershooting this
        count."""
        read_calls = 0
        sent: list[bytes] = []
        # One prompt per hop; each only becomes visible on its own read.
        prompts = [b"Password:", b"Password:"]

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            nonlocal read_calls
            read_calls += 1
            if sent and _is_resync_probe(sent[-1]):
                return _resync_reply(sent[-1])
            return prompts.pop(0) if prompts else b""

        hops = [
            Cred(login="mysql", password="pw1", proxy="su", via="admin"),
            Cred(login="app", password="pw2", proxy="su", via="mysql"),
        ]
        await _replay_proxy_hops(
            write_remote=write_remote,
            read_remote=read_remote,
            newline=b"\n",
            proxy_hops=hops,
            via_login="admin",
            host_id="h1",
        )
        # 2 hops x (1 password prompt + 1 resync marker) = 4 reads. A leftover
        # buffer would let a later expect() re-match stale bytes, undershooting.
        assert read_calls == 4

    @pytest.mark.asyncio
    async def test_via_is_sourced_from_the_previous_hops_login(self):
        """Mirrors SessionManager._apply_login_proxy: hop N's `via` is hop
        N-1's login, not the original transport login repeated for every hop."""
        seen_vias: list[str] = []
        sent: list[bytes] = []

        async def track_via(io: object, ctx: object) -> None:
            seen_vias.append(ctx.via.login)  # type: ignore[attr-defined]

        register_login_proxy("track-via-test", track_via)
        try:

            async def write_remote(data: bytes) -> None:
                sent.append(data)

            async def read_remote() -> bytes:
                if sent and _is_resync_probe(sent[-1]):
                    return _resync_reply(sent[-1])
                return b""

            hops = [
                Cred(login="mysql", proxy="track-via-test"),
                Cred(login="app", proxy="track-via-test"),
            ]
            await _replay_proxy_hops(
                write_remote=write_remote,
                read_remote=read_remote,
                newline=b"\n",
                proxy_hops=hops,
                via_login="admin",
                host_id="h1",
            )
        finally:
            LOGIN_PROXIES.unregister("track-via-test")
        assert seen_vias == ["admin", "mysql"]


# ---------------------------------------------------------------------------
# The engine resync (_resync_shell) must be sound in BOTH pty echo modes.
# _BridgeProxyIO does a REAL unanchored regex.search, so these two tests
# exercise the actual matching _resync_shell relies on (the negative
# lookbehind for its own "echo " probe prefix):
#
# - echo-ON (the interact bridge leaves the pty echoing): the resync's own
#   `echo <marker>` probe is echoed back on the read stream. A BARE marker
#   would match inside that echoed probe (before the shell ran anything) and
#   vacuously "succeed"; the lookbehind must reject it and wait for the real
#   output line.
# - echo-OFF (framed switch_user/as_user/establishment run `stty -echo`, which
#   persists across su/sudo): the probe is NOT echoed, so the shell's marker
#   output glues onto the prior prompt with no leading newline. A pure
#   line-anchor would (wrongly) reject THAT — verified on the live bed to hang
#   the framed path — so the lookbehind (not an anchor) is what's used.
# ---------------------------------------------------------------------------


class TestResyncSoundInBothEchoModes:
    @pytest.mark.asyncio
    async def test_echo_on_ignores_its_own_echoed_probe_and_waits_for_real_line(self):
        """Echo-ON bridge: the resync must skip the marker inside its OWN echoed
        `echo <marker>` command (marker preceded by "echo ") and only accept the
        shell's real output line. Proven by feeding the echoed command back
        FIRST, then the real line, and asserting the resync had to read PAST its
        own echo (2 reads). With a bare/unanchored marker this would match on
        read 1 and stop — reads==1."""
        sent: list[bytes] = []
        reads = 0

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            nonlocal reads
            reads += 1
            probe = sent[-1].decode("utf-8")  # "echo <marker>\n"
            marker = probe.removeprefix("echo ").rstrip("\r\n")
            if reads == 1:
                # Only the echoed command comes back — marker follows "echo ".
                # The lookbehind must reject it.
                return probe.encode()
            # The shell's real output line: marker at line start.
            return f"{marker}\n".encode()

        io = _BridgeProxyIO(write_remote, read_remote, newline=b"\n")
        await _resync_shell(io, host_id="h1", hop_login="mysql")

        # Had to read past its own echo (read 1) to the real output (read 2).
        assert reads == 2

    @pytest.mark.asyncio
    async def test_echo_off_matches_marker_glued_to_prompt(self):
        """Echo-OFF framed path: with `stty -echo` the probe is NOT echoed, so
        the shell's marker output glues directly after the prior prompt with no
        leading newline (e.g. `test@host:~$ <marker>`). The resync must still
        match it — a pure line-start anchor would reject this (marker preceded by
        `$ `, not `^`/`\\r`/`\\n`) and the framed switch_user/as_user path would
        hang, which is exactly what was observed on the live bed. The lookbehind
        matches because the marker is not preceded by `echo `."""
        sent: list[bytes] = []

        async def write_remote(data: bytes) -> None:
            sent.append(data)

        async def read_remote() -> bytes:
            probe = sent[-1].decode("utf-8")
            marker = probe.removeprefix("echo ").rstrip("\r\n")
            # Echo-off: no echoed command; marker glued onto a fake prompt.
            return f"test@test1:/home/vagrant$ {marker}\r\n".encode()

        io = _BridgeProxyIO(write_remote, read_remote, newline=b"\n")
        # Must NOT raise (a line-anchor would time out all 5 attempts and raise).
        await _resync_shell(io, host_id="h1", hop_login="mysql")


# ---------------------------------------------------------------------------
# Task 9: run_ssh_login / run_telnet_login wire proxy_hops through before
# the bridge pumps start
# ---------------------------------------------------------------------------


class TestRunLoginProxyHopWiring:
    @pytest.mark.asyncio
    async def test_run_ssh_login_replays_hops_before_bridge_with_lf_newline(self):
        proc = _fake_process()
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=proc)
        fake_asyncssh = _make_fake_asyncssh()
        hop = Cred(login="mysql", proxy="su")

        calls: list[tuple] = []

        async def fake_replay(**kwargs: object) -> None:
            calls.append(
                (
                    "replay",
                    kwargs["newline"],
                    kwargs["proxy_hops"],
                    kwargs["via_login"],
                    kwargs["host_id"],
                )
            )

        async def fake_bridge(**kwargs: object) -> None:
            calls.append(("bridge",))

        with (
            patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
            patch.object(interact, "_replay_proxy_hops", new=AsyncMock(side_effect=fake_replay)),
            patch.object(interact, "_run_bridge", new=AsyncMock(side_effect=fake_bridge)),
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            await interact.run_ssh_login(
                conn=conn,
                host_name="h",
                proxy_hops=[hop],
                via_login="admin",
                host_id="h1",
            )

        assert calls == [("replay", b"\n", [hop], "admin", "h1"), ("bridge",)]

    @pytest.mark.asyncio
    async def test_run_ssh_login_no_hops_still_calls_replay_as_noop(self):
        """No proxy_hops: _replay_proxy_hops is still called (it no-ops on empty)."""
        proc = _fake_process()
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=proc)
        fake_asyncssh = _make_fake_asyncssh()

        with (
            patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
            patch.object(interact, "_run_bridge", new=AsyncMock()) as bridge,
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            await interact.run_ssh_login(conn=conn, host_name="h")

        bridge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_telnet_login_replays_hops_before_bridge_with_cr_newline(self):
        reader = AsyncMock()
        writer = MagicMock()
        client = MagicMock()
        client.reader = reader
        client.writer = writer
        hop = Cred(login="mysql", proxy="su")

        calls: list[tuple] = []

        async def fake_replay(**kwargs: object) -> None:
            calls.append(
                (
                    "replay",
                    kwargs["newline"],
                    kwargs["proxy_hops"],
                    kwargs["via_login"],
                    kwargs["host_id"],
                )
            )

        async def fake_bridge(**kwargs: object) -> None:
            calls.append(("bridge",))

        with (
            patch.object(interact, "_replay_proxy_hops", new=AsyncMock(side_effect=fake_replay)),
            patch.object(interact, "_run_bridge", new=AsyncMock(side_effect=fake_bridge)),
            patch.object(interact, "_setup_raw_mode", return_value=None),
            patch.object(interact, "_restore_terminal"),
            patch.object(interact.sys, "stdin"),
            patch.object(interact.os, "write"),
        ):
            interact.sys.stdin.isatty = lambda: False
            interact.sys.stdin.fileno = lambda: 0
            await interact.run_telnet_login(
                client=client,
                host_name="h",
                proxy_hops=[hop],
                via_login="admin",
                host_id="h1",
            )

        assert calls == [("replay", b"\r", [hop], "admin", "h1"), ("bridge",)]
