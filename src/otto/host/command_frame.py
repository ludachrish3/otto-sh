"""
Pluggable shell command *framing* for :class:`~otto.host.session.ShellSession`.

otto drives a remote shell by wrapping each command in unique sentinels — a
BEGIN marker, the command itself, a way to recover the exit code, and an END
marker — then parsing the echoed byte stream back into ``(output, retcode)``.
*How* that wrapping and parsing is done is the shell's **dialect**, and it
differs per target:

- a POSIX **bash** shell bakes ``$?`` into the END marker;
- the **Zephyr** RTOS shell has no ``$?`` and no generic ``echo``, so it
  appends a stock ``retval`` command whose output carries the code on its own
  line, and parses output *positionally* (the shell prints its prompt after
  every executed line);
- a Zephyr **2.7** target has neither — ``retval`` only exists from 3.x — so a
  custom frame is needed (see ``todo/command_frame_protocol.md``).

This module makes the dialect a first-class, composable strategy: a
:class:`CommandFrame` is a small **stateless value object** that a session
*holds* rather than *is*. The per-session sentinels (unique per connection so
two sessions can't cross-talk) live on the session and are passed to the frame
as a :class:`SessionMarkers` value object — keeping the frame pure and
unit-testable without a live session, exactly like
:class:`~otto.host.embedded_filesystem.EmbeddedFileSystem`.

Built-in frames
---------------
- :class:`BashFrame` (``"bash"``) — POSIX bash; used by SSH/telnet/local unix
  sessions.
- :class:`ZephyrFrame` (``"zephyr"``) — the stock Zephyr ``fs``/``retval``
  shell (3.7 / 4.4 LTS).
- :class:`RawFrame` (``"raw"``) — a landing-only dialect with no handshake and
  no framing, for a console whose landing state answers no frame; used with a
  ``session_setup`` hook that manoeuvres to the real shell.

A project can register additional dialects via :func:`register_command_frame`
from a ``.otto`` init module — the same extension hook
:func:`otto.host.embedded_filesystem.register_filesystem` follows.
"""

import enum
import re
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from typing_extensions import final, override

from ..logger.mode import ExecLogContext
from ..registry import ClassEntry, Ref, Registry, registration_boundary, resolved
from .errors import RawLandingError
from .exit_status import subshell_status_of
from .limitation import Limitation
from .terminal_mapping import (
    BASELINE_VAR,
    CALIBRATION_COMMAND,
    ECHO_OFF_COMMAND,
    ECHO_RESET,
    ECHO_UNSET,
    ERREXIT_VAR,
    START_VAR,
    TABS_OFF_COMMAND,
    establish_exec_state,
    save_settings_command,
    saved_into,
)

__all__ = [
    "FRAME_CLASSES",
    "BashFrame",
    "CommandFrame",
    "ExecInput",
    "ExecRestore",
    "RawFrame",
    "SessionMarkers",
    "ZephyrFrame",
    "command_value",
    "register_command_frame",
]


def command_value(output: str, *, line_ending: str = "\n") -> str:
    r"""Return a command's ``value``: *output* with exactly one trailing *line_ending* removed.

    The one statement of the rule in :ref:`command-output-contract`. Nothing
    else is stripped or added: earlier newlines, trailing spaces and tabs and
    the program's own ``\r`` are all kept. Every route that builds a
    ``value`` from output it read calls this, so no two routes can disagree.

    >>> from otto.host.command_frame import command_value
    >>> command_value("a\n\n"), command_value("a \r\n"), command_value("abc")
    ('a\n', 'a \r', 'abc')
    >>> command_value("a\r\n", line_ending="\r\n")
    'a'
    """
    return output.removesuffix(line_ending)


def begin_line_ending(buffer: str, start: int) -> str:
    r"""Return the line ending the BEGIN marker's line ends in at *start* of *buffer*.

    ``"\r\n"`` when the terminal's CR is still in the folded buffer (a
    session that left an unknown mapping unfolded), else ``"\n"``. The same
    ending is the one cut from the end of the output, so a session that logs
    lines as they arrive decides from this whether a line's final ``\r`` may
    be the terminal's.

    >>> from otto.host.command_frame import begin_line_ending
    >>> begin_line_ending("B\r\nx", 1), begin_line_ending("B\nx", 1)
    ('\r\n', '\n')
    """
    return "\r\n" if buffer.startswith("\r\n", start) else "\n"


READY_SUFFIX = "READY__"
"""The fixed end of :attr:`SessionMarkers.ready`, where :func:`whole_line_echo` splits it.

It follows the head's ``_`` separator, so the head a probe line's echo word
carries ends in ``_`` (the cut rules of :func:`whole_line_echo`)."""

RECOVER_SUFFIX = "RECOVER__"
"""The fixed end of :attr:`SessionMarkers.recover`, where :func:`whole_line_echo` splits it.

It follows the head's ``_`` separator, as :data:`READY_SUFFIX` does."""

RESTORED_SUFFIX = "RESTORED__"
"""The fixed end of :attr:`SessionMarkers.restored`, where :func:`whole_line_echo` splits it.

It follows the head's ``_`` separator, as :data:`READY_SUFFIX` does."""

SESSION_SHELL = '"${SHELL:-/bin/sh}"'
"""The session's own shell program, as a word the session's shell expands.

What a console exec starts its clean login shell with, and what an exec
under sudo's ``shell`` form runs its command in: the login's shell, so the
command is written in the language of the session it was typed into."""

EXEC_CREDENTIAL_FD = 3
"""The file descriptor an isolated ``exec`` keeps the caller's stdin on, for a password.

The exec frame renders ``3<&0`` on its subshell only for a call that arms a
credential (:attr:`ExecInput.credential`), so an elevation whose command's
own stdin is at end of file can still read the password the caller's
terminal carries from this descriptor; the elevation closes it before the
command runs. Any other exec types no redirect, so its command never sees
the descriptor, and a shell that refuses to duplicate fd 0 (BusyBox 1.16.1's
ash) still runs it. The one definition of it."""


class ExecRestore(enum.Enum):
    """What an isolated ``exec`` puts the terminal back to when it ends, or is interrupted.

    ``BASELINE``: the record the session's handshake keeps in the shell (a
    pooled session's ``exec``). ``START``: the terminal as this ``exec``
    found it, snapshotted at its own start (a console's ``exec``, which
    shares the line ``run`` uses).
    """

    BASELINE = "baseline"
    START = "start"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecInput:
    """What one ``exec`` call asks of the frame that isolates it.

    Built once per call by :meth:`~otto.host.host.BaseHost.exec` and carried
    unchanged to the session; ``None`` where one is accepted means a ``run``
    (its frame, its status, no restore).
    """

    stdin_eof: bool
    """Whether the command's stdin is at end of file (``</dev/null``)."""

    restore: ExecRestore = ExecRestore.BASELINE
    """Which terminal record the frame restores, per :class:`ExecRestore`."""

    credential: bool = False
    """Whether otto answers a password on ``EXEC_CREDENTIAL_FD`` for this call.

    Decided where the elevation is applied (an elevation form that reads the
    descriptor sets it); only then does the frame open the descriptor."""

    identity: str | None = None
    """The identity mark the session's switched shell carries, or ``None``.

    Set by the exec pool for a session its open reached by a switch
    (``SessionManager.open_session``); the frame then renders the payload
    under its identity guard (:meth:`CommandFrame.identity_guard`). ``None``
    for every session that carries no mark."""

    disposable: bool = False
    """Whether the session is closed, not recovered, when otto gives up on this call.

    Set by the exec pool for every pooled ``exec``. A command otto gives up
    on there (a timeout, a cancel, a refused password) is stopped without
    the shell being restored, and the session is left dead for
    the pool to close (``ShellSession._stop_disposable``). ``False`` for an
    ``exec`` on a host's default session, which is recovered as a ``run``'s
    is."""

    exec_log: ExecLogContext | None = None
    """A delegating host's log context, or ``None`` for a call logged by the host it runs on.

    Set only by ``BaseHost._exec_delegated`` and read only through
    ``delegated_exec_log``: by every route's log resolver and by the host that
    logs the line it really runs at DEBUG."""

    on_typed: Callable[[], None] | None = None
    """Called when the call's framed command has been written to its session, or ``None``.

    Set only by ``BaseHost._exec_spawned``, which returns the call's task the
    first time it is called. It is called again when the command is written
    once more, on a fresh session, after the first session lost its user;
    never when the write fails or is cancelled."""


@final
@dataclass(frozen=True)
class SessionMarkers:
    """Per-session sentinel tokens a ``CommandFrame`` renders into commands and scans on parsing.

    Built once per session from the session id (see
    :meth:`for_session`), so two concurrent sessions never match each other's
    markers. A frame receives this and never generates markers itself, which
    keeps frames stateless.
    """

    begin: str
    """BEGIN sentinel — ``__OTTO_<id>_BEGIN__``."""

    end_prefix: str
    """END sentinel prefix — ``__OTTO_<id>_END__`` (bash appends ``<code>__``)."""

    ready: str
    """Readiness-probe token — ``__OTTO_<id>_READY__``."""

    recover: str
    """Post-timeout re-sync token — ``__OTTO_<id>_RECOVER__``."""

    saved: str
    """Tag the handshake prints the saved terminal settings behind — ``__OTTO_<id>_SAVED__``.

    See :func:`~otto.host.terminal_mapping.save_settings_command`."""

    restored: str
    """Post-recovery restore token — ``__OTTO_<id>_RESTORED__``.

    See :meth:`CommandFrame.restore_line`."""

    @classmethod
    def for_session(cls, *, session_id: str) -> "SessionMarkers":
        """Build the marker set for a session id.

        *session_id* must be shell word characters (``[0-9A-Za-z_]``): the
        markers are typed into shell lines unquoted (see
        :func:`whole_line_echo`), where any other character could expand,
        split or glob. Not validated here; every id otto mints is a
        truncated ``uuid4().hex``.
        """
        return cls(
            begin=f"__OTTO_{session_id}_BEGIN__",
            end_prefix=f"__OTTO_{session_id}_END__",
            ready=f"__OTTO_{session_id}_{READY_SUFFIX}",
            recover=f"__OTTO_{session_id}_{RECOVER_SUFFIX}",
            saved=f"__OTTO_{session_id}_SAVED__",
            restored=f"__OTTO_{session_id}_{RESTORED_SUFFIX}",
        )


