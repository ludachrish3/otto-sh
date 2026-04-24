"""
Interactive shell bridging for remote hosts.

``RemoteHost._interact()`` uses this module to run ``otto host <id> login``:
an interactive SSH or telnet shell whose stdin/stdout are bridged to the
user's terminal and whose output is also recorded to ``otto.log`` in the
active output directory.

The bridge is structured around three coroutines:

- a thread-backed stdin reader (``_spawn_stdin_reader``) that pushes chunks
  onto an :class:`asyncio.Queue` so the event loop never blocks on
  ``os.read``;
- ``_pump_stdin_to_remote``, which forwards those chunks verbatim to the
  remote side, intercepting the local escape byte (``Ctrl+]``, the same
  escape character used by the classic ``telnet(1)`` binary);
- ``_pump_remote_to_stdout``, which writes remote bytes directly to
  ``fd 1`` for full terminal fidelity and also feeds them to a line buffer
  that strips ANSI escape sequences and appends clean lines to
  ``otto.log``.

Only the remote-read stream is logged. The user's keystrokes reach the log
transcript naturally via the remote PTY's echo — the same trick
``script(1)`` uses — so there is no need to differentiate stdin from stdout
on a telnet connection that has no directional labelling.

Window-size changes are forwarded to the remote PTY on ``SIGWINCH``. Without
this, remote TUIs keep drawing at their original dimensions when the local
terminal is resized: SSH and telnet both need an out-of-band resize
message because the remote kernel has no link to the local terminal.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..logger import getOttoLogger

logger = getOttoLogger()


# Ctrl+] — the classic telnet(1) escape character. Single byte, no common
# shell/editor binding, leaves Ctrl+D free to act as the normal remote EOT.
_ESCAPE_BYTE = 0x1d

# CSI (``\x1b[...``), OSC (``\x1b]...\x07`` or ``\x1b]...\x1b\\``) and
# two-byte ``\x1b<char>`` escape sequences. Mirrors
# ``otto.logger.formatters._ANSI`` so log output renders the same way
# whether it came from the normal logger path or from this module.
_ANSI_ESCAPE_RE = re.compile(
    rb'\x1b'
    rb'(?:'
    rb'\[[0-9;]*[a-zA-Z]'
    rb'|\][^\x07\x1b]*'
    rb'(?:\x07|\x1b\\)'
    rb'|[@-_][^@-_]*'
    rb')'
)


def _strip_ansi(data: bytes) -> bytes:
    """Remove ANSI escape sequences from ``data``.

    >>> _strip_ansi(b'\\x1b[31mred\\x1b[0m')
    b'red'
    >>> _strip_ansi(b'plain text')
    b'plain text'
    """
    return _ANSI_ESCAPE_RE.sub(b'', data)


class _LineBuffer:
    """Accumulate bytes and emit completed lines through a callback.

    Carriage returns and ANSI escape sequences are stripped before
    emission so log output stays readable for sessions that touch TUIs.

    >>> emitted: list[str] = []
    >>> buf = _LineBuffer(emitted.append)
    >>> buf.feed(b'hello\\nworld')
    >>> emitted
    ['hello']
    >>> buf.flush()
    >>> emitted
    ['hello', 'world']
    """

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._buf = bytearray()
        self._on_line = on_line

    def feed(self, data: bytes) -> None:
        """Append ``data`` to the buffer and emit any completed lines."""
        self._buf.extend(data)
        while True:
            idx = self._buf.find(b'\n')
            if idx < 0:
                return
            line = bytes(self._buf[:idx])
            del self._buf[:idx + 1]
            self._emit(line)

    def flush(self) -> None:
        """Emit any residual bytes as a final (unterminated) line."""
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()

    def _emit(self, line: bytes) -> None:
        cleaned = _strip_ansi(line).decode('utf-8', errors='replace').rstrip('\r')
        if cleaned:
            self._on_line(cleaned)


class _SessionLogFile:
    """Append interactive session lines to ``otto.log`` in the active output dir.

    Bypasses the Python logging machinery deliberately: interactive I/O
    is not an otto-issued command, so ``HostFilter`` and the console
    ``RichHandler`` aren't the right path for it. Writing directly to
    the file keeps the terminal clean (only raw bytes reach the user's
    stdout) while still preserving the transcript in ``otto.log``.

    Each line is formatted to match the style produced by the normal
    logger's ``RichFormatter`` so a reader scanning ``otto.log`` doesn't
    see two different layouts.
    """

    def __init__(self, log_path: Path, host_name: str) -> None:
        self._file: Optional[Any] = None
        self._host_name = host_name
        try:
            self._file = log_path.open('a', encoding='utf-8')
        except OSError as exc:
            logger.debug(f"Interactive session log unavailable ({log_path}): {exc}")

    def write_line(self, line: str) -> None:
        if self._file is None:
            return
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.') + f"{datetime.now().microsecond // 1000:03d}"
        record = f"{timestamp} [ INFO  ] @{self._host_name} > | {line}\n"
        try:
            self._file.write(record)
            self._file.flush()
        except OSError:
            pass

    def write_marker(self, text: str) -> None:
        """Write a bookend line without the ``@host > |`` output preamble."""
        if self._file is None:
            return
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.') + f"{datetime.now().microsecond // 1000:03d}"
        record = f"{timestamp} [ INFO  ] @{self._host_name}   | {text}\n"
        try:
            self._file.write(record)
            self._file.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def _session_log_path() -> Optional[Path]:
    """Return the path to the current invocation's ``otto.log``, if any."""
    try:
        output_dir = logger.output_dir
    except AttributeError:
        return None
    if output_dir is None:
        return None
    return Path(output_dir) / 'otto.log'


