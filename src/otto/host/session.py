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

from __future__ import annotations

import asyncio
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from typing_extensions import Self, override

from .command_frame import BashFrame, CommandFrame, SessionMarkers
from .telnet import TelnetClient

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from .connections import ConnectionManager

from ..logger import get_logger
from ..logger.mode import LogMode
from ..result import CommandResult, Results
from ..utils import Status
from .host import ShellCommand

logger = get_logger()

# Type alias for expect patterns: (regex_pattern, response_text)
Expect = tuple[str | re.Pattern[str], str]

# Max length hint for asyncssh regex readuntil (performance optimization)
_MAX_SEPARATOR_LEN = 256

# Log-preview truncation limits to keep debug output readable.
_LOG_PREVIEW_FRAMED = 256  # max chars of a framed command payload in the log
_LOG_PREVIEW_HANDSHAKE = 512  # max chars of handshake data shown in the log
_LOG_PREVIEW_BUFFER = 1024  # buffer length above which run_cmd shows head+tail excerpt

# Timeout for session recovery after Ctrl+C
_RECOVERY_TIMEOUT = 5.0

# Ceiling on the post-open marker handshake (_ensure_initialized). Generous
# enough for slow MOTD generation on real hardware, but bounded so a failed
# telnet login — where the READY marker can never appear — surfaces as a clear
# error instead of hanging indefinitely.
_INIT_TIMEOUT = 3.0

# Default pause between a failed first readiness handshake and the single retry
# in ``SessionManager._ensure_session``. Calibrated to single-client RTOS telnet
# servers (Zephyr's ``CONFIG_SHELL_BACKEND_TELNET``) that don't free the console
# slot the instant the FIN lands. Injectable via ``SessionManager(retry_backoff=)``
# so fakes-only unit tests can zero it instead of paying the real wall-clock wait.
_HANDSHAKE_RETRY_BACKOFF = 2.0


def _drop_output(_line: str) -> None:
    """Output sink that discards a command's streamed output.

    Used to honor an effective ``LogMode.NEVER`` without mutating any shared logging state.
    """


def _sink_for(
    log_output: Callable[[str, LogMode], None],
    mode: LogMode,
) -> Callable[[str], None]:
    """Per-command output sink: NEVER discards; else forward each line tagged with the mode."""
    if mode is LogMode.NEVER:
        return _drop_output
    return lambda line: log_output(line, mode)