LITERAL_NEXT = "\x16"
"""Ctrl-V: typed before a character, it makes a line editor or a terminal take that character
literally (readline's ``quoted-insert``, the line discipline's ``lnext``)."""


class CommandFrame(ABC):
    """A shell's command-framing dialect: wrap commands for execution and parse the echoed stream.

    Concrete frames are stateless value objects. The render half
    (:meth:`handshake` / :meth:`frame` / :meth:`recover`) and the parse half
    (:meth:`end_pattern` / :meth:`marks_begin` / :meth:`parse_output` /
    :meth:`extract_retcode`) live together because they co-vary through *where
    the retcode lives* — splitting them would let mismatched halves combine.
    """

    type_name: ClassVar[str]
    """Lab-data string for this dialect (e.g. ``'bash'``). Looked up against
    ``FRAME_CLASSES`` by the host factory; unique across frames."""

    single_client_console: ClassVar[bool] = False
    """Whether this dialect's console serves exactly ONE client at a time.

    Declared here because it changes what a failed readiness handshake MEANS
    (issue #260). On a multi-client shell the likely causes are a shell that
    never started or a login that never completed; on a single-client console
    the overwhelmingly likely cause is that the one slot is already held — a
    condition with a completely different remedy, and one otto's session error
    used to mis-report as "the device is unresponsive or login failed". Read by
    ``ShellSession._fail_init`` in :mod:`otto.host.session`.

    A project registering its own dialect for an exclusive console (a serial
    line, a single-session debug port) should set this True.

    Deliberately the same name as
    :attr:`otto.host.options.TelnetOptions.single_client_console`, which is the
    same real-world property declared for a different purpose one layer down:
    that one is per-CONNECTION and registers the transport so the embedded test
    teardown can force-release a half-held slot. This one is per-DIALECT and only
    shapes the diagnostic. They are not plumbed together because ``ShellSession``
    holds a frame and not the connection's options; if that ever changes, collapse
    them rather than letting the two drift."""

    measures_terminal_mapping: ClassVar[bool] = False
    """Whether :meth:`handshake` prints the terminal-mapping calibration line.

    True means the payload runs
    :data:`~otto.host.terminal_mapping.CALIBRATION_COMMAND` immediately before
    it prints READY on a line of its own, ending in a line ending. The session
    then waits for READY's line ending too, reads how the terminal mapped the
    calibration, and undoes exactly that from every command's output (see
    :mod:`otto.host.terminal_mapping`). False (the default) leaves the session
    ``unmeasured`` and the output as it arrived, so a dialect whose READY is
    not a line of its own (Zephyr's is an error message) is unaffected."""

    streams_output_live: ClassVar[bool] = False
    """Whether the session logs this dialect's output line by line as it arrives.

    Default False: the session buffers to the END sentinel and logs the lines
    of ``parse_output(buffer)`` once, so the log shows exactly the command's
    parsed output, with no shell prompts or retcode scaffolding. True: each
    line is logged as it completes, after the
    :attr:`~otto.host.command_frame.CommandFrame.live_skip_lines`, through
    :meth:`~otto.host.command_frame.CommandFrame.clean_output`, with the newest
    :attr:`~otto.host.command_frame.CommandFrame.live_hold_lines` held back. The
    lines logged must be the lines of what
    :meth:`~otto.host.command_frame.CommandFrame.parse_output` returns. Bash with
    echo off streams as its raw stream arrives; Zephyr streams with its tail
    held back."""

    live_hold_lines: ClassVar[int] = 0
    """How many of the newest complete lines a streaming dialect holds until END.

    The lines the shell sends after the command's output and before END (a
    prompt, an exit-code line) look like output until END arrives; holding
    this many keeps them out of the log. ``0`` (the default) holds none. A
    dialect that holds lines logs, at END, the lines of ``parse_output``'s
    value not yet logged, and drops what it held; at a timeout it logs the
    complete lines of the timed-out value not yet logged. Read by the
    session's log only, when
    :attr:`~otto.host.command_frame.CommandFrame.streams_output_live`: never
    by a parse, so it changes what is logged when, never a value."""

    @property
    def live_skip_lines(self) -> int:
        """How many shell lines follow the BEGIN line before a streaming dialect's output.

        ``0`` by default: the output starts on the line after BEGIN's. Read
        only when :attr:`~otto.host.command_frame.CommandFrame.streams_output_live`.
        """
        return 0

    def clean_output(self, text: str) -> str:
        """Return *text*, raw output with the terminal mapping undone, less what the shell adds.

        The one per-line cleaning of a streaming dialect: the session applies
        it to each line before logging it, so a dialect whose ``parse_output``
        removes bytes the shell adds to the output removes them through this
        too. The default removes nothing.
        """
        return text

    exact_output: ClassVar["Limitation | None"] = None
    """``None``: :meth:`parse_output` follows the exact-output rule -- a
    command's ``value`` is its output with exactly one trailing newline
    removed (:data:`otto.testing.EXACT_OUTPUT_CASES`). A
    :class:`~otto.host.limitation.Limitation`: this dialect's device cannot
    report the exact output, and the record says why and what it was
    measured on.

    A limitation is declared on the class body of the frame it applies to,
    never inherited: registration refuses a subclass that would silently
    carry its parent's. :func:`otto.testing.assert_command_frame_conforms`
    proves either answer against the table."""

    # --- render half: command -> bytes to write ---

    @abstractmethod
    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        """Readiness-probe payload, echoing :attr:`SessionMarkers.ready`.

        The READY marker must begin a line of the shell's OUTPUT in every echo
        state the dialect can be in when the handshake runs — the first open
        (echo on) and frame entry after a session-setup hook (echo off, prompt
        pending). The session matches it line-anchored so a login prompt that
        merely echoes the probe cannot pass. A dialect with no probe to render
        returns ``""`` instead — what :class:`RawFrame` does, and what
        ``ShellSession._handshake`` reads as "nothing to confirm".

        *prefix* is the statements that must share this first line (history
        suppression, from :meth:`CommandFrame.quiet_history`; ``""`` for a dialect with no
        history). The frame composes the whole line, so it places *prefix*
        itself: after anything that has to open the line (bash's anchor, see
        :func:`whole_line_echo`) and before the probe.
        """
        ...

    def typed(self, *, cmd: str) -> str:
        """Return *cmd* as it must be typed at a terminal for this dialect's shell to read it whole.

        A session on a terminal types the command into the shell's line
        reader, which may act on a character instead of taking it. The
        default changes nothing; a dialect whose reader would change the
        command overrides this. A session that writes to a pipe never calls
        it (:func:`otto.host.session.render_payload`).
        """
        return cmd

    @abstractmethod
    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
        """Full write payload that runs ``cmd`` bracketed by the sentinels."""
        ...

    def frame_exec(
        self,
        cmd: str,
        *,
        m: SessionMarkers,
        stdin_eof: bool,  # noqa: ARG002 — read only by a frame that isolates
        restore: ExecRestore,  # noqa: ARG002 — read only by a frame that isolates
        credential: bool = False,  # noqa: ARG002 — read only by a frame that isolates
    ) -> str:
        """Full write payload that runs ``cmd`` for an ``exec``, isolated from the session.

        The default is :meth:`frame`: a dialect that does not override this
        runs an ``exec`` exactly as a ``run``, sharing the session's state,
        and its status is :meth:`extract_exec_retcode`'s. A dialect that
        overrides it promises isolation: nothing the command changes (working
        directory, variables, options, an ``exit``) reaches the next command,
        its stdin is at end of file when *stdin_eof*, *restore* names the
        terminal record its end puts back, and *credential* (otto answers a
        password) keeps the caller's terminal on ``EXEC_CREDENTIAL_FD``,
        which is otherwise not opened. Such a dialect also provides the
        post-recovery restore (:meth:`restore_line`, :meth:`restored_pattern`),
        which registration refuses it without, and reports in its end record
        whether the terminal record was there (:meth:`exec_baseline_present`
        reads it). Overriding this only to
        delegate to :meth:`frame` claims an isolation the frame does not give:
        :attr:`~otto.host.command_frame.CommandFrame.isolates_exec` reads the
        override, not the payload.
        """
        return self.frame(cmd, m=m)

    @property
    def isolates_exec(self) -> bool:
        """Whether this dialect's ``exec`` runs isolated: it overrides :meth:`frame_exec`.

        Derived, never declared, so a subclass inherits its parent's answer
        with no edit of its own.
        """
        return type(self).frame_exec is not CommandFrame.frame_exec

    def identity_mark_statement(self, *, mark: str) -> str:
        """Return the statement that gives this dialect's shell the identity *mark*.

        Only a dialect that ``guards_identity`` has one; the default refuses.
        The statement is quote-free and rides the identity proof's own line.
        """
        raise NotImplementedError(f"{type(self).__name__} has no identity guard")

    def identity_guard(self, *, payload: str, mark: str, m: SessionMarkers) -> str:
        """Return the exec *payload* run only in the shell that carries the identity *mark*.

        Only a dialect that ``guards_identity`` builds one: the guard is
        this dialect's own syntax, and its "not run" answer is this dialect's
        own end record. The default refuses: an application dialect, a raw
        landing or an RTOS shell has no shell-local variable to test.
        """
        raise NotImplementedError(f"{type(self).__name__} has no identity guard")

    @property
    def guards_identity(self) -> bool:
        """Whether this dialect can guard a command by an identity mark.

        THE ONE DECISION of whether a session's frame can guard: the dialect
        overrides :meth:`identity_guard`. Derived, never declared, like :attr:`isolates_exec`.
        """
        return type(self).identity_guard is not CommandFrame.identity_guard

    @abstractmethod
    def recover(self, m: SessionMarkers) -> str:
        """Re-synchronization payload, echoing :attr:`SessionMarkers.recover`."""
        ...

    def restore_line(self, restore: ExecRestore, m: SessionMarkers) -> str | None:  # noqa: ARG002 — the default restores nothing
        """Return the line putting back what an interrupted isolated ``exec`` left changed.

        Written once by the session after it recovered from an interrupted
        ``exec`` (a timeout, a refused password), when the ``exec`` frame's own
        end never ran. It prints :attr:`SessionMarkers.restored`, which
        :meth:`restored_pattern` matches. ``None`` (the default) for a dialect
        whose ``exec`` is not isolated: there is nothing to restore, and the
        session that would need one marks itself dead rather than guess.
        """
        return None

    def restored_pattern(self, m: SessionMarkers) -> re.Pattern[str] | None:  # noqa: ARG002 — the default restores nothing
        """Pattern proving :meth:`restore_line` ran; ``None`` where there is no restore line."""
        return None

    def quiet_history(self) -> str:
        """Statement prefix stopping this dialect's shell recording commands.

        Empty when the dialect has no persistent history to suppress — which
        is how non-unix dialects are excluded from history suppression
        entirely, without any host-family or ``isinstance`` branching at the
        call sites. Zephyr keeps its history in a RAM ring buffer, so the
        inherited empty default is already correct for it.

        The result is placed after the line's anchor in the *first line
        written into a fresh shell* (the readiness handshake, and the resync
        probe after a login-proxy hop), so an override MUST emit no output of
        its own and MUST be idempotent — ``confirm_live`` resends those
        payloads on a loop until one lands.
        """
        return ""

    def restore_interactive(self) -> str | None:
        """Line that undoes what :meth:`handshake` did to the terminal, for a human.

        The interactive ``login`` bridge runs a session-setup hook over a
        framed session and then hands the pty to a person; whatever the
        handshake changed (bash turns echo off) has to be put back first.
        On a terminal that outlives the session, where the handshake saved
        the settings it found
        (:meth:`~otto.host.command_frame.CommandFrame.save_terminal`), the
        session sends
        :meth:`~otto.host.command_frame.CommandFrame.restore_terminal`
        instead; this line is the hand-back where nothing was saved, so
        bash's tab setting (``stty tab0``) stays for the rest of a login
        whose pty goes away with it
        (:mod:`otto.host.terminal_mapping`).
        ``None`` (the default) means the handshake changed nothing a human
        would notice. Dialect-owned for the same reason the handshake is.

        The returned string is a bare line with no terminator of its own —
        the caller writes it *through* the frame, whose own framing supplies
        the newline, never raw. There is no history suppression to undo: the
        ``login`` bridge never applies any, whatever the host's
        ``shell_history`` field says, so the human keeps their own recall.
        """
        return None

    def save_terminal(self, m: SessionMarkers) -> str:  # noqa: ARG002 — the default saves nothing
        """Statement prefix printing the settings behind ``m.saved``, for :meth:`restore_terminal`.

        Sent only where the terminal outlives the session
        (:attr:`~otto.host.session.ShellSession.terminal_outlives_session`),
        and only in the first handshake on it: the session places it first
        in the handshake's prefix, before anything the handshake changes, and
        keeps what it printed behind :attr:`SessionMarkers.saved`
        (:func:`~otto.host.terminal_mapping.saved_settings`), so no other
        text on the line can pass for the record.
        Empty (the default) for a dialect with no terminal settings to save,
        which is how Zephyr's frames are left untouched. An override keeps
        the rules of :meth:`quiet_history`'s payload (silent on failure, no
        output but the record) and :func:`whole_line_echo`'s cut rules.
        """
        return ""

    def restore_terminal(self, saved: str) -> str | None:  # noqa: ARG002 — the default saved nothing, so restores as restore_interactive does
        """Line that puts back the settings :meth:`save_terminal` printed as *saved*.

        The one restore step on a terminal that outlives the session, sent
        at the hand-back to an interactive user and before the session
        closes: it supersedes :meth:`restore_interactive` there, since the
        saved settings include what that line puts back. The default is
        :meth:`restore_interactive`, for a dialect that saves nothing. A bare
        line, written through the frame like :meth:`restore_interactive`'s.
        """
        return self.restore_interactive()

    # --- parse half: bytes read -> structured result ---

    @abstractmethod
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        """Regex marking the end of a command's output in the stream.

        The session compiles this once per session and uses it both to detect
        completion in the streaming read loop and to bound parsing.
        """
        ...

    @abstractmethod
    def marks_begin(self, data: str, *, m: SessionMarkers) -> bool:
        """Return True if ``data`` is the chunk carrying the BEGIN sentinel."""
        ...

    @abstractmethod
    def parse_output(self, buffer: str, *, cmd: str, m: SessionMarkers) -> str:
        """Extract the command's output from the accumulated ``buffer``."""
        ...

    @abstractmethod
    def extract_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        """Recover the command's exit code; ``-1`` when none can be read."""
        ...

    def extract_exec_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        """Recover an ``exec``'s exit code from what :meth:`frame_exec` printed; ``-1`` when none.

        The default is :meth:`extract_retcode`, matching the default
        :meth:`frame_exec`.
        """
        return self.extract_retcode(buffer, m=m)

    def exec_baseline_present(self, buffer: str, *, m: SessionMarkers) -> bool:  # noqa: ARG002 — the default keeps no record to lose
        """Whether *buffer*'s end record says the record an ``exec`` restores was there.

        ``True`` by default: a dialect whose ``exec`` restores nothing has no
        record to lose. A buffer with no end record reads ``True``: there is
        no evidence of a missing record.
        """
        return True

    def parse_partial_output(self, buffer: str, *, m: SessionMarkers) -> str:
        r"""Extract what an UNFINISHED command had written from *buffer*: its timed-out value.

        *buffer* is what the session read up to the deadline, with the
        terminal mapping undone as for :meth:`parse_output`; it holds no END
        marker and nothing the frame sends after the command (a prompt, an
        exit-code line). The default is the text after the last BEGIN marker
        and the one line ending BEGIN's own line contributes, through
        :func:`command_value` (one final line ending removed), and ``""``
        before BEGIN has arrived. A frame whose shell prints more lines
        between BEGIN and the command's output (a prompt, an exit-code report)
        overrides it, as :class:`ZephyrFrame` does.
        """
        begin = buffer.rfind(m.begin)
        if begin == -1:
            return ""
        start = begin + len(m.begin)
        line_ending = "\r\n" if buffer.startswith("\r\n", start) else "\n"
        if buffer.startswith(line_ending, start):
            start += len(line_ending)
        return command_value(buffer[start:], line_ending=line_ending)

    # --- liveness confirmation (concrete default; dialects may strengthen) ---

    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        r"""Pattern that PROVES the shell executed the recover probe.

        Default: the bare ``RECOVER`` token, matching the convention where
        ``recover`` echoes it back (Zephyr rejects it as an unknown command and
        its error handler prints it; a third-party frame inherits this). Bash
        strengthens it to the exit-code form an echo/REPL cannot fake — see
        :meth:`BashFrame.recover_pattern`.
        """
        return re.compile(re.escape(m.recover))


