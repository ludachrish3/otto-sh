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
import dataclasses
import math
import os
import re
import secrets
import uuid
import warnings
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from typing_extensions import Never, Self, override

from ..errors import OttoError
from ..models.base import Cred
from .cancel_safe import result_within
from .command_frame import (
    AshFrame,
    BashFrame,
    CommandFrame,
    ExecInput,
    ExecRestore,
    RawFrame,
    SessionMarkers,
    begin_line_ending,
    command_value,
    frame_or_default,
    handshake_prefix,
    shell_prefix,
)
from .errors import (
    ConsoleError,
    PasswordRefusedError,
    RawLandingError,
    SessionSetupError,
    UntypableCommandError,
)
from .identity import ROOT_LOGIN
from .limitation import Limitation
from .login_proxy import (
    LoginProxyError,
    cred_for,
    prove_identity,
    run_proxy,
    run_undo,
    switch_and_stamp,
)
from .output_codec import (
    MAX_SEPARATOR_LEN,
    OUTPUT_ENCODING,
    READ_CHUNK,
    PatternReader,
    bounded_pattern,
    finish_character,
    is_bounded,
    output_decoder,
)
from .secret_wire import reveal, shown
from .shell_liveness import confirm_live
from .telnet import (
    TELNET_EOL,
    ReadGate,
    ReadGateClosed,
    TelnetClient,
    retries_failed_open,
    telnet_line_ends,
)
from .terminal_mapping import (
    ONLCR,
    UNMEASURED,
    Measurement,
    is_unknown,
    measure,
    reported,
    saved_settings,
)
from .userland import ASH_ECHOING_LINE_MAX, PTY_TYPED_LINE_MAX, gap_for, refuse_if_gapped

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from .connections import ConnectionManager
    from .console import ConsoleClient

    # Annotations only. `otto.host.session_setup` must stay OFF the CLI
    # startup import graph and this module is on it, so the runtime import
    # lives inside `SessionManager._apply_session_setup`, after the no-hook
    # early return — see that method and its row in
    # tests/unit/test_import_contracts.py.
    from .session_setup import SessionSetup, SetupKind

import logging

from typing_extensions import final

from ..logger.mode import ExecLogContext, LogMode
from ..logger.relay import log_lines
from ..result import CommandNotRunError, CommandResult, Results
from ..utils import Status
from . import process_stop
from .host import (
    DEFAULT_COMMAND_TIMEOUT,
    ContextClaims,
    CredentialExpect,
    Expect,
    ShellCommand,
    _announce_dry_send,
    is_dry_run,
    refuse_declined_elevation,
    refuse_declined_match,
)

logger = logging.getLogger(__name__)

_UNMEASURED = Measurement(UNMEASURED, calibrated=False)
"""A session no handshake has measured (or one whose dialect has no calibration line)."""


class _CredentialRefusedError(Exception):
    """A :class:`~otto.host.host.CredentialExpect` matched again after its one answer.

    Raised inside the command's read loop and turned into
    :exc:`~otto.host.errors.PasswordRefusedError` by
    :meth:`ShellSession.run_cmd`, which recovers the shell first, outside the
    command's timeout.
    """

    def __init__(self, expect: CredentialExpect) -> None:
        super().__init__(expect.pattern)
        self.expect = expect


# Hex characters of ``uuid.uuid4()`` that become a session id, and so the
# length of every marker built from it. Named because a second reader depends
# on it: ``refuse_if_typed_line_would_be_cut`` has to size otto's own
# framing BEFORE any session exists, and it does that by rendering the frame
# against a synthetic marker set of exactly this shape. Pinned against a real
# session's markers by ``tests/unit/host/test_run_line_length.py``.
_SESSION_ID_LEN = 12

# Log-preview truncation limits to keep debug output readable.
_LOG_PREVIEW_FRAMED = 256  # max chars of a framed command payload in the log
_LOG_PREVIEW_HANDSHAKE = 512  # max chars of handshake data shown in the log
_LOG_PREVIEW_BUFFER = 1024  # buffer length above which run_cmd shows head+tail excerpt

# Timeout for session recovery after Ctrl+C
_RECOVERY_TIMEOUT = 5.0

_EXEC_POOL_PREFIX = "__exec_pool_"
"""The name prefix of the exec pool's sessions (``SessionManager._open_exec_session``)."""


# Per-probe wait inside the recovery resend loop (confirm_live).
_RECOVERY_PROBE_TIMEOUT = 0.5

# Pause between the interrupt and the first recovery probe, so the shell has
# taken the interrupt before it is asked whether it is back.
_RECOVERY_SETTLE = 0.1

# How long an idle local shell is given to exit on the end of its input
# (LocalSession.end_input) before its close stops its process group. A shell
# between commands reads the end at once; this bounds only one that does not.
_LOCAL_EOF_EXIT_WAIT = 2.0

# Ceiling on the post-open marker handshake (ShellSession._handshake, whose
# ``timeout`` argument defaults to it and can override it per call — see
# ShellSession.enter_frame). A Unix host sets it per host through its
# ``init_timeout`` field, and OTTO_INIT_TIMEOUT replaces this default for
# every host without one (see resolve_init_timeout). Over telnet the window opens
# the moment the password is written, so it covers the peer's PAM check,
# login(1), the MOTD and the shell's startup files, all read back through
# telnetlib3 on a client that may itself be busy: 3 s was missed on a loaded
# nested-virtualisation lab (#732) while 10 s leaves room for that. Still
# bounded, so a failed telnet login — where the READY marker can never
# appear — surfaces as a clear error instead of hanging indefinitely.
_INIT_TIMEOUT = 10.0

INIT_TIMEOUT_ENV_VAR = "OTTO_INIT_TIMEOUT"
"""Seconds that replace every host family's readiness default; read by
:func:`resolve_init_timeout`."""

# INIT_TIMEOUT_ENV_VAR replaces the readiness default of every host family:
# bash's 10 s and an embedded host's 15 s alike; only a host's own
# ``init_timeout`` field beats it. For a machine that runs otto slowly, where
# every host needs the same room: read straight from the environment when a
# session is built, never through the settings model, so the host layer
# pulls in nothing to read it.