class ShellSession(ABC):
    """Abstract base for persistent shell sessions.

    A session is **transport + dialect**. Subclasses implement the I/O
    primitives (_write, _read_until_pattern, _open, close) — that is the
    transport (SSH, telnet, local subprocess). The *dialect* — how a command is
    wrapped in sentinels and how output/retcode are parsed back — is composed
    in as a :class:`~otto.host.command_frame.CommandFrame` (default
    :class:`~otto.host.command_frame.BashFrame`), not inherited. The base class
    provides the shared engine: sentinel-wrapped command execution, expect
    handling, and timeout recovery, all delegating the dialect to the frame.
    """

    # Ceiling on the marker handshake in _ensure_initialized. Class-level so
    # tests can shrink it without patching the module constant. A session built
    # against a slow-to-start shell (e.g. a Zephyr QEMU telnet console) gets a
    # more generous value via the ``init_timeout`` constructor argument, which
    # shadows this with an instance attribute.
    _init_timeout: float = _INIT_TIMEOUT

    # How long to wait for one readiness probe before resending it. Bounds the
    # cost of the telnet login-flush race (a probe lost before the shell is
    # reading) to roughly one interval.
    _init_probe_interval: float = 0.5

    def __init__(
        self,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
    ) -> None:
        self._session_id = uuid.uuid4().hex[:12]
        # The dialect: how commands are framed and parsed. Defaults to bash; an
        # embedded host injects a ZephyrFrame (or a project-registered frame).
        self._frame: CommandFrame = command_frame or BashFrame()
        # Unique per-session sentinels, handed to the frame for every
        # render/parse call. Aliased as individual attributes too because the
        # session orchestration (and its tests) reference them directly.
        self._markers = SessionMarkers.for_session(self._session_id)
        self._begin_marker = self._markers.begin
        self._end_marker_prefix = self._markers.end_prefix
        self._ready_marker = self._markers.ready
        self._recover_marker = self._markers.recover
        # The frame owns the end-of-output pattern (bash bakes the retcode into
        # it; Zephyr's is the bare token). Compiled once per session.
        self._end_pattern = self._frame.end_pattern(self._markers)
        # A non-default readiness ceiling (slow embedded shells) is set as an
        # instance attribute so it shadows the monkeypatchable class default
        # only for the sessions that need it.
        if init_timeout is not None:
            self._init_timeout = init_timeout
        self._initialized = False
        self._alive = False
        # Set when an in-flight operation (run_cmd / expect) is externally
        # cancelled, leaving the remote shell mid-command with possibly
        # buffered output we never consumed. The next operation runs
        # _recover_session before doing anything to drain that state and
        # return the shell to a clean prompt.
        self._needs_recovery = False
        self._on_output: Callable[[str], None] = lambda _: None
        # Optional per-command write-progress sink: (bytes_written, total).
        # Set transiently around a single framed write (see _run_cmd_inner) and
        # honored by transports that pace their writes (TelnetSession). Used to
        # drive a transfer-style bar for bulk console pushes (EmbeddedHost.load).
        self._write_progress: Callable[[int, int], None] | None = None
        # The OS user this shell is currently running as. Seeded by
        # SessionManager from the host's login user; mutated only by the
        # elevation flow (switch_user/as_user). '' on loginless shells.
        self.current_user: str = ""

    @property
    def alive(self) -> bool:
        """Whether the session is initialized and responsive."""
        return self._alive

    @property
    def _log_tag(self) -> str:
        """Stable tag for debug log lines: ``<class>@<session_id>``.

        Identifies which subclass and which session a log line came from when
        multiple are running concurrently — useful when bringing up a new
        embedded OS subclass alongside the existing Zephyr/Unix ones.
        """
        return f"{type(self).__name__}@{self._session_id}"

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

    async def _ensure_ready(self) -> None:
        """Initialize the session if needed, and recover it if the prior op was cancelled."""
        await self._ensure_initialized()
        if self._needs_recovery:
            # Clear the flag first so a recovery that itself fails doesn't
            # loop forever on subsequent calls — _recover_session marks
            # _alive=False on its own failures.
            self._needs_recovery = False
            await self._recover_session()

    async def _ensure_initialized(self) -> None:
        """Open the transport and initialize the session. Idempotent.

        After the transport opens, a marker handshake confirms the shell is
        live: write ``stty -echo; echo <READY marker>`` and read until that
        marker comes back. This is the deterministic readiness check that
        replaced telnet login's silence-drain — prompt-independent and
        content-independent.

        The probe is *retried* on a fixed interval rather than written once.
        This matters for telnet: ``login(1)`` typically flushes pending
        terminal input before it exec's the shell, so a probe written in the
        window between the password and the shell starting is silently
        discarded. Resending until one lands closes that race. SSH and local
        shells are ready immediately and answer the first probe, so they
        never actually retry.

        Bounded by ``_init_timeout``: a failed login (no shell ever spawns,
        so no probe can echo back) surfaces as a clear ``ConnectionError``
        instead of hanging until some far-outer timeout.
        """
        if self._initialized:
            return
        await self._open()

        # Anchor the marker to line-start (or buffer-start, for pipe-backed
        # shells whose output has no preceding echo). Without the anchor the
        # pattern also matches the marker *inside the echoed probe command*
        # ("... echo __OTTO_..._READY__") — which is fatal on a failed telnet
        # login, where the device loops back to "login:" and echoes our probe
        # as a username, making a rejected login look successful.
        #
        # ``(?:\x1b\[[0-9;]*m)*`` after the anchor absorbs any ANSI colour
        # codes a shell emits between the line start and the marker — the
        # Zephyr RTOS shell colours its ``command not found`` line, so the
        # rejected marker arrives as ``\n\x1b[1;31m__OTTO_..._READY__``.
        # The group matches zero times for a plain bash shell, so this is a
        # no-op there.
        marker = re.compile(r"(?:^|\r|\n)(?:\x1b\[[0-9;]*m)*" + re.escape(self._ready_marker))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._init_timeout
        handshake_cmd = self._frame.handshake(self._markers)
        logger.debug(
            f"{self._log_tag}: handshake start "
            f"cmd={handshake_cmd!r} marker={self._ready_marker!r} "
            f"timeout={self._init_timeout}s"
        )
        start = loop.time()
        attempt = 0
        while True:
            attempt += 1
            await self._write(handshake_cmd)
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._fail_init(attempt=attempt)
            try:
                data = await asyncio.wait_for(
                    self._read_until_pattern(marker),
                    timeout=min(self._init_probe_interval, remaining),
                )
                elapsed = loop.time() - start
                # Truncate the matched data so a noisy banner doesn't flood
                # the log; the tail (where the marker landed) is the part
                # that matters for diagnosing a future bring-up.
                shown = (
                    data
                    if len(data) <= _LOG_PREVIEW_HANDSHAKE
                    else f"...{data[-_LOG_PREVIEW_HANDSHAKE:]}"
                )
                logger.debug(
                    f"{self._log_tag}: handshake matched in {elapsed:.2f}s "
                    f"(attempts={attempt}, {len(data)} bytes): {shown!r}"
                )
                break
            except asyncio.TimeoutError:
                # No response to this probe — the shell may not be reading
                # input yet. Resend, unless we've run out the clock.
                logger.debug(
                    f"{self._log_tag}: handshake probe #{attempt} timed out, "
                    f"resending (deadline in {deadline - loop.time():.2f}s)"
                )
                if loop.time() >= deadline:
                    await self._fail_init(attempt=attempt)
                continue
            except asyncio.IncompleteReadError:
                # Peer EOF mid-handshake — the connection is gone; retrying
                # cannot help.
                logger.debug(f"{self._log_tag}: handshake hit EOF on attempt #{attempt}")
                await self._fail_init(attempt=attempt)

        self._initialized = True
        self._alive = True

    async def _fail_init(self, attempt: int = 0) -> None:
        """Tear down a session whose readiness handshake never completed."""
        logger.debug(
            f"{self._log_tag}: handshake FAILED after {attempt} attempt(s); "
            f"marking session dead and closing"
        )
        self._alive = False
        with suppress(Exception):  # pragma: no cover - best-effort cleanup
            await self.close()
        raise ConnectionError(
            "shell never became ready after open — the device is "
            "unresponsive or login failed (e.g. bad credentials)"
        )

    # --- Public API ---

    async def send(self, text: str) -> None:
        """Send raw text to the session's stdin.

        Use this for driving interactive programs (REPLs, custom CLIs).
        The caller is responsible for including line endings.
        """
        await self._ensure_ready()
        try:
            await self._write(text)
        except asyncio.CancelledError:
            self._needs_recovery = True
            raise

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
        await self._ensure_ready()
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        try:
            return await asyncio.wait_for(
                self._read_until_pattern(compiled),
                timeout=timeout,
            )
        except asyncio.IncompleteReadError:
            self._alive = False
            raise
        except asyncio.CancelledError:
            self._needs_recovery = True
            raise

    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
        on_output: Callable[[str], None] | None = None,
        redact: bool = False,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandResult:
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
            CommandResult with exit code extracted from the sentinel.
        """
        await self._ensure_ready()
        sink = on_output if on_output is not None else self._on_output
        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    self._run_cmd_inner(cmd, expects, sink, redact, write_progress),
                    timeout=timeout,
                )
            return await self._run_cmd_inner(cmd, expects, sink, redact, write_progress)

        except asyncio.TimeoutError:
            partial = await self._recover_session()
            return CommandResult(
                status=Status.Error,
                value=f"Command timed out after {timeout}s" + (f"\n{partial}" if partial else ""),
                command=cmd,
                retcode=-1,
            )
        except asyncio.CancelledError:
            # External cancellation (e.g., asyncio.wait_for at the caller
            # level) leaves the remote shell mid-command with buffered output
            # we never consumed. Mark for recovery on next use rather than
            # running it inline — running here would race the cancellation
            # propagation and could leave the recover-marker write detached.
            self._needs_recovery = True
            raise
        except asyncio.IncompleteReadError:
            self._alive = False
            return CommandResult(
                status=Status.Error,
                value="Session died unexpectedly (EOF)",
                command=cmd,
                retcode=-1,
            )

    # --- Internal implementation ---

    async def _run_cmd_inner(
        self,
        cmd: str,
        expects: list[Expect] | None,
        on_output: Callable[[str], None],
        redact: bool = False,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandResult:
        """Send sentinel-wrapped command, handle expects, surface output.

        Output is read line-by-line. How it reaches ``on_output`` depends on the
        frame's :attr:`~otto.host.command_frame.CommandFrame.streams_output_live`:
        a *live* frame (e.g. bash) emits each content line as it arrives, with
        sentinels and echoed command text stripped; a *buffered* frame (e.g.
        Zephyr) emits nothing mid-stream and surfaces ``parse_output(buffer)``
        once on completion, so shell prompts and retcode scaffolding never reach
        the log. ``on_output`` is the per-command sink (the host's logger, or a
        no-op when the effective mode is ``LogMode.NEVER``).
        """
        live = self._frame.streams_output_live

        # Write the framed command. The framing — BEGIN/END sentinels and how
        # the exit code is captured — is supplied by the session's CommandFrame
        # so that a target can diverge from the bash form (see ZephyrFrame).
        framed = self._frame.frame(cmd, self._markers)
        # Truncate for the log so a multi-line script doesn't dominate the
        # output. The frame seam is the natural call site to instrument once —
        # every dialect then gets the visibility for free.
        shown = (
            framed
            if len(framed) <= _LOG_PREVIEW_FRAMED
            else f"{framed[:_LOG_PREVIEW_FRAMED]}...({len(framed)} bytes total)"
        )
        if redact:
            logger.debug(
                f"{self._log_tag}: framed write cmd=<redacted> "
                f"payload=<redacted {len(framed)} bytes>"
            )
        else:
            logger.debug(f"{self._log_tag}: framed write cmd={cmd!r} payload={shown!r}")
        self._write_progress = write_progress
        try:
            await self._write(framed)
        finally:
            self._write_progress = None

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
                if live and seen_begin:
                    pre = data[: end_match.start()].replace("\r", "").strip()
                    if pre:
                        on_output(pre)
                break

            # An expect pattern matched — find which one and send its response
            expect_matched = False
            if expects:
                for pat_str, response in expects:
                    pat = re.compile(pat_str) if isinstance(pat_str, str) else pat_str
                    if pat.search(data):
                        await self._write(response)
                        logger.debug(
                            f"{self._log_tag}: expect matched "
                            f"pattern={getattr(pat, 'pattern', pat)!r} "
                            f"response={response!r}"
                        )
                        expect_matched = True
                        break

            # Stream each content line to the callback.
            # The begin marker check uses rstrip + startswith to avoid
            # false matches when the shell echoes the wrapped command
            # (which embeds the marker inside quotes on the same line).
            if not expect_matched:
                if not seen_begin:
                    if self._frame.marks_begin(data, self._markers):
                        if not redact:
                            logger.debug(f"{self._log_tag}: begin marker matched on chunk={data!r}")
                        seen_begin = True
                elif live:
                    line = data.rstrip("\r\n").replace("\r", "")
                    if line:
                        on_output(line)

        output = self._frame.parse_output(buffer, cmd, self._markers)
        retcode = self._frame.extract_retcode(buffer, self._markers)
        status = Status.Success if retcode == 0 else Status.Failed
        # Buffered frames (raw stream not clean line-by-line) emit the parsed
        # output once here — identical to the returned CommandResult.value, so
        # the log never shows shell prompts or the retcode scaffolding.
        if not live and output:
            on_output(output)
        # Log a per-command summary at the seam. The full buffer is dumped at
        # DEBUG so a future-dialect bring-up can see exactly what the frame's
        # extract_retcode / parse_output had to work with.
        buffer_preview = (
            buffer
            if len(buffer) <= _LOG_PREVIEW_BUFFER
            else f"{buffer[:_LOG_PREVIEW_HANDSHAKE]}...({len(buffer)}b)...{buffer[-_LOG_PREVIEW_HANDSHAKE:]}"  # noqa: E501 — long f-string with buffer slices
        )
        if redact:
            logger.debug(
                f"{self._log_tag}: run_cmd done cmd=<redacted> retcode={retcode} "
                f"output_len={len(output)} buffer=<redacted {len(buffer)} bytes>"
            )
        else:
            logger.debug(
                f"{self._log_tag}: run_cmd done cmd={cmd!r} retcode={retcode} "
                f"output_len={len(output)} buffer={buffer_preview!r}"
            )
        return CommandResult(status=status, value=output, command=cmd, retcode=retcode)

    def _build_combined_pattern(
        self,
        expects: list[Expect] | None,
    ) -> re.Pattern[str]:
        r"""Build a combined regex: expect patterns | end sentinel | newline.

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

    async def _recover_session(self) -> str:
        """Attempt session recovery after timeout: Ctrl+C, then recovery sentinel.

        Returns any partial output captured during recovery.
        Sets self._alive = False if recovery fails.
        """
        recover_cmd = self._frame.recover(self._markers)
        logger.debug(
            f"{self._log_tag}: recover_session entry "
            f"marker={self._recover_marker!r} cmd={recover_cmd!r}"
        )
        try:
            # Send Ctrl+C (SIGINT) to interrupt the hung foreground process
            await self._write("\x03")
            await asyncio.sleep(0.1)

            # Send recovery sentinel to re-synchronize
            await self._write(recover_cmd)
            data = await asyncio.wait_for(
                self._read_until_pattern(re.compile(re.escape(self._recover_marker))),
                timeout=_RECOVERY_TIMEOUT,
            )
            # Session is recovered and usable for the next command
            partial = data.split(self._recover_marker)[0].strip()
            logger.debug(f"{self._log_tag}: recover_session ok partial_len={len(partial)}")

        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            # Shell itself is unresponsive — mark session as dead
            logger.debug(
                f"{self._log_tag}: recover_session failed ({type(exc).__name__}); "
                f"session marked dead"
            )
            self._alive = False
            return ""
        else:
            return partial