_PROBE_VAR = "__otto_probe"
"""The shell variable a probe line holds the end of its marker in (see :func:`whole_line_echo`)."""


def whole_line_echo(marker: str, suffix: str, statements: str = "", after: str = "") -> str:
    r"""Render a probe line that prints *marker* only when the shell ran it from its first byte.

    The line's first statement sets ``__otto_probe`` to *suffix*, the
    marker's fixed end (:data:`READY_SUFFIX` or :data:`RECOVER_SUFFIX`).
    *statements* follow it, and the closing echo prints the marker's head
    followed by the variable, then *after*; the line ends by unsetting the
    variable. A console server that delivers input in pieces lets login(1)
    or su flush a line's start, so the shell may run only a tail of it (#761):

    * the typed line never contains *marker*, so no echo of it and no error
      message quoting it can carry the marker;
    * only the statement at offset 0 assigns the variable, so every tail
      reaches the echo with it empty and prints the head alone;
    * the variable is never stale: every whole line unsets it, and it is
      never exported. Under ``set -u`` a tail may print an unset-variable
      error instead of the marker's head; that is harmless, because the
      typed line never carries the marker.

    **The cut rules** (#853). Every probe line keeps them, *statements*
    included, and the sites that build one link here. Under ``sh -c`` a
    broken tail is only a syntax error, but a login shell on a pty reads
    interactively, and there a tail can leave the shell waiting for more
    input: every whole line resent after it is swallowed, and the shell
    never answers. So no tail, cut at any byte, may:

    * **open a quote.** The line holds no quoted span, because a cut inside
      one turns its closing quote into an opening one (PS2).
    * **open a brace or another construct.** The echo word expands
      ``$__otto_probe``, not ``${__otto_probe-}``: a tail cut just after
      that ``$`` began with a ``{`` that zsh waited to see closed. A brace
      group, such as the history prefix's, is safe only because every tail
      that holds its ``{`` holds its ``}``.
    * **be a redirection-only statement that reads stdin.** zsh runs one as
      ``$NULLCMD`` (``cat``), which reads the terminal and swallows every
      resend. So a redirection comes before its command (``2>/dev/null
      stty -echo``), where a tail that keeps the redirection keeps the
      command too. One that must follow a brace group's ``}`` is paired with
      ``<&-``, so a leftover ``cat`` finds stdin closed and ends at once.
    * **start with a word that runs a program that reads the terminal.** A
      tail's first word is a suffix of some word of the line, and the shell
      runs it, so no word a tail can start inside may have a suffix naming
      such a program on ``PATH`` or as a builtin: it would swallow every
      resend. (Fragments that run and exit, such as ``tty`` or ``set``, are
      harmless; a user's own aliases are out of reach.) The
      calibration's last escape is octal (``\\012``), because a cut inside
      ``\\n`` left ``n`` (node's version manager on GitHub's runner). The
      marker's head ends in its ``_`` separator, here in the echo word and
      not in the variable's value, because the id is random hex and one
      ending in ``ed``, ``fc`` or ``b`` left that command (BusyBox's ``ed``
      read the resend); every fragment of the head ends in ``_``.

    The marker's id must be shell word characters (see
    :meth:`SessionMarkers.for_session`), so neither the assignment nor the
    echo word needs quoting against expansion or globbing. *after* is
    expanded unquoted as well: ``$?__$(id -un)__`` is safe for any user name
    made of word characters, dots and dashes (no blanks, no glob
    characters). It must not begin with a variable-name character, which
    would extend the unbraced variable's name, nor with ``[`` or ``:``, which
    zsh reads as a subscript or a modifier of it.

    Raises:
        ValueError: *marker* does not end in *suffix*, or the head before it
            is empty or does not end in ``_``; or *after* begins with a
            variable-name character, ``[`` or ``:``.
    """
    head = marker.removesuffix(suffix)
    if not marker.endswith(suffix) or not head.endswith("_"):
        raise ValueError(f"marker {marker!r} does not end in {suffix!r} after a head ending in _")
    if re.match(r"[A-Za-z0-9_\[:]", after):
        raise ValueError(f"after {after!r} would extend the variable name {_PROBE_VAR!r}")
    return (
        f"{_PROBE_VAR}={suffix}; {statements}echo {head}${_PROBE_VAR}{after}; unset {_PROBE_VAR}\n"
    )