async def _pump_stdin_to_remote(
    stdin_queue: 'asyncio.Queue[Optional[bytes]]',
    write_remote: Callable[[bytes], Awaitable[None]],
) -> None:
    """Forward stdin chunks to the remote side until EOF or ``Ctrl+]``.

    Returns normally on either end condition. Any bytes before the
    escape byte are forwarded; the escape byte and anything after it
    are discarded.
    """
    while True:
        chunk = await stdin_queue.get()
        if chunk is None:
            return
        idx = chunk.find(bytes([_ESCAPE_BYTE]))
        if idx >= 0:
            if idx > 0:
                await write_remote(chunk[:idx])
            return
        await write_remote(chunk)


async def _pump_remote_to_stdout(
    read_remote: Callable[[], Awaitable[bytes]],
    line_buffer: _LineBuffer,
) -> None:
    """Forward remote bytes to fd 1 and the line buffer until EOF."""
    try:
        while True:
            data = await read_remote()
            if not data:
                return
            # Feed the log buffer first so a broken terminal pipe doesn't
            # drop bytes from the transcript.
            line_buffer.feed(data)
            try:
                os.write(1, data)
            except BrokenPipeError:
                return
    finally:
        line_buffer.flush()


def _spawn_stdin_reader(
    loop: asyncio.AbstractEventLoop,
    stdin_queue: 'asyncio.Queue[Optional[bytes]]',
    shutdown: threading.Event,
) -> 'asyncio.Future[None]':
    """Spawn a worker thread that feeds stdin chunks into ``stdin_queue``.

    The thread polls ``select`` on fd 0 with a short timeout so it can
    notice the shutdown flag without needing a stdin sentinel write.
    On EOF or shutdown, a ``None`` sentinel is pushed onto the queue so
    consumers can unblock.
    """
    import select

    def _run() -> None:
        try:
            while not shutdown.is_set():
                try:
                    r, _, _ = select.select([0], [], [], 0.1)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    data = os.read(0, 4096)
                except OSError:
                    break
                if not data:
                    break
                loop.call_soon_threadsafe(stdin_queue.put_nowait, data)
        finally:
            loop.call_soon_threadsafe(stdin_queue.put_nowait, None)

    return loop.run_in_executor(None, _run)


