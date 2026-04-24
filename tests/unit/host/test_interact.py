"""Unit tests for the interactive session bridge module.

These tests exercise the pure helpers (``_strip_ansi``, ``_LineBuffer``)
directly, and the two pump coroutines plus the shared ``_run_bridge``
via protocol-agnostic fake write/read callables. The asyncssh and
telnetlib3 back-ends are not touched — the bridge is protocol-free by
design, so there is no need to fake either library here.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.host import interact
from otto.host.interact import (
    _ANSI_ESCAPE_RE,
    _ESCAPE_BYTE,
    _LineBuffer,
    _SessionLogFile,
    _pump_remote_to_stdout,
    _pump_stdin_to_remote,
    _run_bridge,
    _strip_ansi,
)


# ---------------------------------------------------------------------------
# _strip_ansi
# ---------------------------------------------------------------------------


class TestStripAnsi:

    def test_csi_sequences_removed(self):
        assert _strip_ansi(b'\x1b[31mred\x1b[0m') == b'red'

    def test_osc_sequence_bel_terminated(self):
        assert _strip_ansi(b'before\x1b]0;title\x07after') == b'beforeafter'

    def test_osc_sequence_st_terminated(self):
        assert _strip_ansi(b'a\x1b]0;title\x1b\\b') == b'ab'

    def test_plain_text_unchanged(self):
        assert _strip_ansi(b'plain text\n') == b'plain text\n'

    def test_two_byte_escape_removed(self):
        # ESC followed by a char in [@-_] is stripped. The trailing char must
        # also be in [@-_] (or be EOF) for the tail to stop matching — here
        # `B` (0x42) qualifies, so only ESC+M is consumed.
        assert _strip_ansi(b'a\x1bMBC') == b'aBC'


# ---------------------------------------------------------------------------
# _LineBuffer
# ---------------------------------------------------------------------------


class TestLineBuffer:

    def test_emits_on_newline(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b'hello\nworld')
        assert out == ['hello']

    def test_flush_emits_residual(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b'partial')
        buf.flush()
        assert out == ['partial']

    def test_multiple_lines_in_one_feed(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b'a\nb\nc\n')
        assert out == ['a', 'b', 'c']

    def test_strips_ansi_and_carriage_return(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b'\x1b[1mbold\x1b[0m\r\n')
        assert out == ['bold']

    def test_empty_lines_are_skipped(self):
        out: list[str] = []
        buf = _LineBuffer(out.append)
        buf.feed(b'\n\n')
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
        log = _SessionLogFile(tmp_path / 'otto.log', host_name='router1')
        log.write_line('hello world')
        log.close()
        content = (tmp_path / 'otto.log').read_text()
        assert 'hello world' in content
        assert '@router1 > |' in content

    def test_write_marker_uses_bookend_preamble(self, tmp_path: Path):
        log = _SessionLogFile(tmp_path / 'otto.log', host_name='router1')
        log.write_marker('Entering interactive session')
        log.close()
        content = (tmp_path / 'otto.log').read_text()
        assert 'Entering interactive session' in content
        assert '@router1   |' in content

    def test_unopenable_path_degrades_silently(self, tmp_path: Path):
        log = _SessionLogFile(tmp_path / 'nonexistent-dir' / 'otto.log', host_name='h')
        log.write_line('should not raise')
        log.write_marker('should not raise')
        log.close()


# ---------------------------------------------------------------------------
# _pump_stdin_to_remote — escape-byte handling
# ---------------------------------------------------------------------------


class TestStdinPump:

    @pytest.mark.asyncio
    async def test_forwards_plain_chunks(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b'hello')
        await queue.put(b' world')
        await queue.put(None)  # EOF

        received: list[bytes] = []

        async def write(data: bytes) -> None:
            received.append(data)

        await _pump_stdin_to_remote(queue, write)
        assert received == [b'hello', b' world']

    @pytest.mark.asyncio
    async def test_escape_byte_stops_pump(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b'abc' + bytes([_ESCAPE_BYTE]) + b'def')

        received: list[bytes] = []

        async def write(data: bytes) -> None:
            received.append(data)

        await _pump_stdin_to_remote(queue, write)
        # Pre-escape bytes are forwarded; escape + post-escape are dropped.
        assert received == [b'abc']

    @pytest.mark.asyncio
    async def test_escape_byte_at_start_forwards_nothing(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(bytes([_ESCAPE_BYTE]) + b'ignored')

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
        chunks = [b'hello\n', b'world\n', b'']

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, 'write') as mock_write:
            await _pump_remote_to_stdout(read, buf)

        # Raw bytes reached fd 1 unchanged.
        written = b''.join(call.args[1] for call in mock_write.call_args_list)
        assert written == b'hello\nworld\n'
        # Lines captured.
        assert logged == ['hello', 'world']

    @pytest.mark.asyncio
    async def test_flushes_residual_on_eof(self):
        chunks = [b'trailing no newline', b'']

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, 'write'):
            await _pump_remote_to_stdout(read, buf)

        assert logged == ['trailing no newline']

    @pytest.mark.asyncio
    async def test_broken_pipe_exits_cleanly(self):
        chunks = [b'hello\n']  # no follow-up read; should exit on BrokenPipeError first

        async def read() -> bytes:
            return chunks.pop(0)

        logged: list[str] = []
        buf = _LineBuffer(logged.append)

        with patch.object(interact.os, 'write', side_effect=BrokenPipeError()):
            await _pump_remote_to_stdout(read, buf)

        # Residual should still be flushed even on broken pipe.
        assert logged == ['hello']


# ---------------------------------------------------------------------------
# _run_bridge — integration of the pumps with a fake protocol
# ---------------------------------------------------------------------------


class TestRunBridge:

    @pytest.mark.asyncio
    async def test_remote_eof_ends_session_and_logs(self):
        # Fake remote side: one line then EOF.
        remote_chunks = [b'welcome\n', b'']

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
        with patch.object(interact, '_spawn_stdin_reader') as mock_reader, \
             patch.object(interact, '_setup_raw_mode', return_value=None), \
             patch.object(interact, '_restore_terminal'), \
             patch.object(interact.sys, 'stdin'), \
             patch.object(interact.os, 'write'):
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

        assert logged == ['welcome']


# ---------------------------------------------------------------------------
# RemoteHost._interact dispatch — verifies CLI path reaches the right runner
# ---------------------------------------------------------------------------


class TestRemoteHostInteractDispatch:

    @pytest.mark.asyncio
    async def test_ssh_dispatch_uses_cached_connection(self):
        from otto.host.remoteHost import RemoteHost

        host = RemoteHost(
            ip='10.0.0.1', ne='router', creds={'u': 'p'},
            term='ssh', log=False,
        )

        fake_conn = object()
        host._connections.ssh = AsyncMock(return_value=fake_conn)  # type: ignore[method-assign]

        with patch('otto.host.remoteHost.run_ssh_login', new=AsyncMock()) as mock_ssh_login:
            await host._interact()

        mock_ssh_login.assert_awaited_once()
        call_kwargs = mock_ssh_login.await_args.kwargs
        assert call_kwargs['conn'] is fake_conn
        assert call_kwargs['host_name'] == host.name
        await host.close()

    @pytest.mark.asyncio
    async def test_telnet_dispatch_builds_dedicated_interactive_client(self):
        from otto.host.remoteHost import RemoteHost

        host = RemoteHost(
            ip='10.0.0.1', ne='router', creds={'u': 'p'},
            term='telnet', log=False,
        )

        # Avoid the real TelnetClient; patch it at the import site.
        fake_client = AsyncMock()
        fake_client.connect = AsyncMock()
        fake_client.close = AsyncMock()
        with patch('otto.host.remoteHost.TelnetClient', return_value=fake_client) as mock_cls, \
             patch('otto.host.remoteHost.run_telnet_login', new=AsyncMock()) as mock_login:
            await host._interact()

        # A fresh client was constructed with auto_window_resize=True.
        construct_kwargs = mock_cls.call_args.kwargs
        assert construct_kwargs['options'].auto_window_resize is True
        assert construct_kwargs['user'] == 'u'
        assert construct_kwargs['password'] == 'p'

        fake_client.connect.assert_awaited_once_with(interactive=True)
        mock_login.assert_awaited_once()
        fake_client.close.assert_awaited_once()
        await host.close()