_STATUS_VAR = "__otto_rc"
"""The unexported variable the exec frame holds its subshell's status in."""

_FLAG_VAR = "__otto_f"
"""The unexported variable the exec frame holds its end record's flag in: ``B`` or ``N``."""

IDENTITY_MARK_PREFIX = "__otto_as_"
"""The name prefix of the alias a switched shell carries its identity mark as.

An alias, not a variable or a function: no shell exports an alias, not even
under ``set -a`` (dash and BusyBox ash export every variable to a child then,
and bash's ``export -f`` hands a function on); the ``alias NAME`` query is a
builtin that looks nowhere else, so neither an executable on ``PATH`` nor a
function of the same name answers it; and the name holds the mark, so no
older definition can stand in for it. Defined in the switched shell only
(:meth:`BashFrame.identity_mark_statement`), so the shell underneath never
has it, and queried by :meth:`BashFrame.identity_guard`. A ``( ... )``
subshell sees it, and nothing a command does inside one removes it."""


def _validated_mark(mark: str) -> str:
    """Return *mark*, refused unless it is a plain word of letters and digits."""
    if not mark.isalnum():
        raise ValueError(f"identity mark {mark!r} must be letters and digits")
    return mark


def _restore_var(restore: "ExecRestore") -> str:
    """Return the shell variable holding the terminal record *restore* names."""
    return START_VAR if restore is ExecRestore.START else BASELINE_VAR


def _runs_something(cmd: str) -> bool:
    """Whether *cmd* has a line that is neither blank nor a ``#`` comment."""
    return any(line.strip() and not line.lstrip().startswith("#") for line in cmd.splitlines())


def _continues(cmd: str) -> bool:
    """Whether *cmd* ends in an unescaped backslash, which continues its last line."""
    trailing = len(cmd) - len(cmd.rstrip("\\"))
    return trailing % 2 == 1


_CLEAN_LOGIN_ENV = ["HOME", "TERM", "USER", "LOGNAME", "PATH"]
"""What a console exec's clean login shell keeps of the console shell's environment, as it is.

``SHELL`` is passed too, but resolved (:data:`SESSION_SHELL`), not as it is:
it names the program the clean login shell is, also where the console's
shell had it unset.
"""