def resolve_init_timeout(
    explicit: float | None, family_default: float | None = None
) -> float | None:
    """Return the readiness deadline a session gets.

    *explicit* (a host's own ``init_timeout``), else ``OTTO_INIT_TIMEOUT``,
    else *family_default* (an embedded host's 15 s). ``None`` when none is
    set, meaning the class default (``ShellSession._init_timeout``, 10 s). An
    empty variable counts as unset; any other value must be a finite number of
    seconds greater than 0, as the host field requires, and anything else
    raises :class:`~otto.errors.OttoError` naming the variable rather than
    falling back silently.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(INIT_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return family_default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise OttoError(f"{INIT_TIMEOUT_ENV_VAR}={raw!r} is not a number of seconds greater than 0")
    return value


# Default pause between a failed first readiness handshake and the single retry
# in ``SessionManager._open_confirmed``. Calibrated to single-client RTOS telnet
# servers (Zephyr's ``CONFIG_SHELL_BACKEND_TELNET``) that don't free the console
# slot the instant the FIN lands. Injectable via ``SessionManager(retry_backoff=)``
# so fakes-only unit tests can zero it instead of paying the real wall-clock wait.
_HANDSHAKE_RETRY_BACKOFF = 2.0


_GLOBAL_FLAG = re.compile(r"\(\?([aiLmsux]+)\)")
"""One global inline flag group such as ``(?i)``, which :func:`scoped_source` scopes."""

_INLINE_COMMENT = re.compile(r"\(\?#[^)]*\)")
"""An inline comment, ``(?#...)``, which matches nothing wherever it sits."""

_VERBOSE_FILLER = re.compile(r"\s+|#[^\n]*(?:\n|\Z)")
"""Whitespace or a ``#`` comment, which a VERBOSE pattern ignores."""


def _without_leading_flags(source: str, *, verbose: bool = False) -> str:
    """*source* with its leading global flags removed, and whatever sits between them.

    Python accepts global flags only at the start, but the start is read past
    inline comments and, once a leading flag turned VERBOSE on, past
    whitespace and ``#`` comments: ``(?x) (?i)a`` and ``(?x)(?#c)(?i)a`` are
    valid alone. *verbose* starts it in that state, for a pattern compiled
    with ``re.VERBOSE``, whose source needs no ``(?x)`` for it. So the scan
    skips those, and only those, while it collects the flags; leading
    whitespace without ``x`` (`` (?x)a``) ends it, and the flag left behind is
    then refused by the compile of the scoped group.
    What it removes matches nothing, so the rest matches as before.
    """
    at, end = 0, 0
    while True:
        flag = _GLOBAL_FLAG.match(source, at)
        if flag is not None:
            verbose = verbose or "x" in flag.group(1)
            at = end = flag.end()
            continue
        skip = _INLINE_COMMENT.match(source, at) or (
            _VERBOSE_FILLER.match(source, at) if verbose else None
        )
        if skip is None or skip.end() == at:
            return source[end:]
        at = skip.end()


_SCOPED_LETTERS = [
    (re.IGNORECASE, "i"),
    (re.MULTILINE, "m"),
    (re.DOTALL, "s"),
    (re.VERBOSE, "x"),
    (re.ASCII, "a"),
]
"""The flags a str pattern can carry into a scoped group, with their letters.

``u`` is left out: it is every str pattern's default and cannot be combined
with ``a``. ``L`` is invalid for a str pattern, and ``re.DEBUG`` changes no
match."""


def scoped_source(pattern: str | re.Pattern[str]) -> str:
    r"""Render *pattern* as one group that matches as *pattern* alone does, whatever surrounds it.

    A caller's ``expects`` pattern joins otto's framing parts in one regex
    (``ShellSession._build_combined_pattern``), and a pattern's source
    alone loses its compiled flags (``re.compile(p, re.IGNORECASE)``), while a
    leading global flag such as ``(?i)`` would land mid-regex, where Python
    3.11+ rejects it and 3.10 applies it to every part. So the flags, from the
    source and the compile alike, become the group's own: ``(?i:...)``. A
    VERBOSE pattern's group closes on a new line, so a trailing ``#`` comment
    cannot swallow the ``)``.

    Raises:
        ValueError: *pattern* is invalid alone, carries ``re.TEMPLATE``, or
            has a global inline flag anywhere but at its start, which no
            group can scope (Python 3.10 only warns about one alone).
    """
    source = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    with warnings.catch_warnings():
        # Python 3.10 only warns about a global flag past the start; 3.11+ raises.
        warnings.simplefilter("error", DeprecationWarning)
        try:
            flags = pattern.flags if isinstance(pattern, re.Pattern) else re.compile(source).flags
            if flags & getattr(re, "TEMPLATE", 0):
                raise ValueError(f"expects pattern {source!r}: re.TEMPLATE cannot be scoped")
            letters = "".join(letter for flag, letter in _SCOPED_LETTERS if flags & flag)
            close = "\n)" if flags & re.VERBOSE else ")"
            body = _without_leading_flags(source, verbose=bool(flags & re.VERBOSE))
            scoped = f"(?{letters}:{body}{close}"
            re.compile(scoped)
        except (re.error, DeprecationWarning) as exc:
            # An invalid pattern, or a global inline flag past its start
            # (open it with the flag, or scope it as `(?i:...)`).
            raise ValueError(f"expects pattern {source!r} is not a valid pattern: {exc}") from exc
    return scoped


def ready_pattern(m: SessionMarkers, *, line_ending: bool = False) -> re.Pattern[str]:
    r"""Compile the pattern a readiness handshake's reply has to satisfy.

    Line-anchored (or buffer-start) + ANSI-absorbing, so the marker can't
    match inside the echoed probe command — fatal on a failed telnet login
    that loops back to "login:" echoing our probe — and producing that
    leading newline in every echo state is the frame's job (see
    :meth:`~otto.host.command_frame.CommandFrame.handshake`).

    *line_ending* (for a frame that
    :attr:`~otto.host.command_frame.CommandFrame.measures_terminal_mapping`)
    also requires READY's own line ending, however the terminal mapped it.
    The read then stops after it, deterministically: an optional ending
    would match or not depending on whether it had arrived yet, and the
    handshake's measurement needs the whole line.

    Module level rather than a closure in ``ShellSession._handshake`` so
    the shape can be pinned directly against what the dialects render.
    """
    tail = r"\r*\n" if line_ending else ""
    return re.compile(r"(?:^|\r|\n)(?:\x1b\[[0-9;]*m)*" + re.escape(m.ready) + tail)


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


def _log_sent(
    log_command: Callable[[str, LogMode], None], text: "str | SecretStr", mode: LogMode
) -> None:
    """Log a live ``send`` of *text* under *mode*, the send's folded mode; nothing under NEVER.

    The one log line of a written ``send``: the host's default session's
    (:meth:`SessionManager.send`, whose caller folded the mode), a named
    session's and a login hop's (:class:`_SessionProxyIO`). A
    :class:`~pydantic.SecretStr` is shown masked.
    """
    if mode is not LogMode.NEVER:
        log_command(shown(text).rstrip(), mode)


def delegated_exec_log(exec_input: "ExecInput | None") -> "ExecLogContext | None":
    """Return the log context a delegating host handed this ``exec`` call, or ``None``.

    The one read of whether a call is delegated: the attempt's log resolver
    (:func:`resolve_exec_log`) and the host that logs the line it really runs
    (``BaseHost._exec_one``) both ask it.
    """
    return exec_input.exec_log if exec_input is not None else None


@dataclass(frozen=True, slots=True)
class ExecLog:
    """How one exec attempt logs: the context it logs under, and what that context implies."""

    context: "ExecLogContext"
    """What the attempt announces where, where its lines go, and under which mode.

    The mode is already folded: nothing folds over it again."""

    @property
    def redact(self) -> bool:
        """Whether the session's own diagnostics of this attempt are redacted."""
        return self.context.mode is LogMode.NEVER

    def announce(self) -> None:
        """Log the command line, unless the mode is NEVER."""
        if self.context.mode is not LogMode.NEVER:
            self.context.log_command(self.context.command, self.context.mode)


def resolve_exec_log(
    *,
    cmd: str,
    mode: LogMode,
    log_command: Callable[[str, LogMode], None],
    log_output: Callable[[str, LogMode], None],
    exec_input: "ExecInput | None",
) -> ExecLog:
    """Return the one answer to how an exec attempt logs: its own host's, or a delegating host's.

    Every exec attempt asks this once, through :func:`begin_exec_log`, and
    uses only its answer. *mode* is the
    attempt's own fold of the call's mode, and *log_command*/*log_output* its
    host's sinks; a delegated call (``exec_input.exec_log``) ignores them and
    logs the issued command and its output on the delegating host under the
    context's already-resolved mode.
    """
    context = delegated_exec_log(exec_input) or ExecLogContext(
        command=cmd, mode=mode, log_command=log_command, log_output=log_output
    )
    return ExecLog(context)


def begin_exec_log(
    cmd: str,
    mode: LogMode,
    log_command: Callable[[str, LogMode], None],
    log_output: Callable[[str, LogMode], None],
    *,
    exec_input: "ExecInput | None",
) -> ExecLog:
    """Resolve how an exec attempt logs and log its command line: an attempt's first step.

    The one opening of an attempt's log, so no attempt resolves without
    announcing or announces what it did not resolve. The arguments are
    :func:`resolve_exec_log`'s.
    """
    xl = resolve_exec_log(
        cmd=cmd, mode=mode, log_command=log_command, log_output=log_output, exec_input=exec_input
    )
    xl.announce()
    return xl


async def _log_and_run(
    session: "ShellSession",
    log_command: Callable[[str, LogMode], None],
    log_output: Callable[[str, LogMode], None],
    cmd: str,
    *,
    mode: LogMode,
    expects: "list[Expect | CredentialExpect] | None",
    timeout: float,
    exec_input: "ExecInput | None",
    host: str,
    write_progress: "Callable[[int, int], None] | None" = None,
) -> CommandResult:
    """Log *cmd* unless *mode* is NEVER, then run it on *session*, its sink and redaction set.

    The one live step of a typed command under its folded *mode*: the host's
    default session (:meth:`SessionManager.run_cmd`, whose caller folded the
    mode), a named session (``HostSession._run_one``, which folds it) and a
    pooled one (``SessionManager._run_framed``, its caller folding it). Under
    NEVER nothing is logged and the session's own debug trace is redacted. A
    delegated call (``exec_input.exec_log``) logs as ``resolve_exec_log``
    answers instead.
    """
    # Before the command is logged: a refused command was never sent. *host*
    # names the host in the refusal.
    session._refuse_untypable(cmd=cmd, host=host)  # noqa: SLF001 -- this module's own session
    xl = begin_exec_log(cmd, mode, log_command, log_output, exec_input=exec_input)
    return await session.run_cmd(
        cmd=cmd,
        expects=expects,
        timeout=timeout,
        on_output=_sink_for(xl.context.log_output, xl.context.mode),
        redact=xl.redact,
        write_progress=write_progress,
        exec_input=exec_input,
    )


def identity_fold(mode: LogMode) -> LogMode:
    """Return *mode* unchanged: the fold of a sink that adds no standing mode of its own.

    A container's named sessions (``DockerContainerHost``'s session fold) and
    the ``otto login`` bridge, whose session log records every line it is
    handed, use it.
    """
    return mode


def _relay_expect_read(
    session: "ShellSession",
    read: str,
    log_output: Callable[[str, LogMode], None],
    mode: LogMode,
) -> None:
    """Log what *session*'s ``expect()`` read, one record per line, under *mode*.

    The one relay of the three places that wait on a session's output (the
    host's default session, a named session, a login hop). *mode* is the
    session's folded mode, as its ``run`` and ``send`` log under: what
    ``expect`` reads is output of the same session, so a ``NEVER`` host logs
    none of it (a terminal's echo of a password typed with ``send`` is in it)
    and a ``QUIET`` host logs it at ``QUIET``. The lines are the command rule
    (``ShellSession._logged_lines``).
    """
    if mode is LogMode.NEVER:
        return
    for line in session._logged_lines(read):  # noqa: SLF001 — the one relay of a session's expect output, for the three handles that wrap the session
        log_output(line, mode)


_ESTABLISHING = ContextClaims("otto_establishing")
"""The ids of the hosts whose ``session_setup`` hook is running above this context.

Claimed by :func:`~otto.host.session_setup.apply_session_setup` for the hook's
duration, for every caller (the session manager and the ``otto login``
bridge), and read by ``BaseHost._refuse_establishing``: a hook that calls back
into any host still opening above it (its own, or the host whose hook called
the one now opening) is refused by name rather than waiting for the session
being opened. A task the hook spawns carries the claims, so it is refused
while the hook runs; once the hook has ended, every copy reads released.
"""

NEVER_RAN = "the command never ran: its end record arrived with no BEGIN marker before it"
"""The value of a command whose end record came with no BEGIN marker before it.

Every frame prints BEGIN before the command, so a normal command never
gives this. An identity guard that finds itself in the wrong shell prints
its end record alone and gives this, with ``retcode`` -1 and
``Status.Failed``, whatever status its end record carries."""

_BEFORE_ANY_SESSION_ECHOES = False
"""The echo fact that gives the widest typed-line ceiling: what a check made before any session
opens assumes, so a line no session here could carry is refused without costing a connection."""


_TERMINAL_KEYS = frozenset(chr(c) for c in (*range(0x20), 0x7F)) - {"\n", "\t"}
"""The characters a terminal or a shell's line editor acts on instead of delivering.

Every C0 control character and DEL, except newline (the line's end) and TAB
(typed literally, :meth:`~otto.host.command_frame.CommandFrame.typed`).
Measured through ``run()`` on bash: Ctrl-C, Ctrl-R, Ctrl-U and ESC hang the
command until its timeout, Ctrl-W, DEL and backspace erase what was typed
before them, Ctrl-D and Ctrl-V vanish and a carriage return becomes a newline
(#1224). A carriage return that is half of a CRLF line ending is not counted."""

_KEY_NAMES = {0x08: "backspace", 0x0D: "a carriage return", 0x1B: "ESC", 0x7F: "DEL"}


def _key_name(ch: str) -> str:
    """Return a reader's name for control character *ch*: ``Ctrl-C (0x03)``, ``DEL (0x7f)``."""
    code = ord(ch)
    return f"{_KEY_NAMES.get(code) or 'Ctrl-' + chr(code + 0x40)} (0x{code:02x})"


def refuse_if_terminal_would_act_on(
    frame: "CommandFrame | None", cmd: str, *, terminal: bool, host: str = ""
) -> None:
    """Refuse *cmd* when it holds a character the terminal it is typed at would act on.

    Only for a POSIX shell dialect (:class:`~otto.host.command_frame.BashFrame`
    and its subclasses) typed at a terminal: a session on a pipe delivers
    every byte, and another dialect's console is not measured. Nothing has
    been typed when this raises.

    Raises:
        ~otto.host.errors.UntypableCommandError: *cmd* holds one of
            ``_TERMINAL_KEYS``.
    """
    if not terminal or not isinstance(frame, BashFrame):
        return
    # A CRLF line ending is typed as it always was (the terminal reads the CR as
    # a second Enter): a script saved with them is ordinary user input.
    found = sorted({ch for ch in cmd.replace("\r\n", "\n") if ch in _TERMINAL_KEYS})
    if not found:
        return
    names = ", ".join(_key_name(ch) for ch in found)
    where = f"{host}: " if host else ""
    raise UntypableCommandError(
        f"{where}the command holds {names}, which a terminal acts on as a keystroke "
        f"instead of passing to the command, so nothing was sent. To send a keystroke "
        f"to the session, use `send()`. To give the command that byte as data, spell "
        f"it in the command, for example `printf '\\{ord(found[0]):03o}'`."
    )


def render_payload(
    frame: CommandFrame,
    cmd: str,
    *,
    m: SessionMarkers,
    exec_input: "ExecInput | None",
    terminal: bool,
) -> str:
    """Return what a session types for *cmd*: the ``run`` frame, or the ``exec`` frame.

    *terminal* says the payload is typed at a terminal, not written to a
    pipe: the command is then spelled the way the dialect's line reader takes
    it whole (:meth:`~otto.host.command_frame.CommandFrame.typed`).

    *exec_input* ``None`` is a ``run``: :meth:`~otto.host.command_frame.CommandFrame.frame`.
    Otherwise an ``exec``:
    :meth:`~otto.host.command_frame.CommandFrame.frame_exec` with the call's
    stdin, restore and credential. The one renderer of a command's payload.
    """
    if exec_input is None:
        payload = frame.frame(cmd, m=m)
    else:
        payload = frame.frame_exec(
            cmd,
            m=m,
            stdin_eof=exec_input.stdin_eof,
            restore=exec_input.restore,
            credential=exec_input.credential,
        )
        if exec_input.identity is not None:
            payload = frame.identity_guard(payload=payload, mark=exec_input.identity, m=m)
    # The frame is built from the command as written (it reads the command
    # to place its own tokens); only what is typed is spelled for the terminal.
    return frame.typed(cmd=payload) if terminal else payload


def typed_line_ceiling(frame: "CommandFrame | None", *, echoes: bool) -> int:
    """Longest line, framing included, a terminal delivers intact into *frame*'s shell.

    THE ONE PLACE THE TYPED-LINE CEILING IS DECIDED; :func:`typed_line_budget`,
    :func:`refuse_if_typed_line_would_be_cut`, :meth:`SessionManager.line_budget`
    and through it the shell transfer's chunk sizing all ask it.

    :data:`~otto.host.userland.ASH_ECHOING_LINE_MAX` (1022) for an ash frame
    whose terminal *echoes*: BusyBox ash's line editor cuts there, and engages
    only while the terminal echoes. :data:`~otto.host.userland.PTY_TYPED_LINE_MAX`
    (4095, the Linux terminal's canonical line) otherwise. *echoes* is the
    session's :attr:`~otto.host.terminal_mapping.Measurement.echoes`, which is
    ``True`` where the handshake could not show echo off (no ``stty``, #953, or
    no calibration line at all), so an unmeasured ash terminal gets the
    conservative 1022. Keyed on ``isinstance(frame, AshFrame)`` for the reasons
    :func:`refuse_if_typed_line_would_be_cut` gives.
    """
    if echoes and isinstance(frame, AshFrame):
        return ASH_ECHOING_LINE_MAX
    return PTY_TYPED_LINE_MAX


def refuse_if_typed_line_would_be_cut(
    frame: CommandFrame | None,
    cmd: str,
    *,
    echoes: bool,
    host: str = "",
    exec_escapes: bool = True,
    exec_input: "ExecInput | None" = None,
) -> None:
    """Refuse *cmd* when the terminal it is typed through would silently shorten it.

    **The gap registry's first product call site.** Everything otto knows about
    this failure lives in the ``run-command-line-length`` record in
    :data:`~otto.host.userland.GAPS`; this function supplies the only two things
    a record cannot — whether THIS host is one the measurement covers, and
    whether THIS command is over the bound — and then hands the raise back to
    :func:`~otto.host.userland.refuse_if_gapped` so the message is the record's
    and not a second, drifting copy of it.

    WHAT IT KEYS ON: ``isinstance(frame, AshFrame)``, the host's declared shell
    DIALECT. Not the os_profile (a device can be BusyBox-userland with a
    different shell, and ``userland_options`` deliberately refuses to be named
    ``busybox_*`` for the mirror-image reason -- see
    :class:`~otto.host.options.UserlandOptions`), not ``has_bash=False`` (dash
    and ksh hosts are not ash, and otto has measured the bound only on ash), and not
    :attr:`~otto.host.userland.Userland.command_frame`. That last one is the
    tempting wrong answer: on a host that declares no frame it is the probe
    that MEASURES ash-ness, but reading it here would drag a whole userland
    resolution -- up to
    ``_RESOLVE_BUDGET_S``, 30 seconds against a host that will not answer --
    onto every ``run()`` on every unix host, to decide a question the lab entry
    has already answered. The frame is declared, synchronous, and free, and
    ``os_profile``'s ``busybox`` profile sets it (``command_frame: "ash"``) for
    exactly the devices this covers. Identity, not ``type_name``, because
    ``AshFrame``'s own docstring records a registry bypass that produces a
    ``BashFrame`` answering to ``"ash"``; only an isinstance check sees through
    it.

    WHAT IT MEASURES: the longest PHYSICAL LINE otto would type, framing
    included. Two corrections to the obvious ``len(cmd)``, and both matter:

    * otto never types the caller's command bare -- it wraps it in the frame's
      BEGIN/END sentinels. On the bash/ash frame the command's line carries
      the BEGIN half, 38 characters (the END half has a line of its own). A
      guard on ``len(cmd)`` would leave every command between 4058 and 4095
      characters silently truncated, which is the original defect with a
      narrower mouth.
    * the buffer holds ONE line. A multi-line script is many lines, each
      independently edited, so the total is the wrong number: summing it would
      refuse a here-doc every line of which the device handles fine.

    WHAT IT COMPARES AGAINST: :func:`typed_line_ceiling` for *frame* and
    *echoes*, the session's measured echo fact. :meth:`SessionManager.run_cmd`
    asks twice: before the session opens with ``_BEFORE_ANY_SESSION_ECHOES`` (the widest
    ceiling any session here can have, so a line over it is refused without
    costing a connection), and once the session is open, before anything is
    typed, with that session's measured fact.

    Renders the framing against a synthetic marker set rather than a real
    session's, so the first check needs no session. Only the marker LENGTH
    affects the arithmetic and that is fixed by this module's
    ``_SESSION_ID_LEN``, which ``tests/unit/host/test_run_line_length.py``
    pins against a real session.

    *exec_input* is the payload kind the session will type (:func:`render_payload`):
    ``None`` for a ``run``'s frame, an ``exec``'s for the exec frame, whose
    opening is longer, so each entry is measured against its own line.

    *exec_escapes* says whether the message may advise ``exec(...,
    terminal=False)``: the host's answer for the call
    (``BaseHost._exec_plain`` with the call's user, elevation and prompts),
    the same decision that would refuse that ``terminal=False``.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: *frame* is ash and the
            typed line is over :func:`typed_line_ceiling`.
            Never raised for another dialect, and never raised while the record
            is anything but ``measured-broken`` -- the firing rule stays in
            :func:`~otto.host.userland.refuse_if_gapped`.
    """
    if not isinstance(frame, AshFrame):
        return
    longest = max(len(line) for line in _synthetic_lines(frame, cmd, exec_input=exec_input))
    ceiling = typed_line_ceiling(frame, echoes=echoes)
    if longest <= ceiling:
        return
    overhead = framed_line_overhead(frame, exec_input=exec_input)
    terminal = (
        "BusyBox ash's line editor keeps intact on one line while the terminal still "
        "echoes (`stty -echo` did not take at the handshake)"
        if ceiling == ASH_ECHOING_LINE_MAX
        else "a terminal delivers intact on one line"
    )
    way_out = (
        "`exec(..., terminal=False)` types into a session with no terminal and no "
        "line limit; otherwise split it"
        if exec_escapes
        else "an `exec()` of this call has a terminal too, so split it"
    )
    refuse_if_gapped(
        "run-command-line-length",
        host=host,
        attempted=(
            f"a {len(cmd)}-character command, which otto types as a {longest}-character "
            f"line once otto's framing ({overhead} characters) is added to it — over "
            f"the {ceiling} {terminal}, so "
            f"{ceiling - overhead} characters is the most any one line of a "
            f"command may be here. {way_out}"
        ),
    )


def typed_line_budget(
    frame: "CommandFrame | None", *, echoes: bool, exec_input: "ExecInput | None" = None
) -> int:
    """Characters of ONE line of a caller's command a line-disciplined path carries.

    The BUDGET half of what :func:`refuse_if_typed_line_would_be_cut`
    REFUSES on, reading the same measurement, and the difference between
    the two verbs is the whole reason this exists. A refusal is right for
    ``run()``, whose caller wrote the command and can shorten it. It is wrong
    for a caller that is generating the line itself -- ``ShellFileTransfer``
    sizes its own chunk commands, so telling it "no" would stop the backend on
    exactly the devices it exists for (see the ``run-command-line-length``
    record's OPEN paths, and the ``_SHELL_CHUNK_BYTES`` note in
    ``otto.host.transfer.shell``). Handed a number instead, it emits a shorter
    line and the transfer works.

    The ceiling is :func:`typed_line_ceiling`'s for *frame* and *echoes*.
    *exec_input* picks the payload as :func:`render_payload` does, so the
    budget is the one the refusal measures for the same entry.

    Returns the budget for the caller's COMMAND, with what otto's frame adds to
    the command's own line already subtracted (see
    :func:`framed_line_overhead`): the caller never types its command bare,
    and a budget that ignored the framing would leave a band of commands over
    the ceiling by exactly the framing's width. Rendered against a synthetic
    marker set, like the refusal's, because only the marker LENGTH affects the
    arithmetic and no session need exist to ask.

    ON A MULTI-LINE COMMAND THIS IS CONSERVATIVE BY DESIGN. Only the line the
    command starts on carries the frame's opening; every later line of the
    command is typed as written. Subtracting the overhead from every line
    costs a multi-line caller a few dozen characters it could have spent on
    those later lines, and the alternative is a budget whose value depends on
    the shape of a command the caller has not built yet.
    """
    return typed_line_room(frame, "", echoes=echoes, exec_input=exec_input)


def typed_line_room(
    frame: "CommandFrame | None",
    cmd: str,
    *,
    echoes: bool,
    exec_input: "ExecInput | None" = None,
) -> int:
    """Characters that text inserted into *cmd*'s first line may add before its line is cut.

    The ceiling (:func:`typed_line_ceiling`) less the line *cmd*'s first line
    is typed on, RENDERED (:func:`render_payload`), so whatever the frame does
    to the command is counted: a console's exec frame quotes it as one word,
    and every single quote in it then types as four characters. Negative
    when that line is already over. The inserted text must be quote-free
    (base64 is), so it types as itself.

    The one sizing helper for a caller that builds its own command around a
    payload (``ShellFileTransfer``'s chunk command): it measures its command
    with an empty payload and spends what is left, never restating the
    frame's arithmetic. :func:`typed_line_budget` is this for an empty
    command.
    """
    frame = frame_or_default(frame)
    return typed_line_ceiling(frame, echoes=echoes) - _command_line_length(
        frame, cmd, exec_input=exec_input
    )


_LINE_PROBE = "OTTOFRAMEPROBE"
"""A quote-free word that marks, in a rendered payload, the line a command starts on."""


def _synthetic_lines(
    frame: "CommandFrame", cmd: str, *, exec_input: "ExecInput | None"
) -> "list[str]":
    """Return the physical lines *frame* types for *cmd*, against the synthetic marker set."""
    markers = SessionMarkers.for_session(session_id="0" * _SESSION_ID_LEN)
    # terminal=True: a line is only ever measured for a session that types at one.
    return (
        render_payload(frame, cmd, m=markers, exec_input=exec_input, terminal=True)
        .rstrip("\n")
        .split("\n")
    )


def _command_line_length(frame: "CommandFrame", cmd: str, *, exec_input: "ExecInput | None") -> int:
    """Length of the line *cmd*'s first line is typed on, framing and quoting included.

    Rendered with :data:`_LINE_PROBE` in front of *cmd*, so the line is
    found, and the probe's own length taken off again. A frame that
    transforms the command beyond recognition is charged its whole frame
    for an empty command, plus the command.
    """
    line = next(
        (
            x
            for x in _synthetic_lines(frame, _LINE_PROBE + cmd, exec_input=exec_input)
            if _LINE_PROBE in x
        ),
        None,
    )
    if line is None:
        # A frame that transforms the command beyond recognition: charge the
        # whole empty frame, as before the bash/ash frame split its lines.
        return len("\n".join(_synthetic_lines(frame, "", exec_input=exec_input))) + len(cmd)
    return len(line) - len(_LINE_PROBE)


def framed_line_overhead(frame: "CommandFrame", *, exec_input: "ExecInput | None" = None) -> int:
    """Characters the frame adds to the physical line a command is typed on.

    *exec_input* picks the payload as :func:`render_payload` does: ``None`` for
    ``run``'s frame, an ``exec``'s for the exec frame.

    Measured on the line that carries the command, not on the whole frame: the
    bash/ash frame closes its brace group and echoes END on a line of its own,
    so a command line pays only the BEGIN half. A frame that quotes or
    escapes the command (``{cmd!r}``) still contains a plain probe word, so
    its quotes are counted too. Rendered against a synthetic marker set; only
    the marker LENGTH affects the arithmetic.
    """
    return _command_line_length(frame, "", exec_input=exec_input)


class ShellSession(ABC):
    """Abstract base for persistent shell sessions.

    A session is **transport + dialect**. Subclasses implement the I/O
    primitives (write_transport, read_transport_until, open_transport, close) — that is the
    transport (SSH, telnet, local subprocess). The *dialect* — how a command is
    wrapped in sentinels and how output/retcode are parsed back — is composed
    in as a :class:`~otto.host.command_frame.CommandFrame` (default
    :class:`~otto.host.command_frame.BashFrame`), not inherited. The base class
    provides the shared engine: sentinel-wrapped command execution, expect
    handling, and timeout recovery, all delegating the dialect to the frame.
    """

    # Default ceiling on the marker handshake in _handshake. Class-level so
    # tests can shrink it without patching the module constant. A session built
    # against a slow-to-start shell (e.g. a Zephyr QEMU telnet console) gets a
    # more generous value via the ``init_timeout`` constructor argument (or,
    # without one, ``OTTO_INIT_TIMEOUT``), which shadows this with an instance
    # attribute; one handshake can override both
    # for itself via ``_handshake(timeout=)`` / ``enter_frame(timeout=)``.
    _init_timeout: float = _INIT_TIMEOUT

    # How long to wait for one readiness probe before resending it. Bounds the
    # cost of the telnet login-flush race (a probe lost before the shell is
    # reading) to roughly one interval.
    _init_probe_interval: float = 0.5

    # Whether SessionManager may rebuild the transport and retry a handshake
    # that failed with the generic readiness error: ``retries_failed_open``
    # (otto.host.telnet), narrowed by a console session whose client typed
    # a password. True on every term without a console client. A named or
    # pooled open on a line that serves one client is not retried either
    # (``_retries_named_open``, the same rule's ``named``).
    _retry_failed_handshake: bool = True
    # Whether the handshake makes the shell read a typed line as a script
    # line (CommandFrame.script_parsing). False only where the shell is
    # handed to a person, who keeps their own `!` recall and `#` reading.
    _script_lines: bool = True

    def __init__(
        self,
        *,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        shell_history: bool = True,
        failure_context: str = "",
        console_client: "ConsoleClient | None" = None,
        terminal: bool | None = None,
    ) -> None:
        self._session_id = uuid.uuid4().hex[:_SESSION_ID_LEN]
        # Whether THIS session's shell reads a terminal. ``None`` keeps the
        # transport's own answer (the class attribute); a transport that can
        # open either way (ssh) passes what it was asked for.
        if terminal is not None:
            self.reads_a_terminal = terminal
        # The console term's client, when this session is a shell on a serial
        # console (TelnetSession's own line, or the login bridge's): it owns
        # the line. ``None`` on every other transport. Held here because it
        # is also what decides terminal_outlives_session.
        self.console_client = console_client
        # The terminal's settings as the first handshake on it found them
        # (a `stty -g` record), on a terminal that outlives the session; put
        # back by the one restore step (_restore_line). None until saved,
        # and wherever nothing is saved.
        self._saved_terminal: str | None = None
        # Words naming WHAT this session is a shell on, appended in brackets
        # to a failed-readiness message. Empty for ssh/telnet, whose message
        # needs none; a console term names its server, port and dial mode, so
        # the failure reads as a console failure rather than a telnet one.
        self._failure_context = failure_context
        # What the line showed after a failed handshake, as a ready-made
        # message suffix (``" Seen: '<quote>'."``). Filled only by a transport
        # that can read it back — a console session, via its client — else "".
        self._handshake_seen = ""
        # The dialect: how commands are framed and parsed. Defaults to bash; an
        # embedded host injects a ZephyrFrame (or a project-registered frame).
        self._frame: CommandFrame = frame_or_default(command_frame)
        # Whether otto's commands are recorded in this host's shell history.
        # Defaults True — "leave the shell as we found it" — so every caller
        # that doesn't opt in (LocalHost, DockerContainerHost, EmbeddedHost)
        # is unaffected. UnixHost passes its own field through, defaulting
        # False. Same name and same meaning at every layer; see
        # command_frame.history_prefix for the feature's only negation.
        self._shell_history = shell_history
        # Unique per-session sentinels, handed to the frame for every
        # render/parse call. Aliased as individual attributes too because the
        # session orchestration (and its tests) reference them directly.
        self._markers = SessionMarkers.for_session(session_id=self._session_id)
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
        # A second read through the same resolver, reached only by a session
        # no manager built: a manager passes the deadline it already resolved
        # (with its host family's default), so this returns it unchanged.
        init_timeout = resolve_init_timeout(init_timeout)
        if init_timeout is not None:
            self._init_timeout = init_timeout
        self._initialized = False
        self._alive = False
        # Set while a stop of a command otto gave up on has not confirmed
        # the shell (left set, with the session dead, when it never does),
        # and when a send() or expect() is cancelled mid-flight: the next
        # operation then runs the stop before doing anything else. A
        # send() or expect() waits for that next use rather than stopping at
        # once: it ran no command of otto's, so there is nothing to stop.
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
        # How this session's terminal maps its output, as the last handshake
        # measured it (see otto.host.terminal_mapping), and whether an unknown
        # mapping, or tabs still expanded, has been warned about yet: each
        # once per session.
        self._terminal = _UNMEASURED
        self._warned_terminal_mapping = False
        self._warned_tabs = False
        self._warned_echo = False
        # Set, once, when an exec frame's end record said the shell's terminal
        # record was gone (BashFrame.exec_baseline_present): the session
        # warns once, never re-snapshots, and the exec pool closes it.
        self._exec_baseline_lost = False
        # App-shell lock: the AppShell instance currently attached to this
        # session, or None. Typed `object` (not AppShell) to avoid an import
        # cycle — session.py must not import app_shell at module load. While
        # set, run_cmd refuses to type its sentinel command frame into the app
        # (see the guard at the top of run_cmd); raw send/expect stay usable.
        self._app_shell: object | None = None
        # Whether the open reached this shell by a switch (a login-proxy hop
        # or a switch to the user), so another shell sits underneath it; the
        # shell-local mark a pooled such session carries (the frame's
        # ``identity_mark_statement``), or None; whether an exec found it gone
        # (the frame's ``identity_guard``); and whether its frame has no
        # guard, so the exec pool does not keep it.
        self._reached_by_switch = False
        self.identity_mark: str | None = None
        self._identity_lost = False
        self.identity_unguarded = False

    @property
    def alive(self) -> bool:
        """Whether the session is initialized and responsive."""
        return self._alive

    @property
    def reached_by_switch(self) -> bool:
        """Whether the open reached this shell by a switch, so another shell sits underneath.

        THE ONE ANSWER to "does this pooled session need an identity guard",
        fed by what the open did: every login-proxy hop and every switch to
        the session's user (:meth:`note_reached_by_switch`). A session on its
        own direct connection (the login itself, the user's own ssh login)
        never sets it.
        """
        return self._reached_by_switch

    def note_reached_by_switch(self) -> None:
        """Record that the open reached this shell by a switch (see :attr:`reached_by_switch`)."""
        self._reached_by_switch = True

    @property
    def identity_lost(self) -> bool:
        """Whether an ``exec`` found this switched shell's identity mark gone.

        Set when the frame's guard (:meth:`~otto.host.command_frame.CommandFrame.identity_guard`)
        typed with the exec did not find the mark: the switched
        shell has ended and another shell (the login's, underneath) answered.
        The command was not run, and the exec pool discards the session.
        """
        return self._identity_lost

    @property
    def exec_baseline_lost(self) -> bool:
        """Whether an ``exec`` found the shell's terminal record gone.

        The record (``__otto_base``) is set on every handshake and kept by
        the shell; a command that unsets it, or a shell that replaced
        itself, loses it. From then on every ``exec`` reports the loss and
        restores nothing, otto never takes a new snapshot over a terminal it
        cannot vouch for, and the exec pool does not keep the session.
        """
        return self._exec_baseline_lost

    reads_a_terminal: bool = True
    """Whether this session's shell reads a terminal; ``False`` for a pipe.

    The class value is the transport's own answer (``False`` on
    :class:`LocalSession`); a session built with ``terminal=`` carries its
    own (:class:`SshSession` opened without a pty). Everything that depends
    on a line reader asks the session, never its class: the typed spelling of
    a command, the typed-line refusal, and how a command is interrupted.

    A pipe echoes nothing, so its ``stty -echo`` failing (there is no
    terminal to set) is no echo: the session records echo as off."""

    @property
    def terminal_outlives_session(self) -> bool:
        """Whether this session's terminal stays after the session ends.

        The one place that decides it. True on a serial console (the session
        holds a :class:`~otto.host.console.ConsoleClient`): the line is
        still there for the next user once otto logs out, so what the
        handshake changed on it is put back
        (:mod:`otto.host.terminal_mapping`). False on an ssh, telnet or local
        session, whose pty or pipe goes away with it.
        """
        return self.console_client is not None

    def _restore_line(self, frame: CommandFrame | None = None) -> str | None:
        """Return the one line undoing what the handshake did to the terminal, in *frame*'s dialect.

        *frame* defaults to the session's current one. Where the settings
        were saved, :meth:`~otto.host.command_frame.CommandFrame.restore_terminal`
        of them (echo and tabs as they were found); elsewhere
        :meth:`~otto.host.command_frame.CommandFrame.restore_interactive`.
        Used at the login bridge's hand-back and by :meth:`_put_terminal_back`.
        """
        frame = frame or self._frame
        if self._saved_terminal is not None:
            return frame.restore_terminal(self._saved_terminal)
        return frame.restore_interactive()

    async def _put_terminal_back(self) -> None:
        """Before a close, restore the settings the handshake saved; a no-op where none were.

        Only a terminal that outlives the session has saved settings, so an
        ssh, telnet or local close sends nothing. The restore is written as
        one framed command and its reply is NOT awaited: the close's next
        step (a console's logout) reads the line through to the login
        prompt, and the shell runs the restore before it reads the logout's
        EOF, so the close costs no round trip more and cannot wait on a
        shell that is going away (a reboot). A write that fails is logged
        and the close goes on. A session killed without a clean close never
        gets here; that is documented, not guarded (no timer, no watchdog).
        """
        saved, self._saved_terminal = self._saved_terminal, None
        # Not into a command still running (it would read the line as its
        # input) nor into an attached app shell.
        if saved is None or not self._alive or self._needs_recovery or self._app_shell is not None:
            return
        line = self._frame.restore_terminal(saved)
        if line is None:
            return
        try:
            await self.write_transport(data=self._frame.frame(line, m=self._markers))
        except Exception as exc:  # noqa: BLE001 — best-effort courtesy on close, like the console logout
            logger.warning(f"{self._log_tag}: could not restore the terminal's settings: {exc}")

    @property
    def _log_tag(self) -> str:
        """Stable tag for debug log lines: ``<class>@<session_id>``.

        Identifies which subclass and which session a log line came from when
        multiple are running concurrently — useful when bringing up a new
        embedded OS subclass alongside the existing Zephyr/Unix ones.
        """
        return f"{type(self).__name__}@{self._session_id}"

    # --- Abstract I/O primitives (implemented by subclasses) ---
    # Their docstrings qualify every role: SshSession, TelnetSession and
    # LocalSession override them without docstrings, so autodoc renders this text
    # under each subclass, where a bare role does not resolve.

    @abstractmethod
    async def open_transport(self) -> None:
        """Open the underlying transport (SSH process, telnet stream, etc.).

        A hook: a transport implements it, and the session calls it to bring the
        transport up before its handshake. Callers never do.
        """
        ...

    @abstractmethod
    async def write_transport(self, *, data: str | SecretStr) -> None:
        """Write raw text to the transport's stdin.

        A hook: a transport implements it, and callers use
        :meth:`~otto.host.session.ShellSession.send`, which checks the session is
        alive first and marks it for recovery if the write is cancelled.

        A ``SecretStr`` is a password answering a prompt: an implementation
        writes :func:`~otto.host.secret_wire.reveal` of it (its value and a
        newline) inside the transport call expression itself, so no otto
        frame binds the raw value.
        """
        ...

    @abstractmethod
    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        """Read the transport until *pattern* matches; return the data, match included.

        A hook: a transport implements it, and callers use
        :meth:`~otto.host.session.ShellSession.expect`.
        """
        ...

    @abstractmethod
    async def _take_unread(self) -> str:
        """Return and consume what the transport has received and no read has matched yet.

        A hook, called by :meth:`~otto.host.session.ShellSession.run_cmd` when a
        command's budget runs out and before anything is written to stop it,
        so a timed-out result keeps the unfinished output its read loop was
        still waiting on. It never waits: it takes only what has already
        arrived, and each piece once, including any the transport holds
        between reads (a buffer, a partly decoded character). Every transport
        implements it; the contract and what a wrong implementation breaks are
        in the "Implementing a session transport" section of the sessions
        extension page.
        """
        ...

    async def _refused_login(self) -> Exception | None:
        """Name a refused login from the output the failed handshake left behind, or ``None``.

        ``None`` by default: the generic message :meth:`_fail_init` raises
        already says a login may not have completed, and no transport but a
        console can tell. Overridden by :class:`TelnetSession` for a console
        term, whose client knows the line's prompts. Called from
        :meth:`_fail_init` before :meth:`close`, while the transport can
        still be read.
        """
        return None

    async def _seen_at_open(self, partial: str) -> str:  # noqa: ARG002 — the parameter is the override's; this quotes-nothing default needs none
        """Quote what the device sent before the open's handshake failed, or ``""``.

        ``""`` by default. Overridden by :class:`TelnetSession` off a
        console, where a login(1) that refused the login printed why.
        *partial* is what the read that met end of stream had gathered.
        Called from :meth:`_fail_init` before :meth:`close`.
        """
        return ""

    def _retries_named_open(self) -> bool:
        """Whether a named or pooled open of this session is rebuilt after a failed handshake.

        ``True`` by default. :class:`TelnetSession` asks the one rule
        (:func:`~otto.host.telnet.retries_failed_open`), which refuses on a
        line that serves one client.
        """
        return True

    def _connection_lost_errors(self) -> list[type[BaseException]]:
        """Exceptions that mean **this session's own transport** dropped mid-command.

        Transport-neutral by default: EOF on the read side
        (``asyncio.IncompleteReadError``), a write landing on an
        already-dead channel (``BrokenPipeError``), and the peer or the
        network ending the connection under a read or write
        (``ConnectionResetError``, ``ConnectionAbortedError``: a telnet
        reader that failed carries the first). Every transport agrees on
        these, so a reset gives the same outcome on ssh, telnet, console and
        local sessions.

        The ``ConnectionError`` base class is deliberately NOT listed. Many
        of otto's own errors subclass it on purpose (``SessionSetupError``,
        ``ConsoleError``, the host-key errors, ``ReadGateClosed``) and are not
        a transport dropping: they keep their own handling, and listing the
        base would swallow them into "the session died". Name a new
        transport failure here by its concrete class instead.

        Overridden by :class:`SshSession` to add asyncssh's own
        ``ConnectionLost`` —
        asyncssh is a heavy import (the cryptography stack, and ``ldconfig``
        runs to look for liboqs and nettle: :mod:`otto.host._asyncssh`),
        so only the transport that can actually raise that error pays to
        import it, and only when a session of that transport exists. Consulted
        at every choke point that must treat "the transport is gone" as one
        class of event regardless of which transport it is: :meth:`expect`,
        :meth:`run_cmd`, :meth:`_recover_session`, :meth:`_confirm_recovered`.
        """
        return [
            asyncio.IncompleteReadError,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ]

    async def console_login(self) -> None:
        """Run the console login state machine on this session's own streams.

        Refused here: only :class:`TelnetSession` on a console term can run
        it. See :meth:`TelnetSession.console_login` for what it does and who
        calls it.
        """
        raise ConsoleError(
            f"{self._log_tag}: console_login() needs a console term; this session is not one"
        )

    @abstractmethod
    async def close(self) -> None:
        """Close the session and release resources."""
        ...

    async def end_input(self) -> None:  # noqa: B027 — an empty default is the POINT: only a pipe shell exits on its input's end
        """Before a :meth:`close`, let an idle shell exit on the end of its input, where it can.

        A shell that exits this way is closed without a signal, so a process
        an earlier command left running is left as it is. Only the local
        shell does (:meth:`LocalSession.end_input`); every other session's
        :meth:`close` is unchanged by it.
        """

    # --- Session lifecycle ---

    async def _ensure_ready(self) -> None:
        """Initialize the session if needed, and recover it if the prior op was cancelled.

        The deferred recovery is the stop a timed-out command gets
        (:meth:`_stop_under_bound`), bounded, and a session it cannot confirm
        is left dead. A cancelled ``run_cmd`` never defers to it: it stops
        its command, restore line included, before the cancel returns.

        A session already known dead is refused here, before the deferred
        recovery: nothing more (no interrupt, no recovery probe) is typed
        into a session a stop could not confirm.
        """
        await self._ensure_initialized()
        self._require_alive()
        if self._needs_recovery:
            await self._stop_under_bound(restore=None)

    def _handshake_payload(self, m: SessionMarkers) -> str:
        """Render the complete first line written into a fresh shell.

        The dialect's readiness probe, carrying the shell prefix
        (:func:`~otto.host.command_frame.shell_prefix`: the script-line options, and
        history suppression when the host asked for it). It rides this existing line
        rather than being a command of its own: the payload is silent and idempotent, so
        it costs no extra round-trip and stays invisible to the frame parser even though
        ``confirm_live`` resends the probe on a loop. It has to be on the FIRST line the
        shell executes — anything written before it is
        already in the history list. The frame places it in the line
        (:meth:`~otto.host.command_frame.CommandFrame.handshake`), after the
        anchor that has to open it. On the first handshake of a terminal that
        outlives the session the settings save rides in front of it
        (:func:`~otto.host.command_frame.handshake_prefix`).
        """
        prefix = handshake_prefix(
            self._frame,
            m,
            shell_history=self._shell_history,
            save_terminal=self._saves_terminal(),
            script_lines=self._script_lines,
        )
        return self._frame.handshake(m, prefix=prefix)

    def _new_shell_prefix(self) -> str:
        """Return the statements a new shell this session starts is told first.

        :func:`~otto.host.command_frame.shell_prefix` of the frame the
        session is in now, so a user switch or a login-proxy hop after frame
        entry tells the new shell what the handshake told this one. Empty on
        the person's login bridge, which sets none of them.
        """
        return shell_prefix(
            self._frame, shell_history=self._shell_history, script_lines=self._script_lines
        )

    def _saves_terminal(self) -> bool:
        """Whether this handshake saves the terminal's settings.

        Only where the terminal outlives the session, and only while nothing
        is saved: a second handshake (frame entry) on the same line must not
        save otto's own settings as the user's.
        """
        return self.terminal_outlives_session and self._saved_terminal is None

    async def _ensure_initialized(self) -> None:
        """Open the transport, then run the dialect handshake. Idempotent.

        Two halves, split so the second can be run again on its own: :meth:`open_transport`
        brings the transport up, and :meth:`_handshake` confirms the shell that
        answers on it is at a prompt (the marker handshake, and what bounds it,
        are documented there).
        """
        if self._initialized:
            return
        await self.open_transport()
        await self._handshake(opening=True)

    @property
    def markers(self) -> SessionMarkers:
        """The sentinel set the session is currently framing with.

        Re-minted by :meth:`enter_frame`; a caller that captured the old set
        (tests, mostly) must re-read it after frame entry.
        """
        return self._markers

    async def _handshake(self, timeout: float | None = None, *, opening: bool = False) -> None:
        """Confirm the current frame's shell is at a prompt; the dialect half of open.

        Renders the frame's readiness payload (the shell prefix included)
        and resends it via :func:`~otto.host.shell_liveness.confirm_live`
        until the READY marker comes back or *timeout* (default
        ``_init_timeout``) passes; a failure tears the session down through
        :meth:`_fail_init`. An EMPTY payload means "nothing to confirm" — the
        contract :class:`~otto.host.command_frame.RawFrame` relies on — and
        marks the session ready without writing a byte.

        On bash that payload is ``__otto_probe=READY__; __otto_echo=;
        stty -echo || __otto_echo=E; stty tab0 || :; echo; <calibration> || :;
        unset __otto_echo; echo __OTTO_<id>_$__otto_probe`` (on the first handshake of a terminal
        that outlives the session, ``stty -g || :;`` right after the anchor,
        whose record the session keeps: :mod:`otto.host.terminal_mapping`), read back until
        the marker and its line ending return: the deterministic readiness
        check that replaced telnet login's silence-drain — prompt-independent
        and content-independent. The calibration line
        (:data:`~otto.host.terminal_mapping.CALIBRATION_COMMAND`) is how the
        session learns its terminal's line-ending mapping, whether tabs
        survive and whether ``stty -echo`` took (its ``%s`` prints
        ``__otto_echo`` before the ``X``), reported as ``terminal_mapping``. READY is typed split
        across a variable that only the line's first statement sets
        (:func:`~otto.host.command_frame.whole_line_echo`): an echo of the
        typed line (echo is still on at the first handshake) never carries
        it, even where a wrapped echo puts a carriage return before it, and a
        line the shell ran only the tail of prints half the marker, so the
        line is sent again whole.

        The probe is *resent* on a fixed interval rather than written once
        (``confirm_live``'s resend loop). This matters for telnet:
        ``login(1)`` typically flushes pending terminal input before it
        exec's the shell, so a probe written in the window between the
        password and the shell starting is silently discarded. Resending
        until one lands closes that race. SSH and local shells are ready
        immediately and answer the first probe, so they never actually
        retry.

        Bounded by the deadline: a failed login (no shell ever spawns, so no
        probe can echo back) surfaces as a clear ``ConnectionError`` instead
        of hanging until some far-outer timeout.

        Split from :meth:`_ensure_initialized` so :meth:`enter_frame` can run
        a SECOND handshake, in a different dialect, on the same transport.
        *opening* is set only by the open's own handshake: its failure may
        quote what the device sent (:meth:`_seen_at_open`), a frame entry's
        never does, since a hook or a hop may have typed other credentials.
        """
        deadline = self._init_timeout if timeout is None else timeout
        # Decided once per handshake, before any send: every resend of this
        # handshake carries the save (or not) alike.
        saves = self._saves_terminal() and self._frame.save_terminal(self._markers) != ""
        handshake_cmd = self._handshake_payload(self._markers)
        if handshake_cmd == "":
            logger.debug(f"{self._log_tag}: raw landing — no handshake to send")
            self._terminal = _UNMEASURED
            self._initialized = True
            self._alive = True
            return

        logger.debug(
            f"{self._log_tag}: handshake start cmd={handshake_cmd!r} "
            f"marker={self._ready_marker!r} timeout={deadline}s"
        )
        measures = self._frame.measures_terminal_mapping
        # The expect adapter keeps what the probe read: confirm_live only
        # answers whether it matched, and the measurement needs the bytes.
        reply = ""

        async def _expect(pat: re.Pattern[str], t: float) -> str:
            nonlocal reply
            reply = await result_within(self.read_transport_until(pattern=pat), t)
            return reply

        confirmed = False
        # What the read that met end of stream had gathered: the far side's
        # last words, kept for the failure's quote.
        partial = ""
        try:
            confirmed = await confirm_live(
                lambda data: self.write_transport(data=data),
                _expect,
                self._handshake_payload,
                lambda m: ready_pattern(m, line_ending=measures),
                lambda: self._markers,
                settle=0.0,
                probe_timeout=self._init_probe_interval,
                deadline=deadline,
            )
        except asyncio.IncompleteReadError as exc:
            partial = exc.partial.decode(errors="replace")
        if not confirmed:
            await self._fail_init(partial=partial, opening=opening)

        # confirm_live owns the resend bookkeeping internally and doesn't
        # expose a per-probe attempt count to its caller, so this is
        # deliberately leaner than the bespoke loop it replaces — just enough
        # to confirm the handshake landed.
        logger.debug(f"{self._log_tag}: handshake matched")
        self._measure_terminal_mapping(reply if measures else None)
        if saves:
            # The reply holds everything the line printed up to READY, so the
            # first record behind this session's tag is the first run's: the
            # settings as found.
            self._saved_terminal = saved_settings(reply, self._markers.saved)

        self._initialized = True
        self._alive = True

    @property
    def terminal_mapping(self) -> str:
        """How this session's terminal maps its output, as the last handshake measured it.

        The line-ending mapping, with ``, tabs expanded`` after it where tabs
        did not survive and ``, echo on`` where echo is still on
        (:func:`~otto.host.terminal_mapping.reported`).
        """
        return reported(self._terminal)

    @property
    def echoes(self) -> bool:
        """Whether this session's terminal may still echo, as the last handshake measured it.

        :attr:`~otto.host.terminal_mapping.Measurement.echoes`: ``True`` where
        ``stty -echo`` failed and where nothing was measured (no handshake yet,
        no calibration line, a dialect that sends none). It sets the typed-line
        ceiling of an ash shell (:func:`typed_line_ceiling`).
        """
        return self._terminal.echoes

    def _measure_terminal_mapping(self, reply: str | None) -> None:
        """Record how the terminal maps line endings, from the handshake's *reply*.

        ``None`` (a frame with no calibration line) records ``unmeasured``.
        Measured afresh at every handshake; an ``unknown(...)`` mapping logs a
        WARNING the first time this session sees one, and never fails anything:
        the output is then returned as it arrived. Tabs still expanded after
        the handshake's ``stty tab0`` log a WARNING of their own, once, and
        so does a terminal that still echoes after ``stty -echo``, where the
        frame's typed-line ceiling depends on it (:func:`typed_line_ceiling`);
        elsewhere that is only a debug line.
        """
        if reply is None:
            self._terminal = _UNMEASURED
            return
        seen = measure(reply, self._markers.ready)
        if not self.reads_a_terminal:
            seen = replace(seen, echoes=False)
        mapping = seen.mapping
        self._terminal = seen
        logger.debug(f"{self._log_tag}: terminal mapping: {reported(seen)}")
        if seen.tabs_expanded and not self._warned_tabs:
            self._warned_tabs = True
            logger.warning(
                f"{self._log_tag}: terminal mapping: {reported(seen)}. The terminal "
                f"expands tabs and `stty tab0` could not turn it off, so a tab a "
                f"command writes arrives as spaces"
            )
        if seen.calibrated and seen.echoes and not self._warned_echo:
            self._warned_echo = True
            note = (
                f"{self._log_tag}: terminal mapping: {reported(seen)}. `stty -echo` did "
                f"not take (no `stty` on the target?), so the terminal still echoes "
                f"what otto types"
            )
            if typed_line_ceiling(self._frame, echoes=True) != typed_line_ceiling(
                self._frame, echoes=False
            ):
                # Where the answer matters, decided where the ceiling is.
                logger.warning(
                    f"{note}; on an ash shell a typed line is then limited to "
                    f"{ASH_ECHOING_LINE_MAX} characters"
                )
            else:
                logger.debug(note)
        if is_unknown(mapping) and not self._warned_terminal_mapping:
            self._warned_terminal_mapping = True
            why = (
                "The terminal translates line endings in a way otto cannot undo exactly"
                if seen.calibrated
                else "No calibration line came back (no `printf` on the target?)"
            )
            logger.warning(
                f"{self._log_tag}: terminal mapping: {mapping}. {why}, so this session's "
                f"command output is left as it arrived"
            )

    async def enter_frame(self, *, frame: CommandFrame, timeout: float | None = None) -> None:
        """Switch this session to *frame* and run that frame's handshake.

        The whole of "invoking the CommandFrame" after a session-setup hook
        has manoeuvred the console into the target shell: fresh markers (the
        landing set may have an unread READY reply still in the stream, which
        must not confirm anything), the new frame and its end pattern, any
        pending recovery cleared (the handshake supersedes it), then
        ``_handshake()`` — never ``open_transport()``, so the transport and the
        shell the hook reached are kept. Every call runs the handshake; the
        manager calls it once more after the hook unconditionally, and that
        repeat IS the post-hook confirmation.

        Raises :class:`~otto.host.app_shell.AppShellActiveError` while an
        AppShell is attached (the handshake would be typed into the REPL) and
        :class:`~otto.host.errors.SessionSetupError` on a session
        already marked dead. A handshake that never confirms surfaces as the
        ``ConnectionError`` ``_fail_init()`` raises, with the session closed.
        """
        if self._app_shell is not None:
            from .app_shell import AppShellActiveError

            raise AppShellActiveError(
                f"{type(self._app_shell).__name__} is attached to this session; "
                f"enter_frame() would type the handshake into it"
            )
        if not self._alive:
            raise SessionSetupError(
                f"{self._log_tag}: enter_frame() on a dead session — the landing "
                f"or the hook already lost the shell"
            )
        self._session_id = uuid.uuid4().hex[:_SESSION_ID_LEN]
        self._markers = SessionMarkers.for_session(session_id=self._session_id)
        self._begin_marker = self._markers.begin
        self._end_marker_prefix = self._markers.end_prefix
        self._ready_marker = self._markers.ready
        self._recover_marker = self._markers.recover
        self._frame = frame
        self._end_pattern = frame.end_pattern(self._markers)
        self._needs_recovery = False
        self._initialized = False
        logger.debug(f"{self._log_tag}: entering frame {type(frame).__name__}")
        await self._handshake(timeout=timeout)

    async def _fail_init(
        self, attempt: int = 0, *, partial: str = "", opening: bool = False
    ) -> None:
        """Tear down a session whose readiness handshake never completed.

        The message must not accuse the device of being down (issue #260).
        This method is reachable ONLY from :meth:`_handshake`, which runs only
        after :meth:`open_transport` has already returned — and for telnet the transport
        is established before the session object even exists. So the transport
        is up EVERY time this raises, and the old wording ("the device is
        unresponsive or login failed") was never true of the first clause. It
        cost a real investigation of power and credentials on a Zephyr guest
        that was up, answering TCP, and merely holding its one console slot.

        The dialect decides the second clause, because the two conditions have
        opposite remedies: on a
        :attr:`~otto.host.command_frame.CommandFrame.single_client_console`
        the cause is contention (find the other client, or restart the
        console); anywhere else it is a shell that never started or a login
        that never completed (check credentials).

        *partial* is what the read that met end of stream had gathered, and
        *opening* says the failed handshake was the open's own; together they
        feed :meth:`_seen_at_open`.
        """
        logger.debug(
            f"{self._log_tag}: handshake FAILED after {attempt} attempt(s); "
            f"marking session dead and closing"
        )
        self._alive = False
        # Read what the handshake left unread BEFORE close() can discard it:
        # on a console it is where a refused login shows.
        refused: Exception | None = None
        with suppress(Exception):  # pragma: no cover - best-effort diagnosis
            refused = await self._refused_login()
            if opening and refused is None and not self._handshake_seen:
                self._handshake_seen = await self._seen_at_open(partial)
        # On a single-client console the close drains the line to quiet
        # before it releases it, as every release of such a line does.
        with suppress(Exception):  # pragma: no cover - best-effort cleanup
            await self.close()
        if self._frame.single_client_console:
            why = (
                "this console serves ONE client at a time, so its slot is most "
                "likely still held — by another otto run, a stray client, or a "
                "wedged console that needs restarting"
            )
        else:
            why = (
                "the shell may never have started, or the login never completed "
                "(e.g. bad credentials)"
            )
        if refused is not None:
            raise refused
        msg = (
            f"shell never became ready after open — the transport connected but the "
            f"shell never reached a prompt; {why}"
        )
        if self._failure_context:
            msg += f" [{self._failure_context}]"
        msg += self._handshake_seen
        raise ConnectionError(msg)

    # --- Public API ---

    def _require_alive(self) -> None:
        """Fail fast if the session is known dead.

        Called after :meth:`_ensure_ready` (which self-heals a recoverable
        session): if the shell is still not alive — a prior recovery gave up and
        marked it dead — raise immediately rather than issuing an I/O that can
        only hang until its timeout.
        """
        if not self._alive:
            raise RuntimeError(f"{self._log_tag}: session is not alive")

    def _refuse_untypable(self, *, cmd: str, host: str) -> None:
        """Refuse *cmd* if this session's terminal would act on a character of it.

        :func:`refuse_if_terminal_would_act_on` for this session's dialect and
        transport; *host* names the host in the refusal.
        """
        refuse_if_terminal_would_act_on(self._frame, cmd, terminal=self.reads_a_terminal, host=host)

    async def send(self, text: str | SecretStr) -> None:
        """Send raw text to the session's stdin.

        Use this for driving interactive programs (REPLs, custom CLIs).
        The caller is responsible for including line endings; a
        ``SecretStr`` is written as its value and a newline (see
        :meth:`write_transport`).
        """
        await self._ensure_ready()
        self._require_alive()
        try:
            await self.write_transport(data=text)
        except asyncio.CancelledError:
            self._needs_recovery = True
            raise

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for a pattern in the output stream.

        Returns captured data up to and including the match.
        Raises asyncio.TimeoutError if the pattern isn't seen within timeout.
        Marks the session as dead if EOF is received or the transport is lost.
        """
        await self._ensure_ready()
        self._require_alive()
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        try:
            return await result_within(
                self.read_transport_until(pattern=compiled),
                timeout=timeout,
            )
        except tuple(self._connection_lost_errors()):
            # The try body above only reads, so BrokenPipeError has no
            # reachable path here the way it does at the write-adjacent choke
            # points (run_cmd, _recover_session): this is defensive symmetry
            # with those (same triple-catch shape, same "the transport is
            # gone" semantics), kept in case a future caller starts writing
            # ahead of this read, not a currently-reachable write failure.
            # expect() has no CommandResult to encode the failure into (unlike
            # run_cmd() below), so mark dead and let the caller see the
            # exception rather than inventing a return shape for it.
            self._alive = False
            raise
        except asyncio.CancelledError:
            self._needs_recovery = True
            raise

    async def run_cmd(
        self,
        *,
        cmd: str,
        expects: list[Expect | CredentialExpect] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        on_output: Callable[[str], None] | None = None,
        redact: bool = False,
        write_progress: Callable[[int, int], None] | None = None,
        exec_input: ExecInput | None = None,
    ) -> CommandResult:
        """Execute a shell command with sentinel-based output demarcation.

        Output is streamed line-by-line to ``_on_output`` as it arrives.
        Sentinels and echoed command text are filtered out automatically.

        Args:
            cmd: The shell command to execute.
            expects: Optional list of (pattern, response) tuples. If a pattern
                appears in the output between the command's BEGIN marker and
                the end sentinel, the response is automatically sent; nothing
                before BEGIN is matched. Expects are inherently optional — if
                the pattern never appears, the end sentinel matches normally. A
                :class:`~otto.host.host.CredentialExpect` is answered at most once.
            timeout: Seconds before the command is considered hung. On expiry,
                the session attempts recovery via Ctrl+C and returns
                Status.Error. Defaults to :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`;
                pass ``float("inf")`` for a deliberately unbounded command.
            exec_input: ``None`` for a ``run``. For an ``exec``, what its frame
                needs (:func:`render_payload`): on a dialect that isolates
                ``exec``, the command runs in a subshell of its own, its
                status is read by
                :meth:`~otto.host.command_frame.CommandFrame.extract_exec_retcode`,
                and an interrupted one is followed by the dialect's
                post-recovery restore line; an interrupted one marked
                :attr:`~otto.host.command_frame.ExecInput.disposable` is
                stopped and left dead instead (``_stop_disposable``).

        Returns:
            CommandResult with exit code extracted from the sentinel.

        Raises:
            ~otto.host.errors.PasswordRefusedError: a
                :class:`~otto.host.host.CredentialExpect`'s prompt came back
                after it was answered. The command is interrupted and the
                shell probed first, as on a timeout.
        """
        # An attached AppShell has this session parked inside an application
        # REPL (mysql, python3); typing the sentinel command frame into it would
        # be gibberish. Cheap `is not None` guard on the hot path — a no-op when
        # nothing is attached (the default). Local import breaks the
        # app_shell <-> session import cycle.
        if self._app_shell is not None:
            from .app_shell import AppShellActiveError

            raise AppShellActiveError(
                f"{type(self._app_shell).__name__} is attached to this session; "
                f"run() is unavailable until the app shell exits"
            )
        if isinstance(self._frame, RawFrame):
            raise RawLandingError(
                f"{self._log_tag}: run() in a raw landing — use send()/expect() to reach "
                f"the target shell, then enter_frame() before run()"
            )
        await self._ensure_ready()
        # A stop that could not confirm the shell (an unconfirmed restore
        # after a timed-out or cancelled exec) left it dead: nothing more is
        # typed into it.
        self._require_alive()
        sink = on_output if on_output is not None else self._on_output
        output = ExecOutput(valuer=self._partial_value())
        # A live frame's line logger, begun at the BEGIN line; a buffered
        # frame never begins it and logs its parsed output instead.
        line_log = self._line_log(sink)
        # What an interrupted command leaves to restore: only an exec that ran
        # isolated has a tail of its own that the interrupt skipped.
        restore = (
            exec_input.restore if exec_input is not None and self._frame.isolates_exec else None
        )
        # A pooled exec's session is closed after a stop, so nothing confirms it.
        disposable = exec_input is not None and exec_input.disposable
        try:
            # Always bounded: `inf` never fires, so an intentionally unbounded
            # command needs no separate branch.
            return await result_within(
                self._run_cmd_inner(
                    cmd,
                    expects,
                    sink,
                    output,
                    line_log,
                    redact,
                    write_progress,
                    exec_input=exec_input,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # The value is what the command had written when the budget ran
            # out: what the reader holds past its last match, taken before
            # the interrupt is written. What the recovery reads after it (the
            # echoed ^C, the prompt) is the shell's, never the command's.
            taken = await self._take_unread()
            loggable = self._before_end_record(taken)
            unread = output.feed(loggable)
            output.feed(taken[len(loggable) :])
            await self._stop_under_bound(restore=restore, disposable=disposable)
            result = timed_out_result(cmd=cmd, timeout=timeout, output=output)
            if line_log.holds:
                # The complete lines of the timed-out value not yet logged,
                # whatever the frame held: the log is the value's lines.
                line_log.stop(unread)
                line_log.stop_settled(_timed_out_value_lines(output))
            elif line_log.begun:
                # The complete lines the reader still held before any END
                # record are the command's.
                line_log.stop(unread)
            elif not self._frame.streams_output_live:
                OutputLog(sink).stop(_timed_out_lines(output))
            return result
        except _CredentialRefusedError as refused:
            # The prompt came back after the one answer: typing the password
            # again would spend another authentication attempt on a password
            # already refused, and waiting would cost the whole timeout.
            await self._stop_under_bound(restore=restore, disposable=disposable)
            raise PasswordRefusedError(
                f"password refused for {refused.expect.user or 'the session user'} "
                f"on {refused.expect.host or 'this host'}"
            ) from None
        except asyncio.CancelledError as exc:
            # The command is stopped before the cancel returns, by the stop a
            # timeout gets, with this cancellation held: it is the one the
            # stop re-raises, whatever else lands while the stop runs, and the
            # stop is cut by its bound and the teardown deadline.
            from ..lifecycle import held_cancellation

            await self._stop_under_bound(
                restore=restore, held=held_cancellation(exc), disposable=disposable
            )
            raise
        except tuple(self._connection_lost_errors()) as exc:
            # Same class of event either way — the transport is gone: EOF on
            # the stream, asyncssh's own keepalive giving up on a dead peer
            # (e.g. a blackholed SSH connection), or a write landing on a
            # channel that already died while idle (BrokenPipeError — e.g. a
            # docker daemon restart killing the container between two
            # commands — the chaos-hardening write-path case: nothing was in flight to
            # observe the death via a read, so it only surfaces on the next
            # write). Give the caller the same truthful, already-rendered
            # CommandResult shape rather than letting a raw traceback escape.
            self._alive = False
            reason = (
                "EOF"
                if isinstance(exc, asyncio.IncompleteReadError)
                else "broken pipe"
                if isinstance(exc, BrokenPipeError)
                else "connection reset"
                if isinstance(exc, ConnectionResetError)
                else "connection lost"
            )
            return CommandResult(
                status=Status.Error,
                value=f"Session died unexpectedly ({reason})",
                command=cmd,
                retcode=-1,
            )

    # --- Internal implementation ---

    def _undo_terminal_mapping(self, text: str) -> str:
        r"""Undo what the session's terminal added to *text*: the ``onlcr`` fold, or nothing.

        Under ``onlcr`` every ``\n`` arrived with a CR the terminal added in
        front of it, so turning ``\r\n`` back into ``\n`` is the exact
        inverse, and a CR the program wrote stays. A finished command and a
        timed-out one's partial output are both folded here.
        """
        if self._terminal.mapping == ONLCR:
            return text.replace("\r\n", "\n")
        return text

    def _line_log(self, on_output: Callable[[str], None]) -> "OutputLog":
        """Return a command's line logger, begun at its BEGIN line (:meth:`_begin_line_log`).

        Each line is folded as the frame's parse folds its output: the
        terminal mapping undone, then what the frame's shell adds removed
        (:meth:`~otto.host.command_frame.CommandFrame.clean_output`).
        """
        return OutputLog(
            on_output,
            fold=lambda raw: self._frame.clean_output(self._undo_terminal_mapping(raw)),
            begun=False,
        )

    def _begin_line_log(self, line_log: "OutputLog", data: str) -> None:
        """Begin *line_log* at the read *data* that holds the command's BEGIN line.

        It holds each newest line when the BEGIN line, folded, still ends in
        the terminal's CR (:func:`~otto.host.command_frame.begin_line_ending`,
        the decision the frame's parse makes): only the next line or the end
        then tells whether a line's final CR is the terminal's or the
        program's.
        """
        folded = self._undo_terminal_mapping(data)
        start = folded.rfind(self._markers.begin) + len(self._markers.begin)
        line_log.begin(
            hold_newest=begin_line_ending(folded, start) == "\r\n",
            hold=self._frame.live_hold_lines,
            skip=self._frame.live_skip_lines,
        )

    def _logged_lines(self, text: str) -> list[str]:
        """Return the lines the host log shows for *text* this session read outside a command.

        The rule of a command's output (:func:`~otto.logger.relay.log_lines`
        of its ``value``): the terminal mapping undone, one trailing newline
        removed, split on newlines only. What :meth:`expect` returned is
        logged through this.
        """
        return log_lines(command_value(self._undo_terminal_mapping(text)))

    def _partial_value(self) -> Callable[[str], str]:
        """Return how a command's unfinished buffer becomes its timed-out ``value``.

        The frame's :meth:`~otto.host.command_frame.CommandFrame.parse_partial_output`
        over the buffer with the terminal mapping undone, as a finished
        command's :meth:`~otto.host.command_frame.CommandFrame.parse_output` is.
        """
        return lambda raw: self._frame.parse_partial_output(
            self._undo_terminal_mapping(raw), m=self._markers
        )

    def _stop_bound(self) -> float:
        """Return how long stopping a command otto gave up on may take, in seconds.

        The settle after the interrupt, the recovery deadline and one probe
        wait, read at call time so a rebound module value takes effect.
        """
        return _RECOVERY_SETTLE + _RECOVERY_TIMEOUT + _RECOVERY_PROBE_TIMEOUT

    async def _stop_interrupted_command(
        self, *, restore: ExecRestore | None, disposable: bool = False
    ) -> None:
        """Interrupt the running command and confirm the shell is back, or leave it dead.

        :meth:`_recover_session`, which marks the session dead when the
        probe fails; then, for an interrupted isolated ``exec`` (*restore*
        not ``None``), :meth:`_restore_after_interrupt`. A confirmed recovery
        (and restore) clears the pending recovery.
        On a *disposable* session (a pooled ``exec``'s, which its pool closes
        next) only ``_stop_disposable`` runs: the command is stopped (and,
        at a terminal, the shell confirmed back), nothing is restored, and the
        session is left dead.
        Reads no output for the caller: what recovery reads is the shell's.
        """
        if disposable:
            await self._stop_disposable()
            self._alive = False
            self._needs_recovery = False
            return
        await self._recover_session()
        if self._alive and restore is not None:
            await self._restore_after_interrupt(restore)
        if self._alive:
            self._needs_recovery = False

    async def _stop_disposable(self) -> None:
        """Stop a command on a session that is closed next: :meth:`_recover_session`, no restore.

        The recovery's interrupt and its probe, so the call returns only once
        the shell is back and the command it ran is gone; no restore line
        follows, since the session runs nothing more. In a raw landing
        nothing is written (see :meth:`_recover_session`). The caller marks
        the session dead whether or not the probe confirms.
        """
        await self._recover_session()

    async def _restore_after_interrupt(self, restore: ExecRestore) -> None:
        """Put back what an interrupted isolated ``exec``'s own end would have restored.

        The interrupt stops the ``exec`` frame before its tail runs, so the
        shell is left as the frame set it (``set +e``, the terminal as the
        command left it). One write of the frame's
        :meth:`~otto.host.command_frame.CommandFrame.restore_line` and one
        read of its
        :meth:`~otto.host.command_frame.CommandFrame.restored_pattern`, bounded
        by the recovery probe's wait; what it reads is consumed, never output.
        A frame without the restore, a read that times out or a transport
        that drops marks the session dead, and nothing else: it never
        recovers again.
        """
        line = self._frame.restore_line(restore, self._markers)
        pattern = self._frame.restored_pattern(self._markers)
        if line is None or pattern is None:
            logger.debug(f"{self._log_tag}: no restore line for an interrupted exec; dead")
            self._alive = False
            return
        try:
            await self.write_transport(data=line)
            await result_within(self.read_transport_until(pattern=pattern), _RECOVERY_PROBE_TIMEOUT)
        except (asyncio.TimeoutError, *self._connection_lost_errors()):
            logger.debug(f"{self._log_tag}: restore after interrupt failed; session marked dead")
            self._alive = False

    async def _stop_under_bound(
        self,
        *,
        restore: ExecRestore | None,
        held: "asyncio.CancelledError | None" = None,
        disposable: bool = False,
    ) -> None:
        """Stop a command otto gave up on (a timeout, a refused password, a cancel) under one bound.

        The stop runs under :func:`~otto.lifecycle.compensate` with
        :meth:`_stop_bound` as its timeout: a cancellation landing during it
        is held until the stop ends, and a stop that does not confirm within
        the bound (a blocked write, a shell that never answers) leaves the
        session dead, to be closed later, and the pool discards it.
        *restore* is what an interrupted isolated ``exec`` leaves
        to put back (:meth:`_stop_interrupted_command`), ``None`` otherwise.
        *held* is the cancellation that stopped the command, when one did
        (:func:`~otto.lifecycle.held_cancellation`): it is re-raised once the
        stop ends, confirmed or not, and the teardown deadline bounds the
        stop from the start. *disposable* stops without restoring and leaves
        the session dead (:meth:`_stop_interrupted_command`).
        """
        # Imported here, not at module scope: otto.lifecycle is needed only
        # when a command is stopped, as for the proxied undo below.
        from ..lifecycle import compensate

        self._needs_recovery = True
        try:
            await compensate(
                self._stop_interrupted_command(restore=restore, disposable=disposable),
                timeout=self._stop_bound(),
                what=f"{self._log_tag}: stop the interrupted command",
                held=held,
            )
        finally:
            if self._needs_recovery:
                self._alive = False

    def _before_end_record(self, data: str) -> str:
        """Return the part of *data* that may be logged: all of it before the END record.

        Nothing at or after the record's start is ever the command's output,
        whichever path logs it: the read that ends the command, or what a
        reader still held when the budget ran out.
        """
        end = self._end_pattern.search(data)
        return data if end is None else data[: end.start()]

    async def _run_cmd_inner(
        self,
        cmd: str,
        expects: list[Expect | CredentialExpect] | None,
        on_output: Callable[[str], None],
        read: "ExecOutput",  # defined below, with the drain that shares it
        line_log: "OutputLog",
        redact: bool = False,
        write_progress: Callable[[int, int], None] | None = None,
        *,
        exec_input: ExecInput | None = None,
    ) -> CommandResult:
        """Send sentinel-wrapped command, handle expects, surface output.

        Output is read line-by-line. How it reaches ``on_output`` depends on the
        frame's :attr:`~otto.host.command_frame.CommandFrame.streams_output_live`:
        a *live* frame (e.g. bash) emits each content line as it arrives, with
        sentinels and echoed command text stripped, and one that holds its
        newest lines back (Zephyr, whose prompts and retcode line follow the
        output) logs the rest of its value's lines at END; a *buffered* frame
        emits nothing mid-stream and surfaces ``parse_output(buffer)`` once on
        completion, so shell prompts and retcode scaffolding never reach the
        log. ``on_output`` is the per-command sink (the host's logger, or a
        no-op when the effective mode is ``LogMode.NEVER``). Every chunk read
        is fed to *read*, the command's :class:`ExecOutput`, which the caller
        still holds when a timeout cancels this read.
        """
        live = self._frame.streams_output_live
        # Build regex that matches any expect pattern OR the end sentinel OR
        # a newline.  The newline alternative causes read_transport_until to
        # return after every line, enabling incremental output streaming.
        # Built before the write, so a caller pattern it refuses
        # (scoped_source) sends nothing.
        combined = self._build_combined_pattern(expects)

        # Write the framed command. The framing — BEGIN/END sentinels and how
        # the exit code is captured — is supplied by the session's CommandFrame
        # so that a target can diverge from the bash form (see ZephyrFrame);
        # an exec gets the frame's exec frame (render_payload).
        framed = render_payload(
            self._frame,
            cmd,
            m=self._markers,
            exec_input=exec_input,
            terminal=self.reads_a_terminal,
        )
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
            await self.write_transport(data=framed)
        finally:
            self._write_progress = None
        if exec_input is not None and exec_input.on_typed is not None:
            exec_input.on_typed()

        # Read loop: handle expect responses; *read* accumulates every chunk
        # read, so a timeout that cancels this loop keeps them.
        seen_begin = False
        answered: set[int] = set()
        # A live frame logs each line of output as *read* completes it, the
        # terminal mapping undone, and the text before the END record once
        # the command ended. *line_log* begins at the BEGIN line, whose ending
        # says whether a line's final CR may be the terminal's, as the
        # frame's parse decides it.
        # The output line in progress at the END record, from its start to
        # the record: its complete lines and the unfinished last one are what
        # a live frame logs once the command ended.
        before_end = ExecOutput()
        ended: list[str] = []
        try:
            while True:
                # Where this read starts in the line in progress, for a
                # credential's read to be cut from it.
                prior = read.tail()
                start = len(prior)
                data = await self.read_transport_until(pattern=combined)

                end = self._end_pattern.search(data)
                if end:
                    # Nothing at or after the END record is ever logged,
                    # whatever the read spans (an expect match can carry it
                    # past the record): only the text before it is.
                    read.feed(data)
                    ended = before_end.feed(prior + self._before_end_record(data))
                    break
                completed = read.feed(data)

                # BEGIN first, and on every chunk, including one an expect
                # matches: a credential answer is gated on it. The begin marker
                # check uses rstrip + startswith to avoid false matches when the
                # shell echoes the wrapped command (which embeds the marker
                # inside quotes on the same line).
                began_before = seen_begin
                if not seen_begin and self._frame.marks_begin(data, m=self._markers):
                    if not redact:
                        logger.debug(f"{self._log_tag}: begin marker matched on chunk={data!r}")
                    seen_begin = True
                    if live:
                        self._begin_line_log(line_log, data)

                answering = self._matched_expect(
                    data, expects or [], seen_begin=seen_begin, answered=answered
                )

                # Stream each content line to the callback, before the answer
                # is typed: a timeout inside that write finds them logged.
                if line_log.begun and began_before:
                    _log_read(line_log, completed, start, data, answering)
                if answering is not None:
                    await self._answer_expect(answering, redact=redact)
        except BaseException:
            # Timed out, cancelled or refused: the complete lines held are logged.
            line_log.stop()
            raise

        # Everything that decides the session's health and the command's
        # result happens before the tail is logged, so a sink that fails on
        # it costs only log lines.
        buffer = self._undo_terminal_mapping(read.raw())
        if exec_input is not None:
            self._note_exec_baseline(buffer)
        begun = seen_begin or self._begun_before_end(buffer)
        if not begun and exec_input is not None and exec_input.identity is not None:
            # The identity guard skipped the command: this shell is not the
            # one the session was switched to.
            self._identity_lost = True
            logger.warning(f"{self._log_tag}: switched shell gone; the command was not run")
        if begun:
            output = self._frame.parse_output(buffer, cmd=cmd, m=self._markers)
            retcode = (
                self._frame.extract_retcode(buffer, m=self._markers)
                if exec_input is None
                else self._frame.extract_exec_retcode(buffer, m=self._markers)
            )
            status = Status.Success if retcode == 0 else Status.Failed
        else:
            # An end record with no BEGIN before it: the command never ran
            # (an identity guard in the wrong shell). Never a
            # success, whatever its digits; the raw text is in the DEBUG
            # line below, and the value is one stable sentence.
            output, retcode, status = NEVER_RAN, -1, Status.Failed
        # A live frame logged its lines as they completed and logs the rest,
        # up to the END record, here. A buffered frame (raw stream not clean
        # line-by-line), or a command whose BEGIN and END came in one read, logs the lines
        # of the parsed output once here, so the log never shows shell
        # prompts or the retcode scaffolding. A command that never ran
        # printed nothing: its value is otto's sentence, never logged as
        # output, on every frame.
        if line_log.holds:
            # A frame that held its newest lines back logs the rest of its
            # value's lines: what it held may be its tail.
            line_log.end(ended, before_end.tail(), log_lines(output))
        elif line_log.begun:
            for raw in ended:
                line_log.line(raw)
            line_log.finish(before_end.tail())
        elif begun:
            for line in log_lines(output):
                on_output(line)
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
                f"{self._log_tag}: run_cmd done "
                f"cmd={cmd!r} retcode={retcode} "
                f"output_len={len(output)} buffer={buffer_preview!r}"
            )
        return CommandResult(status=status, value=output, command=cmd, retcode=retcode)

    def _begun_before_end(self, buffer: str) -> bool:
        """Whether a line of *buffer* before its end record is the BEGIN marker's (marks_begin).

        For a read that brought BEGIN and END in one chunk, which the read
        loop's end check takes before its BEGIN check. Line by line, through
        the frame's own test, so an echoed copy of the typed line, which
        holds the marker inside a quoted word, is not taken for it.
        """
        end = self._end_pattern.search(buffer)
        head = buffer[: end.start()] if end else buffer
        return any(self._frame.marks_begin(line, m=self._markers) for line in head.splitlines())

    def _note_exec_baseline(self, buffer: str) -> None:
        """Record, and warn about once, an exec end record that reports the terminal record lost."""
        if self._exec_baseline_lost or self._frame.exec_baseline_present(buffer, m=self._markers):
            return
        self._exec_baseline_lost = True
        logger.warning(f"{self._log_tag}: exec terminal baseline lost; not restoring")

    def _matched_expect(
        self,
        data: str,
        expects: list[Expect | CredentialExpect],
        *,
        seen_begin: bool,
        answered: set[int],
    ) -> "Expect | CredentialExpect | None":
        """Return the first expect whose pattern is in *data*, to be answered; or ``None``.

        Nothing is answered before the command's BEGIN marker, on any frame:
        what precedes it is the shell's (the echoed command line, a console
        login profile's output), never the command's, so no pattern, the
        caller's or otto's own, is matched against it. A
        :class:`~otto.host.host.CredentialExpect` answers once: its index goes
        into *answered*. A later
        match raises :exc:`_CredentialRefusedError` when the pair is
        ``refused_on_repeat``, and is plain output otherwise.
        """
        if not seen_begin:
            return None
        for i, expect in enumerate(expects):
            pattern = expect.pattern if isinstance(expect, CredentialExpect) else expect[0]
            if not re.search(pattern, data):
                continue
            if isinstance(expect, CredentialExpect):
                if i in answered:
                    if expect.refused_on_repeat:
                        raise _CredentialRefusedError(expect)
                    continue  # answered once already: this match is output
                answered.add(i)
            return expect
        return None

    async def _answer_expect(self, expect: "Expect | CredentialExpect", *, redact: bool) -> None:
        """Type *expect*'s answer.

        The DEBUG line names the pattern and the answer's length, never the
        answer; under ``LogMode.NEVER`` (*redact*) there is no line at all.
        """
        if isinstance(expect, CredentialExpect):
            pattern, response = expect.pattern, expect.response
        else:
            pattern, response = expect
        await self.write_transport(data=response)
        if not redact:
            shown = pattern if isinstance(pattern, str) else pattern.pattern
            logger.debug(
                f"{self._log_tag}: expect matched "
                f"pattern={shown!r} response=<{len(response)} chars>"
            )

    def _build_combined_pattern(
        self,
        expects: list[Expect | CredentialExpect] | None,
    ) -> re.Pattern[str]:
        r"""Build a combined regex: expect patterns | end sentinel | newline.

        The ``\\n`` alternative (lowest priority) causes
        ``read_transport_until`` to return after every line, enabling
        incremental output streaming. Without *expects* the pattern is
        otto's own and is recorded as such
        (:func:`~otto.host.output_codec.bounded_pattern`).

        Each caller pattern joins as :func:`scoped_source` renders it, so it
        matches with its own flags, as it would alone under ``re.search``,
        and lends none of them to otto's parts or to another caller's.
        """
        parts: list[str] = []
        if expects:
            for i, expect in enumerate(expects):
                pattern = expect.pattern if isinstance(expect, CredentialExpect) else expect[0]
                parts.append(f"(?P<expect_{i}>{scoped_source(pattern)})")
        parts.append(f"(?P<sentinel>{self._end_pattern.pattern})")
        parts.append(r"(?P<newline>\n)")
        combined = re.compile("|".join(parts))
        # Recorded only when no caller pattern joined otto's parts: the
        # contract bounded_pattern states is otto's to keep, never a caller's.
        return combined if expects else bounded_pattern(combined)

    async def _recover_session(self, deadline: float | None = None) -> None:
        """Interrupt the hung command, then confirm the shell is back (echo-proof).

        Sends Ctrl+C, then drives :func:`~otto.host.shell_liveness.confirm_live`
        with the dialect's recover probe. In a
        :class:`~otto.host.command_frame.RawFrame` landing it writes nothing at
        all and marks the session dead: there is no probe to render, so the
        interrupt could confirm nothing, and otto has no dialect knowledge of
        what that byte means to the console it would land on.
        On bash the probe is exit-code framed,
        so a session parked inside a REPL (which can only echo the literal ``$?``)
        correctly fails to confirm and is marked dead rather than falsely
        "recovered". What the probe reads is consumed and returned to no one.

        ``deadline`` bounds the confirm-live resend loop (``None`` reads
        :data:`_RECOVERY_TIMEOUT` at CALL time — a def-time default would
        freeze the module value and make test rebinds silently inert, which
        is exactly what happened for two years); callers doing a *graceful*
        teardown under possible CPU starvation (see
        :meth:`otto.host.app_shell.AppShell._exit`) pass a larger budget so a
        load-slowed shell hand-back still confirms.
        """
        if isinstance(self._frame, RawFrame):
            # Same verdict as _confirm_recovered's guard, taken one step
            # earlier so the interrupt is never written: a raw landing has no
            # dialect, so otto does not know what 0x03 means to that console
            # (on a boot loader it interrupts autoboot) and nothing could
            # confirm the state it left behind. The confirm-side guard stays
            # for AppShell._exit's graceful path, which enters there directly.
            logger.debug(f"{self._log_tag}: recovery in a raw landing; session marked dead")
            self._alive = False
            return

        logger.debug(f"{self._log_tag}: recover_session entry marker={self._recover_marker!r}")
        try:
            await self.write_transport(data="\x03")
        except tuple(self._connection_lost_errors()):
            # This entry write is its OWN choke point, upstream of
            # _confirm_recovered's: a dead-but-idle channel (e.g. a docker
            # daemon restart killing the container between two commands) can
            # raise BrokenPipeError right here, before confirm_live ever
            # runs. Both call sites that reach _recover_session -- the
            # post-timeout leg in run_cmd and the pre-flight leg in
            # _ensure_ready -- have no CommandResult/exception boundary of
            # their own for this, so mirror _confirm_recovered's handling:
            # mark dead and return "" rather than letting a raw traceback
            # escape (run_cmd's sibling `except (...BrokenPipeError)` clause
            # on the same try can't see an exception raised inside this
            # handler's body).
            logger.debug(
                f"{self._log_tag}: recover_session entry write failed; session marked dead"
            )
            self._alive = False
            return
        await asyncio.sleep(_RECOVERY_SETTLE)
        await self._confirm_recovered(deadline)

    async def _confirm_recovered(self, deadline: float | None = None) -> None:
        """Drive :func:`~otto.host.shell_liveness.confirm_live` to confirm recovery.

        Shared by every ``_recover_session`` override — only how the hung
        command is interrupted (Ctrl+C byte vs. SIGINT-to-children) differs
        between them; confirming recovery is identical everywhere. ``deadline``
        is the overall give-up ceiling for the resend loop (``None`` reads
        :data:`_RECOVERY_TIMEOUT` at call time — the single resolution point
        for the whole recovery stack); it only bounds how long confirmation
        persists before declaring the shell dead — a responsive shell
        confirms on the first probe regardless.

        What it reads is recovery traffic, consumed and returned to no one; a
        shell that never confirms leaves ``self._alive`` False.
        """
        if isinstance(self._frame, RawFrame):
            # A raw landing has no probe to render. A recovery here means an
            # expect() was cancelled mid-landing; nothing can confirm the
            # console's state, so the session is dead rather than crashed.
            logger.debug(f"{self._log_tag}: recovery in a raw landing; session marked dead")
            self._alive = False
            return

        if deadline is None:
            deadline = _RECOVERY_TIMEOUT
        captured = ""

        async def _expect(pat: re.Pattern[str], t: float) -> str:
            nonlocal captured
            captured = await result_within(self.read_transport_until(pattern=pat), t)
            return captured

        try:
            confirmed = await confirm_live(
                lambda data: self.write_transport(data=data),
                _expect,
                self._frame.recover,
                self._frame.recover_pattern,
                lambda: self._markers,
                settle=0.0,  # the caller's post-interrupt sleep already settled
                probe_timeout=_RECOVERY_PROBE_TIMEOUT,
                deadline=deadline,
            )
        except tuple(self._connection_lost_errors()):
            # Same class of event as the EOF case: the transport is gone
            # mid-recovery (e.g. a blackholed connection's keepalive giving up
            # during the post-timeout Ctrl+C probe, or a still-dead connection
            # being re-confirmed after a prior cancellation) -- or the
            # confirm-live probe write itself lands on a channel that already
            # died while idle (BrokenPipeError; the confirm_live() call above
            # writes via self.write_transport, so a dead-but-idle channel surfaces
            # here too, not just as a read failure). This is the choke point
            # every recovery caller's CONFIRM leg funnels through -- the
            # post-timeout leg in run_cmd, the pre-flight leg in _ensure_ready
            # when _needs_recovery is set, and AppShell._exit's teardown (which
            # reaches both _recover_session and this method directly) -- so
            # widening it here closes all of them without touching
            # send()/_ensure_initialized. (It is not the SOLE recovery choke
            # point, though: _recover_session's own Ctrl+C entry write, just
            # before this method runs, is guarded the same way at its own
            # call site.)
            confirmed = False

        if not confirmed:
            logger.debug(f"{self._log_tag}: recover_session failed; session marked dead")
            self._alive = False
            return
        # What the probe read is recovery traffic (the echoed ^C, the prompt,
        # the probe's reply): consumed so it cannot reach the next command,
        # and never a command's output. A timed-out command's value was taken
        # before the interrupt (ShellSession._take_unread).
        logger.debug(f"{self._log_tag}: recover_session ok consumed={len(captured)}")


class _PatternReaderTransport:
    """The one :meth:`ShellSession._take_unread` of the sessions that read through a PatternReader.

    Mixed in ahead of :class:`ShellSession` by the ssh, local and
    interactive-bridge sessions, which all keep their reader in ``_reader``.
    """

    _reader: "PatternReader | None"

    async def _take_unread(self) -> str:
        """Return :meth:`PatternReader.take_unmatched`, or ``""`` before the transport opened."""
        return self._reader.take_unmatched() if self._reader is not None else ""


class SshSession(_PatternReaderTransport, ShellSession):
    """SSH persistent shell session via asyncssh create_process()."""

    def __init__(
        self,
        conn: "SSHClientConnection | None",
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        shell_history: bool = True,
        terminal: bool = True,
    ) -> None:
        super().__init__(
            command_frame=command_frame,
            init_timeout=init_timeout,
            shell_history=shell_history,
            terminal=terminal,
        )
        self._conn = conn
        self._process: Any = None
        # One per channel: open_transport replaces it with each new process.
        self._reader: PatternReader | None = None
        # When set by a subclass, open_transport passes this as the command to
        # create_process() instead of opening the channel's default shell.
        self._open_cmd: str | None = None
        # The token a shell without a terminal is started with, which
        # everything it starts inherits: how _recover_session finds what to
        # stop. None with a terminal, where Ctrl+C does that.
        self._mark: str | None = None

    @override
    def _connection_lost_errors(self) -> list[type[BaseException]]:
        from ._asyncssh import asyncssh

        return [*super()._connection_lost_errors(), asyncssh.ConnectionLost]

    @override
    async def open_transport(self) -> None:
        from ._asyncssh import asyncssh

        assert self._conn is not None, "SshSession._conn must be set before open_transport()"  # noqa: S101 — internal invariant: _conn set by subclass before open_transport()
        # Bytes (``encoding=None``), read in chunks through the session's
        # PatternReader: decoded by otto's one output decoder and searched the
        # way every chunked reader searches, so a pattern matches here exactly
        # as it does on a local, bridge or telnet session. The channel is this
        # session's own and nothing else reads it, so what the reader holds
        # past a match is never another reader's.
        args = [] if self._open_cmd is None else [self._open_cmd]
        # A terminal is a "dumb" pty. Without one no pty is requested, said
        # outright so a connection option (``RequestTTY force``) cannot ask
        # for one: the login shell then reads a pipe.
        pty: dict[str, Any] = (
            {"term_type": "dumb"} if self.reads_a_terminal else {"request_pty": False}
        )
        if not self.reads_a_terminal and self._open_cmd is None:
            # The same login shell, started marked (see _recover_session).
            self._mark = secrets.token_hex(16)
            args = [process_stop.marked_shell_command(process_stop.SESSION_MARK, self._mark)]
        # The shell without a terminal joins its own stderr to stdout on the
        # server (marked_shell_command). Merging stderr here still carries
        # what the server's first shell prints before it execs that one, and
        # a terminal session's single stream.
        self._process = await self._conn.create_process(
            *args, **pty, stderr=asyncssh.STDOUT, encoding=None
        )
        stdout = self._process.stdout

        async def read_chunk() -> bytes:
            return await stdout.read(READ_CHUNK)

        self._reader = PatternReader(read_chunk)

    @override
    async def write_transport(self, *, data: str | SecretStr) -> None:
        assert self._process is not None  # noqa: S101 — internal invariant: open_transport() must run before write_transport()
        self._process.stdin.write(reveal(data).encode(OUTPUT_ENCODING))

    @override
    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        assert self._reader is not None  # noqa: S101 — internal invariant: open_transport() must run before read_transport_until()
        return await self._reader.read_transport_until(pattern)

    @override
    async def _recover_session(self, deadline: float | None = None) -> None:
        """Stop the hung command: Ctrl+C at a terminal, the end of the session without one.

        With a terminal, the base session's Ctrl+C and confirmation. Without
        one a Ctrl+C byte is only a byte (a pipe has no line reader to turn
        it into a signal), and closing the channel stops nothing (no
        terminal, no SIGHUP). The session's shell was started with a token
        in its environment (:data:`otto.host.process_stop.SESSION_MARK`), so the stop is a second
        channel on the same connection that kills every process carrying it:
        the shell, its subshells, the command and whatever the command
        started. The same on every ssh server, since nothing depends on how
        the server delivers a signal.

        The shell is NOT kept, and the session is marked dead; its owner
        opens a fresh one for the next command. A shell that reads a pipe is
        not interactive, so one that outlived the stop would go on to run the
        rest of the line otto gave up on: after ``sleep 600; rm -rf build``
        timed out, the ``rm`` would run.

        The channel's own ``KILL`` request follows, for a host where the
        sweep found nothing to read (no ``/proc``) or could not run. What it
        reaches is the server's choice: OpenSSH signals the shell's whole
        process group, dropbear the shell alone. ``KILL`` because no shell
        outlives it: bash, alone signalled with ``INT``, waits for its
        command and carries on.
        """
        if self.reads_a_terminal or isinstance(self._frame, RawFrame):
            await super()._recover_session(deadline)
            return
        from ._asyncssh import asyncssh

        assert self._conn is not None  # noqa: S101 — internal invariant: open_transport() ran before any command
        assert self._process is not None  # noqa: S101 — internal invariant: open_transport() ran before any command
        gone = (OSError, ValueError, asyncssh.Error, *self._connection_lost_errors())
        try:
            if self._mark is not None:
                await result_within(
                    self._conn.run(
                        process_stop.reap_marked_command(process_stop.SESSION_MARK, self._mark),
                        check=False,
                        request_pty=False,
                    ),
                    _RECOVERY_TIMEOUT if deadline is None else deadline,
                )
        except (asyncio.TimeoutError, *gone) as exc:
            logger.debug(f"{self._log_tag}: the marked stop did not run: {exc!r}")
        finally:
            # Sent even when the sweep is cut short (its bound, a cancel).
            # The channel may be gone already, as it is once the sweep ended
            # the shell.
            with suppress(*gone):
                self._process.send_signal("KILL")
            logger.debug(f"{self._log_tag}: no terminal: stop sent, session marked dead")
            # The shell is gone with its command: a later open of this
            # session has nothing left to recover.
            self._needs_recovery = False
            self._alive = False

    @override
    async def close(self) -> None:
        if self._process is not None:
            self._process.close()
            self._process = None
            self._reader = None
        self._alive = False
        self._initialized = False


async def _readuntil_past_limit(
    reader: Any, pattern: re.Pattern[bytes], *, bounded: bool, taken: bytearray
) -> bytes:
    r"""telnetlib3's ``readuntil_pattern``, for a match further away than the reader's limit.

    telnetlib3 raises :exc:`asyncio.LimitOverrunError` once the bytes up to a
    match, or the unmatched buffer, outgrow its limit (64 KiB by default), and
    leaves every byte in its buffer. One long line with no newline would fail
    the read. Below the limit this is ``readuntil_pattern`` itself; past it,
    *bounded* (the caller asked
    :func:`~otto.host.output_codec.is_bounded` of the pattern it was given)
    picks one of two ways on.

    **Otto's own pattern** (*bounded*): every match is at most
    :data:`~otto.host.output_codec.MAX_SEPARATOR_LEN` long and needs no
    context further back, so on each overrun all but the last
    ``MAX_SEPARATOR_LEN`` bytes of what telnetlib3 reports as ``consumed``
    (the match's end, or the buffer's length) are taken with ``readexactly``
    (at least one, so the read always moves), and the read is asked again.
    The buffer telnetlib3 rescans on every feed stays under the limit, so a
    long line costs work in proportion to its length.

    **A caller's pattern**: the reader's limit is raised for this read (at
    least doubled, and past the match the overrun reports) and the same read
    is asked again; nothing is taken, so every search is over the whole line
    since the call, in its true context: ``^``, ``\A`` and a lookbehind see
    the bytes that really precede them, never a cut that taking a prefix
    would have made look like the start of the text. That is the match
    :func:`~otto.host.output_codec.bounded_pattern` states for every
    pattern, and its cost here is the one case left QUADRATIC:
    telnetlib3 searches its whole buffer after every feed, so a caller's
    pattern waiting past 64 KiB of one line costs work in proportion to the
    line's length times the number of pieces it arrived in.

    Either way the match is the one a reader with no limit would find, only
    the bytes up to its end are consumed, so the other readers of the stream
    see what follows it, and the reader's limit is the one it had when the
    read ends. End of stream raises :exc:`asyncio.IncompleteReadError` with
    everything read since the call, as ``readuntil_pattern`` does. The bound
    is in bytes, as telnetlib3's limit is.

    *taken* is the caller's: the bytes otto's own pattern took past the limit
    wait there until the match returns them, so a read cancelled before its
    match (a command's timeout) leaves them where the caller can take them.

    The reader's transport pauses at twice the limit and its wait resumes it.
    telnetlib3 5.0.1 has no public setter for the limit; ``_limit`` is pinned
    by tests/unit/host/test_telnet_long_line.py, so an upgrade that renames it
    fails there by name.
    """
    if bounded:
        return await _readuntil_taking_prefix(reader, pattern, taken)
    return await _readuntil_raising_limit(reader, pattern)


async def _readuntil_taking_prefix(
    reader: Any, pattern: re.Pattern[bytes], taken: bytearray
) -> bytes:
    """Read past the limit for otto's own pattern: take all but a tail into *taken*, retry."""
    while True:
        try:
            raw: bytes = await reader.readuntil_pattern(pattern)  # ty: ignore[unsound-assignment] — telnetlib3 reader is untyped here (Any)
        except asyncio.LimitOverrunError as exc:
            taken += await reader.readexactly(max(1, exc.consumed - MAX_SEPARATOR_LEN))
            continue
        except asyncio.IncompleteReadError as exc:
            head = bytes(taken)
            taken.clear()
            raise asyncio.IncompleteReadError(head + exc.partial, exc.expected) from exc
        head = bytes(taken)
        taken.clear()
        return head + raw


async def _readuntil_raising_limit(reader: Any, pattern: re.Pattern[bytes]) -> bytes:
    """Read past the limit for a caller's pattern: raise the limit, search again."""
    original: int | None = None  # the reader's own limit, once this read has raised it
    try:
        while True:
            try:
                raw: bytes = await reader.readuntil_pattern(pattern)  # ty: ignore[unsound-assignment] — telnetlib3 reader is untyped here (Any)
            except asyncio.LimitOverrunError as exc:
                if original is None:
                    original = reader._limit  # noqa: SLF001  # ty: ignore[unsound-assignment] — telnetlib3 is untyped (Any) and has no public limit setter; pinned by test_telnet_long_line.py
                reader._limit = max(2 * reader._limit, exc.consumed + 1)  # noqa: SLF001 — as above
                continue
            return raw
    finally:
        if original is not None:
            reader._limit = original  # noqa: SLF001 — as above


@final
class TelnetSession(ShellSession):
    """Telnet persistent shell session via telnetlib3 streams.

    Built on exactly one client that holds the streams: a
    :class:`~otto.host.telnet.TelnetClient` (*client*) or, on a console term,
    a :class:`~otto.host.console.ConsoleClient` (*console_client*). The
    session reads through that client's read gate and closes through its
    ``close()``, so a stream otto releases is always released by its client,
    which drains a single-client console to quiet first.

    Raises:
        ValueError: neither client is given, or both are.
    """

    def __init__(
        self,
        reader: Any,
        writer: Any,
        client: "TelnetClient | None" = None,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        write_chunk_size: int = 0,
        write_chunk_delay: float = 0.0,
        shell_history: bool = True,
        console_client: "ConsoleClient | None" = None,
        failure_context: str = "",
    ) -> None:
        if (client is None) == (console_client is None):
            raise ValueError("a TelnetSession needs exactly one client: client= or console_client=")
        # The one client holding the streams.
        holder = client or console_client
        assert holder is not None  # noqa: S101 — internal invariant: exactly one was given, checked above
        super().__init__(
            command_frame=command_frame,
            init_timeout=init_timeout,
            shell_history=shell_history,
            failure_context=failure_context,
            console_client=console_client,
        )
        self._reader = reader
        self._writer = writer
        # The client whose streams these are: shared (the manager's cached
        # client, for the default session) or owned (a named or exec-pool
        # session's own). Either way its close() is the one closer; it is
        # idempotent and joins a close already running.
        self._client = client
        # Every read of the stream runs inside its client's gate, which the
        # client's release closes: a read that starts after that fails as a
        # dropped line instead of colliding with the release's own read.
        self._read_gate: ReadGate = holder.read_gate
        # ``readuntil_pattern`` stops where a byte pattern matched, which can
        # be inside a multi-byte character; the held decoder keeps that
        # character's bytes until read_transport_until reads the rest.
        self._decoder = output_decoder()
        # What a read has taken from the stream and not yet returned: bytes
        # taken past the reader's limit before the match, and the decoded
        # match while the rest of a split character is read. Held here, not
        # in the read's locals, so a read cancelled by a command's timeout
        # leaves them for _take_unread.
        self._taken = bytearray()
        self._unreturned = ""
        # On a console term (``console_client``, held by the base) the
        # *reader*/*writer* are that client's streams, and closing this
        # session closes it (logout, then release). Every telnet-term session
        # has ``_client`` instead.
        # The retry verdict starts from the one rule (``retries_failed_open``);
        # a client that typed a password anyway narrows it.
        if console_client is not None:
            self._retry_failed_handshake = (
                retries_failed_open("console", console_client.options)
                and not console_client.logged_in
            )
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
    async def open_transport(self) -> None:
        # Transport already established by TelnetClient login — nothing to open
        pass

    @override
    async def write_transport(self, *, data: str | SecretStr) -> None:
        # Line ends are TELNET_EOL (CR alone); see its docstring for why.
        if isinstance(data, SecretStr):
            # A password is one short line: never chunked, and unwrapped only
            # inside the write itself.
            self._writer.write(reveal(data, TELNET_EOL).encode())
            return
        data = telnet_line_ends(data)
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
    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        # telnetlib3 operates in bytes mode — compile a bytes version of the
        # pattern, with its flags (MULTILINE, DOTALL...): only re.UNICODE, which
        # every str pattern carries and a bytes pattern refuses, is dropped.
        bytes_pattern = re.compile(pattern.pattern.encode(), pattern.flags & ~re.UNICODE)
        # Bound once, and both reads run inside the gate: a read admitted
        # before the client's release began finishes on the reader it started
        # with, and the release waits for it; a read that starts afterwards
        # raises ReadGateClosed, a lost connection.
        reader = self._reader
        with self._read_gate.reading():
            try:
                raw = await _readuntil_past_limit(
                    reader, bytes_pattern, bounded=is_bounded(pattern), taken=self._taken
                )
            except asyncio.IncompleteReadError as exc:
                # End of stream: flush the decoder, as every output reader does,
                # so an incomplete character still held shows in ``partial`` as
                # U+FFFD.
                tail = self._decoder.decode(exc.partial, final=True)
                raise asyncio.IncompleteReadError(tail.encode(), exc.expected) from exc
            self._unreturned = self._decoder.decode(raw)
            # The byte match can end inside a character: read the rest of it,
            # so the text holds the whole match.
            rest = await finish_character(self._decoder, reader.readuntil_pattern)
            text, self._unreturned = self._unreturned + rest, ""
            return text

    @override
    async def _take_unread(self) -> str:
        """Return what the cancelled read held, then what telnetlib3 buffered, in order.

        The read's own held state comes first (the decoded match waiting on a
        split character, or the bytes taken past the reader's limit), then
        the reader's buffer, which a cancelled ``readuntil_pattern`` leaves
        whole. The buffer is taken with ``readexactly`` of its length, which
        never waits (the bytes are there) and resumes a paused transport, and
        under the read gate like every other read of the stream: entering the
        gate never blocks, it only refuses once the client's release began.
        A refused gate or a reader carrying the connection's failure takes
        nothing from the stream: the recovery that follows meets the dead
        transport and gives the session's verdict on it. telnetlib3 5.0.1 has
        no public buffered length; ``_buffer`` is pinned by
        tests/unit/host/test_telnet_long_line.py as ``_limit`` is.
        """
        held = self._unreturned
        self._unreturned = ""
        if self._reader.exception() is not None:
            taken = bytes(self._taken)
            self._taken.clear()
            return held + self._decoder.decode(taken)
        try:
            with self._read_gate.reading():
                size = len(self._reader._buffer)  # noqa: SLF001 — telnetlib3 exposes no buffered length; pinned by test_telnet_long_line.py
                buffered = await self._reader.readexactly(size) if size else b""
        except tuple(self._connection_lost_errors()):
            buffered = b""
        taken = bytes(self._taken) + buffered
        self._taken.clear()
        return held + self._decoder.decode(taken)

    @override
    def _connection_lost_errors(self) -> list[type[BaseException]]:
        """Add :class:`~otto.host.telnet.ReadGateClosed`: a read refused because the release began.

        Another task closing the client while a command still reads takes the
        stream away under that command: the same event as EOF.
        """
        return [*super()._connection_lost_errors(), ReadGateClosed]

    @override
    async def _refused_login(self) -> Exception | None:
        """On a console, ask the client whether getty is back on the line.

        The client owns the prompt rules (see
        :meth:`~otto.host.console.ConsoleClient.refused_after_login`): its
        refusal is returned for :meth:`_fail_init` to raise once the session
        is closed; otherwise what it quoted from the line is kept for the
        generic message. ``None`` for every non-console telnet session.
        """
        client = self.console_client
        if client is None:
            return None
        try:
            self._handshake_seen = await client.refused_after_login()
        except ConsoleError as exc:
            return exc
        return None

    @override
    def _retries_named_open(self) -> bool:
        if self.console_client is not None:
            return retries_failed_open("console", self.console_client.options, named=True)
        options = None if self._client is None else self._client.options
        return retries_failed_open("telnet", telnet_options=options, named=True)

    @override
    async def _seen_at_open(self, partial: str) -> str:
        """Quote the device's words before a failed open, off a console.

        The text is *partial* then what no read took (:meth:`_take_unread`).
        Lines carrying this session's marker are otto's own probe, echoed by
        the tty, and are dropped; the rest goes through the console client's
        formatter (:func:`~otto.host.console.seen_suffix`) with this client's
        password, which the formatter scrubs before it cuts: a login that
        discarded the password echoed it first. A console session quotes
        through :meth:`_refused_login` instead, so this returns ``""`` there.
        The two hooks stay separate because they decide different things:
        ``_refused_login`` names a console's refusal and makes the open raise
        it, while this one only adds a quote to the generic message.
        """
        if self.console_client is not None or self._client is None:
            return ""
        from .console import seen_suffix  # off the session import path: a failed open only

        text = partial + await self._take_unread()
        lines = text.splitlines(keepends=True)
        device = "".join(line for line in lines if self._session_id not in line)
        return seen_suffix(device, self._client.password)

    @override
    async def console_login(self) -> None:
        """Run the console login state machine on this session's own streams.

        For a ``landing_frame: "raw"`` host whose hook drives a boot menu
        before Linux reaches ``login:``: the host declares
        ``console_options.login: false`` so the transport does not try, and
        the hook calls this once the prompt is in sight. Same errors, same
        cred, one implementation.
        """
        if self.console_client is None:
            raise ConsoleError(
                f"{self._log_tag}: console_login() needs a console term; this session is not one"
            )
        await self.console_client.login_sequence()
        # The client just typed the password: a retry would type it again.
        # This narrows ``retries_failed_open``, whose rule cannot know that a
        # hook logs in on a console declared ``login: false``.
        # Set is unconditional (rather than mirroring console_client.logged_in
        # live) so this stays correct even if a future client subclass
        # returns False for a login it still performed.
        self._retry_failed_handshake = False

    @override
    async def close(self) -> None:
        """Close through the client that holds the streams; never close them directly.

        The client's close is the one release: a console logs out first, and
        a single-client line is drained to quiet before it is closed. It is
        idempotent and joins a close already running, so a default session
        and its manager closing the same shared client release it once.
        Before that, a console's settings go back to what the handshake found
        (``_put_terminal_back``), while the shell can still run it.
        """
        await self._put_terminal_back()
        if self.console_client is not None:
            await self.console_client.close()
        elif self._client is not None:
            await self._client.close()
        self._alive = False
        self._initialized = False


@final
class LocalSession(_PatternReaderTransport, ShellSession):
    """Local persistent bash shell session via asyncio subprocess.

    Implements the ShellSession I/O primitives using a long-running bash
    process, giving LocalHost the same sentinel-wrapped execution, expect
    handling, and timeout recovery that remote sessions enjoy.
    """

    reads_a_terminal = False  # a pipe: nothing echoes, whatever `stty -echo` answers

    def __init__(self) -> None:
        super().__init__()
        self._process: asyncio.subprocess.Process | None = None
        self._transport: asyncio.SubprocessTransport | None = None
        self._pid: int | None = None
        self._leader_exited = asyncio.Event()
        # Set once end_input() has ended the shell's input.
        self._input_ended = False
        # One per stream: open_transport replaces it with each new process.
        self._reader: PatternReader | None = None

    @override
    async def open_transport(self) -> None:
        # Drive loop.subprocess_exec() directly (rather than the higher-level
        # asyncio.create_subprocess_exec) so that we hold an explicit reference
        # to the transport. close() uses it to release the pipe fds without
        # reaching into the private Process._transport attribute.
        loop = asyncio.get_running_loop()

        # The shell leads its own session and process group, as local exec's
        # command does, so close() reaches what the session started.
        # LeaderExitProtocol says when the shell itself is reaped, apart from
        # pipe EOF a surviving member may hold off.
        def protocol_factory() -> process_stop.LeaderExitProtocol:
            return process_stop.LeaderExitProtocol(limit=2**16, loop=loop)

        transport, protocol = await loop.subprocess_exec(
            protocol_factory,
            "bash",
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._leader_exited = protocol.leader_exited
        self._input_ended = False
        self._transport = transport
        self._process = asyncio.subprocess.Process(transport, protocol, loop)
        self._pid = self._process.pid
        assert self._process.stdout is not None  # noqa: S101 — internal invariant: stdout=PIPE above
        self._bind_stdout(self._process.stdout)

    def _bind_stdout(self, stdout: asyncio.StreamReader) -> None:
        """Read the session from *stdout*, a fresh stream, through a fresh reader.

        The reader asks for :data:`~otto.host.output_codec.READ_CHUNK` bytes
        at a time and searches once per chunk, so a long line with no newline
        costs a few searches, not one per byte.
        """

        async def read_chunk() -> bytes:
            return await stdout.read(READ_CHUNK)

        self._reader = PatternReader(read_chunk)

    @override
    async def write_transport(self, *, data: str | SecretStr) -> None:
        assert self._process is not None  # noqa: S101 — internal invariant: process created in open_transport() before write_transport()
        assert self._process.stdin is not None  # noqa: S101 — internal invariant: process created in open_transport() before write_transport()
        self._process.stdin.write(reveal(data).encode())
        await self._process.stdin.drain()

    @override
    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        assert self._process is not None  # noqa: S101 — internal invariant: process created in open_transport() before read_transport_until()
        assert self._reader is not None  # noqa: S101 — internal invariant: reader created in open_transport() before read_transport_until()
        return await self._reader.read_transport_until(pattern)

    @override
    async def _recover_session(self, deadline: float | None = None) -> None:
        """Interrupt the hung command, then confirm the shell is back (echo-proof).

        SIGINT is sent to the child processes instead of a Ctrl+C byte, since a
        Ctrl+C byte does not cross a subprocess stdin PIPE.

        Same echo-proof confirmation as the base session: a shell parked inside a
        REPL can only echo the literal ``$?`` and so fails to confirm, is marked
        dead rather than falsely "recovered" (fixes I-3). Only the interrupt
        mechanism differs from the base. ``deadline`` is forwarded to
        :meth:`_confirm_recovered` (see the base override for its meaning).
        """
        import signal

        if self._pid is not None:
            self._signal_children(self._pid, signal.SIGINT)
        await asyncio.sleep(_RECOVERY_SETTLE)
        await self._confirm_recovered(deadline)

    @override
    async def _stop_disposable(self) -> None:
        """Write nothing: :meth:`close`, which the pool calls next, stops the shell's process group.

        A Ctrl-C byte does not cross a pipe, and the group's ``TERM`` reaches
        the command and what it started in the group.
        """

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
    async def end_input(self) -> None:
        """End an idle shell's input and wait, bounded, for the shell to exit on it.

        Only a live shell between commands is asked (not one still running a
        command or needing recovery); it reads the end at once, within
        ``_LOCAL_EOF_EXIT_WAIT``. Once it has exited, :meth:`close` sends
        no signal. A shell that has not exited by then is stopped by the
        close as always.

        What this spares is only the signal: the close still releases the
        shell's pipes, so a background process that kept the shell's output
        gets ``EPIPE`` (or ``SIGPIPE``) on its next write. One that should
        outlive the call redirects its output, and detaches (``setsid``) so
        that no later stop of a group reaches it.
        """
        proc = self._process
        if proc is None or not self._alive or self._needs_recovery:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        self._input_ended = True
        with suppress(asyncio.TimeoutError):
            await result_within(self._leader_exited.wait(), _LOCAL_EOF_EXIT_WAIT)

    @override
    async def close(self) -> None:
        """Stop the shell's process group through the shared stop, then release the pipes.

        ``process_stop.stop_local_session_group`` sends ``TERM`` to the
        group, and ``KILL`` to what is left of it after the grace; it holds a
        cancellation until the stop ends. A shell that already exited on the
        end of its input (:meth:`end_input`) gets no signal, so what an
        earlier command left running in its group is left as it is. The pipes
        and the transport are released whatever the stop does, so their
        ``__del__`` never calls into a closed loop. A second close finds no
        process and does nothing.
        """
        proc = self._process
        exited_on_its_input = self._input_ended and self._leader_exited.is_set()
        try:
            if proc is not None and not exited_on_its_input:
                await process_stop.stop_local_session_group(
                    process_stop.LocalStoppable(proc, self._leader_exited),
                    what=f"{self._log_tag}: close",
                )
        finally:
            if proc is not None:
                if proc.stdin is not None:
                    proc.stdin.close()
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

    ``user_getter`` is read at the same moment and for the same reason. The
    ``-u`` flag is fixed for the life of the channel, so the user has to be
    decided by whoever opens it — and an open can be initiated by ``run``,
    ``send`` or ``expect`` alike. The getter answers "what user would this
    host open a channel as, right now"; ``on_open`` is how the host learns
    what it actually got.

    ``on_open`` is invoked with that user AFTER the transport is up, and only
    for the DEFAULT run channel — named auxiliary sessions are built from a
    factory that leaves it ``None``, so they open with the same user but do
    not touch the host's record of which user the run channel is on. See
    :meth:`DockerContainerHost._record_run_channel_open`.
    """

    def __init__(
        self,
        conn_provider: "Callable[[], Awaitable[SSHClientConnection]]",
        container_id_getter: Callable[[], str],
        user_getter: "Callable[[], str | None] | None" = None,
        on_open: "Callable[[str | None], None] | None" = None,
    ) -> None:
        super().__init__(conn=None)
        self._conn_provider = conn_provider
        self._cid_getter = container_id_getter
        self._user_getter = user_getter
        self._on_open = on_open

    @override
    async def open_transport(self) -> None:
        import shlex

        self._conn = await self._conn_provider()
        cid = self._cid_getter()
        if not cid:
            raise RuntimeError(
                "DockerSshSession opened with empty container_id — "
                "DockerContainerHost._ensure_running must populate the id first."
            )
        opened_as = self._user_getter() if self._user_getter is not None else None
        u = f" -u {shlex.quote(opened_as)}" if opened_as is not None else ""
        self._open_cmd = f"docker exec -it{u} {shlex.quote(cid)} sh"
        await super().open_transport()
        # AFTER the transport is up, never before: an open that raises must
        # leave the host's bind record untouched.
        if self._on_open is not None:
            self._on_open(opened_as)


def _stamp_of(session: "ShellSession") -> Callable[[str], None]:
    """Return the stamp a switch on *session* makes: who its shell now runs as."""

    def stamp(login: str) -> None:
        session.current_user = login

    return stamp


class HostSession:
    """A named persistent shell session on any host type.

    Obtained via ``await host.open_session(name)``. Supports the async context
    manager protocol for automatic cleanup.

    **This class answers for itself under a dry run.** It talks to its
    :class:`ShellSession` directly and never passes through ``BaseHost.run`` /
    ``exec`` / ``expect``, so none of the host-layer dry-run hardening reaches
    it — until 2026-08-15 there was no ``is_dry_run()`` anywhere in the class
    and a dry run drove real commands down a session it already held. The arms
    now live on :meth:`run`, :meth:`send`, :meth:`expect`, :meth:`switch_user`
    and :meth:`as_user`, which is deliberately a SECOND guard rather than a
    redundant one: ``host.open_session`` hands out a
    :class:`DeclinedSession` under a dry run, but a handle opened in setup and
    used inside a LATER dry-run block is a real session that never went near
    that path.

    :meth:`close` is exempt, and that is a decision. Closing a transport the
    caller owns is not a command on a device, it is the caller's own cleanup,
    and it runs in ``finally`` blocks (including this class's ``__aexit__``)
    where raising would mask the exception that got there first.

    :meth:`enter_frame` is exempt too, for a narrower reason: it exists only
    on the handle a session-setup hook receives, and that handle exists only
    while a session is being established — which a dry run never does, since
    no session opens under one.

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

    def __init__(  # noqa: PLR0913 — wide session-handle API: each arg is one thing the manager already resolved (creds, identity, shell prefix, frame, term)
        self,
        name: str,
        session: ShellSession,
        log_command: Callable[[str, LogMode], None],
        log_output: Callable[[str, LogMode], None],
        deregister: Callable[[str], None],
        creds: "list[Cred] | None" = None,
        host_id: str = "",
        target_frame: "CommandFrame | None" = None,
        establishing: bool = False,
        *,
        term: "str | None" = None,
        fold_log: "Callable[[LogMode], LogMode]",
    ) -> None:
        self._name = name
        self._session = session
        self._log_command = log_command
        self._log_output = log_output
        # The host's standing mode folded into a command's, as the host's own
        # `run` folds it (`BaseHost._effective_log`): one log mode per command
        # on every entry, dry and live.
        self._fold_log = fold_log
        self._deregister = deregister
        # Creds this session's switch_user/as_user resolve targets against
        # (via otto.host.login_proxy.perform_switch). None on non-posix
        # hosts → this session cannot elevate; [] means supported but
        # passwordless / ad-hoc. Set by SessionManager.
        self._creds = creds
        self._host_id = host_id
        # Set only on the handle a session-setup hook receives: the frame
        # enter_frame() switches to, and the guard that keeps the hook from
        # closing a session the manager is mid-way through establishing.
        self._target_frame = target_frame
        self._establishing = establishing
        # The protocol this session speaks, so its switch_user/as_user resolve
        # a target (and its via account) under the term's scope -- a login
        # with a term-scoped password gets that password, both switching in
        # and unwinding (spec 2026-09-13 cred-scope 3.3). None on a session
        # whose manager has no term concept, which keeps the by-login lookup.
        self._term = term

    @property
    def alive(self) -> bool:
        """Whether the underlying shell session is still active."""
        return self._session.alive

    @property
    def exec_baseline_lost(self) -> bool:
        """Whether an ``exec`` found the shell's terminal record gone.

        The underlying session's :attr:`ShellSession.exec_baseline_lost`.
        """
        return self._session.exec_baseline_lost

    @property
    def name(self) -> str:
        """The name this session was opened under (``open_session``'s argument)."""
        return self._name

    @property
    def current_user(self) -> str:
        """User this named session is currently running as.

        Seeded from the host's login user; changed only via
        :meth:`switch_user` / :meth:`as_user`.
        """
        return self._session.current_user

    @property
    def terminal_mapping(self) -> str:
        """How this session's terminal maps its output, as its last handshake measured it.

        ``identity``, ``onlcr``, ``unknown(<observed>)`` or ``unmeasured``,
        with ``, tabs expanded`` after it where tabs did not survive and
        ``, echo on`` where echo is still on; see :mod:`otto.host.terminal_mapping`.
        """
        return self._session.terminal_mapping

    @property
    def echoes(self) -> bool:
        """Whether this session's terminal may still echo (:attr:`ShellSession.echoes`)."""
        return self._session.echoes

    @property
    def reads_a_terminal(self) -> bool:
        """Whether this session's shell reads a terminal (:attr:`ShellSession.reads_a_terminal`)."""
        return self._session.reads_a_terminal

    def _refuse_elevation(self, verb: str, user: str) -> Never:
        """Bind this session's identity to the shared elevation refusal.

        The reasoning lives with the refusal itself
        (:func:`~otto.host.host.refuse_declined_elevation`), which the
        HOST-level ``switch_user``/``as_user`` share: same refusal, two
        objects, one wording.

        Args:
            verb: the method the caller invoked, named in the caller's words.
            user: the elevation target, already defaulted for display.

        Raises:
            ~otto.result.CommandNotRunError: always.
        """
        refuse_declined_elevation(verb, user, self._host_id, self._log_command)

    def _refuse_switch_without_a_terminal(self) -> None:
        """Refuse a user switch on a session opened with ``terminal=False``.

        The host's own refusal (``BaseHost._refuse_terminal``), for a named
        session: the switch's password prompt is answered on a terminal. A
        session whose family never had one (the local host's) is not such a
        session and switches as it always did: only a session whose mode
        differs from its transport's own was opened that way.
        """
        if self._session.reads_a_terminal or not type(self._session).reads_a_terminal:
            return
        from .errors import terminal_refusal  # a refusal only: off the startup graph
        from .host import _NO_TERMINAL_SWITCH

        raise terminal_refusal(host=self._host_id, terminal=False, reason=_NO_TERMINAL_SWITCH)

    async def switch_user(self, *, user: str = "", password: str | SecretStr | None = None) -> None:
        """``su`` *this* session to *user* (default root), tracking :attr:`current_user`.

        Posix-only — raises ``NotImplementedError`` on
        hosts whose sessions do not support elevation (no creds).

        See Also:
            :meth:`as_user` — the scoped form; prefer it unless the switch
            should outlive the block.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run — see
                ``_refuse_elevation``.
        """
        if self._creds is None:
            raise NotImplementedError("switch_user is not supported on this host's sessions")
        self._refuse_switch_without_a_terminal()
        if is_dry_run():
            self._refuse_elevation("switch_user", user or ROOT_LOGIN)
        await switch_and_stamp(
            io=self,
            creds=self._creds,
            user=user,
            password=password,
            current_user=self._session.current_user,
            stamp=_stamp_of(self._session),
            host_id=self._host_id,
            shell_prefix=self._session._new_shell_prefix(),  # noqa: SLF001 — the session this handle wraps
            term=self._term,
        )

    @asynccontextmanager
    async def as_user(
        self, *, user: str = ROOT_LOGIN, password: str | SecretStr | None = None
    ) -> "AsyncGenerator[HostSession, None]":
        """Run a block as *user* on this session, restoring the prior user on exit.

        Undoes each applied hop in reverse (innermost first) so a multi-hop
        ``via`` chain unwinds correctly, mirroring
        :meth:`~otto.host.privilege.PosixPrivilege.as_user`.

        The scoped form of :meth:`switch_user` — same engine
        (:func:`~otto.host.login_proxy.perform_switch`), plus undo.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run — see
                ``_refuse_elevation``. It fires on ENTRY, above the
                ``try``, so no undo is scheduled for a switch that never
                happened.
        """
        if self._creds is None:
            raise NotImplementedError("as_user is not supported on this host's sessions")
        self._refuse_switch_without_a_terminal()
        if is_dry_run():
            self._refuse_elevation("as_user", user)
        prev = self.current_user
        applied = await switch_and_stamp(
            io=self,
            creds=self._creds,
            user=user,
            password=password,
            current_user=self._session.current_user,
            stamp=_stamp_of(self._session),
            host_id=self._host_id,
            shell_prefix=self._session._new_shell_prefix(),  # noqa: SLF001 — the session this handle wraps
            term=self._term,
        )
        # Narrowed local: `self._creds` re-widens to `list[Cred] | None` once
        # captured by the nested `_undo` closure below (attribute narrowing
        # doesn't cross closure boundaries), so bind it here where the
        # `is None` check above still narrows it.
        creds = self._creds

        async def _undo() -> None:
            for i, hop in enumerate(reversed(applied)):
                via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
                # Full via cred (password/params intact) under this
                # session's own term, mirroring the forward path — keeps
                # forward/undo symmetric for custom undo callables, and a
                # term-scoped cred cannot answer the two directions with
                # different passwords for one login.
                via = cred_for(creds, via_login, self._term) or Cred(login=via_login)
                await run_undo(
                    self,
                    hop,
                    via,
                    self._host_id,
                    self._session._new_shell_prefix(),  # noqa: SLF001 — the session this handle wraps
                )
            self._session.current_user = prev

        # Same contract as PosixPrivilege.as_user's shielded undo: hold a
        # cancellation until every hop is unwound, then re-raise; the body's
        # own cancellation is passed in as held=, so a failing undo cannot
        # replace it. Any other body error keeps its precedence.
        # Imported here, not at module scope: otto.lifecycle is only needed
        # once a switch has been applied, and a top-level import drags it
        # onto every CLI --help path (import-budget guard).
        from ..lifecycle import undone_after

        async with undone_after(
            _undo, what=f"{self._host_id}: session as_user undo to {prev or 'login user'!r}"
        ):
            yield self

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        *,
        expects: Expect | list[Expect] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> Results:
        """Execute one or more commands on this named session.

        Mirrors :meth:`~otto.host.host.Host.run`: accepts a ``str``, a
        :class:`~otto.host.host.ShellCommand`, or a sequence mixing the two, and always returns a
        :class:`~otto.result.Results`. Per-command ``expects`` / ``timeout`` on a
        :class:`~otto.host.host.ShellCommand` override the run-level
        defaults; a scalar ``Expect`` tuple at the run level is normalized to a
        one-element list.

        *timeout* defaults to :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`
        and behaves exactly as on :meth:`~otto.host.host.Host.run`: a
        per-command bound for a single command, a cumulative budget for a
        sequence. Pass ``float("inf")`` for a deliberately unbounded command.

        Under a dry run every command is announced and declined; see
        ``_declined_results``.
        """
        from .host import (
            _normalize_expects,
            _prepared,
            _PreparedCommand,
            _resolve_command,
            _run_cmds_with_budget,
            _validate_timeout,
        )

        if is_dry_run():
            # ABOVE everything, so there is no arrangement of arguments that
            # reaches `self._session.run_cmd` below. A hardened return value
            # protects a caller that BRANCHES on it; only an early return
            # protects the ACTION, and sending a command IS the action.
            # `_declined_results` runs the caller-input validation
            # (`_validate_timeout`, `_normalize_expects`, `_resolve_command`)
            # for both of its callers -- `DeclinedSession.run` has nothing
            # above it -- so it is deliberately not repeated here.
            return self._declined_results(cmds, expects, timeout, log)

        timeout = _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)

        # `_prepared` is the one place a caller's expects become the internal
        # list a session runs: `_resolve_command` leaves a ShellCommand's own
        # scalar pair as it was given.
        async def _run_pc(pc: _PreparedCommand, t: float) -> CommandResult:
            return await self._run_one(
                pc.cmd,
                expects=pc.expects,
                timeout=t,
                log=pc.log,
            )

        if isinstance(cmds, (str, ShellCommand)):
            pc = _prepared(_resolve_command(cmds, default_expects, timeout, log))
            # _resolve_command collapsed the None sentinel into a concrete float.
            result = await _run_pc(pc, pc.timeout if pc.timeout is not None else timeout)
            return Results.collect(items=[result])

        prepared = [_prepared(_resolve_command(c, default_expects, None, log)) for c in cmds]
        return await _run_cmds_with_budget(_run_pc, prepared, timeout)

    async def _run_one(
        self,
        cmd: str,
        *,
        expects: list[Expect | CredentialExpect] | None,
        timeout: float,
        log: LogMode,
        exec_input: ExecInput | None = None,
    ) -> CommandResult:
        """Log and run one command on this session: :meth:`run`'s step.

        Internal because *expects* may carry an elevation's
        :class:`~otto.host.host.CredentialExpect`, which no public signature
        does; *timeout* is already validated. A dry run declines here too, so
        no call reaches the session's ``run_cmd`` under one. *exec_input* is
        an ``exec``'s (:meth:`ShellSession.run_cmd`); ``None`` for a ``run``.
        """
        declined = self._declined_under_dry_run(cmd, timeout=timeout, log=log)
        if declined is not None:
            return declined
        return await _log_and_run(
            self._session,
            self._log_command,
            self._log_output,
            cmd,
            mode=self._fold_log(log),
            expects=expects,
            timeout=timeout,
            exec_input=exec_input,
            host=self._host_id or self._name,
        )

    def _declined_under_dry_run(
        self, cmd: str, *, timeout: float, log: LogMode
    ) -> CommandResult | None:
        """Return the decline of *cmd* under a dry run, or ``None`` when otto is not in one.

        The one dry-run check of every path that types into this session:
        :meth:`_run_one` and the manager's pooled ``exec``. ``Host.exec``
        declines earlier; this keeps a private caller of
        ``SessionManager.exec_pooled`` from typing under a dry run too.
        """
        if not is_dry_run():
            return None
        return self._decline_command(ShellCommand(cmd=cmd, timeout=timeout, log=log))

    def _decline_command(self, sc: ShellCommand) -> CommandResult:
        """Announce a command this session will not send, and return the decline.

        Through :func:`~otto.host.host.decline_command`, the one decline a
        host's ``run`` and ``exec`` return too: ``Status.NotRun`` (``is_ok``
        False), ``retcode`` -1, and a :class:`~otto.result.NotRunResult` whose
        ``value`` RAISES rather than handing a parser something that was never
        measured. The announcement is logged under the mode this session's live
        path logs the command under: the command's own, with the host's
        standing mode folded in. Only the name differs: ``host_id``, the
        correlation id the manager stamped on this session, which is the only
        host identity a :class:`HostSession` holds.
        """
        from .host import command_log_mode, decline_command

        return decline_command(
            sc.cmd,
            command_log_mode(sc.log),
            host_name=self._host_id,
            log_command=self._log_command,
            fold=self._fold_log,
        )

    def _declined_results(
        self,
        cmds: "str | ShellCommand | Sequence[str | ShellCommand]",
        expects: "Expect | list[Expect] | None",
        timeout: float,
        log: LogMode,
    ) -> Results:
        """Announce every command in *cmds* and return one decline per command.

        The ONE authority for what a declined ``run`` produces, shared by this
        class's dry-run arm and by :class:`DeclinedSession`, which has no other
        behaviour. Two objects reaching the same answer by two routes is the
        mirrored-default drift this codebase has been bitten by before.

        ``_resolve_command`` still runs, deliberately: a :class:`ShellCommand`
        carrying its own ``timeout=float("nan")`` is the CALLER's input and
        costs no device contact to reject, so a dry run should reject it
        exactly where a real run does. Nothing else it computes is used — the
        decline reads only ``cmd`` and ``log``.
        """
        from .host import _normalize_expects, _resolve_command, _validate_timeout

        _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)
        items = [cmds] if isinstance(cmds, (str, ShellCommand)) else list(cmds)
        return Results.collect(
            items=[
                self._decline_command(_resolve_command(c, default_expects, None, log))
                for c in items
            ]
        )

    async def send(self, text: str | SecretStr, *, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to this session's stdin.

        See :meth:`~otto.host.host.BaseHost.send`. Under a dry run the
        text is announced and nothing is written to the transport — the same
        arm the host-level ``send`` carries, and for the same reason: a raw
        write is an action, and returning ``None`` invents no device fact.
        """
        # The caller's mode with the host's standing mode folded in, as `run`'s.
        mode = self._fold_log(log)
        if is_dry_run():
            _announce_dry_send(self._log_command, text, mode)
            return
        _log_sent(self._log_command, text, mode)
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for a pattern in this session's output. See :meth:`~otto.host.host.BaseHost.expect`.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run, which issues no
                command for a pattern to match — see
                :func:`~otto.host.host.refuse_declined_match`.
        """
        from .host import _validate_timeout

        timeout = _validate_timeout(timeout)
        if is_dry_run():
            refuse_declined_match(pattern, self._host_id, self._log_command)
        result = await self._session.expect(pattern, timeout=timeout)
        _relay_expect_read(self._session, result, self._log_output, self._fold_log(LogMode.NORMAL))
        return result

    async def enter_frame(self, *, timeout: float | None = None) -> None:
        """Enter the host's ``command_frame`` now, from inside a session-setup hook.

        Optional: a hook that returns without calling it has frame entry
        performed for it, and one that does still gets the unconditional
        post-hook confirmation. *timeout* overrides the handshake ceiling for
        this call (a slow-starting application can need more than a Unix
        host's default). See :meth:`ShellSession.enter_frame`.
        """
        if self._target_frame is None:
            raise RuntimeError(
                "enter_frame() is only available on the session handle a "
                "session_setup hook receives"
            )
        await self._session.enter_frame(frame=self._target_frame, timeout=timeout)

    async def console_login(self) -> None:
        """Run the console login sequence, for a raw-landing hook that just drove a boot menu.

        A host with ``console_options.login: false`` lands its ``"raw"``
        dialect with the transport's own login skipped; once the hook's
        boot-menu handling has the prompt in sight, it calls this to run the
        nudge/username/password sequence the transport would otherwise have
        run itself. See :doc:`/configuration/host-options`, "Two dialects:
        `landing_frame`", for the raw-landing hook this pairs with, and
        :meth:`~otto.host.session.TelnetSession.console_login` for what runs.

        Raises:
            ~otto.host.errors.ConsoleError: this session is not a console
                term (no ``console_client``) — named with this handle's own
                host id and term, since :class:`ShellSession`/
                :class:`TelnetSession` only know their session identity, not
                the host. A refusal from the console itself (a bad
                credential, already logged in) already names the console —
                see :meth:`~otto.host.console.ConsoleClient.login_sequence`
                — and passes through unchanged.
        """
        try:
            await self._session.console_login()
        except ConsoleError as exc:
            if getattr(self._session, "console_client", None) is not None:
                # A real console refused the login itself (bad password,
                # already logged in): that message already names the
                # console and must reach the caller unchanged.
                raise
            raise ConsoleError(
                f"{self._host_id}: console_login() needs a console term (term is {self._term!r})"
            ) from exc

    async def close(self) -> None:
        """Close this session and remove it from the host's session registry.

        Deliberately has NO dry-run arm; see the class docstring. The
        session leaves the registry even when its close raises (a
        cancellation the close held, say), so no dead entry is left behind.
        """
        if self._establishing:
            raise SessionSetupError(
                f"{self._host_id}: close() is not available inside a session_setup "
                f"hook — the session is being established and the manager owns its "
                f"teardown"
            )
        try:
            await self._session.close()
        finally:
            self._deregister(self._name)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()


class _NoTransport(ShellSession):
    """The :class:`ShellSession` a :class:`DeclinedSession` holds, which never opened.

    Its only job is FULL SURFACE FIDELITY. :class:`DeclinedSession` overrides
    every :class:`HostSession` method that reaches a device today; this covers
    the ones that do not exist yet. Anything inherited that reaches through to
    the transport — a method added upstream, a caller poking
    ``session._session`` — lands here and declines, instead of raising
    ``AttributeError`` at a line that made no mistake. That "error at a place
    that is not the mistake" failure is the one this whole workstream keeps
    fighting; see :class:`~otto.result.NotRunResult`'s dunder note for the
    other half of the same rule.

    ``close`` is the exception and returns quietly: nothing was opened, so
    there is nothing to tear down, and a teardown that raises in a ``finally``
    masks the exception that got there first.
    """

    def __init__(self, host_id: str = "") -> None:
        # Explicit, so OTTO_INIT_TIMEOUT is never read here: this session
        # never handshakes, and a dry run on any host family (embedded
        # included) must not fail on a deadline it never waits.
        super().__init__(init_timeout=_INIT_TIMEOUT)
        self._host_id = host_id

    @override
    async def open_transport(self) -> None:
        raise CommandNotRunError("open a shell session", host=self._host_id)

    @override
    async def write_transport(self, *, data: str | SecretStr) -> None:
        raise CommandNotRunError(shown(data), host=self._host_id)

    @override
    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        raise CommandNotRunError(f"read until {pattern.pattern!r}", host=self._host_id)

    @override
    async def _take_unread(self) -> str:
        """Nothing was read, so nothing is held."""
        return ""

    @override
    async def close(self) -> None:
        """Nothing was opened, so there is nothing to close."""


class DeclinedSession(HostSession):
    """The handle a dry run returns instead of opening a session.

    :class:`~otto.result.NotRunResult`'s philosophy one level up: hold it,
    pass it, close it — every method that would touch a device declines AT THE
    POINT OF USE, and each declined call announces the command it would have
    sent, so the caller still gets a preview. One mental model covers both
    layers: ``host.exec`` under a dry run answers ``Status.NotRun``, and so
    does ``session.run``.

    Per method, the shape follows the RETURN TYPE, exactly as it does on
    :class:`~otto.host.host.BaseHost`:

    * :meth:`run` returns :class:`~otto.result.Results` — a collection, which
      carries declines natively. One :class:`~otto.result.NotRunResult` per
      command, announced.
    * :meth:`send` returns ``None``. Nothing to harden; announce and do
      nothing.
    * :meth:`expect` returns ``str``, and **a ``str`` cannot carry a decline**.
      The empty string IS the fabrication — a caller's
      ``if "READY" in await s.expect(...)`` reads it as "the pattern did not
      appear" — so this raises, in ``refuse_declined_fact``'s vocabulary
      (:exc:`~otto.result.CommandNotRunError`). It is usually where a
      session preview ENDS, which is honest rather than broken: see
      :func:`~otto.host.host.refuse_declined_match`.
    * :attr:`alive` is ``False``. A fact about this object, not about the
      device: nothing was opened, and ``False`` is also the branch that keeps
      a caller from driving it.
    * ``current_user`` ANSWERS, and is not overridden at all — the inherited
      read path returns what the transport was stamped with, exactly as a live
      handle's does. **It is not a device fact and never was.** On the real
      path every writer of a named session's user is config-derived or already
      refused: ``ShellSession.__init__`` (``""``), ``SessionManager._seed_user``
      (``_login_user()``), ``SessionManager._apply_login_proxy``
      (``login_target``), and :meth:`HostSession.switch_user` /
      :meth:`HostSession.as_user`, whose dry-run arms refuse above the stamp.
      ``SessionManager.current_user`` already answers this question from
      configuration before any session exists, so declining here would invent
      a gap the product does not have — and it would make two handles disagree
      under one dry run, because a LIVE handle held from setup still reports
      its stamped user inside a dry-run block. Seeded by
      ``BaseHost._dry_run_session`` from the same ``_login_user()`` the real
      seeding uses.
    * :meth:`switch_user` / :meth:`as_user` raise. They return ``None``, so
      ``send``'s announce-and-return looks safe, but both finish by STAMPING
      ``current_user``; a session claiming a user it never became steers
      ``as_user``'s undo chain and every "am I root here?" check.
    * :meth:`close` (and therefore ``__aexit__``) does nothing at all. A
      fabricated teardown is the same class of bug as a fabricated open, and a
      raise there would mask whatever exception reached the ``finally`` first.

    Subclassing :class:`HostSession` rather than hand-rolling a stand-in, so
    the surface cannot drift: a method nobody thought to override is inherited
    rather than missing, and what it reaches is ``_NoTransport``, which
    declines. A duck-typed lookalike would also fail ``isinstance`` checks and
    the declared ``-> HostSession`` return type of every ``open_session``.
    """

    def __init__(
        self,
        name: str,
        log_command: Callable[[str, LogMode], None],
        log_output: Callable[[str, LogMode], None],
        host_id: str = "",
        current_user: str = "",
        *,
        fold_log: "Callable[[LogMode], LogMode]",
    ) -> None:
        transport = _NoTransport(host_id)
        # The same stamp `SessionManager._seed_user` puts on a real named
        # session, from the same `_login_user()`. Set on the TRANSPORT rather
        # than shadowed by a property override, so the declined handle and a
        # live one answer `current_user` through ONE read path
        # (`HostSession.current_user` -> `self._session.current_user`) and
        # cannot drift.
        transport.current_user = current_user
        super().__init__(
            name=name,
            session=transport,
            log_command=log_command,
            log_output=log_output,
            deregister=lambda _name: None,
            creds=None,
            host_id=host_id,
            fold_log=fold_log,
        )

    @property
    @override
    def alive(self) -> bool:
        """Always ``False`` — no session was opened."""
        return False

    @override
    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        *,
        expects: "Expect | list[Expect] | None" = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> Results:
        """Announce each command and return one decline per command."""
        return self._declined_results(cmds, expects, timeout, log)

    @override
    async def send(self, text: str | SecretStr, *, log: LogMode = LogMode.NORMAL) -> None:
        """Announce the text and write nothing."""
        _announce_dry_send(self._log_command, text, self._fold_log(log))

    @override
    async def expect(
        self,
        pattern: str | re.Pattern[str],
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Announce the wait and refuse to invent its match.

        Raises:
            ~otto.result.CommandNotRunError: always — see
                :func:`~otto.host.host.refuse_declined_match`.
        """
        from .host import _validate_timeout

        _validate_timeout(timeout)
        refuse_declined_match(pattern, self._host_id, self._log_command)

    @override
    async def switch_user(self, *, user: str = "", password: str | SecretStr | None = None) -> None:
        """Announce the elevation and refuse to claim it happened.

        The dry-run stand-in for :meth:`HostSession.switch_user`; its scoped
        sibling :meth:`as_user` declines the same way.

        Raises:
            ~otto.result.CommandNotRunError: always — see
                ``HostSession._refuse_elevation``.
        """
        self._refuse_elevation("switch_user", user or ROOT_LOGIN)

    @asynccontextmanager
    @override
    async def as_user(
        self, *, user: str = ROOT_LOGIN, password: str | SecretStr | None = None
    ) -> "AsyncGenerator[HostSession, None]":
        """Announce the elevation and refuse to claim it happened.

        The dry-run stand-in for :meth:`HostSession.as_user`, the scoped form
        of :meth:`switch_user` — it declines the same way.

        Raises:
            ~otto.result.CommandNotRunError: always, on ENTRY, so no undo is
                scheduled for a switch that never happened.
        """
        self._refuse_elevation("as_user", user)
        yield self  # pragma: no cover — unreachable; the line above always raises

    @override
    async def close(self) -> None:
        """Tear down nothing — nothing was opened, so there is nothing to close."""


class _SessionProxyIO:
    """Adapts a raw :class:`ShellSession` to the :class:`~otto.host.login_proxy.ProxyIO` protocol.

    Used wherever hops are replayed over a raw session, before a
    :class:`HostSession` wrapper exists — the raw session has no logging of its
    own (that lives one layer up, mirroring ``HostSession.send``/``expect``), so
    this adapter routes through whichever ``log_command``/``log_output`` sinks it
    is given — the manager's at session establishment, the session log file's on
    the login bridge. A hop's password send arrives with ``log=LogMode.NEVER``
    (see ``login_proxy._su_proxy``) and is never logged — the same redaction
    real command traffic gets.
    """

    def __init__(
        self,
        session: "ShellSession",
        log_command: Callable[[str, LogMode], None],
        log_output: Callable[[str, LogMode], None],
        *,
        fold_log: "Callable[[LogMode], LogMode]",
    ) -> None:
        self._session = session
        self._log_command = log_command
        self._log_output = log_output
        # The standing mode of the host whose session this is, as its
        # named sessions fold it: a hop's send and what its expect reads log
        # as the session's own run would.
        self._fold_log = fold_log

    async def send(self, text: str | SecretStr, *, log: LogMode = LogMode.NORMAL) -> None:
        _log_sent(self._log_command, text, self._fold_log(log))
        await self._session.send(text)

    async def expect(
        self, pattern: str | re.Pattern[str], *, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
        out = await self._session.expect(pattern, timeout=timeout)
        _relay_expect_read(self._session, out, self._log_output, self._fold_log(LogMode.NORMAL))
        return out


_LINE_EDITOR_RECORD = "run-command-line-length"
"""The :data:`~otto.host.userland.GAPS` record holding BusyBox ash's line-editor measurement."""


# The typed-line measurements, as short dated entries for the user-facing record.
# The investigation behind them -- why the 2026-08-13 harness saw BusyBox ash's
# 1022-character line editor and otto's own sessions never do (the editor engages
# only while the terminal echoes, and otto's handshake turns echo off), and that
# the same harness carried 18437 characters into bash and over 20000 into dash and
# so ruled itself out -- is the `measured_on` of the :data:`_LINE_EDITOR_RECORD`
# gap record, its one home. The 2026-08-21 telnet entry's stall at 4090 did not
# reproduce on 2026-11-13 and is no longer recorded here.
_TYPED_LINE_MEASUREMENTS = [
    (
        "2026-08-13, BusyBox 1.35.0 over dropbear 2022.83, OpenSSH and a local pty, typed "
        "by a raw harness into a shell whose terminal still echoed: 1022 characters "
        "arrive intact; at 1023 the line is cut, with no error"
    ),
    (
        "2026-08-13, BusyBox 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 through a local "
        "pty, the same harness: the same 1022/1023 boundary on every release"
    ),
    (
        "2026-11-13, the five BusyBox QEMU guests over telnet, and BusyBox 1.35.0 over "
        "dropbear ssh and its serial console, through otto's own session after its "
        f"handshake (echo off): {PTY_TYPED_LINE_MAX} characters arrive intact; at "
        f"{PTY_TYPED_LINE_MAX + 1} the line is cut to {PTY_TYPED_LINE_MAX}, with no error"
    ),
    (
        "2026-11-13, the same sessions after `stty echo`: cut at 1022 on every release "
        "and route, the 2026-08-13 boundary"
    ),
    (
        "2026-11-13, BusyBox 1.35.0 over telnet, dropbear ssh and its serial console, "
        "through otto's session under the bed's normal (bash) frame after a hook turned "
        "echo back on and made `stty` fail: the handshake read `onlcr, echo on`, and a raw "
        f"line of {ASH_ECHOING_LINE_MAX + 1} characters was cut to {ASH_ECHOING_LINE_MAX}; "
        "GNU bash on test1 over ssh, the same way, cut nothing"
    ),
    (
        "2026-11-13, the same three routes with the `ash` frame declared and the same hook: "
        "the line budget (the "
        f"{ASH_ECHOING_LINE_MAX}-character limit less the characters otto adds around "
        f"the command) read {typed_line_budget(AshFrame(), echoes=True)} and a "
        f"{ASH_ECHOING_LINE_MAX + 1}-character `run` was refused naming {ASH_ECHOING_LINE_MAX}"
    ),
]


def _line_budget_limitation() -> Limitation:
    """Build the ``line_budget`` limitation of a session that types into a pty shell.

    Its measurements are :data:`_TYPED_LINE_MEASUREMENTS`; the gap record they
    summarise must exist.

    Raises:
        LookupError: if the gap record is missing, so a renamed record breaks
            the import instead of silently dropping its measurement.
    """
    gap = gap_for(_LINE_EDITOR_RECORD)
    if gap is None:
        raise LookupError(
            f"the {_LINE_EDITOR_RECORD!r} record is missing from otto.host.userland.GAPS; "
            "the line_budget limitation reads its measurement from it"
        )
    return Limitation(
        kind="device",
        affects="how long one line of a command can be",
        summary=(
            "in a shell session (pooled or the host's default one), "
            f"{PTY_TYPED_LINE_MAX} characters per line; {ASH_ECHOING_LINE_MAX} on a "
            "BusyBox shell whose terminal still echoes"
        ),
        reason=(
            "In a session otto types the command into a terminal, and the characters "
            "otto adds around the command count against the line's length. A Linux "
            f"terminal cuts a typed line past {PTY_TYPED_LINE_MAX} characters, with no "
            "error: the kernel holds a typed line in a 4096-byte buffer, its newline "
            "included (measured on BusyBox guests). BusyBox's shell, ash, cuts a line at "
            f"{ASH_ECHOING_LINE_MAX} characters, but only while the terminal echoes what "
            "is typed back. otto turns echo off when it opens a session, so the limit is "
            f"{PTY_TYPED_LINE_MAX} where that worked and {ASH_ECHOING_LINE_MAX} where it "
            "did not (the device has no `stty`, or its `stty` refuses). otto also uses "
            f"{ASH_ECHOING_LINE_MAX} when it sizes a file transfer's lines before it has "
            "opened any session to the host, since it cannot yet know whether echo is off. "
            "`otto host <id> probe` prints the terminal mapping it measured, with "
            "`, echo on` after it where echo stayed on, so the limit there is "
            f"{ASH_ECHOING_LINE_MAX} on a BusyBox shell.\n\n"
            "On a host whose `command_frame` is `ash`, `run` counts each line before typing "
            "it, and refuses a line "
            "over the limit with an error naming it. Elsewhere the terminal cuts it "
            "without an error.\n\n"
            "Not limited this way: a local host's session, which reads the command from a "
            "pipe, not a terminal. A Zephyr board's own shell buffer is a separate limit, "
            "never measured."
        ),
        measured_on=_TYPED_LINE_MEASUREMENTS,
    )


LINE_BUDGET_LIMITATION = _line_budget_limitation()
"""How long one line of a command typed into a session's terminal can be."""

SESSION_EXEC_LIMITATIONS: "dict[str, Limitation]" = {"line_budget": LINE_BUDGET_LIMITATION}
"""Where an ``exec`` on a shell session falls short of otto's promises: the one declaration.

Every ``exec`` that otto runs itself types into a shell session, pooled or the
host's default one, so one table holds for both. :meth:`SessionManager.line_room`
budgets a line on a manager whose sessions type into a terminal, and the docs
generator reads its limitations from here.
"""


@dataclass(frozen=True)
class BridgeSetup:
    """A host's session-setup hook and the session fields it runs with, for ``otto login``.

    ``otto login`` drives its own interactive bridge rather than a managed
    session, so it cannot read the manager's fields the way ``run`` and
    ``exec`` sessions do. Built only by ``SessionManager._bridge_setup()``,
    from the same fields the manager's sessions are built with, so the hook
    runs under the dialects, creds and readiness deadline every other session
    of the host gets.
    """

    setup: "SessionSetup"
    """The hook to run once the bridge's landing shell is ready."""

    landing_frame: CommandFrame | None
    """The dialect the bridge lands in; ``None`` lands in :attr:`target_frame`."""

    target_frame: CommandFrame
    """The dialect the hook leaves the shell in, entered after it."""

    creds: "list[Cred] | None"
    """The host's creds, for elevation from inside the hook."""

    init_timeout: float | None
    """The readiness deadline for the landing handshake; ``None`` keeps the session default."""


def output_line(raw: str) -> str | None:
    r"""Turn one raw line of a command's output into the line it logs.

    :class:`OutputLog`, the line logger of a streaming shell session, logs
    what this returns. The per-line form of
    :func:`~otto.host.command_frame.command_value`, so a logged line is cut by
    the same rule as ``value``: the contract in :ref:`command-output-contract`.

    *raw* is one line as read, ending in its ``\n`` unless it is the last
    line of output that had no final newline. The result is *raw* with at most
    one trailing ``\n`` removed: trailing spaces, tabs and the program's own
    ``\r`` are kept. ``None`` means *raw* is not output at all: the empty
    partial ``''`` that asyncssh yields when end-of-file lands after the last
    line (timing-dependent, #711). A real blank line is ``'\n'``, never ``''``,
    so a ``None`` is never a line the command printed.

    >>> from otto.host.session import output_line
    >>> output_line("a \r\n"), output_line("\n"), output_line("abc"), output_line("")
    ('a \r', '', 'abc', None)
    """
    if raw == "":
        return None
    return command_value(raw)


class ExecOutput:
    r"""The one owner of a command's output as it is read, the unfinished last line included.

    :meth:`ShellSession.run_cmd` feeds it the decoded text it reads, at
    whatever boundary the text arrived, so a timed-out command keeps what it
    printed up to the deadline, its last unfinished line too. *valuer* turns
    everything fed into the command's ``value``: by default
    :func:`~otto.host.command_frame.command_value`, on a shell session its
    frame's parse of the folded buffer.

    >>> from otto.host.session import ExecOutput
    >>> out = ExecOutput()
    >>> out.feed("a\nha"), out.feed("lf"), out.raw(), out.value()
    (['a\n'], [], 'a\nhalf', 'a\nhalf')
    """

    def __init__(self, valuer: Callable[[str], str] = command_value) -> None:
        self._valuer = valuer
        self._fed: list[str] = []
        # The unfinished line, as the pieces it arrived in: each chunk is split
        # on its own, never the line so far, so a long line costs work in
        # proportion to its length.
        self._pending: list[str] = []

    def feed(self, text: str) -> list[str]:
        r"""Record *text*; return the complete raw lines it finished, each ending in ``\n``."""
        if not text:
            return []
        self._fed.append(text)
        first, *rest = text.split("\n")
        if not rest:
            self._pending.append(first)
            return []
        *complete, last = rest
        lines = ["".join(self._pending) + first + "\n", *(line + "\n" for line in complete)]
        self._pending = [last] if last else []
        return lines

    def tail(self) -> str:
        """Return the unfinished last line fed so far, ``""`` when the output ended on a newline."""
        return "".join(self._pending)

    def raw(self) -> str:
        """Everything fed, the unfinished tail included."""
        return "".join(self._fed)

    def value(self) -> str:
        """Return the command's ``value`` for everything fed so far."""
        return self._valuer(self.raw())


class OutputLog:
    r"""The one logger of a command's output lines as they complete, on every session.

    Each line handed to :meth:`line` is one complete raw line, ending in its
    ``\n``; :meth:`finish` takes the unfinished last line once the command
    has ended. The lines reaching *on_output* are then exactly
    :func:`~otto.logger.relay.log_lines` of the command's ``value``, whatever
    boundary the output arrived at, for a session whose ``value`` is its lines
    with one final line ending cut (a frame that streams: see
    ``streams_output_live``): a line is logged when it completes
    (*fold* undoes the terminal's mapping, :func:`output_line` cuts its
    ``\n``), and the unfinished line at :meth:`finish`. Nothing here reads
    the ``value``, so the output is never split a second time.

    Two lines wait for the next one instead. A first line that is blank may be
    the whole of an empty value, which logs nothing. With *hold_newest* (a
    shell whose terminal mapping is unknown) every newest line waits: only the
    next line or the end tells whether its ``\r`` is the terminal's, cut from
    the value, or the program's, kept. A read a credential was typed into
    (:meth:`withhold_read`) is cut from the line it lands on; the rest of that
    line is logged, and a line left with nothing is not.

    A frame whose own tail follows its output holds that many newest lines
    to the end (:meth:`begin`'s *hold*), when the value says which of them
    are output (:meth:`settle`). That is exact because every line of the
    stream takes one place in order, cut or not, complete or the unfinished
    last one (:meth:`end`), and a read's complete lines take theirs before
    any answer is typed: the value's lines then match the places one to
    one, and a place cut by a credential is logged from its cut text.

    >>> from otto.host.session import OutputLog
    >>> logged = []
    >>> log = OutputLog(logged.append)
    >>> log.line("a\n"), log.line("\n"), logged
    (None, None, ['a', ''])
    >>> log.finish("b"), logged
    (None, ['a', '', 'b'])
    """

    def __init__(
        self,
        on_output: Callable[[str], None],
        *,
        hold_newest: bool = False,
        fold: Callable[[str], str] = lambda raw: raw,
        begun: bool = True,
    ) -> None:
        self._on_output = on_output
        self._hold_newest = hold_newest
        self._fold = fold
        self._begun = begun
        self._held: list[_HeldLine] = []
        self._hold = 0
        self._skip = 0
        self._count = 0
        self._logged = 0
        # The reads to cut from the line in progress, as (start, length) in
        # its raw characters; a read that runs past the line's end carries
        # on into the next line from its start.
        self._cuts: list[tuple[int, int]] = []

    @property
    def begun(self) -> bool:
        """Whether the command's output has begun, so that its lines are logged.

        A shell session's log begins at the command's BEGIN line
        (:meth:`begin`); what a shell prints before it is never the command's.
        """
        return self._begun

    def begin(self, *, hold_newest: bool, hold: int = 0, skip: int = 0) -> None:
        """Begin logging lines, holding each newest one when *hold_newest* (see the class).

        *skip* lines that follow are the shell's, never logged. *hold* is
        how many of the newest lines wait for the end of the command
        (:meth:`settle`), as a streaming frame's
        :attr:`~otto.host.command_frame.CommandFrame.live_hold_lines` says.
        """
        self._hold_newest = hold_newest
        self._hold = hold
        self._skip = skip
        self._begun = True

    def withhold_read(self, start: int, read: str) -> None:
        """Cut *read*, which began *start* raw characters into the line in progress, from the log.

        A credential was typed into it: the matched prompt is kept out, as
        is any line the read ran across, and text after it is logged.
        """
        self._cuts.append((start, len(read)))

    def _cut(self, raw: str) -> tuple[str, bool]:
        """Return *raw* with the pending cuts applied, and whether any of it was cut."""
        if not self._cuts:
            return raw, False
        kept, carried, at = [], [], 0
        for start, length in sorted(self._cuts):
            stop = start + length
            if start >= len(raw):
                carried.append((start - len(raw), length))
                continue
            kept.append(raw[at:start] if start > at else "")
            at = max(at, min(stop, len(raw)))
            if stop > len(raw):
                carried.append((0, stop - len(raw)))
        kept.append(raw[at:])
        self._cuts = carried
        return "".join(kept), True

    def line(self, raw: str) -> None:
        """Log the complete raw line *raw*, or hold it until the next one."""
        self._release(self._hold)
        slot = self._slot(raw)
        if slot is None:
            return
        if slot.text is None:
            if self._hold:
                # Still one of the value's lines: it takes its place, logging nothing.
                self._held.append(slot)
                self._release(self._hold)
            return
        self._held.append(slot)
        newest = self._hold_newest or (self._count == 1 and not slot.text)
        self._release(max(self._hold, 1 if newest else 0))

    def _slot(self, raw: str) -> "_HeldLine | None":
        """Return the place the complete raw line *raw* takes in the value, its cuts applied.

        ``None``: *raw* is no line of the value (one of the shell's skipped
        lines, or not output at all). A line a credential read was cut from
        takes its place even when the cut took all of it, its newline too.
        """
        raw, cut = self._cut(raw)
        if self._skip:
            self._skip -= 1
            return None
        line = output_line(self._fold(raw))
        if line is None and not cut:
            return None
        self._count += 1
        if cut and not (line or "").rstrip("\r"):
            return _HeldLine(None, cut=True)
        return _HeldLine(line, cut=cut)

    def finish(self, tail: str) -> None:
        r"""Log what is left once the command ended: the held line and *tail*, its unfinished line.

        *tail* is the raw text after the last complete line (``""`` when the
        output ended on a newline). Then the held line is the value's last:
        under *hold_newest* its final ``\r`` is the terminal's, cut with the
        value's line ending, and a lone blank first line is an empty value,
        which logs nothing.
        """
        held = self._held.pop().text if self._held else None
        if tail:
            if held is not None:
                self._emit(held)
            line, cut = self._cut(tail)
            line = self._fold(line)
            if line or not cut:
                self._emit(line)
            return
        if held is None:
            return
        if self._hold_newest:
            held = held.removesuffix("\r")
        if self._count > 1 or held:
            self._emit(held)

    @property
    def holds(self) -> bool:
        """Whether this log holds its newest lines back to the end (:meth:`settle`)."""
        return self._begun and bool(self._hold)

    def end(self, ended: Iterable[str], tail: str, lines: Iterable[str]) -> None:
        """Log the rest of a held log once the command ended: :meth:`settle` after the END read.

        *ended* are the complete raw lines the END record's read brought,
        *tail* the unfinished line before the record: each takes its place,
        its cuts applied as :meth:`line` and :meth:`finish` apply them, so
        :meth:`settle` walks every line of the stream.
        """
        for raw in ended:
            slot = self._slot(raw)
            if slot is not None:
                self._held.append(slot)
        if tail:
            line, cut = self._cut(tail)
            line = self._fold(line)
            self._held.append(_HeldLine(line if line or not cut else None, cut=cut))
        self.settle(lines)

    def settle(self, lines: Iterable[str]) -> None:
        """Log the command's *lines* not yet logged, once it ended: a frame that held lines back.

        *lines* are the lines of its ``value`` (:func:`~otto.logger.relay.log_lines`),
        or the complete ones of a timed-out ``value``. It is exact because it
        is positional: every line of the stream after the shell's skipped
        ones, cut or not, takes one place in order, either already logged
        (counted, a line cut to nothing among them) or held. So the first of
        *lines* are the ones logged, and each line after them is the held
        line in the same place: logged from *lines*, except that one a
        credential read was cut from is logged as cut (or not at all, when
        nothing was left), since the value still holds what was withheld. A
        line is never logged twice nor dropped, and a withheld read never
        logged. What was held beyond *lines* is the shell's tail, and is
        dropped.
        """
        held, self._held = self._held, []
        for at, line in enumerate(list(lines)[self._logged :]):
            if at < len(held) and held[at].cut:
                self._emit(held[at].text)
            else:
                self._emit(line)

    def stop_settled(self, lines: Iterable[str]) -> None:
        """:meth:`settle` for a command that stopped unfinished; a failing sink is reported."""
        self._reported(lambda: self.settle(lines))

    def stop(self, lines: Iterable[str] = ()) -> None:
        """Log the complete lines left, nothing unfinished: the command stopped without finishing.

        *lines* are complete raw lines read after the last one handed to
        :meth:`line` (what a reader still held at a timeout); they are logged
        as :meth:`line` logs them, then the line held. Called while an
        exception (a timeout, a cancellation) is on its way out, or for a
        result already decided, so a sink that fails here is reported, and
        nothing raised while reporting it replaces the exception or the
        result.
        """

        def log_rest() -> None:
            for raw in lines:
                self.line(raw)
            self._release()

        self._reported(log_rest)

    def _reported(self, log: Callable[[], None]) -> None:
        """Run *log*; a sink that fails in it is reported, never raised."""
        try:
            log()
        except (
            Exception
        ):  # the exception being raised, or the result, is what must reach the caller
            with suppress(Exception):  # a handler that fails the report fails it alone
                logger.warning("the held output line could not be logged", exc_info=True)

    def _release(self, keep: int = 0) -> None:
        """Log the oldest held lines until *keep* are left.

        A blank first line that held lines follow waits for one more: only a
        line after it that is itself output says the value is not empty.
        """
        while len(self._held) > keep + self._blank_first_waits():
            self._emit(self._held.pop(0).text)

    def _blank_first_waits(self) -> int:
        first = self._held[0].text if self._held else None
        return int(bool(self._hold) and self._logged == 0 and first == "")

    def _emit(self, line: str | None) -> None:
        """Log *line* in the value's next place; ``None`` takes the place and logs nothing."""
        self._logged += 1
        if line is not None:
            self._on_output(line)


def _log_read(
    line_log: OutputLog,
    completed: list[str],
    start: int,
    data: str,
    answering: "Expect | CredentialExpect | None",
) -> None:
    """Log the lines *completed* by the read *data*, which began *start* characters into its line.

    A read a credential is typed into (*answering*) stays out of the log.
    """
    if isinstance(answering, CredentialExpect):
        line_log.withhold_read(start, data)
    for raw in completed:
        line_log.line(raw)


@dataclass(frozen=True)
class _HeldLine:
    """A line :class:`OutputLog` holds: its text (``None``: cut to nothing), and if it was cut."""

    text: str | None
    cut: bool


def timed_out_result(*, cmd: str, timeout: float, output: ExecOutput) -> CommandResult:
    """Build the one result of a command that ran past its *timeout*, on every session.

    Its ``value`` is ``Command timed out after <timeout>s``, then a newline and
    *output*'s value when the command printed anything before the deadline.
    Its ``msg`` is that first line alone, so the command line, which prints a
    failed result's ``msg``, says why the command failed (#1098).
    """
    printed = output.value()
    reason = f"Command timed out after {timeout}s"
    return CommandResult(
        status=Status.Error,
        value=reason + (f"\n{printed}" if printed else ""),
        msg=reason,
        command=cmd,
        retcode=-1,
        timed_out=True,
    )


def _timed_out_lines(output: ExecOutput) -> list[str]:
    """Return the complete lines a timed-out buffered frame printed, as raw lines to log.

    A buffered frame logs nothing while it reads, so at a timeout its log is
    the lines of its partial value (*output*'s, the one the timed-out result
    carries after its diagnostic line), without the unfinished last line
    when *output* ended mid-line: the complete lines printed before the
    deadline, as a streaming frame has logged them.
    """
    return [f"{line}\n" for line in _timed_out_value_lines(output)]


def _timed_out_value_lines(output: ExecOutput) -> list[str]:
    """Return the complete lines of a timed-out command's value (*output*'s), as logged."""
    lines = log_lines(output.value())
    if output.tail() and lines:
        lines.pop()
    return lines


@final
class SessionManager:
    """Manages persistent shell sessions for any host type.

    Owns the default session (used by ``run_cmd``/``send``/``expect``) and all
    named sessions (created via ``open_session``).

    Session creation is pluggable: provide a ``session_factory`` callable to
    control how sessions are created (e.g. ``LocalSession`` for local hosts),
    or pass a ``ConnectionManager`` to use the default SSH/Telnet dispatch.
    The shell *dialect* is selected by ``command_frame`` (default bash; an embedded
    host passes a :class:`~otto.host.command_frame.ZephyrFrame`) — it is handed
    to every session this manager builds, independent of the transport — unless
    the host also declares a ``landing_frame``, in which case THAT is what
    sessions are built with and the ``command_frame`` is what the post-hook
    ``enter_frame()`` switches to. A slow target's readiness ceiling is raised
    via ``init_timeout`` (the host's own) or ``default_init_timeout`` (its
    family's, which ``OTTO_INIT_TIMEOUT`` replaces); a ``session_setup`` hook
    runs once per session open, after the handshake and every login-proxy hop.
    """

    def __init__(  # noqa: PLR0913 — wide session-construction API (transport/dialect/elevation seams)
        self,
        *,
        connections: "ConnectionManager | None" = None,
        name: str = "",
        log_command: Callable[[str, LogMode], None] = lambda *_: None,
        log_output: Callable[[str, LogMode], None] = lambda *_: None,
        fold_log: "Callable[[LogMode], LogMode]",
        session_factory: "Callable[[], ShellSession] | None" = None,
        named_session_factory: "Callable[[], ShellSession] | None" = None,
        command_frame: CommandFrame | None = None,
        init_timeout: float | None = None,
        default_init_timeout: float | None = None,
        retry_backoff: float | None = None,
        creds: "list[Cred] | None" = None,
        host_id: str = "",
        shell_history: bool = True,
        session_setup: "SessionSetup | None" = None,
        landing_frame: CommandFrame | None = None,
        types_into_terminal: bool = True,
        login_user: str = "",
    ) -> None:
        self._connections = connections
        # Who the sessions of a manager WITHOUT a transport log in as: the
        # local host's process user. A transport names its own login, so
        # this is read only when there is none (`_login_user`).
        self._loginless_user = login_user
        # Whether this manager's sessions read a command through a terminal's
        # line discipline: its session class's `reads_a_terminal` (False for
        # LocalHost, whose shell reads a pipe), which the caller passes because
        # the manager needs it before any session exists.
        self._types_into_terminal = types_into_terminal
        self._name = name
        self._log_command = log_command
        self._log_output = log_output
        self._fold_log = fold_log
        self._session_factory = session_factory
        # How to build a NAMED auxiliary session, when that differs from the
        # default channel. ``None`` (the norm) means "same factory as the
        # default"; DockerContainerHost passes a distinct one whose sessions do
        # not record a run-channel binding. Resolved at USE, not folded into
        # ``_session_factory`` here, because ``_session_factory`` is a seam
        # tests reassign on a live manager — snapshotting the fallback would
        # silently ignore every such reassignment.
        self._named_session_factory = named_session_factory
        # Creds forwarded to HostSessions so named sessions can elevate via
        # otto.host.login_proxy.perform_switch. None on non-posix hosts →
        # named-session elevation is unsupported there; [] means supported
        # but passwordless / ad-hoc.
        self._creds = creds
        self._host_id = host_id
        # Shell dialect handed to every session built on the ConnectionManager
        # dispatch path (SSH or telnet alike). ``None`` resolves to bash inside
        # ``ShellSession``; an embedded host passes a ZephyrFrame so its
        # sessions speak the RTOS shell's framing. Decoupled from the transport,
        # so e.g. Zephyr-framing-over-SSH needs no new session class.
        self._command_frame = command_frame
        # Whether otto's commands are recorded in this host's shell history,
        # forwarded to every session built here. True (leave the shell as we
        # found it) keeps LocalHost/DockerContainerHost/EmbeddedHost — and any
        # caller passing a session_factory — on their existing behavior;
        # UnixHost passes its own field, which defaults False.
        self._shell_history = shell_history
        # Session setup hook and landing dialect. The hook runs once per
        # session open, after the handshake and every proxy hop; the landing
        # frame (default: the command frame) is what every session is BUILT
        # with, and the command frame is what enter_frame() switches to after
        # the hook. With no hook, nothing here changes a single byte.
        self._session_setup = session_setup
        self._landing_frame = landing_frame
        # The host's own readiness ceiling (``None``: none declared), and its
        # family's default (``None``: the session's class default, 10 s), which
        # OTTO_INIT_TIMEOUT replaces; a slow shell such as a Zephyr QEMU
        # telnet console has a larger family default.
        self._init_timeout = init_timeout
        self._default_init_timeout = default_init_timeout
        # Pause before the single handshake retry in ``_open_confirmed``. ``None``
        # resolves to the production default; tests pass ``0`` to skip the real
        # wall-clock wait without changing the retry logic itself.
        self._retry_backoff = _HANDSHAKE_RETRY_BACKOFF if retry_backoff is None else retry_backoff
        self._session: ShellSession | None = None
        # The default session WITHOUT a terminal, on a transport whose own
        # default session has one (ssh): what `run(..., terminal=False)`
        # uses. A session of its own, because shell state belongs to a
        # session. None until asked for, and always on a manager whose
        # default session reads a pipe already (`_types_into_terminal` false).
        self._plain_session: ShellSession | None = None
        # The user the DEFAULT session was opened as (the login target, after
        # proxy hops), recorded when the session comes up. `ambient_user`
        # compares the live identity against it: anything else is a switch
        # some as_user()/switch_user() made, which `exec` then inherits.
        self._baseline_user: str | None = None
        self._named_sessions: dict[str, HostSession] = {}
        # Free-lists of idle shell sessions every ssh, telnet and local
        # `exec()` runs on, one per user (`_pool_key`) and terminal mode
        # (with a terminal, or without: `plain`): a session is opened for its
        # user and mode and stays both. Serial callers reuse one session
        # so the TCP+auth handshake is paid once; concurrent callers each pull
        # their own session off the list (opening a fresh one if none are
        # free), preserving `exec()`'s documented contract that concurrent
        # calls run independently.
        self._exec_pools: dict[tuple[str, bool], list[HostSession]] = {}
        self._exec_pool_count: int = 0
        # Serializes the get-or-create paths so concurrent callers can't all
        # observe a dead/missing session, all close it, and all create
        # replacements that clobber each other (each clobber leaks the prior).
        self._ensure_session_lock = asyncio.Lock()
        # One lock *per session name* rather than a single shared lock.  The
        # get-or-create path only needs to dedupe callers requesting the *same*
        # name; a shared lock additionally serializes the (slow, ~1-2 s telnet)
        # connect of callers requesting *different* names — which collapses the
        # exec pool's concurrency back to serial.  Keyed locks let distinct
        # names (notably the unique `__exec_pool_N__` names) connect in
        # parallel while same-name callers still resolve to one session.
        self._named_session_locks: dict[str, asyncio.Lock] = {}

    @property
    def has_live_default_session(self) -> bool:
        """Whether the DEFAULT session is currently open and responsive.

        The default session is the one ``run_cmd``/``send``/``expect`` share.

        Narrower than :attr:`has_live_sessions`, which any named session
        satisfies. Callers that need to know whether *the* persistent channel
        is up (rather than whether this host holds any shell at all) want this
        one.
        """
        return self._session is not None and self._session.alive

    @property
    def has_live_sessions(self) -> bool:
        """Whether any session (default, of either mode, or named) is currently alive."""
        if any(s is not None and s.alive for s in (self._session, self._plain_session)):
            return True
        return any(s.alive for s in self._named_sessions.values())

    def no_terminal_limitation(self) -> str | None:
        """Why this manager cannot open a session without a terminal, or ``None`` if it can.

        The one answer for ``run``, ``open_session`` and ``exec``: only an
        ssh transport can leave the pty out, and only for a login that needs
        no terminal to arrive (no proxied login to type a password into, no
        session-setup hook written against a terminal). Read fresh on every
        call.
        """
        term = getattr(self._connections, "term", None)
        if term != "ssh" or self._session_factory or self._named_session_factory:
            # A factory builds the one kind of session it knows.
            theirs = f"; this host's is {term}" if term and term != "ssh" else ""
            return f"needs an ssh transport{theirs}"
        if getattr(self._connections, "proxy_hops", []):
            return "cannot reach a login that is proxied through another user"
        if self._session_setup is not None:
            return (
                f"cannot run the session setup {self._session_setup.name!r}, "
                f"which is written for a terminal"
            )
        return None

    def _idle_exec_sessions_kept(self) -> int | None:
        """How many idle exec sessions this transport keeps per user and mode; ``None`` is no limit.

        On ``ssh``, one: every idle pooled session holds a channel of the one
        connection, and a server allows few (OpenSSH's default is ten): a
        burst's idle sessions would leave none for a stop sweep, SCP or SFTP.
        For sessions whose shell reads a pipe (the local host's: the
        manager was built with ``types_into_terminal`` false), none: a kept
        shell would hold the working directory and environment of the moment
        it opened, not the Python process's, and closing it later would stop
        what an earlier call left running in its group. Every other manager
        keeps every idle session (no shared channel budget, and an open costs
        seconds), a container's included, which has no transport of its own
        either.

        Reading ``types_into_terminal`` false as "local pipe shells" is
        deliberate: only the local host builds a manager that way today. A
        second kind of manager whose shells read a pipe but SHOULD keep them
        would need this rule to key on what the manager is (its connections),
        not on that flag.
        """
        if not self._types_into_terminal:
            return 0
        return 1 if getattr(self._connections, "term", None) == "ssh" else None

    def _wants_plain(self, terminal: bool | None) -> bool:
        """Whether mode *terminal* is the second default session, the one without a terminal.

        ``None`` and the transport's own mode are the one default session
        every caller had before; ``False`` on a terminal transport is the
        second one (:attr:`_plain_session`).
        """
        return terminal is False and self._types_into_terminal

    def _checked_plain(self, terminal: bool | None) -> bool:
        """Answer :meth:`_wants_plain`, refusing a session without a terminal that cannot be opened.

        The check of the manager's own entries (:meth:`run_cmd`,
        :meth:`open_session`), so a caller that reaches them without the
        host's refusal gets the same one and never a session of the other mode.

        Raises:
            ~otto.host.errors.UnsupportedOptionError: :meth:`no_terminal_limitation` says why.
        """
        plain = self._wants_plain(terminal)
        if plain and (why := self.no_terminal_limitation()) is not None:
            from .errors import terminal_refusal  # a refusal only: off the startup graph

            raise terminal_refusal(host=self._name, terminal=False, reason=why)
        return plain

    def _default_session(self, *, plain: bool) -> "ShellSession | None":
        """Return the default session held for a mode (:meth:`_wants_plain`), or ``None``."""
        return self._plain_session if plain else self._session

    def _bridge_setup(self) -> BridgeSetup | None:
        """Return what ``otto login`` needs to run this host's hook, or ``None`` without one."""
        if self._session_setup is None:
            return None
        return BridgeSetup(
            setup=self._session_setup,
            landing_frame=self._landing_frame,
            target_frame=self._target_frame(),
            creds=self._creds,
            init_timeout=self._readiness_deadline(),
        )

    def _readiness_deadline(self) -> float | None:
        """Return the deadline this manager's sessions get; ``None`` for the class default.

        The host's own ``init_timeout``, else ``OTTO_INIT_TIMEOUT``, else the
        family default (see :func:`resolve_init_timeout`). Every session this
        manager builds, the ``otto login`` bridge and :meth:`open_budget` read
        it here, so all of them agree.
        """
        return resolve_init_timeout(self._init_timeout, self._default_init_timeout)

    def open_budget(self, *, term: str | None = None) -> float:
        """Seconds opening a session can take to report a shell that never became ready.

        Two attempts when ``_open_confirmed`` (the open of the default, a
        named and a pooled session alike) retries a failed handshake on a
        rebuilt transport, with the pause before the retry between them; one
        on a term it does not retry (a console that logs in, see
        :meth:`~otto.host.connections.ConnectionManager.retries_failed_open`).
        It budgets the default session's open: a named or pooled open on a
        line that serves one client is not retried, so it takes one attempt.
        Each attempt is the transport's bounded waits on *term* (the active
        one by default; see
        :meth:`~otto.host.connections.ConnectionManager.connect_allowance`)
        plus the readiness deadline, plus what the failed attempt's close spends
        draining a single-client line before releasing it (see
        :meth:`~otto.host.connections.ConnectionManager.release_allowance`).
        A caller that bounds the open with less reports a refused telnet
        password as its own timeout rather than as the ``ConnectionError`` the
        session raises. Waits otto leaves unbounded are not counted, so such a
        caller still adds a margin per attempt.
        """
        # Resolved as every session this manager builds resolves it; with
        # neither override, the class attribute rather than the module
        # constant: it is what such a session waits, and the seam tests patch.
        resolved = self._readiness_deadline()
        default = ShellSession._init_timeout  # noqa: SLF001 — the class default every session without an override uses
        deadline = default if resolved is None else resolved
        conns = self._connections
        connect = 0.0 if conns is None else conns.connect_allowance(term=term)
        release = 0.0 if conns is None else conns.release_allowance(term=term)
        retried = conns is None or conns.retries_failed_open(term=term)
        attempt = connect + deadline + release
        return 2 * attempt + self._retry_backoff if retried else attempt

    def _login_user(self) -> str:
        """Return the host's login username, or '' when loginless / no creds.

        With no connection manager, the ``login_user`` this manager was built
        with: the local host's invoking user, who its shells run as. Otherwise
        prefers ``login_target`` (the *requested* login — may be a proxied
        account reached via a hop rather than the transport's direct-auth
        user). Falls back to the resolved direct cred's username, then to ''.
        Best-effort: session seeding runs this on every build, so it tolerates
        a connection manager that exposes neither attribute (e.g. minimal test
        fakes or a loginless transport).
        """
        if self._connections is None:
            return self._loginless_user
        login_target = getattr(self._connections, "login_target", None)
        if login_target:
            return login_target
        creds = getattr(self._connections, "credentials", None)
        if not creds:
            return ""
        return creds[0]

    @property
    def _session_frame(self) -> CommandFrame | None:
        """The dialect every session is built with: the landing frame, else the command frame."""
        return self._landing_frame if self._landing_frame is not None else self._command_frame

    def _target_frame(self) -> CommandFrame:
        """Return the dialect enter_frame() switches to after the hook.

        The host's command frame, or bash when it declares none.
        """
        return self._command_frame if self._command_frame is not None else BashFrame()

    def _seed_user(self, session: "ShellSession", user: str | None = None) -> None:
        """Stamp a freshly built session with *user*, or the login user when ``None``.

        *user* is given only for a session whose transport authenticated as
        that user itself (:meth:`open_session`'s own-connection case).
        """
        session.current_user = self._pool_key(user)

    def _pool_key(self, user: str | None) -> str:
        """Name the exec pool bucket *user*'s ``exec`` takes a session from: *user*, else the login.

        ``exec()`` and ``exec(user=<the login>)`` therefore share one bucket.
        """
        return user or self._login_user()

    def can_ssh_as(self, *, user: str) -> bool:
        """Whether *user* can authenticate an ssh connection of their own here.

        The term is ``ssh`` and they have a direct credential
        (:meth:`~otto.host.connections.ConnectionManager.has_direct_cred`): the
        one answer both the exec session mode (whether *user*'s pooled session can
        run without a terminal) and ``_own_ssh_login`` (a pooled session over
        their own login) read.
        """
        if getattr(self._connections, "term", None) != "ssh":
            return False
        has_direct_cred = getattr(self._connections, "has_direct_cred", None)
        return has_direct_cred is not None and has_direct_cred(user=user)

    def _own_ssh_login(self, as_user: str | None) -> str | None:
        """*as_user* when a session for them is opened over their own ssh login, else ``None``.

        That is: *as_user* is not the login and :meth:`can_ssh_as` them. Such
        a session needs no switch and no login proxy: its transport is
        :meth:`~otto.host.connections.ConnectionManager.ssh_as`.
        """
        if as_user is None or as_user == self._login_user() or not self.can_ssh_as(user=as_user):
            return None
        return as_user

    async def _apply_login_proxy(self, session: "ShellSession") -> None:
        """Replay the connection manager's proxy hops over *session*.

        Runs immediately after a fresh session's marker handshake succeeds
        (both the default-session path in ``_ensure_session`` and the
        named-session build in ``open_session``) so the shell ends up as
        ``login_target`` rather than the transport's directly-authenticated
        user. Reads ``proxy_hops``/``login_target``/``credentials`` off
        ``self._connections``, tolerating fakes that expose none of them (no
        hops -> no-op, matching a plain direct login).

        Each hop's ``via`` is the previous hop's login — or, for the first
        hop, the resolved direct cred's login (the account the transport
        actually authenticated as). The via login is resolved to its FULL
        cred under the manager's TERM (``cred_for(self._creds, via_login,
        term) or Cred(login=via_login)``) — the term picks among same-login
        creds of different scope, so this is the same cred
        ``perform_switch`` and its undo path resolve for that account: no
        registered proxy reads ``ctx.via.password`` today (the built-in
        ``su`` ignores ``via`` entirely), but a future custom proxy that
        needs the via account's password gets it rather than a silent None.

        Raises :class:`~otto.host.login_proxy.LoginProxyError` on a failed
        hop; the caller is responsible for tearing the session down (a proxy
        failure is not a handshake race — it must not consume a retry).
        """
        hops = getattr(self._connections, "proxy_hops", [])
        if not hops:
            return
        target = getattr(self._connections, "login_target", "")
        creds = getattr(self._connections, "credentials", None)
        via_login = creds[0] if creds else ""
        switch_creds = self._creds or []
        term = getattr(self._connections, "term", None)
        io = _SessionProxyIO(session, self._log_command, self._log_output, fold_log=self._fold_log)
        for hop in hops:
            via = cred_for(switch_creds, via_login, term) or Cred(login=via_login)
            prefix = session._new_shell_prefix()  # noqa: SLF001 — the manager's own session
            await run_proxy(io, hop, via=via, host_id=self._host_id, shell_prefix=prefix)
            session.note_reached_by_switch()
            via_login = hop.login
        session.current_user = target

    async def _apply_session_setup(self, session: "ShellSession", kind: "SetupKind") -> None:
        """Run the host's session-setup hook over *session*, then enter the command frame.

        A no-op for a host without a hook — which is what keeps every such
        host byte-identical. Otherwise the hook gets a HostSession handle in
        establishing mode (close() refused, enter_frame() available), a
        SetupContext naming which session this is, and afterwards
        ``enter_frame(command_frame)`` runs UNCONDITIONALLY: that repeat is
        the post-hook confirmation, the same probe on every family. Errors
        are wrapped by :func:`~otto.host.session_setup.apply_session_setup`;
        the caller tears the session down.

        The import is local and BELOW the early return, deliberately:
        ``otto.host.session_setup`` is off the CLI startup import graph and
        this module is on it, so a host without a hook must not pull it in
        (a row in tests/unit/test_import_contracts.py pins it).
        """
        if self._session_setup is None:
            return
        from .session_setup import SetupContext, apply_session_setup

        # ONE frame object for both uses. On a host that declares no
        # command_frame `_target_frame()` mints a fresh BashFrame per call, and
        # the handle's enter_frame() and the confirmation below would then be
        # switching to two different objects — equivalent today (frames are
        # stateless value objects) and a trap the moment one is not.
        target = self._target_frame()
        handle = HostSession(
            name=f"__setup_{kind}__",
            session=session,
            log_command=self._log_command,
            log_output=self._log_output,
            fold_log=self._fold_log,
            deregister=lambda _n: None,
            creds=self._creds,
            host_id=self._host_id,
            target_frame=target,
            establishing=True,
            term=getattr(self._connections, "term", None),
        )
        ctx = SetupContext(
            host_id=self._host_id,
            host_name=self._name,
            user=session.current_user,
            params=dict(self._session_setup.params),
            kind=kind,
        )
        await apply_session_setup(session, handle, ctx, self._session_setup, target)

    @property
    def current_user(self) -> str:
        """User the default session is currently running as.

        Seeded from the login user; changed only via switch_user/as_user.
        Falls back to the login user before the default session is built.
        """
        if self._session is not None:
            return self._session.current_user
        return self._login_user()

    @property
    def terminal_mapping(self) -> str:
        """How the default session's terminal maps its output; ``unmeasured`` before it opens.

        See :mod:`otto.host.terminal_mapping` for the four line-ending
        outcomes and the ``, tabs expanded`` and ``, echo on`` suffixes.
        """
        if self._session is not None:
            return self._session.terminal_mapping
        return UNMEASURED

    @property
    def ambient_user(self) -> str | None:
        """The user the default session was SWITCHED to, or ``None``.

        ``None`` when there is no default session, or when it runs as the
        user it was opened as — the login target, after proxy hops. This is
        what lets ``exec`` inside ``async with host.as_user(X)`` run as X: the
        switch lives on the default session, and ``exec`` never touches that
        session, so it has to be told.
        """
        if self._session is None or self._baseline_user is None:
            return None
        current = self._session.current_user
        return current if current != self._baseline_user else None

    def _live_default_session(self) -> "ShellSession | None":
        """Return the default session when it exists and is alive, else ``None``.

        What the host's scoped switch reads: its undo types only into the
        session it switched, and only while that session is still the live
        default one.
        """
        if self._session is not None and self._session.alive:
            return self._session
        return None

    def _set_current_user(self, user: str) -> None:
        """Private bookkeeping for the default session.

        Called only by the elevation flow (PosixPrivilege.switch_user/as_user) after a real
        ``su`` has run — never a public API (that would let callers desync
        the tracked user from the shell's actual user).
        """
        if self._session is not None:
            self._session.current_user = user

    async def _ensure_session(self, *, terminal: bool | None = None) -> "ShellSession":
        """Return the default session of mode *terminal*, creating it if absent or dead.

        *terminal* picks the slot (:meth:`_wants_plain`); the open is the
        same sequence for both. A session without a terminal is only asked
        for where :meth:`no_terminal_limitation` is ``None``, so the proxy
        and setup steps below find nothing to do on it.

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

        The session is built and its handshake confirmed by
        :meth:`_open_confirmed`, which rebuilds it once after a failed
        handshake; the login proxy's hops and the setup hook run after, and
        are not retried.
        """
        plain = self._wants_plain(terminal)
        current = self._default_session(plain=plain)
        if current and current.alive:
            return current

        async with self._ensure_session_lock:
            # Re-check after acquiring the lock — another task may have
            # created the session while we waited.
            current = self._default_session(plain=plain)
            if current and current.alive:
                return current

            # Close the old dead session to release its subprocess transport.
            # Without this, the orphaned transport is GC'd after the event loop
            # closes and raises "RuntimeError: Event loop is closed" from __del__.
            if current is not None:
                await current.close()

            new_session = await self._open_confirmed(
                lambda: self._build_session(terminal=not plain)
            )
            # The handshake is confirmed; a hop or the setup hook failing
            # now is not a handshake race, so it is not retried: the session
            # is closed and the error propagates.
            try:
                await self._apply_login_proxy(new_session)
                await self._apply_session_setup(new_session, "default")
            except BaseException:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await new_session.close()
                raise
            if plain:
                self._plain_session = new_session
            else:
                self._session = new_session
                self._baseline_user = new_session.current_user
            return new_session

    async def _open_confirmed(
        self,
        build: "Callable[[], Awaitable[ShellSession]]",
        *,
        user: str | None = None,
        named: bool = False,
    ) -> "ShellSession":
        """Build a session with *build* and return it once its marker handshake is confirmed.

        The one open sequence of the default session (``_ensure_session``)
        and of a named or pooled session (``open_session``): wire the output
        sink, seed *user* (:meth:`_seed_user`), run the handshake. Nothing is
        published here; the caller does that with the confirmed session.

        A handshake that fails with a ``ConnectionError`` is retried **once**,
        on a session *build* makes afresh, after ``_retry_backoff``. Two
        failures were measured on healthy devices:

        - a single-slot console not yet ready: concurrent fan-out across
          embedded targets sharing one ssh hop (``do_for_all_hosts`` over
          several Zephyr boards) can land a fresh telnet socket on a device
          that accepts the TCP connection then closes it before the marker
          probe lands (``IncompleteReadError`` of 0 bytes). Single-client
          RTOS telnet servers do not always free the slot the instant the
          FIN lands, hence the backoff;
        - a login whose password the device discarded: BusyBox 1.21.1 and
          1.22.1 ``login`` print the password prompt before they flush
          pending input, so a password sent the instant the prompt appears
          can be thrown away and ``login`` exits on what follows.

        A genuine never-reached-a-prompt failure (see
        :meth:`ShellSession._fail_init`) fails the same way on the second
        attempt and propagates. A wrong password is so offered twice per
        session opened. Never retried: a :class:`ConsoleError`, and a
        session whose ``_retry_failed_handshake`` is false (a console whose
        client typed a password: a rebuild would type it into the line
        again), and a *named* (named or pooled) open on a line that serves
        one client (:func:`~otto.host.telnet.retries_failed_open`). Any
        failure, cancellation included, closes the session it built before
        propagating.
        """
        # A retry of the one-shot build on a handshake race, not a poll of
        # a state (the rule's note names it).
        # ast-grep-ignore: no-handrolled-deadline-poll
        for attempt in range(2):
            session = await build()
            session._on_output = _sink_for(self._log_output, LogMode.NORMAL)  # noqa: SLF001 — intra-package wiring of output callback on freshly-built ShellSession
            self._seed_user(session, user)
            try:
                await session._ensure_initialized()  # noqa: SLF001 — intra-package access to ShellSession._ensure_initialized for handshake
            except ConsoleError:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await session.close()
                raise
            except ConnectionError as exc:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await session.close()
                retries = session._retry_failed_handshake and (  # noqa: SLF001 — intra-package read of the session's own retry verdict
                    not named or session._retries_named_open()  # noqa: SLF001 — the same verdict, for a named or pooled open
                )
                if attempt == 0 and retries:
                    logger.info(
                        f"SessionManager[{self._name}]: handshake failed "
                        f"on first attempt ({exc!r}); rebuilding "
                        f"transport and retrying once"
                    )
                    await asyncio.sleep(self._retry_backoff)
                    continue
                raise
            except BaseException:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await session.close()
                raise
            return session
        # The second attempt returns or raises.
        raise AssertionError("unreachable")  # pragma: no cover

    async def _build_session(self, *, terminal: bool = True) -> "ShellSession":
        """Construct a fresh ShellSession from the configured factory or connection manager.

        :meth:`_open_confirmed` calls it again to rebuild the transport after a failed handshake.
        *terminal* false is the transport's session without a terminal; a
        factory builds the one kind of session it knows.
        """
        if self._session_factory is not None:
            return self._from_factory(self._session_factory)
        return await self._open_transport_session(named=False, terminal=terminal)

    async def _build_named_session(
        self, *, own_login: str | None, terminal: bool | None
    ) -> "ShellSession":
        """Construct a fresh ShellSession for ``open_session``.

        The named factory (else the default one) unless the session
        authenticates as *own_login*, its own ssh user; else the transport's
        named session, of mode *terminal* (``None`` is a terminal).
        """
        named_factory = self._named_session_factory or self._session_factory
        if named_factory is not None and own_login is None:
            return self._from_factory(named_factory)
        return await self._open_transport_session(
            named=True, ssh_user=own_login, terminal=terminal is not False
        )

    def _from_factory(self, factory: "Callable[[], ShellSession]") -> "ShellSession":
        """Build a session from *factory*, holding the deadline this manager resolves.

        A factory takes no arguments, so the session it builds knows nothing
        of the host's ``init_timeout`` or its family default; without this, a
        LocalHost or Docker session would wait what the environment says
        while :meth:`open_budget` sized the open for the host's own value.
        Whatever deadline the factory set itself is replaced whenever the
        manager resolves one: the manager's is the value the budget counts.
        """
        deadline = self._readiness_deadline()
        session = factory()
        if deadline is not None:
            session._init_timeout = deadline  # noqa: SLF001 — intra-package: the manager owns its sessions' readiness deadline
        return session

    async def _open_transport_session(
        self, *, named: bool, ssh_user: str | None = None, terminal: bool = True
    ) -> "ShellSession":
        """Build a ShellSession over the manager's term: ssh, telnet or console.

        The ONE place the term is dispatched, for the default session
        (:meth:`_build_session`) and named ones (:meth:`open_session`) alike.
        *named* selects the telnet dedicated-client path: a named session
        owns its own ``TelnetClient`` rather than sharing the manager's. A
        console has no such path — the line serves ONE client, and the
        default session already holds it — so a named request there is
        refused by name rather than dialled. ``exec`` never asks for one on a
        console: the host sends it to the default session
        (:meth:`exec_on_default_session`).

        *ssh_user* (``ssh`` only) builds the session over that user's own
        connection (:meth:`~otto.host.connections.ConnectionManager.ssh_as`)
        instead of the login's.

        *terminal* false (``ssh`` only, where :meth:`no_terminal_limitation`
        allows it) builds the session without a pty.
        """
        assert self._connections is not None  # noqa: S101 — internal invariant: _connections required when no session_factory
        # Resolved before anything dials: an unusable OTTO_INIT_TIMEOUT is
        # refused here, not after a client is connected (or a console logged
        # in) only to be dropped unclosed when the session refuses it.
        init_timeout = self._readiness_deadline()
        term = self._connections.term
        if term == "ssh":
            ssh_conn = (
                await self._connections.ssh_as(user=ssh_user)
                if ssh_user is not None
                else await self._connections.ssh()
            )
            return SshSession(
                ssh_conn,
                command_frame=self._session_frame,
                init_timeout=init_timeout,
                shell_history=self._shell_history,
                terminal=terminal,
            )
        if term == "telnet" and not named:
            telnet_conn = await self._connections.telnet()
            logger.debug(
                f"SessionManager[{self._name}]: building telnet session "
                f"with frame={type(self._session_frame).__name__}"
            )
            return TelnetSession(
                telnet_conn.reader,
                telnet_conn.writer,
                client=telnet_conn,
                command_frame=self._session_frame,
                init_timeout=init_timeout,
                write_chunk_size=telnet_conn.options.write_chunk_size,
                write_chunk_delay=telnet_conn.options.write_chunk_delay,
                shell_history=self._shell_history,
            )
        if term == "telnet":
            user, password = self._connections.credentials
            # Ask the ConnectionManager WHERE to dial rather than
            # reading `.ip` off it: behind a hop those differ, and
            # the raw address is the guest's own loopback — i.e.
            # this machine. See `ConnectionManager.telnet_target`.
            target = await self._connections.telnet_target()
            client = TelnetClient(
                target.host,
                user=user,
                password=password,
                options=self._connections.telnet_options,
                connect_port=target.port,
            )
            # A caller-side ``wait_for`` cancellation can land
            # anywhere in ``connect()`` (TCP, ECHO negotiation,
            # credential exchange) and would otherwise drop
            # ``client`` on the floor with its socket still open.
            # Tear down on any exception (including CancelledError)
            # so the FD is released. The marker handshake that
            # follows is guarded separately, in the caller.
            try:
                await client.connect()
            except BaseException:
                with suppress(Exception):
                    await client.close()
                raise
            return TelnetSession(
                client.reader,
                client.writer,
                client=client,
                command_frame=self._session_frame,
                init_timeout=init_timeout,
                write_chunk_size=client.options.write_chunk_size,
                write_chunk_delay=client.options.write_chunk_delay,
                shell_history=self._shell_history,
            )
        if term == "console":
            opts = self._connections.console_options
            if named:
                raise ConsoleError(
                    f"{self._name}: console is single-client ({opts.server}:{opts.port}, "
                    f"dial={opts.dial}); named sessions are not available on this "
                    f"term — use run() or exec(), or select another term"
                )
            # Connected AND logged in on return (or it raised ConsoleError):
            # the console login runs in the transport slot, before the
            # landing handshake this session goes on to run.
            console = await self._connections.console()
            return TelnetSession(
                console.reader,
                console.writer,
                console_client=console,
                command_frame=self._session_frame,
                init_timeout=init_timeout,
                write_chunk_size=opts.write_chunk_size,
                write_chunk_delay=opts.write_chunk_delay,
                shell_history=self._shell_history,
                failure_context=(
                    f"console {opts.server}:{opts.port} (dial={opts.dial}); a login prompt "
                    f"in the output means the login was refused"
                ),
            )
        raise ValueError(f'{self._name}: unsupported terminal type "{term}"')

    async def run_cmd(
        self,
        *,
        cmd: str,
        expects: list[Expect | CredentialExpect] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
        write_progress: Callable[[int, int], None] | None = None,
        exec_input: ExecInput | None = None,
        terminal: bool | None = None,
        exec_escapes: bool = False,
    ) -> CommandResult:
        """Run *cmd* on the default session, creating it if needed.

        *terminal* false runs it on the default session WITHOUT a terminal
        (``_wants_plain``), a session of its own with its own shell
        state; ``None`` and the transport's own mode are the default session.

        Wraps :meth:`~otto.host.session.ShellSession.run_cmd` with automatic
        session creation. Shell state persists across calls (same session is
        reused until it dies).

        THE PER-COMMAND PATH OF :meth:`otto.host.host.BaseHost.run`, for every
        host family (``BaseHost._run_one``, which every family runs) -- and
        ``exec()`` on a
        ``console`` term, which runs on this same session. Everywhere else
        ``exec()`` and named sessions reach a :class:`ShellSession` without
        passing through here. A pooled ``exec()`` makes the same refusal in
        ``_run_framed``, the step it shares with this method; a named
        session's ``HostSession.run`` is the one path left unguarded,
        deliberately, because ``ShellFileTransfer`` rides it
        (see the ``run-command-line-length`` gap record). The refusal
        advises ``exec(..., terminal=False)`` only where the host says that
        call would be carried (*exec_escapes*, from ``BaseHost._exec_plain``).

        The refusal is checked twice, through the same function. BEFORE
        ``_ensure_session`` against the widest ceiling any session here can
        have (echo off), so a line no session could carry costs no connection;
        then, once the session is open and before anything is typed, against
        the ceiling of the session the line will be typed into, from its
        measured echo fact (:func:`typed_line_ceiling`). ``run()`` opens the
        session anyway, so the second check costs nothing on the sunny path.

        *exec_input* is an ``exec``'s on this session (a console's, an
        embedded host's): it gets this session's restore
        (``_exec_restore_for``, here and nowhere else for this session), is
        measured by the refusal as the exec frame it types, and
        :meth:`ShellSession.run_cmd` runs it in that frame. ``None`` for a ``run``.
        """
        exec_input = self._with_restore_for(True, exec_input)
        # The host's answer for this call: a run's (*exec_escapes*), or the exec's own.
        if exec_input is not None:
            exec_escapes = exec_input.exec_escapes
        # Before the open the mode asked for answers; an open session
        # answers for itself, here as below.
        plain = self._checked_plain(terminal)
        held = self._default_session(plain=plain)
        opened = held if held is not None and held.alive else None
        asked = self._types_into_terminal and not plain
        if asked if opened is None else opened.reads_a_terminal:
            refuse_if_typed_line_would_be_cut(
                self._command_frame,
                cmd,
                echoes=_BEFORE_ANY_SESSION_ECHOES,
                host=self._name,
                exec_escapes=exec_escapes,
                exec_input=exec_input,
            )
        session = await self._ensure_session(terminal=terminal)
        # *log* is folded already: the host's `_run_one` folds its standing mode in.
        return await self._run_framed(
            session,
            cmd,
            log_command=self._log_command,
            log_output=self._log_output,
            mode=log,
            expects=expects,
            timeout=timeout,
            exec_input=exec_input,
            exec_escapes=exec_escapes,
            write_progress=write_progress,
        )

    async def _run_framed(
        self,
        session: ShellSession,
        cmd: str,
        *,
        log_command: Callable[[str, LogMode], None],
        log_output: Callable[[str, LogMode], None],
        mode: LogMode,
        expects: "list[Expect | CredentialExpect] | None",
        timeout: float,
        exec_input: ExecInput | None,
        exec_escapes: bool,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandResult:
        """Run *cmd* framed on the open *session*.

        THE ONE STEP of :meth:`run_cmd` (``run()`` and a default-session
        ``exec``) and of a pooled ``exec`` (:meth:`_exec_on_pooled`); each
        caller chooses and releases its session and hands it here with what
        differs. Before anything is logged or typed, a session that reads a
        terminal refuses a line its terminal would cut
        (:func:`refuse_if_typed_line_would_be_cut`, measured against the
        session's echo fact and in the frame *exec_input* selects); then
        :func:`_log_and_run` logs it under the folded *mode* through
        *log_command* and *log_output* and runs it. *exec_input* is passed on
        exactly as the caller holds it.
        """
        if session.reads_a_terminal:
            refuse_if_typed_line_would_be_cut(
                self._command_frame,
                cmd,
                echoes=session.echoes,
                host=self._name,
                exec_escapes=exec_escapes,
                exec_input=exec_input,
            )
        return await _log_and_run(
            session,
            log_command,
            log_output,
            cmd,
            mode=mode,
            expects=expects,
            timeout=timeout,
            exec_input=exec_input,
            host=self._name,
            write_progress=write_progress,
        )

    @staticmethod
    def _exec_restore_for(shared: bool) -> ExecRestore:
        """Return what an isolated ``exec`` restores: the one predicate.

        On the host's *shared* default session (a console's, an embedded
        target's), the one ``run`` uses, it restores the terminal as it found
        it (``START``); a pooled session restores the record its handshake
        kept (``BASELINE``).
        """
        return ExecRestore.START if shared else ExecRestore.BASELINE

    @classmethod
    def _with_restore_for(cls, shared: bool, exec_input: ExecInput | None) -> ExecInput | None:
        """Return *exec_input* with its session's restore (:meth:`_exec_restore_for`).

        ``None`` (a run) stays ``None``.
        """
        if exec_input is None:
            return None
        return dataclasses.replace(exec_input, restore=cls._exec_restore_for(shared))

    async def exec_on_default_session(
        self,
        *,
        cmd: str,
        timeout: float,
        log: LogMode,
        expects: "list[Expect | CredentialExpect] | None" = None,
        user: str | None = None,
        exec_input: ExecInput | None = None,
    ) -> CommandResult:
        """Run *cmd* as an ``exec`` on the default session, the one ``run`` uses.

        A host whose ``run`` and ``exec`` share one line
        (``BaseHost._shares_one_line``, a console term) sends its ``exec``
        here. What the command leaves in the session's shell is the dialect's
        :attr:`~otto.host.command_frame.CommandFrame.isolates_exec` answer;
        :meth:`run_cmd` gives *exec_input* the default session's restore.

        A console line serves one client, so the session ``run`` uses is the
        only one there is: a pooled session would be a second connection to a
        port that refuses it. The command therefore goes through
        :meth:`run_cmd`, with ``run``'s marker and exit-code handling, in the
        frame's exec frame (*exec_input*), so on a dialect that isolates
        ``exec`` nothing it changes (a ``cd``, a variable) stays for the next
        command.

        This layer does not serialise concurrent callers and cannot switch
        the session's identity. Both belong to the host, which owns the
        switch (:meth:`~otto.host.privilege.PosixPrivilege.as_user`) and so
        holds its line turn (``BaseHost._line_turn``, taken in
        ``BaseHost.exec`` through ``BaseHost._host_entry``) across the switch,
        the command and the switch back (see ``UnixHost._run_exec``). A *user* other than the
        session's current one reaching here is refused by name rather than
        run as the wrong user.
        """
        current = self.current_user
        if user is not None and user != current:
            raise ConsoleError(
                f"{self._name}: console is single-client; exec runs on its one "
                f"session as {current!r}, so it cannot run as {user!r} here — "
                f"switch the session first (host.exec(user=...) does)"
            )
        return await self.run_cmd(
            cmd=cmd, expects=expects, timeout=timeout, log=log, exec_input=exec_input
        )

    async def exec_pooled(
        self,
        *,
        cmd: str,
        timeout: float,
        log: LogMode,
        expects: "list[Expect | CredentialExpect] | None" = None,
        user: str | None = None,
        exec_input: ExecInput | None = None,
        plain: bool = False,
    ) -> CommandResult:
        """Run *cmd* as an ``exec`` on a pooled shell session opened for *user*.

        The session has no terminal when *plain*.

        Every ssh, telnet and local ``exec`` runs here. What the command
        leaves in the session's shell is the dialect's
        :attr:`~otto.host.command_frame.CommandFrame.isolates_exec` answer,
        and *exec_input* gets a pooled session's restore
        (``_exec_restore_for``). Concurrent calls are safe: each takes
        its own session.

        A term this manager has no transport for is refused by name where
        the session's transport is chosen (``_open_transport_session``),
        before anything is opened.

        A shell session is what a proxied login needs anyway (an ssh exec channel authenticates
        as the direct cred and cannot replay the proxy steps, so exec/nc would
        run as the via-user rather than the target): a pooled session,
        which ``open_session`` builds through ``_apply_login_proxy``
        and so ends up as the effective user. Rather than open a fresh
        TCP+auth for every exec call — 1-2 s each on real hardware — a
        free-list of idle persistent sessions is kept and reused. Serial
        callers churn one session; concurrent callers (e.g. ``_put_files_nc``
        launching multiple ``nc -l`` listeners in parallel) each get their
        own, preserving the documented concurrency contract.

        There is one free-list per user (``_pool_key``: *user*, else the
        login) and mode (*plain*: opened without a terminal); the two modes
        never share a session. A session for another user is opened FOR them
        (:meth:`open_session` with ``as_user``): over their own ssh login when
        they have a direct credential on an ssh host, otherwise switched to
        them before the session-setup hook runs, so the hook sets the session
        up as them. Nothing is switched per call.

        RETENTION. A session goes back to its user's free-list whenever it is
        alive, is still running as that user, and has not lost its terminal
        record (:attr:`ShellSession.exec_baseline_lost`); anything else closes
        it, so the next ``exec`` never inherits a session that is someone else
        (a command that ran ``su``, say). A command otto gave up on (a
        timeout, a cancel, a refused password) left its session not alive:
        it is stopped, never restored or kept (:attr:`ExecInput.disposable`),
        so it is closed here. Without a terminal that stop ends everything the
        session started, a background process an earlier ``exec`` left
        included. On ``ssh`` a free-list keeps
        at most one idle session, on the local host none
        (``_idle_exec_sessions_kept``); elsewhere nothing is evicted and
        each free-list holds at most that user's peak of concurrent ``exec``
        calls. :meth:`close_all` closes them all.

        IDENTITY. A session its open reached by a switch (a login-proxy hop or
        a switch to its user: :attr:`ShellSession.reached_by_switch`) has
        another shell underneath it. Where its frame can guard, it carries an
        identity mark set on the line of its identity proof after setup
        (``_mark_identity``), and every exec on it is typed with the
        frame's guard (:meth:`~otto.host.command_frame.CommandFrame.identity_guard`).
        When the switched shell has gone (an idle ``TMOUT``, a kill) and the
        shell underneath answers the guard, the command is not run: the
        session is closed and the call runs once on a fresh session for the
        user. A fresh session that has lost its user too raises
        :class:`~otto.host.login_proxy.LoginProxyError`. Where its frame has no
        guard, the session runs its exec and is closed, never kept.

        A line the session's terminal would cut is refused first, through
        :func:`refuse_if_typed_line_would_be_cut` measured in the exec frame
        and against the acquired session's echo fact, before anything is typed;
        the session goes back to the free-list untouched.
        """
        # NO logging here: `_run_framed` logs the command, once.
        exec_input = self._with_restore_for(False, exec_input)
        key = self._pool_key(user)
        session = await self._acquire_exec_session(user=user, plain=plain)
        result = await self._exec_on_pooled(
            session,
            cmd,
            key=key,
            plain=plain,
            timeout=timeout,
            log=log,
            expects=expects,
            exec_input=exec_input,
        )
        if not session._session.identity_lost:  # noqa: SLF001 — the manager's own pooled session
            return result
        # The switched shell had gone, and the guard kept the command from the
        # shell underneath: open a fresh session for the user, once.
        session = await self._open_exec_session(key, plain=plain)
        result = await self._exec_on_pooled(
            session,
            cmd,
            key=key,
            plain=plain,
            timeout=timeout,
            log=log,
            expects=expects,
            exec_input=exec_input,
        )
        if session._session.identity_lost:  # noqa: SLF001 — the manager's own pooled session
            raise LoginProxyError(
                f"{self._host_id}: a fresh session switched to {key!r} lost that user "
                f"before its command ran; the command was not run"
            )
        return result

    async def _exec_on_pooled(
        self,
        session: "HostSession",
        cmd: str,
        *,
        key: str,
        plain: bool,
        timeout: float,
        log: LogMode,
        expects: "list[Expect | CredentialExpect] | None",
        exec_input: ExecInput | None,
    ) -> CommandResult:
        """Run *cmd* on the pooled *session* of *key* and mode *plain*, then keep or close it."""
        mark = session._session.identity_mark  # noqa: SLF001 — the manager's own pooled session
        if exec_input is not None:
            # A command otto gives up on here is stopped, never restored or
            # kept: the session it leaves dead is closed by _release_pooled.
            exec_input = replace(exec_input, disposable=True)
        if exec_input is not None and mark is not None:
            exec_input = replace(exec_input, identity=mark)
        # A refusal typed nothing: the same keep-or-discard decision as a command's end.
        try:
            declined = session._declined_under_dry_run(cmd, timeout=timeout, log=log)  # noqa: SLF001 — the manager's own pooled session
            if declined is not None:
                return declined
            return await self._run_framed(
                session._session,  # noqa: SLF001 — the manager's own pooled session
                cmd,
                log_command=session._log_command,  # noqa: SLF001 — the manager's own pooled session
                log_output=session._log_output,  # noqa: SLF001 — the manager's own pooled session
                mode=session._fold_log(log),  # noqa: SLF001 — the manager's own pooled session
                expects=expects,
                timeout=timeout,
                exec_input=exec_input,
                exec_escapes=exec_input is not None and exec_input.exec_escapes,
            )
        finally:
            await self._release_pooled(session, key, plain=plain)

    async def _release_pooled(self, session: "HostSession", key: str, *, plain: bool) -> None:
        """Return *session* to its (*key*, *plain*) free-list, or close it: THE ONE retention rule.

        Read after every exec, and after a refusal that typed nothing. A
        session is kept only while it is alive, still *key*, with its terminal
        record and its identity mark intact, and not a switched session whose
        frame has no identity guard; and, where the transport limits its idle
        sessions (:meth:`_idle_exec_sessions_kept`), only while its free-list
        holds fewer live ones than that.
        """
        lost = session._session.identity_lost  # noqa: SLF001 — the manager's own pooled session
        unguarded = session._session.identity_unguarded  # noqa: SLF001 — the manager's own pooled session
        keepable = (
            session.alive
            and session.current_user == key
            and not session.exec_baseline_lost
            and not lost
        )
        idle = self._exec_pools.setdefault((key, plain), [])
        kept = self._idle_exec_sessions_kept()
        if keepable and not unguarded and kept is not None:
            if sum(s.alive for s in idle) >= kept:
                # An idle shell that can exit on its own is let go without a
                # signal (the local host's): the close then signals nothing it
                # left, though it still releases the shell's pipes.
                try:
                    with suppress(Exception):
                        await session._session.end_input()  # noqa: SLF001 — the manager's own pooled session
                finally:
                    with suppress(Exception):
                        await session.close()
            else:
                idle.append(session)
        elif keepable and not unguarded:
            idle.append(session)
        elif keepable:
            # By design: a session reached by a switch whose frame has no
            # identity guard works, but is not reused.
            logger.debug(
                "%s: not pooling exec session %s: reached by a switch, and its frame "
                "has no identity guard",
                self._name,
                session.name,
            )
            with suppress(Exception):
                await session.close()
        else:
            logger.warning(
                "%s: not pooling exec session %s (alive=%s, baseline_lost=%s, user=%r, "
                "opened for %r, identity_lost=%s)",
                self._name,
                session.name,
                session.alive,
                session.exec_baseline_lost,
                session.current_user,
                key,
                lost,
            )
            with suppress(Exception):
                await session.close()

    def line_budget(self, *, shared: bool, plain: bool = False) -> int | None:
        """Longest line of a command an ``exec`` can carry here, or ``None`` if unbounded.

        ``None`` is not "unknown" and not zero: it is the answer for a session
        that types into no line discipline at all (``LocalHost``'s, which
        reads a pipe, or a pooled session opened without a terminal, *plain*),
        and a caller reading it should keep doing whatever it did before.

        WHO ASKS, AND WHY IT IS ANSWERED HERE. ``ShellFileTransfer`` builds its
        own command lines and needs to know how long one may be, but the two
        facts that decide it (which session its ``exec`` takes, the host's
        answer handed in as *shared*, and what otto's own framing costs on
        this host's dialect) are not visible from a transfer backend.

        *shared* is the host's default session (``BaseHost._shares_one_line``):
        its ``exec`` restores more (``START``), so it types a longer frame.

        The number is :func:`typed_line_budget`'s, with the echo fact the open
        sessions measured: echo off only once every open session showed it.
        It is :meth:`line_room` for an empty command, and a caller that builds
        its own command around a payload asks that instead.
        """
        return self.line_room(cmd="", shared=shared, plain=plain)

    def line_room(self, *, cmd: str, shared: bool, plain: bool = False) -> int | None:
        """Characters a quote-free payload inserted into *cmd*'s first line may add in an ``exec``.

        ``None`` exactly where :meth:`line_budget` is (no line discipline:
        a session without a terminal when *plain*, or a manager whose
        sessions read a pipe). Otherwise :func:`typed_line_room`'s, rendered
        through the exec frame typed with the restore of the session
        *shared* names (``_exec_restore_for``), with the echo fact
        :meth:`line_budget` reads.
        """
        if plain or not self._types_into_terminal:
            return None
        # No credential: the budget's one caller, the shell transfer, never
        # elevates, and the credential redirect rides the frame's closing
        # line, not the line the command is typed on, so it moves no budget.
        return typed_line_room(
            self._command_frame,
            cmd,
            echoes=self._measured_echoes(),
            exec_input=ExecInput(stdin_eof=True, restore=self._exec_restore_for(shared)),
        )

    def _measured_echoes(self) -> bool:
        """Whether a line typed on this host may meet an echoing terminal, from the open sessions.

        ``False`` only when at least one session is open and every open session
        measured echo off at its handshake; ``True`` otherwise, which is the
        conservative answer :func:`typed_line_ceiling` wants for an unmeasured
        terminal. A caller that plans before any session opened (a transfer
        sized ahead of its first command) therefore gets the 1022 budget on an
        ash host, which only shortens its chunks.

        It may be more conservative than the session a ``run`` uses (this
        host's budget reads 1022 because a named session echoes, while the
        default session a ``run`` types into allows 4095); that is safe,
        because a smaller budget only shortens a transfer's chunks and
        ``run_cmd`` checks its own session's measured fact before typing.
        """
        # A session without a terminal measured no terminal: it says nothing here.
        opened = [
            s
            for s in [self._session, *self._named_sessions.values()]
            if s is not None and s.alive and s.reads_a_terminal
        ]
        return not opened or any(s.echoes for s in opened)

    async def _acquire_exec_session(
        self, *, user: str | None, plain: bool = False
    ) -> "HostSession":
        """Pop an idle session off *user*'s free-list of mode *plain*, or open a new one for them.

        The free-list is :meth:`_pool_key`'s bucket of that mode. A new session is opened
        FOR that user (``open_session(as_user=...)``), so it is them before
        its session setup runs. Sessions are named by a monotonic index so
        concurrent callers get independent entries in ``_named_sessions``.
        Dead sessions (closed or disconnected) are skipped so callers always
        get a usable one.
        """
        key = self._pool_key(user)
        idle = self._exec_pools.get((key, plain), [])
        while idle:
            session = idle.pop()
            if session.alive:
                return session
        return await self._open_exec_session(key, plain=plain)

    async def _open_exec_session(self, key: str, *, plain: bool = False) -> "HostSession":
        """Open a new pooled session for *key*, without a terminal when *plain*.

        Named by the pool's monotonic index. A pool name is never opened
        again, so an open that fails or is cancelled gives its per-name lock
        back at once (:meth:`_forget_session` does on a close).
        """
        self._exec_pool_count += 1
        name = f"{_EXEC_POOL_PREFIX}{self._exec_pool_count}__"
        try:
            return await self.open_session(
                name=name, as_user=key, terminal=False if plain else None
            )
        except BaseException:
            self._named_session_locks.pop(name, None)
            raise

    def _forget_session(self, name: str) -> None:
        """Drop the closed session *name* from the registry, and a pool session's lock with it.

        An exec-pool name is never opened again, so its lock has no other
        holder to race; an ordinary named session's lock is kept until
        :meth:`close_all`, which no open can race.
        """
        self._named_sessions.pop(name, None)
        if name.startswith(_EXEC_POOL_PREFIX):
            self._named_session_locks.pop(name, None)

    async def _mark_identity(self, session: "ShellSession") -> None:
        """Give a pooled session reached by a switch its identity mark, or record that it has none.

        Run once, at open, after the setup hook and its frame entry, in the
        shell the session will type into (a hook may have started another).
        Where that shell's frame can guard
        (:attr:`~otto.host.command_frame.CommandFrame.guards_identity`), the
        mark is set on the line of the switch's own identity proof
        (:func:`~otto.host.login_proxy.prove_identity`), so the shell that
        answers as the user is the shell that holds the mark; every exec then
        carries the frame's guard. Where it cannot (an application dialect a
        hook entered), or the shell cannot name itself (no ``id``), the
        session is marked unguarded and the exec pool does not keep it.

        Raises:
            ~otto.host.login_proxy.LoginProxyError: the shell did not answer
                as the session's user.
        """
        frame = session._frame  # noqa: SLF001 — the manager's own session, its frame after setup
        if not frame.guards_identity:
            session.identity_unguarded = True
            return
        mark = secrets.token_hex(8)
        proven = await prove_identity(
            _SessionProxyIO(session, self._log_command, self._log_output, fold_log=self._fold_log),
            host_id=self._host_id,
            user=session.current_user,
            marking=frame.identity_mark_statement(mark=mark),
            shell_prefix=session._new_shell_prefix(),  # noqa: SLF001 — the manager's own session
        )
        if not proven:
            # The shell could not name itself (no `id`): a mark it holds
            # vouches for nothing, so the session is treated as unguarded.
            session.identity_unguarded = True
            return
        session.identity_mark = mark

    async def open_session(
        self, *, name: str, as_user: str | None = None, terminal: bool | None = None
    ) -> "HostSession":
        """Open or reuse a named persistent shell session, as *as_user* when given.

        *terminal* false opens it without a terminal (ssh only; the caller
        has asked :meth:`no_terminal_limitation`). A session keeps the mode
        it was opened with: asking for a live name in the other mode is
        refused by name (``_refuse_other_mode``).

        Serialized via a per-name lock with a double-checked-locking
        pattern: concurrent callers requesting the same name resolve to a
        single underlying session rather than each creating their own and
        clobbering the dict.  Callers requesting *different* names take
        different locks and so connect concurrently.

        Eagerly runs ``_ensure_initialized`` inside the lock so the stored
        ``HostSession.alive`` is True on return — this prevents follow-on
        callers from observing a just-created (un-handshaken) session as
        dead and recreating it.

        A new session is opened in this order:

        1. its transport: *as_user*'s own ssh connection when they are not
           the login and have a direct credential on an ``ssh`` term
           (``_own_ssh_login``), else the login's;
        2. the marker handshake; when it fails, the transport is rebuilt
           and the handshake run once more (``_open_confirmed``), except on
           a line that serves one client, whose slot the default session
           may hold (:func:`~otto.host.telnet.retries_failed_open`);
        3. the login proxy's hops (``_apply_login_proxy``), for the
           login's session only;
        4. when *as_user* is not who the session now is, a switch to them
           (``switch_and_stamp``), kept for the session's life;
        5. the session-setup hook (``_apply_session_setup``), which so
           sees *as_user* as the session's user.

        A failure at any step closes the session and leaves no entry; a
        switch that cannot be made raises
        :class:`~otto.host.login_proxy.LoginProxyError` here. *as_user* is
        what the exec pool opens its sessions with; ``None`` is the login.

        An existing live session of *name* is returned as it is when
        *as_user* is ``None`` or the user it is now; one that is another user
        raises :class:`ValueError` naming the session, its user and
        *as_user*, before anything is typed.
        """
        self._checked_plain(terminal)
        existing = self._named_sessions.get(name)
        if existing and existing.alive:
            return self._refuse_other_mode(self._refuse_other_user(existing, as_user), terminal)

        # ``setdefault`` is atomic here — no ``await`` between lookup and use —
        # so concurrent callers for the same name share one lock instance.
        lock = self._named_session_locks.setdefault(name, asyncio.Lock())
        async with lock:
            existing = self._named_sessions.get(name)
            if existing and existing.alive:
                return self._refuse_other_mode(self._refuse_other_user(existing, as_user), terminal)

            # Close the underlying transport of the dead entry (if any) but
            # leave the dict slot intact — we'll overwrite it below.  Calling
            # existing.close() here would deregister via the close-callback
            # and remove the slot, opening a tiny window where _named_sessions
            # has no entry for this name.
            if existing is not None:
                await existing._session.close()  # noqa: SLF001 — intra-package access to HostSession._session to close dead transport

            own_login = self._own_ssh_login(as_user)
            shell_session = await self._open_confirmed(
                lambda: self._build_named_session(own_login=own_login, terminal=terminal),
                user=own_login,
                named=True,
            )

            # Replay proxy hops (if any) so this named session ends up as
            # login_target too — this is also the exec-pool path
            # (_acquire_exec_session -> open_session), so pooled sessions
            # get proxied for free. A failed hop is not retried: it tears the
            # session down and propagates, leaving no entry in _named_sessions.
            # Not on a session that authenticated as its own user: the hops
            # lead to the login, which that session is not.
            try:
                if own_login is None:
                    await self._apply_login_proxy(shell_session)
                if as_user is not None and as_user != shell_session.current_user:
                    await switch_and_stamp(
                        io=_SessionProxyIO(
                            shell_session,
                            self._log_command,
                            self._log_output,
                            fold_log=self._fold_log,
                        ),
                        creds=self._creds or [],
                        user=as_user,
                        password=None,
                        current_user=shell_session.current_user,
                        stamp=_stamp_of(shell_session),
                        host_id=self._host_id,
                        shell_prefix=shell_session._new_shell_prefix(),  # noqa: SLF001 — the manager's own session
                        term=getattr(self._connections, "term", None),
                    )
                    shell_session.note_reached_by_switch()
            except BaseException:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await shell_session.close()
                raise

            # Then the session-setup hook, on the same terms: it runs once per
            # OPEN, so the exec pool's sessions get it too (that is why the
            # pool is where a hooked host's exec runs).
            kind: SetupKind = "exec_pool" if name.startswith(_EXEC_POOL_PREFIX) else "named"
            try:
                await self._apply_session_setup(shell_session, kind)
                self._refuse_hook_switched_user(shell_session, name=name, as_user=as_user)
                if kind == "exec_pool" and shell_session.reached_by_switch:
                    await self._mark_identity(shell_session)
            except BaseException:
                with suppress(Exception):  # pragma: no cover - best-effort cleanup
                    await shell_session.close()
                raise

            host_session = HostSession(
                name=name,
                session=shell_session,
                log_command=self._log_command,
                log_output=self._log_output,
                fold_log=self._fold_log,
                deregister=self._forget_session,
                creds=self._creds,
                host_id=self._host_id,
                # getattr: a test double standing in for the connection
                # manager need not carry a term.
                term=getattr(self._connections, "term", None),
            )
            self._named_sessions[name] = host_session
            return host_session

    def _refuse_hook_switched_user(
        self, session: "ShellSession", *, name: str, as_user: str | None
    ) -> None:
        """Refuse a session a setup hook left as someone other than *as_user*.

        A session opened for a user (an exec-pool session, a named session
        opened ``as_user=``) IS that user for its whole life: the pool keeps
        it in that user's bucket. A hook that switched it permanently would
        have every command asked for *as_user* run as someone else, so the
        open is refused here, after the hook and before anything is typed.
        A session opened without a requested user is not checked.

        Raises:
            ~otto.host.errors.SessionSetupError: the hook left the session as
                another user.
        """
        if as_user is None or session.current_user == as_user or self._session_setup is None:
            return
        raise SessionSetupError(
            f"{self._host_id}: session setup {self._session_setup.name!r} left session "
            f"{name!r} as {session.current_user!r}, not {as_user!r}, the user it was opened "
            f"for; scope the switch to the hook (the handle's as_user) or ask for the user "
            f"at the call (exec(user=...))"
        )

    @staticmethod
    def _refuse_other_user(session: "HostSession", as_user: str | None) -> "HostSession":
        """Return *session*, or raise if *as_user* asks for a user it is not."""
        if as_user is not None and session.current_user != as_user:
            raise ValueError(
                f"session {session.name!r} is open as {session.current_user!r}, "
                f"not {as_user!r}: open it under another name to have it as {as_user!r}"
            )
        return session

    @staticmethod
    def _refuse_other_mode(session: "HostSession", terminal: bool | None) -> "HostSession":
        """Return *session*, or raise if *terminal* asks for the mode it was not opened with."""
        if terminal is not None and session.reads_a_terminal != terminal:
            had, asked = (
                ("with a terminal", "without one")
                if session.reads_a_terminal
                else ("without a terminal", "with one")
            )
            raise ValueError(
                f"session {session.name!r} is open {had}: open it under another name "
                f"to have it {asked}"
            )
        return session

    async def send(self, text: str | SecretStr, *, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the default session, creating it if needed."""
        await self._ensure_session()
        _log_sent(self._log_command, text, log)
        assert self._session is not None  # noqa: S101 — internal invariant: _ensure_session() always sets _session or raises
        await self._session.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> str:
        """Wait for *pattern* in the default session's output, creating the session if needed.

        *log* is the mode the read is logged under, already folded by the host,
        as :meth:`send`'s is: the default session is the host's, so its
        ``send`` and ``expect`` take the host's fold, never this manager's
        named-session fold.
        """
        await self._ensure_session()
        assert self._session is not None  # noqa: S101 — internal invariant: _ensure_session() always sets _session or raises
        result = await self._session.expect(pattern, timeout=timeout)
        _relay_expect_read(self._session, result, self._log_output, log)
        return result

    async def close_all(self) -> None:
        """Close the default session and all named sessions."""
        if self._session:
            await self._session.close()
            self._session = None
            self._baseline_user = None
        if self._plain_session:
            await self._plain_session.close()
            self._plain_session = None

        if self._named_sessions:
            await asyncio.gather(
                *(session.close() for session in self._named_sessions.values()),
                return_exceptions=True,
            )
            self._named_sessions.clear()
        # The pools' sessions are named sessions, closed just above.
        self._exec_pools.clear()

        # Drop the per-name locks too. close_all() is a teardown point with
        # no concurrent open_session() in flight, so clearing here is race-free
        # (unlike per-session reclamation) and caps lock-dict growth at the set
        # of names seen since the last close_all rather than process lifetime.
        self._named_session_locks.clear()
