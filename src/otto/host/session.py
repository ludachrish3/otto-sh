"""
Persistent shell sessions for remote command execution.

ShellSession provides a unified, stateful command execution layer on top of
SSH and telnet connections. All commands share a single shell — state
(working directory, environment, user context) persists between calls.

Key features:
- Sentinel-based output demarcation with exit code extraction
- Expect-enhanced run_cmd for interactive commands (sudo, su, etc.)
- Raw send/expect for driving non-shell interactive programs
- Per-command timeout with Ctrl+C recovery
"""

import asyncio
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

import asyncssh
from asyncssh import SSHClientConnection

from .telnet import TelnetClient

if TYPE_CHECKING:
    from .connections import ConnectionManager

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .host import RunResult, ShellCommand

logger = getOttoLogger()

# Type alias for expect patterns: (regex_pattern, response_text)
Expect = tuple[str | re.Pattern[str], str]

# Max length hint for asyncssh regex readuntil (performance optimization)
_MAX_SEPARATOR_LEN = 256

# Timeout for session recovery after Ctrl+C
_RECOVERY_TIMEOUT = 5.0


class ShellSession(ABC):
    """Abstract base for persistent shell sessions.

    Subclasses implement the I/O primitives (_write, _read_until_pattern, _open, close).
    The base class provides shared logic for sentinel-wrapped command execution,
    expect handling, and timeout recovery.
    """

    def __init__(self) -> None:
        self._session_id = uuid.uuid4().hex[:12]
        self._begin_marker = f"__OTTO_{self._session_id}_BEGIN__"
        self._end_marker_prefix = f"__OTTO_{self._session_id}_END__"
        self._end_pattern = re.compile(rf"__OTTO_{self._session_id}_END__(\d+)__")
        self._ready_marker = f"__OTTO_{self._session_id}_READY__"
        self._recover_marker = f"__OTTO_{self._session_id}_RECOVER__"
        self._initialized = False
        self._alive = False
        self._on_output: Callable[[str], None] = lambda _: None

    @property
    def alive(self) -> bool:
        """Whether the session is initialized and responsive."""
        return self._alive

    # --- Abstract I/O primitives (implemented by subclasses) ---

    @abstractmethod
    async def _open(self) -> None:
        """Open the underlying transport (SSH process, telnet stream, etc.)."""
        ...

    @abstractmethod
    async def _write(self, data: str) -> None:
        """Write raw text to the session's stdin."""
        ...

    @abstractmethod
    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        """Read from stdout until pattern matches. Returns data including the match."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the session and release resources."""
        ...

    # --- Session lifecycle ---

    async def _ensure_initialized(self) -> None:
        """Open the transport and initialize the session. Idempotent."""
        if self._initialized:
            return
        await self._open()
        # Disable input echo and verify the shell is responsive
        await self._write(f"stty -echo 2>/dev/null; echo {self._ready_marker}\n")
        # Anchor with \n so we only match the marker as actual command output.
        # Without it, the pattern also matches inside the shell's echo of the sent
        # command ("… echo __OTTO_…_READY__"), causing premature return.
        await self._read_until_pattern(re.compile(re.escape(self._ready_marker)))
        self._initialized = True
        self._alive = True

    # --- Public API ---

    async def send(self, text: str) -> None:
        """Send raw text to the session's stdin.

        Use this for driving interactive programs (REPLs, custom CLIs).
        The caller is responsible for including line endings.
        """
        await self._ensure_initialized()
        await self._write(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 30.0,
    ) -> str:
        """Wait for a pattern in the output stream.

        Returns captured data up to and including the match.
        Raises asyncio.TimeoutError if the pattern isn't seen within timeout.
        Marks the session as dead if EOF is received.
        """
        await self._ensure_initialized()
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        try:
            return await asyncio.wait_for(
                self._read_until_pattern(compiled),
                timeout=timeout,
            )
        except asyncio.IncompleteReadError:
            self._alive = False
            raise

    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Execute a shell command with sentinel-based output demarcation.

        Output is streamed line-by-line to ``_on_output`` as it arrives.
        Sentinels and echoed command text are filtered out automatically.

        Args:
            cmd: The shell command to execute.
            expects: Optional list of (pattern, response) tuples. If a pattern
                appears in the output before the end sentinel, the response is
                automatically sent. Expects are inherently optional — if the
                pattern never appears, the end sentinel matches normally.
            timeout: Optional timeout in seconds. On expiry, the session
                attempts recovery via Ctrl+C and returns Status.Error.

        Returns:
            CommandStatus with exit code extracted from the sentinel.
        """
        await self._ensure_initialized()
        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    self._run_cmd_inner(cmd, expects),
                    timeout=timeout,
                )
            return await self._run_cmd_inner(cmd, expects)

        except asyncio.TimeoutError:
            partial = await self._recover_session()
            return CommandStatus(
                command=cmd,
                output=f"Command timed out after {timeout}s" + (f"\n{partial}" if partial else ""),
                status=Status.Error,
                retcode=-1,
            )
        except asyncio.IncompleteReadError:
            self._alive = False
            return CommandStatus(
                command=cmd,
                output="Session died unexpectedly (EOF)",
                status=Status.Error,
                retcode=-1,
            )

    # --- Internal implementation ---

    async def _run_cmd_inner(
        self,
        cmd: str,
        expects: list[Expect] | None,
    ) -> CommandStatus:
        """Send sentinel-wrapped command, handle expects, stream output.

        Output is read line-by-line. Each content line (sentinels and echoed
        command text stripped) is streamed to ``self._on_output`` as it arrives.
        """

        # Wrap the command with BEGIN/END sentinels. $? captures the exit code.
        wrapped = (
            f'echo "{self._begin_marker}"; '
            f'{cmd}; '
            f'echo "{self._end_marker_prefix}$?__"'
        )
        await self._write(wrapped + "\n")

        # Build regex that matches any expect pattern OR the end sentinel OR
        # a newline.  The newline alternative causes _read_until_pattern to
        # return after every line, enabling incremental output streaming.
        combined = self._build_combined_pattern(expects)

        # Read loop: handle expect responses and accumulate output
        buffer = ""
        seen_begin = False
        while True:
            data = await self._read_until_pattern(combined)
            buffer += data

            # Check if the end sentinel was matched
            end_match = self._end_pattern.search(data)
            if end_match:
                # Emit any output text that precedes the sentinel on the same
                # line (happens when a command produces no trailing newline).
                if seen_begin:
                    pre = data[:end_match.start()].replace('\r', '').strip()
                    if pre:
                        self._on_output(pre)
                retcode = int(end_match.group(1))
                break

            # An expect pattern matched — find which one and send its response
            expect_matched = False
            if expects:
                for pat_str, response in expects:
                    pat = re.compile(pat_str) if isinstance(pat_str, str) else pat_str
                    if pat.search(data):
                        await self._write(response)
                        expect_matched = True
                        break

            # Stream each content line to the callback.
            # The begin marker check uses rstrip + startswith to avoid
            # false matches when the shell echoes the wrapped command
            # (which embeds the marker inside quotes on the same line).
            if not expect_matched:
                if not seen_begin:
                    stripped = data.rstrip('\r\n')
                    if stripped == self._begin_marker or stripped.endswith(self._begin_marker):
                        seen_begin = True
                else:
                    line = data.rstrip('\r\n').replace('\r', '')
                    if line:
                        self._on_output(line)

        output = self._parse_output(buffer)
        status = Status.Success if retcode == 0 else Status.Failed
        return CommandStatus(command=cmd, output=output, status=status, retcode=retcode)

    def _build_combined_pattern(
        self,
        expects: list[Expect] | None,
    ) -> re.Pattern[str]:
        """Build a combined regex: expect patterns | end sentinel | newline.

        The ``\\n`` alternative (lowest priority) causes
        ``_read_until_pattern`` to return after every line, enabling
        incremental output streaming.
        """

        parts: list[str] = []
        if expects:
            for i, (pattern, _) in enumerate(expects):
                pat_str = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
                parts.append(f"(?P<expect_{i}>{pat_str})")
        parts.append(f"(?P<sentinel>{self._end_pattern.pattern})")
        parts.append(r"(?P<newline>\n)")
        return re.compile("|".join(parts))

    def _parse_output(self, buffer: str) -> str:
        """Extract command output from between BEGIN and END sentinel markers."""

        # Find the LAST BEGIN marker — if the shell echoes the wrapped command,
        # the marker appears twice: once in the echoed command text and once as
        # actual output.  Using rfind ensures we skip the echoed copy.
        begin_idx = buffer.rfind(self._begin_marker)
        if begin_idx != -1:
            start = begin_idx + len(self._begin_marker)
            # Skip trailing newline(s) after the marker
            while start < len(buffer) and buffer[start] in ('\r', '\n'):
                start += 1
        else:
            start = 0

        # Find END marker — output ends before it
        end_match = self._end_pattern.search(buffer, start)
        end = end_match.start() if end_match else len(buffer)

        output = buffer[start:end].rstrip('\r\n')
        # Strip carriage returns left over from PTY \r\n line endings
        output = output.replace('\r', '')
        return output

    async def _recover_session(self) -> str:
        """Attempt session recovery after timeout: Ctrl+C, then recovery sentinel.

        Returns any partial output captured during recovery.
        Sets self._alive = False if recovery fails.
        """
        try:
            # Send Ctrl+C (SIGINT) to interrupt the hung foreground process
            await self._write("\x03")
            await asyncio.sleep(0.1)

            # Send recovery sentinel to re-synchronize
            await self._write(f"echo {self._recover_marker}\n")
            data = await asyncio.wait_for(
                self._read_until_pattern(re.compile(re.escape(self._recover_marker))),
                timeout=_RECOVERY_TIMEOUT,
            )
            # Session is recovered and usable for the next command
            return data.split(self._recover_marker)[0].strip()

        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            # Shell itself is unresponsive — mark session as dead
            self._alive = False
            return ""


class SshSession(ShellSession):
    """SSH persistent shell session via asyncssh create_process()."""

    def __init__(self, conn: SSHClientConnection) -> None:
        super().__init__()
        self._conn = conn
        self._process: Any = None

    async def _open(self) -> None:
        self._process = await self._conn.create_process(
            term_type='dumb',
            stderr=asyncssh.STDOUT,
        )

    async def _write(self, data: str) -> None:
        assert self._process is not None
        self._process.stdin.write(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        assert self._process is not None
        return await self._process.stdout.readuntil(pattern, _MAX_SEPARATOR_LEN)

    async def close(self) -> None:
        if self._process is not None:
            self._process.close()
            self._process = None
        self._alive = False
        self._initialized = False


class TelnetSession(ShellSession):
    """Telnet persistent shell session via telnetlib3 streams."""

    def __init__(
        self,
        reader: Any,
        writer: Any,
        _owned_client: 'TelnetClient | None' = None,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._owned_client = _owned_client

    async def _open(self) -> None:
        # Transport already established by TelnetClient login — nothing to open
        pass

    async def _write(self, data: str) -> None:
        # Use CR (\r) as the sole line terminator. Sending \r\n causes two
        # inputs: the \r executes the command in readline raw mode, and the
        # trailing \n triggers an extra empty prompt (e.g. an extra ">>> " in
        # Python REPL). This stale prompt then matches the next expect() call
        # before the real output arrives.  Using \r alone works for both:
        # - canonical mode (icrnl maps \r → \n, so the shell sees one newline)
        # - readline raw mode (treats \r as Enter / execute)
        data = re.sub(r'\r?\n', '\r', data)
        self._writer.write(data.encode())

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        # telnetlib3 operates in bytes mode — compile a bytes version of the pattern
        bytes_pattern = re.compile(pattern.pattern.encode())
        raw: bytes = await self._reader.readuntil_pattern(bytes_pattern)  # type: ignore[attr-defined]
        return raw.decode('utf-8', errors='replace')

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        if self._owned_client:
            await self._owned_client.close()
        self._alive = False
        self._initialized = False


class LocalSession(ShellSession):
    """Local persistent bash shell session via asyncio subprocess.

    Implements the ShellSession I/O primitives using a long-running bash
    process, giving LocalHost the same sentinel-wrapped execution, expect
    handling, and timeout recovery that remote sessions enjoy.
    """

    def __init__(self) -> None:
        super().__init__()
        self._process: asyncio.subprocess.Process | None = None
        self._transport: asyncio.SubprocessTransport | None = None
        self._pid: int | None = None

    async def _open(self) -> None:
        # Drive loop.subprocess_exec() directly (rather than the higher-level
        # asyncio.create_subprocess_exec) so that we hold an explicit reference
        # to the transport. close() uses it to release the pipe fds without
        # reaching into the private Process._transport attribute.
        loop = asyncio.get_running_loop()
        protocol_factory = lambda: asyncio.subprocess.SubprocessStreamProtocol(
            limit=2 ** 16, loop=loop,
        )
        transport, protocol = await loop.subprocess_exec(
            protocol_factory,
            'bash', '--norc', '--noprofile',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._transport = transport
        self._process   = asyncio.subprocess.Process(transport, protocol, loop)
        self._pid       = self._process.pid

    async def _write(self, data: str) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        assert self._process is not None and self._process.stdout is not None
        buf = ""
        while True:
            chunk = await self._process.stdout.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(buf.encode(), None)
            buf += chunk.decode('utf-8', errors='replace')
            if pattern.search(buf):
                return buf

    async def _recover_session(self) -> str:
        """Recovery via SIGINT to child processes (Ctrl+C byte doesn't work over PIPE)."""
        import signal
        try:
            # Send SIGINT to all children of the bash process (the hung command)
            if self._pid is not None:
                self._signal_children(self._pid, signal.SIGINT)
            await asyncio.sleep(0.1)

            await self._write(f"echo {self._recover_marker}\n")
            data = await asyncio.wait_for(
                self._read_until_pattern(re.compile(re.escape(self._recover_marker))),
                timeout=_RECOVERY_TIMEOUT,
            )
            return data.split(self._recover_marker)[0].strip()

        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            self._alive = False
            return ""

    @staticmethod
    def _signal_children(parent_pid: int, sig: int) -> None:
        """Send a signal to all child processes of the given PID."""
        import os
        from pathlib import Path
        try:
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    ppid_line = (entry / 'stat').read_text().split()
                    # Field 4 (0-indexed: 3) is PPID
                    if int(ppid_line[3]) == parent_pid:
                        os.kill(int(entry.name), sig)
                except (IndexError, ValueError, FileNotFoundError, PermissionError):
                    continue
        except (FileNotFoundError, PermissionError):
            pass

    async def close(self) -> None:
        if self._process is not None:
            if self._process.returncode is None:
                # Process still running — try graceful exit
                if self._process.stdin is not None:
                    try:
                        self._process.stdin.write(b'exit\n')
                        await self._process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
            # Explicitly close pipe transports so their __del__ doesn't
            # attempt loop.call_soon() after the event loop is closed.
            if self._process.stdin is not None:
                self._process.stdin.close()
            # stdout is a StreamReader (no close); close the subprocess
            # transport we stashed at open() time to release the pipe fds.
            if self._transport is not None:
                self._transport.close()
                self._transport = None
            self._process = None
        self._alive = False
        self._initialized = False


class HostSession:
    """A named persistent shell session on any host type.

    Obtained via ``await host.open_session(name)``. Supports the async context
    manager protocol for automatic cleanup.

    Example::

        async with (await host.open_session("monitor")) as mon:
            result = await mon.run("stat /tmp/file.bin")

    Or without a context manager::

        mon = await host.open_session("monitor")
        try:
            result = await mon.run("stat /tmp/file.bin")
        finally:
            await mon.close()
    """

    def __init__(
        self,
        name: str,
        session: ShellSession,
        log_command: Callable[[str], None],
        log_output: Callable[[str], None],
        deregister: Callable[[str], None],
    ) -> None:
        self._name = name
        self._session = session
        self._log_command = log_command
        self._log_output = log_output
        self._deregister = deregister

    @property
    def alive(self) -> bool:
        """Whether the underlying shell session is still active."""
        return self._session.alive

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float | None = 10.0,
    ) -> RunResult:
        """Execute one or more commands on this named session.

        Mirrors :meth:`Host.run`: accepts a ``str``, a :class:`ShellCommand`, or a
        sequence mixing the two, and always returns a :class:`RunResult`. Per-command
        ``expects`` / ``timeout`` on a :class:`ShellCommand` override the run-level
        defaults; a scalar :data:`Expect` tuple at the run level is normalized to a
        one-element list.
        """
        from .host import _run_cmds_with_budget, _normalize_expects, _resolve_command

        default_expects = _normalize_expects(expects)

        async def _run_sc(sc: ShellCommand, t: float | None) -> CommandStatus:
            self._log_command(sc.cmd)
            return await self._session.run_cmd(
                sc.cmd,
                expects=_normalize_expects(sc.expects),
                timeout=t,
            )

        if isinstance(cmds, (str, ShellCommand)):
            sc = _resolve_command(cmds, default_expects, timeout)
            result = await _run_sc(sc, sc.timeout)
            status = result.status if not result.status.is_ok else Status.Success
            return RunResult(status=status, statuses=[result])

        resolved = [_resolve_command(c, default_expects, None) for c in cmds]
        return await _run_cmds_with_budget(_run_sc, resolved, timeout)

    async def send(self, text: str) -> None:
        """Send raw text to this session's stdin. See :meth:`RemoteHost.send`."""
        self._log_command(text.rstrip())
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in this session's output. See :meth:`RemoteHost.expect`."""
        result = await self._session.expect(pattern, timeout)
        self._log_output(result)
        return result

    async def close(self) -> None:
        """Close this session and remove it from the host's session registry."""
        await self._session.close()
        self._deregister(self._name)

    async def __aenter__(self) -> 'HostSession':
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class SessionManager:
    """Manages persistent shell sessions for any host type.

    Owns the default session (used by ``run_cmd``/``send``/``expect``) and all
    named sessions (created via ``open_session``).

    Session creation is pluggable: provide a ``session_factory`` callable to
    control how sessions are created (e.g. ``LocalSession`` for local hosts),
    or pass a ``ConnectionManager`` to use the default SSH/Telnet dispatch.
    Similarly, ``oneshot_factory`` controls stateless command execution.
    """

    def __init__(
        self,
        connections: 'ConnectionManager | None' = None,
        name: str = '',
        log_command: Callable[[str], None] = lambda _: None,
        log_output: Callable[[str], None] = lambda _: None,
        session_factory: 'Callable[[], ShellSession] | None' = None,
        oneshot_factory: 'Callable[[str, float | None], Awaitable[CommandStatus]] | None' = None,
    ) -> None:
        self._connections = connections
        self._name = name
        self._log_command = log_command
        self._log_output = log_output
        self._session_factory = session_factory
        self._oneshot_factory = oneshot_factory
        self._session: ShellSession | None = None
        self._named_sessions: dict[str, HostSession] = {}
        # Free-list of idle shell sessions used by `oneshot()` for terminals
        # (e.g. telnet) that lack a stateless exec primitive.  Serial callers
        # reuse one session so the TCP+auth handshake is paid once; concurrent
        # callers each pull their own session off the list (opening a fresh
        # one if none are free), preserving `oneshot()`'s documented contract
        # that concurrent calls run independently.
        self._oneshot_pool: list[HostSession] = []
        self._oneshot_pool_count: int = 0

    @property
    def has_live_sessions(self) -> bool:
        """Whether any session (default or named) is currently alive."""
        if self._session and self._session.alive:
            return True
        return any(s.alive for s in self._named_sessions.values())

    async def _ensure_session(self) -> None:
        """Create a ShellSession if one doesn't exist or if the current one is dead."""
        if self._session and self._session.alive:
            return

        # Close the old dead session to release its subprocess transport.
        # Without this, the orphaned transport is GC'd after the event loop
        # closes and raises "RuntimeError: Event loop is closed" from __del__.
        if self._session is not None:
            await self._session.close()

        if self._session_factory is not None:
            self._session = self._session_factory()
        else:
            assert self._connections is not None
            match self._connections.term:
                case 'ssh':
                    ssh_conn = await self._connections.ssh()
                    self._session = SshSession(ssh_conn)
                case 'telnet':
                    telnet_conn = await self._connections.telnet()
                    self._session = TelnetSession(
                        telnet_conn.reader,
                        telnet_conn.writer,
                    )
                case _:
                    raise ValueError(f'{self._name}: unsupported terminal type "{self._connections.term}"')

        self._session._on_output = self._log_output

    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
    ) -> CommandStatus:
        await self._ensure_session()
        self._log_command(cmd)
        assert self._session is not None
        result = await self._session.run_cmd(cmd, expects=expects, timeout=timeout)
        return result

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        if self._oneshot_factory is not None:
            return await self._oneshot_factory(cmd, timeout)

        assert self._connections is not None
        self._log_command(cmd)
        match self._connections.term:
            case 'ssh':
                ssh_conn = await self._connections.ssh()
                process = await ssh_conn.create_process(
                    cmd, stderr=asyncssh.STDOUT, stdin=asyncssh.DEVNULL,
                )
                lines: list[str] = []
                try:
                    async for raw_line in process.stdout:
                        line = raw_line.rstrip('\n')
                        lines.append(line)
                        self._log_output(line)
                except asyncio.TimeoutError:
                    process.terminate()
                result = await process.wait()
                status = Status.Success if result.exit_status == 0 else Status.Failed
                return CommandStatus(
                    command=cmd,
                    output='\n'.join(lines),
                    status=status,
                    retcode=result.exit_status or 0,
                )
            case 'telnet':
                # Telnet has no stateless exec primitive (unlike SSH which
                # multiplexes channels over one connection).  Rather than open
                # a fresh TCP+auth for every oneshot call — which in practice cost
                # 1–2 s each on real hardware — we keep a free-list of idle
                # persistent sessions and reuse them.  Serial callers churn
                # one session; concurrent callers (e.g. `_put_files_nc`
                # launching multiple `nc -l` listeners in parallel) each get
                # their own, preserving the documented concurrency contract.
                oneshot_session = await self._acquire_oneshot_session()
                try:
                    return (await oneshot_session.run(cmd, timeout=timeout)).only
                finally:
                    self._oneshot_pool.append(oneshot_session)
            case _:
                raise ValueError(f'{self._name}: unsupported terminal type "{self._connections.term}"')

    async def _acquire_oneshot_session(self) -> 'HostSession':
        """Pop an idle oneshot session off the free-list, or open a new one.

        Sessions are keyed by a monotonic index so concurrent callers get
        independent entries in ``_named_sessions``.  Dead sessions (closed
        or disconnected) are skipped so callers always get a usable one.
        """
        while self._oneshot_pool:
            session = self._oneshot_pool.pop()
            if session.alive:
                return session
        self._oneshot_pool_count += 1
        return await self.open_session(f'__oneshot_pool_{self._oneshot_pool_count}__')

    async def open_session(self, name: str) -> 'HostSession':
        """Open or reuse a named persistent shell session."""
        existing = self._named_sessions.get(name)
        if existing and existing.alive:
            return existing

        if self._session_factory is not None:
            shell_session: ShellSession = self._session_factory()
        else:
            assert self._connections is not None
            match self._connections.term:
                case 'ssh':
                    ssh_conn = await self._connections.ssh()
                    shell_session = SshSession(ssh_conn)
                case 'telnet':
                    user, password = self._connections.credentials
                    client = TelnetClient(
                        self._connections.ip,
                        user=user,
                        password=password,
                        options=self._connections.telnet_options,
                    )
                    await client.connect()
                    shell_session = TelnetSession(
                        client.reader,
                        client.writer,
                        _owned_client=client,
                    )
                case _:
                    raise ValueError(f'{self._name}: unsupported terminal type "{self._connections.term}"')

        shell_session._on_output = self._log_output
        host_session = HostSession(
            name=name,
            session=shell_session,
            log_command=self._log_command,
            log_output=self._log_output,
            deregister=lambda n: (self._named_sessions.pop(n, None), None)[1],
        )
        self._named_sessions[name] = host_session
        return host_session

    async def send(self, text: str) -> None:
        await self._ensure_session()
        self._log_command(text.rstrip())
        assert self._session is not None
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        await self._ensure_session()
        assert self._session is not None
        result = await self._session.expect(pattern, timeout)
        self._log_output(result)
        return result

    async def close_all(self) -> None:
        """Close the default session and all named sessions."""
        if self._session:
            await self._session.close()
            self._session = None

        if self._named_sessions:
            await asyncio.gather(
                *(session.close() for session in self._named_sessions.values()),
                return_exceptions=True,
            )
            self._named_sessions.clear()