class BashFrame(CommandFrame):
    """POSIX bash dialect: ``echo`` brackets and ``$?`` baked into the END marker.

    The default frame for SSH/telnet/local unix sessions.
    """

    type_name = "bash"
    streams_output_live = True  # echo off + single prompt: raw stream == clean output
    measures_terminal_mapping = True  # the handshake carries the calibration line

    @override
    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        # Silence echo so the probe command text doesn't come back, then print
        # the READY marker on a line of its own. The bare `echo` is
        # load-bearing: the session matches READY line-anchored (a failed
        # telnet login echoes the probe, and the anchor rejects that echo), and
        # once `stty -echo` is in effect an interactive shell prints PS1
        # immediately before the next command's output on the SAME line — so a
        # second handshake on this session (frame entry after a session-setup
        # hook) would print `user@host:~$ __OTTO_..._READY__` and never match.
        # With echo still on (the first handshake) the echoed line already ends
        # in a newline, so the extra blank line is harmless.
        #
        # `stty -echo` also sets how long a line otto may type: BusyBox ash's
        # line editor cuts a typed line at 1022 characters only while the
        # terminal echoes, so with echo off the kernel's 4095 is the bound
        # (otto.host.session.typed_line_ceiling, measured 2026-11-13). So the
        # statement's failure is observable: ECHO_RESET empties the echo
        # variable, ECHO_OFF_COMMAND's `||` branch sets it, the calibration
        # prints it before its X and ECHO_UNSET removes it, all on this line
        # (otto.host.terminal_mapping).
        #
        # TABS_OFF_COMMAND turns the terminal's tab expansion off, which has no
        # exact inverse (BusyBox telnetd's pty has it on), in a statement of
        # its own so a `stty` that refuses `tab0` cannot take `-echo` with it.
        # On a terminal that outlives the session the session's *prefix*
        # starts with save_terminal's `stty -g`, so the settings are read
        # before either changes them (see otto.host.terminal_mapping).
        # The calibration line after it shows the session how this terminal
        # maps line endings and whether tabs now survive (see
        # otto.host.terminal_mapping). It sits
        # immediately before READY, so the session reads it from the output
        # copy and never from an echo of the typed line.
        #
        # whole_line_echo holds READY's end in a variable that only the line's
        # first statement sets (#761). login(1) can flush the start of this
        # line on a console that delivers input in pieces, and the shell then
        # runs a tail: one that skipped `stty -echo` or the calibration (or
        # half the history prefix, which rides after the anchor) prints only
        # READY's head, so confirm_live sends the line again whole. The typed
        # line never carries READY, so neither does its echo, even where a
        # wrap puts a carriage return right before the marker. Every statement
        # keeps whole_line_echo's cut rules (#853): each redirection comes
        # before its command, and stdin stays open because `stty` acts on it.
        # `|| :` keeps a target without `stty` or `printf` on the line under
        # `set -e`, which would otherwise exit before READY (#953).
        #
        # establish_exec_state sets the variables frame_exec's tail reads
        # (otto.host.terminal_mapping), on every handshake of the session: the
        # terminal record is a snapshot taken here, after otto's own `stty`
        # statements, so a session-setup hook's changes (the re-handshake
        # after it) become the record an exec restores.
        return whole_line_echo(
            m.ready,
            READY_SUFFIX,
            f"{prefix}{ECHO_RESET}; {ECHO_OFF_COMMAND}; {TABS_OFF_COMMAND}; echo; "
            f"{CALIBRATION_COMMAND} || :; {ECHO_UNSET}; "
            f"{establish_exec_state(replace=True)}; ",
        )

    @staticmethod
    def _frame_body(cmd: str) -> str:
        """Return the command as both frames type it: ``:`` for nothing, a continuation ended.

        An empty group or subshell is a syntax error, so a command with
        nothing to run (blank, or only comments) becomes ``:``. A trailing
        backslash would join the closing line onto the command's last line,
        so a blank line ends the continuation first.
        """
        body = cmd if _runs_something(cmd) else ":"
        if _continues(body):
            body += "\n"
        return body

    @classmethod
    def _clean_login_body(cls, cmd: str, *, m: SessionMarkers) -> str:
        """Return the subshell body that runs *cmd* in a clean login shell: a console's exec.

        ``exec env -i`` replaces the subshell with the console's login shell
        (``${SHELL:-/bin/sh} -l``), its environment emptied but for
        :data:`_CLEAN_LOGIN_ENV`, read from the console's shell as it is, and
        ``SHELL`` set to the program it starts (:data:`SESSION_SHELL`), so the
        sudo ``shell`` form and its probe inside it name that same shell. The
        login shell reads its profile, changes to ``$HOME`` (``/`` when it is
        unset or empty; a shell started
        with ``-l`` keeps the directory it inherits; login(1) is what changes
        it), THEN prints BEGIN and runs the command, so nothing a profile prints
        reaches the value. *cmd* arrives as one quoted word, byte-exact, on its
        own line: the newline before the closing quote ends a here-doc, a
        comment or a continuation however *cmd* ends. File descriptors survive
        ``exec`` and ``env -i``, so the credential descriptor and the caller's
        terminal reach the command. A child that never reaches the command (no
        ``env``, no such shell, a profile that exits) prints no BEGIN, and the
        session reads that as a command that never ran
        (:data:`otto.host.session.NEVER_RAN`).
        """
        # `${V-}`: a console shell under `set -u` may lack USER or LOGNAME.
        keep = " ".join(f'{name}="${{{name}-}}"' for name in _CLEAN_LOGIN_ENV)
        keep += f" SHELL={SESSION_SHELL}"
        # `${HOME:-/}`: a login with no home starts in `/`, as login(1) does.
        word = shlex.quote(f'cd "${{HOME:-/}}" || exit; echo "{m.begin}"; {cls._frame_body(cmd)}\n')
        return f"exec env -i {keep} {SESSION_SHELL} -l -c {word}\n"

    @override
    def typed(self, *, cmd: str) -> str:
        # A line editor (bash, zsh, mksh) takes TAB as its completion key: it
        # dropped a TAB between two words and replaced `/us<TAB>` with `/usr/`
        # (#1224). LITERAL_NEXT (Ctrl-V) before it makes the editor, or the
        # terminal's own line discipline where the shell has no editor, take
        # the TAB as a character. Measured on every shell of
        # tests/_fixtures/interactive_shell.py, command line and here-document,
        # after the handshake turned echo off. Not covered: BusyBox ash's own
        # line editor, which runs only while the terminal echoes (a host with
        # no `stty`, #953) and completes on TAB whatever precedes it.
        return cmd.replace("\t", LITERAL_NEXT + "\t")

    @override
    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
        # Bracket the command with echoes; the END sentinel embeds ``$?`` so the
        # exit code travels back inside the marker. The command sits in a
        # brace group closed on the NEXT line: a trailing `&` or `# comment`
        # used to splice into `&;` (a syntax error that rejected the whole
        # line) or comment out the END echo, so no marker came back and the
        # call waited out its timeout (#678). The newline ends the command
        # however it ends. The shell reads the whole group before running
        # BEGIN, so a continuation prompt prints BEFORE the BEGIN marker,
        # never inside the output. Not `eval '<cmd>'`: quoting makes the typed
        # length depend on the command's quotes, and a line budget sized by a
        # fixed framing overhead would then overrun. The body is _frame_body's,
        # the one rule frame_exec applies too.
        return f'echo "{m.begin}"; {{ {self._frame_body(cmd)}\n}}; echo "{m.end_prefix}$?__"\n'

    @override
    def frame_exec(
        self,
        cmd: str,
        *,
        m: SessionMarkers,
        stdin_eof: bool,
        restore: ExecRestore,
        credential: bool = False,
    ) -> str:
        # Two physical lines. The command runs in a subshell, so nothing it
        # changes (cd, export, set, exit) reaches the session's shell. The
        # parent's errexit is saved in __otto_e and turned off, so a failing
        # subshell cannot end the session's shell; the subshell turns it back
        # on, so an inherited `set -e` still stops the command at its first
        # failure. Only when otto answers a password (*credential*) does
        # `3<&0` keep the caller's stdin on EXEC_CREDENTIAL_FD before
        # *stdin_eof* points stdin at end of file: no other command sees the
        # descriptor, and BusyBox 1.16.1's ash, which refuses to duplicate
        # fd 0, runs every exec that arms none. The tail then
        # restores the terminal and the parent's errexit (terminal_restore,
        # errexit_restore), and the end record carries a flag: `B` when the
        # restore record was there, `N` when it was lost (end_pattern).
        #
        # A console's exec (*restore* START, the one predicate that names a
        # console's default session) does not run the command in the
        # subshell itself: the subshell becomes a clean login shell
        # (_clean_login_body), which prints BEGIN once its profile has run.
        console = restore is ExecRestore.START
        start = f"{saved_into(START_VAR)}; " if console else ""
        begin = "" if console else f'echo "{m.begin}"; '
        body = (
            self._clean_login_body(cmd, m=m)
            if console
            else f"{self.errexit_restore()}; {self._frame_body(cmd)}\n"
        )
        stdin = " </dev/null" if stdin_eof else ""
        keep = f" {EXEC_CREDENTIAL_FD}<&0" if credential else ""
        var = _restore_var(restore)
        return (
            f"{start}{begin}"
            f"case $- in *e*) {ERREXIT_VAR}=e;; *) {ERREXIT_VAR}=;; esac; set +e; "
            f"( {body}"
            f"){keep}{stdin}; {_STATUS_VAR}=$?; "
            f"{_FLAG_VAR}=N; case ${{{var}+x}} in x) {_FLAG_VAR}=B; "
            f"{self.terminal_restore(restore)};; esac; {self.errexit_restore()}; "
            f'echo "{m.end_prefix}${{{_FLAG_VAR}}}${{{_STATUS_VAR}}}__"\n'
        )

    @override
    def identity_mark_statement(self, *, mark: str) -> str:
        # Quote-free, for the identity proof's line.
        return f"alias {IDENTITY_MARK_PREFIX}{_validated_mark(mark)}=:"

    @override
    def identity_guard(self, *, payload: str, mark: str, m: SessionMarkers) -> str:
        # The guard's test heads its own physical line and the payload follows
        # it unchanged; the shell reads the whole `if` before it runs any of
        # it. `alias NAME` is a builtin query of the shell's own aliases: no
        # fork, no PATH lookup. In the marked shell the payload runs. In any
        # other shell nothing runs and the end record comes back with no BEGIN
        # before it, which the session reads as a command that never ran. The
        # record's digits come from a variable, so an echoed copy of the typed
        # text never holds a complete record.
        name = IDENTITY_MARK_PREFIX + _validated_mark(mark)
        return (
            f"if alias {name} >/dev/null 2>&1; then\n{payload}"
            f'else {_STATUS_VAR}=0; echo "{m.end_prefix}${{{_STATUS_VAR}}}__"; fi\n'
        )

    def terminal_restore(self, restore: ExecRestore) -> str:
        """Return the statement restoring the terminal record *restore* names, when non-empty.

        Every read of the record is ``${V-}``, so the statement never aborts
        under ``set -u``; it is quote-free, because it also rides
        :meth:`restore_line`, whose cut rules forbid a quoted span. An empty
        record restores nothing.

        A console's start snapshot (:attr:`ExecRestore.START`) belongs to one
        exec, so the statement then unsets it: a later restore whose own
        frame never took a snapshot (a zero budget, a frame cut before its
        start) finds none and restores nothing. The session's baseline stays.
        """
        var = _restore_var(restore)
        put_back = f"case ${{{var}-}} in ?*) {self.restore_terminal(f'${{{var}-}}')};; esac"
        return f"{put_back}; unset {var}" if restore is ExecRestore.START else put_back

    def errexit_restore(self) -> str:
        """Return the statement turning ``set -e`` back on when the exec frame saved it on.

        A ``case``, so no proper tail of it sets ``-e``. The saved value
        belongs to one exec, so the statement then unsets it: a later restore
        whose own frame never saved one (a zero budget, a frame cut before
        its save) finds none and leaves ``set -e`` as the shell has it.
        """
        return f"case ${{{ERREXIT_VAR}-}} in e) set -e;; esac; unset {ERREXIT_VAR}"

    @override
    def restore_line(self, restore: ExecRestore, m: SessionMarkers) -> str | None:
        # Both halves on an anchored probe line (whole_line_echo's cut rules):
        # a tail of it never prints the RESTORED marker whole, and a record
        # that was lost restores nothing and still answers.
        return whole_line_echo(
            m.restored,
            RESTORED_SUFFIX,
            statements=f"{self.terminal_restore(restore)}; {self.errexit_restore()}; ",
        )

    @override
    def restored_pattern(self, m: SessionMarkers) -> re.Pattern[str] | None:
        return re.compile(re.escape(m.restored))

    @override
    def recover(self, m: SessionMarkers) -> str:
        # Echo-proof liveness probe on a marker distinct from the normal
        # end-of-command sentinel: expand $? after the RECOVER marker (not
        # end_prefix), so a real shell emits `..._RECOVER__<digits>__` while an
        # echo/REPL can only reproduce the literal `$?`. The digits are the
        # status of the line's own anchor assignment, not the hung command's:
        # they prove the shell executed the line, and nothing reads their
        # value (ShellSession._confirm_recovered keeps only what came before
        # the marker). Using RECOVER rather than end_prefix keeps
        # recover_pattern distinct from end_pattern, so a dying command's own
        # compound-line tail echo (`{end_prefix}<code>__`, appended by
        # `frame`) never matches this probe's reply — no collision, nothing
        # to drain.
        #
        # whole_line_echo's cut rules hold here as in the handshake (#853):
        # the interrupt can leave a console that delivers input in pieces,
        # and a tail of a quoted echo left every shell at PS2, swallowing
        # each resend until recovery gave the session up.
        return whole_line_echo(m.recover, RECOVER_SUFFIX, after="$?__")

    @override
    def quiet_history(self) -> str:
        # Neutralize HISTFILE by ASSIGNMENT, never `unset HISTFILE`: ksh falls
        # back to ~/.sh_history when it is unset, so unsetting is the
        # portable-looking choice that silently fails on the oldest targets.
        # /dev/null works on bash, busybox ash, zsh and ksh alike.
        #
        # Emphatically NOT HISTSIZE=0 — that is destructive. bash writes its
        # (now empty) history list OVER $HISTFILE at exit, deleting the user's
        # real history.
        #
        # This payload is written into whatever shell the host's passwd entry
        # names, configured however that host's admin left it. BOTH statements
        # can fail outright rather than merely be unsupported, and POSIX has
        # two SEPARATE rules that turn such a failure into a dead session.
        # Every guard below exists for one of them, and each is pinned
        # per-shell by tests/unit/host/test_history_suppression_portability.py.
        #
        # Rule 1 — an error in a SPECIAL BUILTIN (`set` and `export` both are)
        #   aborts a non-interactive shell entirely. dash exits on the spot,
        #   and `|| :` does NOT save it: dash leaves before any status is
        #   tested. The `command` prefix strips special-builtin status, making
        #   the error ordinary and survivable. The builtin still takes effect
        #   where supported — bash reports `history off` after it.
        #
        # Rule 2 — a failed VARIABLE ASSIGNMENT aborts the rest of the
        #   compound line. That is why HISTFILE is set with
        #   `command export HISTFILE=…` and not a bare `HISTFILE=…`: on a
        #   shell where HISTFILE is readonly, or under `bash --restricted`
        #   (which forbids setting HISTFILE by name), a bare assignment
        #   strands the `echo <READY>` that follows on this same line. The
        #   handshake would then never complete and otto would report "shell
        #   never became ready ... (e.g. bad credentials)" — i.e. a working
        #   host goes UNREACHABLE, blaming the wrong thing.
        #
        # `2>/dev/null` — stdout and stderr are one merged stream on a PTY, so
        #   a complaint would be parsed as command output and corrupt the
        #   READY handshake. (rbash refuses the redirection itself and emits
        #   noise anyway; harmless, as the read-until-pattern discards
        #   anything preceding the marker.)
        #
        # `|| :` — pins $? to 0. This payload also rides after the anchor in
        #   the resync probe (`…RECOVER__$?__$(id -un)__`, see
        #   login_proxy._identity_probe), and without it dash/busybox report
        #   RECOVER__2__/__1__ — harmless to the liveness regex, which only
        #   needs digits, but it reads as a failure in a debug log.
        #
        # zsh needs its own clause. zsh's `command` is a *precommand modifier*
        # that restricts lookup to EXTERNAL commands (the well-known
        # "`command cd` doesn't work in zsh"), and there is no external
        # `export`, so both statements above are silent no-ops there — safe,
        # but the feature would simply not work on a zsh login shell. A bare
        # `export HISTFILE=…` is NOT the answer: zsh aborts the line on a
        # readonly failure exactly like the shells above, so that would
        # reintroduce the strand. `eval` is the construct that is both
        # effective and survivable on zsh (its failure is contained and
        # testable). It is NOT usable as the general mechanism — dash strands
        # on `eval` even in a clean environment, and busybox strands under
        # readonly — hence a zsh-scoped clause rather than a rewrite. Its
        # argument is unquoted, since no word in it needs quoting, and its
        # redirection comes first (whole_line_echo's cut rules, #853).
        #
        # `case` (not `[ ]`/`test`) keeps this a pure shell construct with no
        # external dependency, and `${ZSH_VERSION:-}` is `set -u` safe. It is
        # inert everywhere else: the pattern simply never matches.
        #
        # Net effect on a shell that refuses everything: a few suppressed
        # errors, history not suppressed, session fully functional.
        # The `{ …; } 2>/dev/null` brace group rather than a plain trailing
        # redirect: ksh reports "HISTFILE: is read only" from its assignment
        # processing OUTSIDE the simple command's own redirection, so
        # `command export … 2>/dev/null` still leaks that line onto the merged
        # PTY stream. Redirecting the group captures it. (Not fatal there —
        # ksh carries on — but it is exactly the kind of stray output the
        # READY parse must never see.) A group's redirection has to follow
        # its `}`, so a tail can be that redirection alone; the `<&-` beside
        # it closes stdin for the `cat` zsh would run there
        # (whole_line_echo's cut rules, #853). Nothing in a group reads stdin.
        return (
            "{ command export HISTFILE=/dev/null; } 2>/dev/null <&- || :; "
            "{ command set +o history; } 2>/dev/null <&- || :; "
            "case ${ZSH_VERSION:-} in ?*) "
            "2>/dev/null eval export HISTFILE=/dev/null || :;; esac; "
        )

    @override
    def restore_interactive(self) -> str | None:
        # Mirrors the handshake's `2>/dev/null stty -echo` (ECHO_OFF_COMMAND); silent on a
        # pty-less channel, where there is nothing to restore either, and
        # harmless under `set -e` on a target without `stty` (#953). The
        # handshake's `stty tab0` is NOT undone here: this is the hand-back
        # on a pty that goes away with the login. A terminal that outlives
        # the session gets restore_terminal instead (otto.host.terminal_mapping).
        return "2>/dev/null stty echo || :"

    @override
    def save_terminal(self, m: SessionMarkers) -> str:
        return f"{save_settings_command(m.saved)}; "

    @override
    def restore_terminal(self, saved: str) -> str | None:
        # *saved* is a `stty -g` record (hex and colons only, checked by
        # saved_settings), so it is typed unquoted. It carries echo and tab
        # expansion as the handshake found them: one step puts both back.
        return f"2>/dev/null stty {saved} || :"

    @override
    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # `__OTTO_<id>_RECOVER__<code>__` — digits prove real execution; the
        # RECOVER marker keeps this disjoint from end_pattern.
        return re.compile(re.escape(m.recover) + r"(\d+)__")

    END_FLAG_GROUP: ClassVar[int] = 1
    """The group of :meth:`end_pattern` holding the record's flag (``B``, ``N`` or empty)."""

    END_CODE_GROUP: ClassVar[int] = 2
    """The group of :meth:`end_pattern` holding the status digits."""

    @override
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # ``__OTTO_<id>_END__<flag><code>__``: a ``run`` prints no flag, an
        # ``exec`` ``B`` (its restore record was there) or ``N`` (lost); the
        # digits carry the exit code. One pattern for both, so the read loop
        # ends on either. The flag comes before the digits, so no split of
        # the record ends a read early. The groups are positional, never
        # named: the session's combined read pattern sets a caller's expect
        # patterns beside this one, and a named group could collide with one.
        return re.compile(re.escape(m.end_prefix) + r"([BN]?)(\d+)__")

    @override
    def marks_begin(self, data: str, *, m: SessionMarkers) -> bool:
        # bash emits the marker on a line of its own, so equality (or suffix,
        # for a marker printed after same-line text) is the right test — and it
        # avoids a false hit on an echoed wrapped command.
        stripped = data.rstrip("\r\n")
        return stripped == m.begin or stripped.endswith(m.begin)

    @override
    def parse_output(self, buffer: str, *, cmd: str, m: SessionMarkers) -> str:
        # The command's output, exactly: *buffer* already has the terminal's
        # own line-ending translation undone by the session (the onlcr fold),
        # so every byte left between the markers is the program's.
        #
        # Find the LAST BEGIN marker — if the shell echoes the wrapped command,
        # the marker appears twice (echoed command text + actual output);
        # rfind skips the echoed copy.
        begin_idx = buffer.rfind(m.begin)
        # BEGIN's `echo` contributes exactly one line ending; a blank line
        # after it is the program's. It is CRLF only when the session left an
        # unknown mapping unfolded, and then the output's last line ending is
        # CRLF too, so the same ending is the one removed at the end.
        line_ending = "\n"
        if begin_idx != -1:
            start = begin_idx + len(m.begin)
            line_ending = begin_line_ending(buffer, start)
            if buffer.startswith(line_ending, start):
                start += len(line_ending)
        else:
            start = 0
        end_match = self.end_pattern(m).search(buffer, start)
        end = end_match.start() if end_match else len(buffer)
        output = buffer[start:end]
        # END's `echo` starts where the output stops and contributes no line
        # ending, so the output's own last line ending is the one to remove.
        return command_value(output, line_ending=line_ending)

    @override
    def extract_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        match = self.end_pattern(m).search(buffer)
        if match is None:
            return -1
        return int(match.group(self.END_CODE_GROUP))

    @override
    def extract_exec_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        # The subshell reports a status above 255 only where ksh93 reports a
        # signal death as 256+N; subshell_status_of gives it the shell's 128+N.
        reported = self.extract_retcode(buffer, m=m)
        return reported if reported < 0 else subshell_status_of(reported)

    @override
    def exec_baseline_present(self, buffer: str, *, m: SessionMarkers) -> bool:
        match = self.end_pattern(m).search(buffer)
        return match is None or match.group(self.END_FLAG_GROUP) != "N"