def _initial_term_size() -> tuple[int, int]:
    """Return the current local terminal size, falling back to 80x24."""
    import shutil
    try:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines
    except OSError:
        return 80, 24


def _setup_raw_mode(fd: int) -> Any:
    """Put ``fd`` into raw mode. Returns saved attrs, or ``None`` on no-op."""
    if sys.platform == 'win32':
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setraw(fd)
    except termios.error:
        return None
    return saved


def _restore_terminal(fd: int, saved: Any) -> None:
    """Restore ``fd`` to the attrs returned by :func:`_setup_raw_mode`."""
    if saved is None or sys.platform == 'win32':
        return
    try:
        import termios
    except ImportError:
        return
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except termios.error:
        pass


def _print_stderr(msg: str) -> None:
    try:
        sys.stderr.write(msg + '\n')
        sys.stderr.flush()
    except OSError:
        pass


async def _run_bridge(
    *,
    write_remote: Callable[[bytes], Awaitable[None]],
    read_remote: Callable[[], Awaitable[bytes]],
    install_sigwinch: Callable[[], Callable[[], None]],
    on_output_line: Callable[[str], None],
    banner: Optional[str] = None,
) -> None:
    """Shared interactive bridge loop used by SSH and telnet login paths.

    Sets up raw mode, the threaded stdin reader, the SIGWINCH forwarder,
    and runs the two pump coroutines until one finishes. Guaranteed to
    restore the local terminal state on exit, even on exception.

    If *banner* is provided, it is written to stderr **after** raw mode is
    applied. Printing the banner before raw mode opens a window where a
    test harness (or a real user on a slow box) can type a line between
    the banner and raw mode taking effect — the kernel PTY then echoes
    those bytes back on the master before otto's stdin reader has a
    chance to read them, confusing ``expect``-style drivers and leaving
    the remote shell with no chance to round-trip the command into the
    session log before ``Ctrl+]`` races ahead.
    """
    loop = asyncio.get_running_loop()
    stdin_is_tty = sys.stdin.isatty() and sys.platform != 'win32'
    stdin_fd = sys.stdin.fileno()

    saved_attrs = _setup_raw_mode(stdin_fd) if stdin_is_tty else None

    if banner is not None:
        _print_stderr(banner)

    uninstall_sigwinch: Callable[[], None] = lambda: None
    if stdin_is_tty:
        try:
            uninstall_sigwinch = install_sigwinch()
        except (NotImplementedError, RuntimeError) as exc:
            logger.debug(f"SIGWINCH forwarding unavailable: {exc}")

    stdin_queue: 'asyncio.Queue[Optional[bytes]]' = asyncio.Queue()
    shutdown = threading.Event()
    reader_future = _spawn_stdin_reader(loop, stdin_queue, shutdown)

    line_buffer = _LineBuffer(on_output_line)

    try:
        stdin_task = asyncio.create_task(_pump_stdin_to_remote(stdin_queue, write_remote))
        remote_task = asyncio.create_task(_pump_remote_to_stdout(read_remote, line_buffer))

        _, pending = await asyncio.wait(
            [stdin_task, remote_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if remote_task in pending:
            # stdin finished first (user hit the escape byte). Give the remote
            # pump a bounded window to drain any bytes still in flight — e.g.
            # the echoed response to a command the user just sent — so they
            # reach `line_buffer` and get flushed to the session log. Without
            # this, cancelling immediately forfeits the read-buffer contents.
            await asyncio.wait([remote_task], timeout=0.2)
        for task in pending:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        shutdown.set()
        try:
            await asyncio.wait_for(asyncio.shield(reader_future), timeout=0.5)
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            uninstall_sigwinch()
        except Exception:
            pass
        _restore_terminal(stdin_fd, saved_attrs)


async def run_ssh_login(
    *,
    conn: Any,
    host_name: str,
) -> None:
    """Open a PTY-backed SSH shell on ``conn`` and bridge it to the terminal.

    The process requests ``term_type`` from ``$TERM`` so remote TUIs
    recognize the terminal capabilities. On local ``SIGWINCH`` the new
    size is forwarded to the remote PTY via
    ``SSHClientProcess.change_terminal_size`` — without this, the remote
    has no way to know the local terminal was resized.
    """
    import asyncssh

    term_type = os.environ.get('TERM') or 'xterm'
    cols, rows = _initial_term_size()

    process = await conn.create_process(
        request_pty='force',
        term_type=term_type,
        term_size=(cols, rows),
        stderr=asyncssh.STDOUT,
        encoding=None,
    )

    async def write_remote(data: bytes) -> None:
        process.stdin.write(data)

    async def read_remote() -> bytes:
        try:
            return await process.stdout.read(4096)
        except (asyncssh.misc.ConnectionLost, asyncio.IncompleteReadError):
            return b''

    def install_sigwinch() -> Callable[[], None]:
        loop = asyncio.get_running_loop()

        def handler() -> None:
            try:
                c, r = _initial_term_size()
                process.change_terminal_size(c, r)
            except Exception as exc:
                logger.debug(f"SSH window-change forward failed: {exc}")

        loop.add_signal_handler(signal.SIGWINCH, handler)

        def remove() -> None:
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except (NotImplementedError, RuntimeError):
                pass

        return remove

    _log_path = _session_log_path()
    log_file = _SessionLogFile(_log_path, host_name) if _log_path is not None else None
    log_file_effective = log_file if log_file is not None else _SessionLogFile(Path(os.devnull), host_name)
    log_file_effective.write_marker("Entering interactive session")

    try:
        await _run_bridge(
            write_remote=write_remote,
            read_remote=read_remote,
            install_sigwinch=install_sigwinch,
            on_output_line=log_file_effective.write_line,
            banner=f"[otto] interactive session with {host_name} (ssh). Press Ctrl+] to disconnect.",
        )
    finally:
        try:
            process.close()
        except Exception:
            pass
        log_file_effective.write_marker("Interactive session ended")
        log_file_effective.close()
        _print_stderr(f"[otto] disconnected from {host_name}.")


async def run_telnet_login(
    *,
    client: Any,
    host_name: str,
) -> None:
    """Bridge an already-connected interactive ``TelnetClient`` to the terminal.

    The client must have been opened with ``interactive=True`` so the
    remote is left in its default echo mode (otto's non-interactive
    connect flow sends ``DONT ECHO`` to silence command echo — not
    what we want here). Local ``SIGWINCH`` is forwarded as a NAWS
    subnegotiation via ``TelnetClient._send_naws``.
    """
    reader = client.reader
    writer = client.writer

    async def write_remote(data: bytes) -> None:
        writer.write(data)

    async def read_remote() -> bytes:
        try:
            return await reader.read(4096)
        except Exception:
            return b''

    def install_sigwinch() -> Callable[[], None]:
        loop = asyncio.get_running_loop()

        def handler() -> None:
            try:
                c, r = _initial_term_size()
                client._send_naws(c, r)
            except Exception as exc:
                logger.debug(f"NAWS push failed: {exc}")

        loop.add_signal_handler(signal.SIGWINCH, handler)
        try:
            c, r = _initial_term_size()
            client._send_naws(c, r)
        except Exception:
            pass

        def remove() -> None:
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except (NotImplementedError, RuntimeError):
                pass

        return remove

    log_path = _session_log_path()
    log_file = _SessionLogFile(log_path, host_name) if log_path else _SessionLogFile(Path(os.devnull), host_name)
    log_file.write_marker("Entering interactive session")

    try:
        await _run_bridge(
            write_remote=write_remote,
            read_remote=read_remote,
            install_sigwinch=install_sigwinch,
            on_output_line=log_file.write_line,
            banner=f"[otto] interactive session with {host_name} (telnet). Press Ctrl+] to disconnect.",
        )
    finally:
        log_file.write_marker("Interactive session ended")
        log_file.close()
        _print_stderr(f"[otto] disconnected from {host_name}.")