class SshSession(ShellSession):
    """SSH persistent shell session via asyncssh create_process()."""

    def __init__(
        self,
        conn: SSHClientConnection | None,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
    ) -> None:
        super().__init__(command_frame=command_frame, init_timeout=init_timeout)
        self._conn = conn
        self._process: Any = None
        # When set by a subclass, _open passes this as the command to
        # create_process() instead of opening the channel's default shell.
        self._open_cmd: str | None = None

    @override
    async def _open(self) -> None:
        import asyncssh

        assert self._conn is not None, "SshSession._conn must be set before _open()"  # noqa: S101 — internal invariant: _conn set by subclass before _open()
        if self._open_cmd is not None:
            self._process = await self._conn.create_process(
                self._open_cmd,
                term_type="dumb",
                stderr=asyncssh.STDOUT,
            )
        else:
            self._process = await self._conn.create_process(
                term_type="dumb",
                stderr=asyncssh.STDOUT,
            )

    @override
    async def _write(self, data: str) -> None:
        assert self._process is not None  # noqa: S101 — internal invariant: _open() must run before _write()
        self._process.stdin.write(data)

    @override
    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        assert self._process is not None  # noqa: S101 — internal invariant: _open() must run before _read_until_pattern()
        return await self._process.stdout.readuntil(pattern, _MAX_SEPARATOR_LEN)

    @override
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
        _owned_client: "TelnetClient | None" = None,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        write_chunk_size: int = 0,
        write_chunk_delay: float = 0.0,
    ) -> None:
        super().__init__(command_frame=command_frame, init_timeout=init_timeout)
        self._reader = reader
        self._writer = writer
        self._owned_client = _owned_client
        # Paced-write tuning for slow/RX-limited consoles. ``write_chunk_size``
        # of 0 (the default) writes each payload in a single call — correct for
        # a host-terminated telnet shell (e.g. x86 + E1000). A positive value
        # splits the payload into <=N-byte writes spaced by ``write_chunk_delay``
        # seconds so a UART-backed RTOS shell (e.g. a Zephyr ``-serial telnet:``
        # bridge) doesn't overrun its console RX FIFO on a multi-KB
        # ``llext load_hex`` line. Set per-host via ``telnet_options``.
        self._write_chunk_size = write_chunk_size
        self._write_chunk_delay = write_chunk_delay

    @override
    async def _open(self) -> None:
        # Transport already established by TelnetClient login — nothing to open
        pass

    @override
    async def _write(self, data: str) -> None:
        # Use CR (\r) as the sole line terminator. Sending \r\n causes two
        # inputs: the \r executes the command in readline raw mode, and the
        # trailing \n triggers an extra empty prompt (e.g. an extra ">>> " in
        # Python REPL). This stale prompt then matches the next expect() call
        # before the real output arrives.  Using \r alone works for both:
        # - canonical mode (icrnl maps \r → \n, so the shell sees one newline)
        # - readline raw mode (treats \r as Enter / execute)
        data = re.sub(r"\r?\n", "\r", data)
        encoded = data.encode()
        total = len(encoded)
        chunk = self._write_chunk_size
        if chunk and total > chunk:
            for i in range(0, total, chunk):
                self._writer.write(encoded[i : i + chunk])
                if self._write_progress is not None:
                    self._write_progress(min(i + chunk, total), total)
                if self._write_chunk_delay:
                    await asyncio.sleep(self._write_chunk_delay)
        else:
            self._writer.write(encoded)
            if self._write_progress is not None:
                self._write_progress(total, total)

    @override
    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        # telnetlib3 operates in bytes mode — compile a bytes version of the pattern
        bytes_pattern = re.compile(pattern.pattern.encode())
        raw: bytes = await self._reader.readuntil_pattern(bytes_pattern)  # type: ignore[attr-defined]
        return raw.decode("utf-8", errors="replace")

    @override
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

    @override
    async def _open(self) -> None:
        # Drive loop.subprocess_exec() directly (rather than the higher-level
        # asyncio.create_subprocess_exec) so that we hold an explicit reference
        # to the transport. close() uses it to release the pipe fds without
        # reaching into the private Process._transport attribute.
        loop = asyncio.get_running_loop()

        def protocol_factory() -> asyncio.subprocess.SubprocessStreamProtocol:
            return asyncio.subprocess.SubprocessStreamProtocol(
                limit=2**16,
                loop=loop,
            )

        transport, protocol = await loop.subprocess_exec(
            protocol_factory,
            "bash",
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._transport = transport
        self._process = asyncio.subprocess.Process(transport, protocol, loop)
        self._pid = self._process.pid

    @override
    async def _write(self, data: str) -> None:
        assert self._process is not None  # noqa: S101 — internal invariant: process created in _open() before _write()
        assert self._process.stdin is not None  # noqa: S101 — internal invariant: process created in _open() before _write()
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

    @override
    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        assert self._process is not None  # noqa: S101 — internal invariant: process created in _open() before _read_until_pattern()
        assert self._process.stdout is not None  # noqa: S101 — internal invariant: process created in _open() before _read_until_pattern()
        buf = ""
        while True:
            chunk = await self._process.stdout.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(buf.encode(), None)
            buf += chunk.decode("utf-8", errors="replace")
            if pattern.search(buf):
                return buf

    @override
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
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    ppid_line = (entry / "stat").read_text().split()
                    # Field 4 (0-indexed: 3) is PPID
                    if int(ppid_line[3]) == parent_pid:
                        os.kill(int(entry.name), sig)
                except (
                    IndexError,
                    ValueError,
                    FileNotFoundError,
                    ProcessLookupError,
                    PermissionError,
                ):
                    continue
        except (FileNotFoundError, PermissionError):
            pass

    @override
    async def close(self) -> None:
        if self._process is not None:
            if self._process.returncode is None:
                # Process still running — try graceful exit
                if self._process.stdin is not None:
                    try:
                        self._process.stdin.write(b"exit\n")
                        await self._process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                with suppress(ProcessLookupError):
                    self._process.terminate()
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


class _DockerSshSession(SshSession):
    r"""Persistent shell inside a docker container, reached via the parent's SSH conn.

    Wraps ``docker exec -it <container_id> sh`` on top of a multiplexed channel of
    the parent's existing :class:`SSHClientConnection`. ``-it`` allocates a TTY
    for the container so that ``\\x03`` sent on timeout is delivered as SIGINT
    to the in-container foreground process — recovery semantics match plain
    :class:`SshSession`.

    The container id is resolved lazily at session-open time so that hosts
    constructed with a placeholder ``container_id=""`` (declared but not yet
    up) work correctly once :meth:`DockerContainerHost._ensure_running`
    populates the id.
    """

    def __init__(
        self,
        conn_provider: Callable[[], Awaitable[SSHClientConnection]],
        container_id_getter: Callable[[], str],
    ) -> None:
        super().__init__(conn=None)
        self._conn_provider = conn_provider
        self._cid_getter = container_id_getter

    @override
    async def _open(self) -> None:
        import shlex

        self._conn = await self._conn_provider()
        cid = self._cid_getter()
        if not cid:
            raise RuntimeError(
                "DockerSshSession opened with empty container_id — "
                "DockerContainerHost._ensure_running must populate the id first."
            )
        self._open_cmd = f"docker exec -it {shlex.quote(cid)} sh"
        await super()._open()


class HostSession:
    """A named persistent shell session on any host type.

    Obtained via ``await host.open_session(name)``. Supports the async context
    manager protocol for automatic cleanup.

    Example::

        async with await host.open_session("monitor") as mon:
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
        log_command: Callable[[str, LogMode], None],
        log_output: Callable[[str, LogMode], None],
        deregister: Callable[[str], None],
        user_password: "Callable[[str], str | None] | None" = None,
    ) -> None:
        self._name = name
        self._session = session
        self._log_command = log_command
        self._log_output = log_output
        self._deregister = deregister
        # Resolver for su-target passwords (creds-based). None on non-posix
        # hosts → this session cannot elevate. Set by SessionManager.
        self._user_password = user_password

    @property
    def alive(self) -> bool:
        """Whether the underlying shell session is still active."""
        return self._session.alive

    @property
    def current_user(self) -> str:
        """User this named session is currently running as.

        Seeded from the host's login user; changed only via
        :meth:`switch_user` / :meth:`as_user`.
        """
        return self._session.current_user

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """``su`` *this* session to *user* (default root), tracking :attr:`current_user`.

        Posix-only — raises ``NotImplementedError`` on
        hosts whose sessions do not support elevation (no password resolver).
        """
        if self._user_password is None:
            raise NotImplementedError("switch_user is not supported on this host's sessions")
        from .privilege import _perform_su

        target = await _perform_su(self.send, self.expect, user, password, self._user_password)
        self._session.current_user = target

    @asynccontextmanager
    async def as_user(
        self, user: str = "root", password: str | None = None
    ) -> "AsyncIterator[HostSession]":
        """Run a block as *user* on this session, restoring the prior user on exit."""
        prev = self.current_user
        await self.switch_user(user, password)
        try:
            yield self
        finally:
            await self.send("exit\n")
            self._session.current_user = prev

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: LogMode = LogMode.NORMAL,
    ) -> Results:
        """Execute one or more commands on this named session.

        Mirrors :meth:`~otto.host.host.Host.run`: accepts a ``str``, a
        :class:`~otto.host.host.ShellCommand`, or a sequence mixing the two, and always returns a
        :class:`~otto.result.Results`. Per-command ``expects`` / ``timeout`` on a
        :class:`~otto.host.host.ShellCommand` override the run-level
        defaults; a scalar ``Expect`` tuple at the run level is normalized to a
        one-element list.
        """
        from .host import _normalize_expects, _resolve_command, _run_cmds_with_budget

        default_expects = _normalize_expects(expects)

        async def _run_sc(sc: ShellCommand, t: float | None) -> CommandResult:
            # _resolve_command collapsed the None sentinel into a concrete LogMode.
            mode = sc.log if sc.log is not None else LogMode.NORMAL
            if mode is not LogMode.NEVER:
                self._log_command(sc.cmd, mode)
            return await self._session.run_cmd(
                sc.cmd,
                expects=_normalize_expects(sc.expects),
                timeout=t,
                on_output=_sink_for(self._log_output, mode),
                redact=mode is LogMode.NEVER,
            )

        if isinstance(cmds, (str, ShellCommand)):
            sc = _resolve_command(cmds, default_expects, timeout, log)
            result = await _run_sc(sc, sc.timeout)
            return Results.collect([result])

        resolved = [_resolve_command(c, default_expects, None, log) for c in cmds]
        return await _run_cmds_with_budget(_run_sc, resolved, timeout)

    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to this session's stdin. See :meth:`~otto.host.unix_host.UnixHost.send`."""
        mode = log
        if mode is not LogMode.NEVER:
            self._log_command(text.rstrip(), mode)
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in this session's output. See :meth:`~otto.host.unix_host.UnixHost.expect`."""  # noqa: E501 — Sphinx xref
        result = await self._session.expect(pattern, timeout)
        self._log_output(result, LogMode.NORMAL)
        return result

    async def close(self) -> None:
        """Close this session and remove it from the host's session registry."""
        await self._session.close()
        self._deregister(self._name)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class SessionManager:
    """Manages persistent shell sessions for any host type.

    Owns the default session (used by ``run_cmd``/``send``/``expect``) and all
    named sessions (created via ``open_session``).

    Session creation is pluggable: provide a ``session_factory`` callable to
    control how sessions are created (e.g. ``LocalSession`` for local hosts),
    or pass a ``ConnectionManager`` to use the default SSH/Telnet dispatch.
    Similarly, ``oneshot_factory`` controls stateless command execution. The
    shell *dialect* is selected by ``command_frame`` (default bash; an embedded
    host passes a :class:`~otto.host.command_frame.ZephyrFrame`) — it is handed
    to every session this manager builds, independent of the transport. A slow
    target's readiness ceiling is raised via ``init_timeout``.
    """

    def __init__(
        self,
        connections: "ConnectionManager | None" = None,
        name: str = "",
        log_command: Callable[[str, LogMode], None] = lambda *_: None,
        log_output: Callable[[str, LogMode], None] = lambda *_: None,
        session_factory: "Callable[[], ShellSession] | None" = None,
        oneshot_factory: "Callable[[str, float | None], Awaitable[CommandResult]] | None" = None,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        retry_backoff: float | None = None,
        user_password: "Callable[[str], str | None] | None" = None,
    ) -> None:
        self._connections = connections
        self._name = name
        self._log_command = log_command
        self._log_output = log_output
        self._session_factory = session_factory
        self._oneshot_factory = oneshot_factory
        # Resolver for su-target passwords, forwarded to HostSessions so named
        # sessions can elevate. None on non-posix hosts → named-session
        # elevation is unsupported there.
        self._user_password = user_password
        # Shell dialect handed to every session built on the ConnectionManager
        # dispatch path (SSH or telnet alike). ``None`` resolves to bash inside
        # ``ShellSession``; an embedded host passes a ZephyrFrame so its
        # sessions speak the RTOS shell's framing. Decoupled from the transport,
        # so e.g. Zephyr-framing-over-SSH needs no new session class.
        self._command_frame = command_frame
        # Optional readiness-handshake ceiling for slow shells (e.g. a Zephyr
        # QEMU telnet console); ``None`` keeps the session's class default.
        self._init_timeout = init_timeout
        # Pause before the single handshake retry in ``_ensure_session``. ``None``
        # resolves to the production default; tests pass ``0`` to skip the real
        # wall-clock wait without changing the retry logic itself.
        self._retry_backoff = _HANDSHAKE_RETRY_BACKOFF if retry_backoff is None else retry_backoff
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
        # Serializes the get-or-create paths so concurrent callers can't all
        # observe a dead/missing session, all close it, and all create
        # replacements that clobber each other (each clobber leaks the prior).
        self._ensure_session_lock = asyncio.Lock()
        # One lock *per session name* rather than a single shared lock.  The
        # get-or-create path only needs to dedupe callers requesting the *same*
        # name; a shared lock additionally serializes the (slow, ~1-2 s telnet)
        # connect of callers requesting *different* names — which collapses the
        # oneshot pool's concurrency back to serial.  Keyed locks let distinct
        # names (notably the unique `__oneshot_pool_N__` names) connect in
        # parallel while same-name callers still resolve to one session.
        self._named_session_locks: dict[str, asyncio.Lock] = {}

    @property
    def has_live_sessions(self) -> bool:
        """Whether any session (default or named) is currently alive."""
        if self._session and self._session.alive:
            return True
        return any(s.alive for s in self._named_sessions.values())

    def _login_user(self) -> str:
        """Return the host's login username, or '' when loginless / no creds.

        Best-effort: session seeding runs this on every build, so it tolerates
        a connection manager that exposes no ``credentials`` (e.g. minimal test
        fakes or a loginless transport) by falling back to ''.
        """
        creds = getattr(self._connections, "credentials", None)
        if not creds:
            return ""
        return creds[0]

    def _seed_user(self, session: "ShellSession") -> None:
        """Stamp a freshly built session with the login user."""
        session.current_user = self._login_user()

    @property
    def current_user(self) -> str:
        """User the default session is currently running as.

        Seeded from the login user; changed only via switch_user/as_user.
        Falls back to the login user before the default session is built.
        """
        if self._session is not None:
            return self._session.current_user
        return self._login_user()

    def _set_current_user(self, user: str) -> None:
        """Private bookkeeping for the default session.

        Called only by the elevation flow (PosixPrivilege.switch_user/as_user) after a real
        ``su`` has run — never a public API (that would let callers desync
        the tracked user from the shell's actual user).
        """
        if self._session is not None:
            self._session.current_user = user

    async def _ensure_session(self) -> None:
        """Create a ShellSession if one doesn't exist or if the current one is dead.

        Serialized via ``_ensure_session_lock`` with a double-checked-locking
        pattern: the fast path returns without lock acquisition when the
        existing session is alive; the slow path takes the lock and re-checks
        before recreating, so concurrent callers cannot all create replacement
        sessions that clobber each other.

        Eagerly runs ``_ensure_initialized`` inside the lock so the new
        session's ``alive`` flag is True before the lock is released — this
        prevents a follow-on caller from observing the just-created session
        as ``alive=False`` (because the handshake hasn't run yet) and falling
        through to recreate.

        A failed readiness handshake (``ConnectionError`` from ``_fail_init``)
        is retried **once** with a brief backoff. The failure mode this
        addresses: concurrent fan-out across multiple embedded targets
        sharing a single SSH hop (e.g. ``do_for_all_hosts(EmbeddedHost.put,
        …)`` to several Zephyr boards over one ``basil_seed`` hop) can land
        a fresh telnet socket on a device whose console isn't quite ready —
        the peer accepts the TCP connection then closes it before the
        marker probe lands, producing ``IncompleteReadError(0 bytes)`` →
        ``ConnectionError``. Rebuilding the transport (the closed session's
        teardown drops the stale ``TelnetClient``; ``connections.telnet()``
        re-opens cleanly) and retrying once recovers from the race without
        masking a genuine misconfiguration: a real "device unresponsive /
        bad credentials" failure will fail the same way on the second
        attempt and propagate.
        """
        if self._session and self._session.alive:
            return

        async with self._ensure_session_lock:
            # Re-check after acquiring the lock — another task may have
            # created the session while we waited.
            if self._session and self._session.alive:
                return

            # Close the old dead session to release its subprocess transport.
            # Without this, the orphaned transport is GC'd after the event loop
            # closes and raises "RuntimeError: Event loop is closed" from __del__.
            if self._session is not None:
                await self._session.close()

            last_exc: ConnectionError | None = None
            for attempt in range(2):
                new_session = await self._build_session()
                new_session._on_output = _sink_for(self._log_output, LogMode.NORMAL)  # noqa: SLF001 — intra-package wiring of output callback on freshly-built ShellSession
                self._seed_user(new_session)
                # The marker handshake can take ~1 s on a cold telnet open. A
                # caller-side ``wait_for`` cancellation (or a failed login)
                # landing here would otherwise drop the just-built session — and
                # its open transport FD — on the floor. Close it before the
                # exception propagates.
                try:
                    await new_session._ensure_initialized()  # noqa: SLF001 — intra-package access to ShellSession._ensure_initialized for handshake
                except ConnectionError as exc:
                    with suppress(Exception):  # pragma: no cover - best-effort cleanup
                        await new_session.close()
                    last_exc = exc
                    if attempt == 0:
                        logger.debug(
                            f"SessionManager[{self._name}]: handshake failed "
                            f"on first attempt ({exc!r}); rebuilding transport "
                            f"and retrying once"
                        )
                        # Backoff lets the peer fully release any half-open
                        # slot before the next telnet() rebuilds the TCP
                        # connection. The default (``_HANDSHAKE_RETRY_BACKOFF``,
                        # 2 s) is calibrated to single-client RTOS telnet servers
                        # (Zephyr's ``CONFIG_SHELL_BACKEND_TELNET``) which do not
                        # always free the slot the instant the FIN lands —
                        # observed on live QEMU runs to take well over 500 ms
                        # after the close. Injectable so fakes-only tests skip it.
                        await asyncio.sleep(self._retry_backoff)
                        continue
                    raise
                except BaseException:
                    with suppress(Exception):  # pragma: no cover - best-effort cleanup
                        await new_session.close()
                    raise
                self._session = new_session
                break
            else:  # pragma: no cover - the loop always breaks or raises
                assert last_exc is not None  # noqa: S101 — internal invariant: for-else only reached when loop ran at least once
                raise last_exc

    async def _build_session(self) -> "ShellSession":
        """Construct a fresh ShellSession from the configured factory or connection manager.

        Split out from ``_ensure_session`` so the retry path can rebuild the transport cleanly.
        """
        if self._session_factory is not None:
            return self._session_factory()
        assert self._connections is not None  # noqa: S101 — internal invariant: _connections required when no session_factory
        match self._connections.term:
            case "ssh":
                ssh_conn = await self._connections.ssh()
                return SshSession(
                    ssh_conn,
                    command_frame=self._command_frame,
                    init_timeout=self._init_timeout,
                )
            case "telnet":
                telnet_conn = await self._connections.telnet()
                logger.debug(
                    f"SessionManager[{self._name}]: building telnet session "
                    f"with frame={type(self._command_frame).__name__}"
                )
                return TelnetSession(
                    telnet_conn.reader,
                    telnet_conn.writer,
                    command_frame=self._command_frame,
                    init_timeout=self._init_timeout,
                    write_chunk_size=telnet_conn.options.write_chunk_size,
                    write_chunk_delay=telnet_conn.options.write_chunk_delay,
                )
            case _:
                raise ValueError(
                    f'{self._name}: unsupported terminal type "{self._connections.term}"'
                )

    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: LogMode = LogMode.NORMAL,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandResult:
        """Run *cmd* on the default session, creating it if needed.

        Wraps :meth:`~otto.host.session.ShellSession.run_cmd` with automatic
        session creation. Shell state persists across calls (same session is
        reused until it dies).
        """
        await self._ensure_session()
        mode = log
        if mode is not LogMode.NEVER:
            self._log_command(cmd, mode)
        assert self._session is not None  # noqa: S101 — internal invariant: _ensure_session() always sets _session or raises
        return await self._session.run_cmd(
            cmd,
            expects=expects,
            timeout=timeout,
            on_output=_sink_for(self._log_output, mode),
            redact=mode is LogMode.NEVER,
            write_progress=write_progress,
        )

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run *cmd* without sharing state with the default session.

        Concurrent calls are safe: for SSH each call opens an independent
        channel on the existing connection; for Telnet (which has no stateless
        exec primitive) an idle session is pulled from an internal free-list or
        a new one is opened, preserving the independence contract without the
        overhead of a fresh TCP + auth round-trip per call.
        """
        if self._oneshot_factory is not None:
            return await self._oneshot_factory(cmd, timeout)

        assert self._connections is not None  # noqa: S101 — internal invariant: _connections required when no oneshot_factory
        mode = log
        if mode is not LogMode.NEVER:
            self._log_command(cmd, mode)
        match self._connections.term:
            case "ssh":
                import asyncssh

                ssh_conn = await self._connections.ssh()
                process = await ssh_conn.create_process(
                    cmd,
                    stderr=asyncssh.STDOUT,
                    stdin=asyncssh.DEVNULL,
                )
                lines: list[str] = []
                try:
                    async for raw_line in process.stdout:
                        line = raw_line.rstrip("\n")
                        lines.append(line)
                        if mode is not LogMode.NEVER:
                            self._log_output(line, mode)
                except asyncio.TimeoutError:
                    process.terminate()
                result = await process.wait()
                status = Status.Success if result.exit_status == 0 else Status.Failed
                return CommandResult(
                    status=status,
                    value="\n".join(lines),
                    command=cmd,
                    retcode=result.exit_status or 0,
                )
            case "telnet":
                # Telnet has no stateless exec primitive (unlike SSH which
                # multiplexes channels over one connection).  Rather than open
                # a fresh TCP+auth for every oneshot call — which in practice cost
                # 1-2 s each on real hardware — we keep a free-list of idle
                # persistent sessions and reuse them.  Serial callers churn
                # one session; concurrent callers (e.g. `_put_files_nc`
                # launching multiple `nc -l` listeners in parallel) each get
                # their own, preserving the documented concurrency contract.
                oneshot_session = await self._acquire_oneshot_session()
                try:
                    return (await oneshot_session.run(cmd, timeout=timeout, log=log)).only
                finally:
                    self._oneshot_pool.append(oneshot_session)
            case _:
                raise ValueError(
                    f'{self._name}: unsupported terminal type "{self._connections.term}"'
                )

    async def _acquire_oneshot_session(self) -> "HostSession":
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
        return await self.open_session(f"__oneshot_pool_{self._oneshot_pool_count}__")

    async def open_session(self, name: str) -> "HostSession":
        """Open or reuse a named persistent shell session.

        Serialized via a per-name lock with a double-checked-locking
        pattern: concurrent callers requesting the same name resolve to a
        single underlying session rather than each creating their own and
        clobbering the dict.  Callers requesting *different* names take
        different locks and so connect concurrently.

        Eagerly runs ``_ensure_initialized`` inside the lock so the stored
        ``HostSession.alive`` is True on return — this prevents follow-on
        callers from observing a just-created (un-handshaken) session as
        dead and recreating it.
        """
        existing = self._named_sessions.get(name)
        if existing and existing.alive:
            return existing

        # ``setdefault`` is atomic here — no ``await`` between lookup and use —
        # so concurrent callers for the same name share one lock instance.
        lock = self._named_session_locks.setdefault(name, asyncio.Lock())
        async with lock:
            existing = self._named_sessions.get(name)
            if existing and existing.alive:
                return existing

            # Close the underlying transport of the dead entry (if any) but
            # leave the dict slot intact — we'll overwrite it below.  Calling
            # existing.close() here would deregister via the close-callback
            # and remove the slot, opening a tiny window where _named_sessions
            # has no entry for this name.
            if existing is not None:
                await existing._session.close()  # noqa: SLF001 — intra-package access to HostSession._session to close dead transport

            if self._session_factory is not None:
                shell_session: ShellSession = self._session_factory()
            else:
                assert self._connections is not None  # noqa: S101 — internal invariant: _connections required when no session_factory
                match self._connections.term:
                    case "ssh":
                        ssh_conn = await self._connections.ssh()
                        shell_session = SshSession(
                            ssh_conn,
                            command_frame=self._command_frame,
                            init_timeout=self._init_timeout,
                        )
                    case "telnet":
                        user, password = self._connections.credentials
                        client = TelnetClient(
                            self._connections.ip,
                            user=user,
                            password=password,
                            options=self._connections.telnet_options,
                        )
                        # A caller-side ``wait_for`` cancellation can land
                        # anywhere in ``connect()`` (TCP, ECHO negotiation,
                        # credential exchange) and would otherwise drop
                        # ``client`` on the floor with its socket still open.
                        # Tear down on any exception (including CancelledError)
                        # so the FD is released. The marker handshake that
                        # follows is guarded separately, below.
                        try:
                            await client.connect()
                        except BaseException:
                            with suppress(Exception):
                                await client.close()
                            raise
                        shell_session = TelnetSession(
                            client.reader,
                            client.writer,
                            _owned_client=client,
                            command_frame=self._command_frame,
                            init_timeout=self._init_timeout,
                            write_chunk_size=client.options.write_chunk_size,
                            write_chunk_delay=client.options.write_chunk_delay,
                        )
                    case _:
                        raise ValueError(
                            f'{self._name}: unsupported terminal type "{self._connections.term}"'
                        )

            shell_session._on_output = _sink_for(self._log_output, LogMode.NORMAL)  # noqa: SLF001 — intra-package wiring of output callback on freshly-built ShellSession
            self._seed_user(shell_session)
            # The marker handshake can take ~1 s on a cold telnet open (the
            # slow window that used to live inside ``connect()``'s login
            # drain). A cancellation or failed login landing here must not
            # orphan the just-built session — for telnet that would leak the
            # owned client's socket and skip the session's cleanup duties.
            try:
                await shell_session._ensure_initialized()  # noqa: SLF001 — intra-package access to ShellSession._ensure_initialized for handshake
            except BaseException:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await shell_session.close()
                raise

            host_session = HostSession(
                name=name,
                session=shell_session,
                log_command=self._log_command,
                log_output=self._log_output,
                deregister=lambda n: (self._named_sessions.pop(n, None), None)[1],
                user_password=self._user_password,
            )
            self._named_sessions[name] = host_session
            return host_session

    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the default session, creating it if needed."""
        await self._ensure_session()
        mode = log
        if mode is not LogMode.NEVER:
            self._log_command(text.rstrip(), mode)
        assert self._session is not None  # noqa: S101 — internal invariant: _ensure_session() always sets _session or raises
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for *pattern* in the default session's output, creating the session if needed."""
        await self._ensure_session()
        assert self._session is not None  # noqa: S101 — internal invariant: _ensure_session() always sets _session or raises
        result = await self._session.expect(pattern, timeout)
        self._log_output(result, LogMode.NORMAL)
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

        # Drop the per-name locks too. close_all() is a teardown point with
        # no concurrent open_session() in flight, so clearing here is race-free
        # (unlike per-session reclamation) and caps lock-dict growth at the set
        # of names seen since the last close_all rather than process lifetime.
        self._named_session_locks.clear()