class AshFrame(BashFrame):
    """BusyBox ash: bash's marker scheme, unchanged, under a name of its own.

    The override set is EMPTY, and that is a measured result, not an
    omission. All four payloads `BashFrame` renders — `handshake`, `frame`,
    `recover`, `quiet_history` — run under real BusyBox ash across the
    version matrix (1.16.1, 1.21.1, 1.28.1, 1.31.0, 1.35.0), and every one
    satisfies the contract those payloads make: markers in order around a
    command's own output with no sentinel bleed, `$?` baked correctly into
    both the END marker and the RECOVER marker, the handshake reaching
    READY, and the shell still alive with `$HISTFILE` set to `/dev/null`
    after history suppression. The measurements live in
    `tests/integration/busybox_bed/test_session_frame.py`, which drives one
    telnet session per version into a booted guest. The `stty`-ABSENT arm of
    the handshake is the one thing that cannot be measured there — removing
    an applet would mutate a shared live guest — so it is injected into real
    shells at unit level instead, in
    `tests/unit/host/test_history_suppression_portability.py`.

    No bash arm runs on those guests — it is a single-shell contract check
    against real ash, not a differential — so the separate claim that ash's
    payloads match bash's byte for byte rests on a different, unit-level
    test that compares `AshFrame`'s emitted strings directly against
    `BashFrame`'s: `test_ash_inherits_bashs_marker_scheme_rather_than_restating_it`
    in `tests/unit/host/test_command_frame.py`.

    One real divergence turned up: ash's `set` has no `+o history` option at
    all and rejects it outright (`illegal option +o history`) on every row.
    That needs no override — `quiet_history()`'s `command` prefix and `|| :`
    guards exist precisely so an unsupported builtin option is survivable,
    and the shell was measured alive and still readable afterwards. "No
    override needed because the rejection is tolerated by design" is the
    honest claim; it is not the same as "nothing differs".

    What the measurement does NOT cover: a human-interactive session. The
    guests answer over a real pty (telnet login), which is what the earlier
    chroot-and-pipe measurement could not offer, but otto still drives them
    programmatically — so a real terminal's history file actually being
    written stays unmeasured and may still diverge.

    The class exists anyway, for two reasons that outlive the empty body. It
    gives lab data a truthful name — a host declaring `ash` is documented as
    a BusyBox shell rather than mislabelled `bash` in every diagnostic — and
    it gives the first genuine ash-only divergence (interactive or
    otherwise) a single obvious home, instead of a conditional inside the
    bash frame.

    Cheap to keep, and the alternative is worse. Registering ``BashFrame``
    itself under ``ash`` is refused on both paths:
    `register_command_frame("ash", BashFrame)` and the raw
    `FRAME_CLASSES.register("ash", ClassEntry(BashFrame), overwrite=True)`
    run the same check and raise (measured: `ValueError:
    register_command_frame: type_name 'ash' doesn't match BashFrame.type_name
    = 'bash'`). What no check can see is a refactor that folds this class
    back into bash, because ash's rendered strings ARE bash's by design: a
    payload comparison passes either way. What catches that is an assertion
    of TYPE IDENTITY through the registry — `isinstance(x, AshFrame)`,
    `type(x) is AshFrame`, or a caller one level up that builds a host and
    inspects what class its `command_frame` landed as (as
    `tests/unit/host/test_os_profile.py`'s
    `TestBusyBoxProfile::test_a_busybox_host_builds_from_a_minimal_lab_entry`
    does). That is a claim about the *kind* of assertion, not a count of
    them: a test that only compares rendered strings never joins that list.
    """

    type_name = "ash"


# Terminal control sequences (colored prompt, cursor moves). An application
# shell's parser strips them; a Zephyr frame keeps every one inside a
# command's output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

ZEPHYR_LEVEL_SEQUENCES = ["\x1b[1;31m", "\x1b[1;33m", "\x1b[m"]
r"""The colouring the Zephyr shell's message-level API adds, measured on 2.7, 3.7 and 4.4.

``shell_error`` opens its line with ``\x1b[1;31m`` and ``shell_warn`` with
``\x1b[1;33m``; each closes with ``\x1b[m`` after the line's ``\r\n``, every
call wrapped on its own, so they fall anywhere in a command's output
(``tests/unit/host/shell_transcripts/README.md``, "Message-level colouring").
"""

_SHELL_LEVEL_RE = re.compile("|".join(re.escape(seq) for seq in ZEPHYR_LEVEL_SEQUENCES))


def strip_zephyr_levels(output: str) -> str:
    r"""Remove the Zephyr shell's message-level colouring from *output*, wherever it falls.

    Exactly the :data:`ZEPHYR_LEVEL_SEQUENCES` are removed; every
    other byte stays, the ``help`` formatter's cursor moves (``\x1b[17C``)
    included. A command that itself writes one of those sequences loses it:
    the bytes cannot tell it from the shell's.
    """
    return _SHELL_LEVEL_RE.sub("", output)


_ZEPHYR_TAIL_PIECES = 3
"""The pieces :meth:`ZephyrFrame.parse_output` cuts before END: ``retval``'s line,
the prompt that runs END, and END's own piece."""


class ZephyrFrame(CommandFrame):
    r"""Stock Zephyr RTOS shell dialect (3.7 / 4.4 LTS).

    The Zephyr shell is not bash: no command substitution, no ``$?``, no
    generic ``echo``. otto frames each command as four CR-separated lines::

        __OTTO_<id>_BEGIN__   rejected -> shell emits `<token>: command not found`
        <the real command>    real output appears here
        retval                stock Zephyr builtin: prints <cmd>'s exit code
        __OTTO_<id>_END__     rejected -> shell emits `<token>: command not found`

    The four-line order is load-bearing: ``retval`` must run immediately after
    ``<cmd>`` (any command, even an unknown one, overwrites the shell's stored
    return value). otto never depends on the prompt text and never modifies the
    firmware — ``retval`` is a default-on builtin and the markers are just
    rejected input.

    Parsing is **positional**. The shell prints its prompt after every executed
    line and does not echo input, so between the BEGIN error line and
    ``retval``'s integer line the slice is exactly
    ``[prompt, <output...>, prompt]`` — dropping the bracketing prompt lines
    yields the output without hard-coding the prompt text. The value is the
    command's exact output (:func:`command_value`): only what the shell adds
    is removed — the prompts, the ``retval`` line, the marker lines, the
    colouring of every error and warning line (``strip_zephyr_levels``)
    and the ``\r`` of each ``\r\n``.
    """

    type_name = "zephyr"

    streams_output_live = True

    live_hold_lines: ClassVar[int] = _ZEPHYR_TAIL_PIECES
    """The shell's tail, which is the prompt after the output, ``retval``'s integer
    line and the prompt that runs END: as many lines as :meth:`parse_output`
    cuts, so the line the output ends on stays held when the prompt arrives
    glued to it. Set from the parse's own count, which never reads it: a
    subclass that holds more or fewer changes its log, never its value."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a subclass that streams with a hold below the tail its parse cuts.

        Such a frame would log its prompt and ``retval`` lines as output. A
        subclass with its own ``parse_output`` finds its own tail and is not
        checked here.
        """
        super().__init_subclass__(**kwargs)
        if (
            cls.parse_output is ZephyrFrame.parse_output
            and cls.streams_output_live
            and cls.live_hold_lines < _ZEPHYR_TAIL_PIECES
        ):
            raise TypeError(
                f"{cls.__qualname__}: streams_output_live is True but live_hold_lines is "
                f"{cls.live_hold_lines}, below the {_ZEPHYR_TAIL_PIECES} lines its "
                f"parse_output cuts, so the shell's prompt and retval lines would be "
                f"logged as output: hold at least {_ZEPHYR_TAIL_PIECES}, or set "
                f"streams_output_live = False to log the output once, at END"
            )

    single_client_console = True
    """Zephyr's ``shell_telnet`` backend accepts exactly one client per device
    and the console has no login at all — see ``tests/conformance/
    _console_safety.py``, which serializes this tree's access for that reason."""

    @override
    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        # An unknown token: the shell's error handler echoes it back, which is
        # all the readiness probe needs. No `stty`, no `echo`. *prefix* is
        # always "" here: Zephyr has no history to suppress.
        return f"{prefix}{m.ready}\n"

    shell_lines_after_begin: ClassVar[int] = 2
    """Shell lines between the BEGIN marker and the command's output.

    The BEGIN rejection and the prompt that follows it; a dialect that also
    reports each exit code inline (the getting-started 2.7 frame) has three.
    :meth:`parse_output` and :meth:`parse_partial_output` both read it."""

    @property
    @override
    def live_skip_lines(self) -> int:
        """The :attr:`shell_lines_after_begin` lines after the BEGIN line's own."""
        return self.shell_lines_after_begin - 1

    @override
    def clean_output(self, text: str) -> str:
        r"""Remove the level colouring (``strip_zephyr_levels``) and each ``\r\n``'s ``\r``."""
        return strip_zephyr_levels(text).replace("\r\n", "\n")

    @override
    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
        # Four CR-separated lines (see class docstring). The order
        # BEGIN / cmd / retval / END is load-bearing.
        return f"{m.begin}\r{cmd}\rretval\r{m.end_prefix}\r"

    @override
    def recover(self, m: SessionMarkers) -> str:
        return f"{m.recover}\n"

    @override
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # The Zephyr END marker carries no exit code — `retval` reports it on
        # its own line — so the end pattern is the bare token, not the bash
        # form's `..._END__(\d+)__`.
        return re.compile(re.escape(m.end_prefix))

    @override
    def marks_begin(self, data: str, *, m: SessionMarkers) -> bool:
        # The Zephyr shell rejects the BEGIN token as `<token>: command not
        # found`, so the marker is a substring of the line, not the whole line.
        return m.begin in data

    @override
    def extract_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        """Recover the exit code from ``retval``'s output.

        ``retval`` prints a bare signed integer on its own line just before the
        END marker. Take the last such line in the region preceding END.
        Returns ``-1`` if no integer is found.
        """
        for line in reversed(self.region_before_end(buffer, m=m)):
            stripped = line.strip()
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped)
        return -1

    @override
    def parse_output(self, buffer: str, *, cmd: str, m: SessionMarkers) -> str:
        r"""Return the command's exact output, found positionally.

        The shell sends, after the rejected BEGIN line and its prompt::

            <output>  <prompt> \r\n  <retval's integer> \r\n  <prompt> \r\n  <END>

        The last three ``\r\n``-separated pieces before END are the prompt
        that runs END (with END's own error colour), the ``retval`` line and the
        prompt that runs ``retval``; that prompt's text is read from the final
        prompt, never hard-coded, and removed from where the output stops. What
        remains after BEGIN goes to :meth:`output_value`.
        """
        lines = self.region_before_end(buffer, m=m)
        if len(lines) < _ZEPHYR_TAIL_PIECES:
            return ""
        prompt = lines[-2]
        # The last pieces are retval's line, the prompt that runs END and
        # END's own piece; the prompt before retval ends what remains.
        region = "\r\n".join(lines[:-_ZEPHYR_TAIL_PIECES]).removesuffix(prompt)
        return self.output_value(region, m=m, lines_after_begin=self.shell_lines_after_begin)

    @override
    def parse_partial_output(self, buffer: str, *, m: SessionMarkers) -> str:
        r"""Return what an unfinished command had written: everything after BEGIN's shell lines.

        A command still running has printed no trailing prompt, ``retval``
        line or END yet, so the positional parse of :meth:`parse_output`
        does not apply: the output is all that follows the
        :attr:`shell_lines_after_begin` lines after the last BEGIN, its
        unfinished last line included, through :meth:`output_value`.
        """
        if m.begin not in buffer:
            return ""
        region = "\r\n".join(self.region_before_end(buffer, m=m))
        return self.output_value(region, m=m, lines_after_begin=self.shell_lines_after_begin)

    def output_value(self, region: str, *, m: SessionMarkers, lines_after_begin: int) -> str:
        r"""Return the ``value`` of the output that ends *region*.

        *region* runs from the start of the buffer to where the command's
        output stops. The output starts after the last BEGIN marker and the
        *lines_after_begin* shell lines that follow it (each ending ``\r\n``):
        the BEGIN rejection, then its prompt (and, on a shell that reports
        each exit code inline, that report's line before the prompt). From
        what remains this removes only what the shell adds: the colouring of
        each error and warning line, wherever it falls
        (``strip_zephyr_levels``), and the ``\r`` of each ``\r\n`` line
        ending. Every
        other byte, escape sequences and blank lines included, is kept, and
        the result is :func:`command_value` of it.

        A helper for a subclass that parses the framed response itself (the
        Zephyr 2.7 frame in the getting-started example does). Subclasses call
        it; they do not override it.
        """
        begin = region.rfind(m.begin)
        start = 0
        if begin != -1:
            start = begin
            for _ in range(lines_after_begin):
                end = region.find("\r\n", start)
                if end == -1:
                    return ""
                start = end + 2
        return command_value(self.clean_output(region[start:]))

    def region_before_end(self, buffer: str, *, m: SessionMarkers) -> list[str]:
        r"""Return the buffer up to (not including) the END token, split into lines.

        The split is at the shell's ``\r\n`` line endings, which are removed.
        The END token and anything after it are excluded. Nothing else is
        removed: escape sequences, a lone ``\r`` and blank lines are kept.

        A helper for a subclass that parses the framed response itself (the
        Zephyr 2.7 frame in the getting-started example does). Subclasses call
        it; they do not override it.
        """
        end_idx = buffer.find(m.end_prefix)
        region = buffer if end_idx == -1 else buffer[:end_idx]
        return region.split("\r\n")


class ZephyrSerialFrame(ZephyrFrame):
    """Zephyr 3.7+ dialect for a serial/UART shell reached over a raw byte bridge.

    Identical framing and parsing to :class:`ZephyrFrame` — only the handshake
    differs. The in-guest ``SHELL_BACKEND_TELNET`` honours otto's ``IAC DONT
    ECHO`` and stops echoing input, which is why the stock ``ZephyrFrame``
    handshake assumes a non-echoing shell. A UART shell behind a ``-serial
    telnet:`` bridge never sees that IAC (QEMU consumes it), so it keeps echo
    **on**; the echoed END marker would then match otto's read loop before the
    command's real output arrives, desyncing every command by one. Disable echo
    once, up front — ``shell echo off`` is a stock builtin. The readiness marker
    (rejected as an unknown command) still comes back via the shell's error
    handler, which is shell *output*, not input echo, so the probe is
    unaffected. Mirrors ``project1's ZephyrInlineRetcodeFrame`` for 2.7.
    """

    type_name = "zephyr-serial"

    @override
    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        return f"{prefix}shell echo off\r{m.ready}\n"

    @override
    def restore_interactive(self) -> str | None:
        # The handshake ran `shell echo off`; a human wants their keystrokes back.
        return "shell echo on"


class RawFrame(CommandFrame):
    """The ``raw`` landing dialect: no handshake, no framing, nothing to parse.

    For a console whose landing state answers no frame at all — a boot menu
    that reads a digit, an autoboot countdown, a serial-recovery prompt. A
    host declares ``landing_frame: "raw"`` together with a ``session_setup``
    hook; the session is marked ready without a byte being written, the hook
    manoeuvres with ``send``/``expect`` alone, and frame entry into the
    host's real ``command_frame`` confirms the shell. Landing-only:
    ``command_frame: "raw"`` is refused at lab load.

    The empty :meth:`handshake` is the contract ``ShellSession._handshake``
    reads: an empty payload means "nothing to confirm". :meth:`frame` and
    :meth:`recover` raise :class:`~otto.host.errors.RawLandingError` so a
    ``run()`` before ``enter_frame()`` is named for what it is.

    Subclass this only to keep those semantics (a project may want its own
    name for "no dialect"): ``ShellSession`` treats every ``RawFrame`` as
    raw. A landing that answers commands — a bootloader with a prompt and an
    ``echo`` — is a :class:`CommandFrame` subclass with a real handshake and
    frame, registered like any other, so a hook can ``run()`` in it.
    """

    type_name = "raw"
    streams_output_live = False

    @override
    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:  # no probe to render
        return ""

    @override
    def frame(self, cmd: str, *, m: SessionMarkers) -> str:  # refuses, see class docstring
        raise RawLandingError(
            "a raw landing has no command dialect: use send()/expect() to reach the "
            "target shell, then enter_frame() before run()"
        )

    @override
    def recover(self, m: SessionMarkers) -> str:  # refuses, see class docstring
        raise RawLandingError(
            "a raw landing cannot be recovered by probe: enter_frame() first, or reopen the session"
        )

    @override
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:  # never matches
        # Compiled once per session at construction; must exist, must never match.
        return re.compile(r"(?!x)x")

    @override
    def marks_begin(self, data: str, *, m: SessionMarkers) -> bool:  # nothing frames
        return False

    @override
    def parse_output(self, buffer: str, *, cmd: str, m: SessionMarkers) -> str:  # nothing frames
        return ""

    @override
    def extract_retcode(self, buffer: str, *, m: SessionMarkers) -> int:  # nothing frames
        return -1


def frame_or_default(frame: CommandFrame | None) -> CommandFrame:
    """Return *frame*, or :class:`BashFrame` for ``None``: what an unset frame means.

    A host's ``command_frame`` field is ``None`` until a family sets it, and
    :class:`~otto.host.session.ShellSession` types bash then.
    """
    return frame or BashFrame()


def history_prefix(frame: CommandFrame | None, shell_history: bool) -> str:
    """Prefix that suppresses history recording, or ``""`` when history is kept.

    *frame* None means bash (``frame_or_default``), as it does for
    :class:`~otto.host.session.ShellSession` — so a caller holding a host's
    unresolved ``command_frame`` field doesn't have to repeat it.

    ``shell_history`` carries one meaning everywhere it appears — "are otto's
    commands recorded in this host's shell history" — and is threaded through
    unchanged from :attr:`~otto.host.unix_host.UnixHost.shell_history`. The
    guard below is deliberately the *only* negation in the feature; resist
    introducing a second, inverted flag name alongside it.
    """
    if shell_history:
        return ""
    return frame_or_default(frame).quiet_history()


def handshake_prefix(
    frame: CommandFrame, m: SessionMarkers, *, shell_history: bool, save_terminal: bool
) -> str:
    """Return the prefix a session's readiness handshake carries after its anchor.

    The frame's :meth:`~CommandFrame.save_terminal` first when
    *save_terminal* (the first handshake on a terminal that outlives the
    session), so it reads the settings before anything changes them; then
    :func:`history_prefix`. The one composition, for the session and the
    tests that type the session's line into real shells.
    """
    save = frame.save_terminal(m) if save_terminal else ""
    return save + history_prefix(frame, shell_history)


def _check_frame_class(type_name: str, cls: object) -> None:
    """Refuse anything but a :class:`CommandFrame` subclass whose ``type_name`` is its name.

    One check, two call sites: ``validate`` runs it on a class registered
    eagerly, ``check_resolved`` on the class a ``Ref`` names, once imported.
    It also holds :attr:`CommandFrame.exact_output` to ``None`` or a
    :class:`~otto.host.limitation.Limitation` declared on the class's own
    body, so a subclass never inherits a parent's limitation unseen.
    """
    if not (isinstance(cls, type) and issubclass(cls, CommandFrame)):
        raise TypeError(
            f"register_command_frame({type_name!r}): {cls!r} is not a CommandFrame subclass"
        )
    if cls.type_name != type_name:
        raise ValueError(
            f"register_command_frame: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    limitation = cls.exact_output
    if limitation is not None and not isinstance(limitation, Limitation):
        raise TypeError(
            f"register_command_frame({type_name!r}): {cls.__name__}.exact_output must be "
            f"None or a Limitation, got {limitation!r}"
        )
    if limitation is not None and "exact_output" not in vars(cls):
        raise ValueError(
            f"register_command_frame({type_name!r}): {cls.__name__} must declare "
            "exact_output on its own class body -- it would inherit a parent's Limitation "
            "it never measured. Declare None, or its own Limitation."
        )
    _check_exec_frame(type_name, cls)


def _defining_class(cls: type, name: str) -> type:
    """Return the class in *cls*'s MRO whose own body defines *name*."""
    return next(klass for klass in cls.__mro__ if name in vars(klass))


def _check_exec_frame(type_name: str, cls: "type[CommandFrame]") -> None:
    """Refuse a frame whose ``exec`` frame its own parser cannot read, or cannot restore after.

    A class that isolates ``exec`` (its :meth:`CommandFrame.frame_exec` is
    not the base's) must not re-spell :meth:`CommandFrame.frame` or
    :meth:`CommandFrame.end_pattern` below the class that defines that
    ``frame_exec``: the inherited exec frame would type a record the new
    parser does not read. It must also provide the post-recovery restore
    (:meth:`CommandFrame.restore_line` and
    :meth:`CommandFrame.restored_pattern`), which an interrupted ``exec``
    needs.
    """
    owner = _defining_class(cls, "frame_exec")
    if owner is CommandFrame:
        return
    for name in ("frame", "end_pattern"):
        definer = _defining_class(cls, name)
        if definer is not owner and issubclass(definer, owner):
            raise TypeError(
                f"register_command_frame({type_name!r}): {cls.__name__} re-spells frame or "
                f"end_pattern below {owner.__name__}.frame_exec; override frame_exec too, or "
                "derive from a frame that does not isolate"
            )
    for name in ("restore_line", "restored_pattern"):
        if _defining_class(cls, name) is CommandFrame:
            raise TypeError(
                f"register_command_frame({type_name!r}): {cls.__name__} isolates exec "
                f"(it overrides frame_exec) but keeps CommandFrame.{name}; an isolating "
                "frame provides the restore an interrupted exec needs"
            )


def _validate_frame_entry(
    type_name: str, entry: "ClassEntry[CommandFrame]", _proposed: object
) -> None:
    if not isinstance(entry.cls, Ref):
        _check_frame_class(type_name, entry.cls)


# Registry of dialect name -> frame class, mirroring
# ``embedded_filesystem.FILESYSTEM_CLASSES``. Its built-ins are registered by
# reference at module end, and the same check holds them to the ``type_name``
# rule a third party's frame meets.
FRAME_CLASSES: "Registry[ClassEntry[CommandFrame]]" = Registry(
    "command frame",
    entry=ClassEntry,
    register_hint="otto.host.command_frame.register_command_frame()",
    validate=_validate_frame_entry,
    check_resolved=lambda name, entry: _check_frame_class(name, resolved(entry.cls)),
)
"""The command-frame classes lab data can name, keyed by dialect name."""


@registration_boundary
def register_command_frame(
    type_name: str, cls: "type[CommandFrame] | Ref", *, overwrite: bool = False
) -> None:
    """Make a custom :class:`CommandFrame` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.embedded_filesystem.register_filesystem` follows.
    Once registered, lab-data entries can reference the subclass by *type_name*
    in the ``command_frame`` field.

    *cls* may be a :class:`~otto.registry.Ref` (``"module:Class"``); its module
    is imported, and the class checked, when lab data first builds the frame.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.

    Raises
    ------
    TypeError
        If *cls* is not a :class:`CommandFrame` subclass.
    ValueError
        If *type_name* doesn't match ``cls.type_name`` (the registry key and
        the class constant should agree).
    """
    FRAME_CLASSES.register(name=type_name, entry=ClassEntry(cls=cls), overwrite=overwrite)


def build_command_frame(*, type_name: str) -> CommandFrame:
    """Construct the :class:`CommandFrame` registered under *type_name*.

    Raises
    ------
    ValueError
        If *type_name* is not registered. The error lists the registered names
        so a typo is diagnosable from the message alone.
    """
    return resolved(FRAME_CLASSES.get(type_name).cls)()


@dataclass(frozen=True, kw_only=True)
class BuiltinFrame:
    """One frame otto ships: its dialect name, and a reference to its class."""

    name: str
    ref: Ref


BUILTIN_FRAMES: "list[BuiltinFrame]" = [
    BuiltinFrame(name="bash", ref=Ref(target="otto.host.command_frame:BashFrame")),
    BuiltinFrame(name="ash", ref=Ref(target="otto.host.command_frame:AshFrame")),
    BuiltinFrame(name="zephyr", ref=Ref(target="otto.host.command_frame:ZephyrFrame")),
    BuiltinFrame(name="zephyr-serial", ref=Ref(target="otto.host.command_frame:ZephyrSerialFrame")),
    BuiltinFrame(name="raw", ref=Ref(target="otto.host.command_frame:RawFrame")),
]
"""The frames otto ships, in registration order: the one list of them.

:func:`_register_builtin_frames` registers exactly these, and anything that
must speak about the built-in frames alone (not a caller's registrations,
nor a built-in a caller overwrote) reads this list rather than
:data:`FRAME_CLASSES`."""


def _register_builtin_frames() -> None:
    """Register :data:`BUILTIN_FRAMES` by reference.

    Mirrors ``os_profile._register_builtin_host_classes``.
    """
    for frame in BUILTIN_FRAMES:
        FRAME_CLASSES.register(name=frame.name, entry=ClassEntry(cls=frame.ref))


_register_builtin_frames()
