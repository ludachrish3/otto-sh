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

A project can register additional dialects via :func:`register_command_frame`
from a ``.otto`` init module — the same extension hook
:func:`otto.host.embedded_filesystem.register_filesystem` follows.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import override

from ..registry import Registry, caller_module


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

    @classmethod
    def for_session(cls, session_id: str) -> "SessionMarkers":
        """Build the marker set for a session id."""
        return cls(
            begin=f"__OTTO_{session_id}_BEGIN__",
            end_prefix=f"__OTTO_{session_id}_END__",
            ready=f"__OTTO_{session_id}_READY__",
            recover=f"__OTTO_{session_id}_RECOVER__",
        )


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

    streams_output_live: ClassVar[bool] = False
    """Whether this dialect's raw inter-marker byte stream is already clean
    line-by-line and can be streamed to the log as it arrives. Default False:
    the session buffers to the END sentinel and logs ``parse_output(buffer)`` —
    so the log shows exactly the command's parsed output, with no shell prompts
    or retcode scaffolding. A dialect sets this True only when its raw stream
    has no scaffolding to strip (e.g. bash with echo off)."""

    # --- render half: command -> bytes to write ---

    @abstractmethod
    def handshake(self, m: SessionMarkers) -> str:
        """Readiness-probe payload, echoing :attr:`SessionMarkers.ready`."""
        ...

    @abstractmethod
    def frame(self, cmd: str, m: SessionMarkers) -> str:
        """Full write payload that runs ``cmd`` bracketed by the sentinels."""
        ...

    @abstractmethod
    def recover(self, m: SessionMarkers) -> str:
        """Re-synchronization payload, echoing :attr:`SessionMarkers.recover`."""
        ...

    def quiet_history(self) -> str:
        """Statement prefix stopping this dialect's shell recording commands.

        Empty when the dialect has no persistent history to suppress — which
        is how non-unix dialects are excluded from history suppression
        entirely, without any host-family or ``isinstance`` branching at the
        call sites. Zephyr keeps its history in a RAM ring buffer, so the
        inherited empty default is already correct for it.

        The result is prepended to the *first line written into a fresh
        shell* (the readiness handshake, and the resync probe after a
        login-proxy hop), so an override MUST emit no output of its own and
        MUST be idempotent — ``confirm_live`` resends those payloads on a
        loop until one lands.
        """
        return ""

    # --- parse half: bytes read -> structured result ---

    @abstractmethod
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        """Regex marking the end of a command's output in the stream.

        The session compiles this once per session and uses it both to detect
        completion in the streaming read loop and to bound parsing.
        """
        ...

    @abstractmethod
    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        """Return True if ``data`` is the chunk carrying the BEGIN sentinel."""
        ...

    @abstractmethod
    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str:
        """Extract the command's output from the accumulated ``buffer``."""
        ...

    @abstractmethod
    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        """Recover the command's exit code; ``-1`` when none can be read."""
        ...

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


class BashFrame(CommandFrame):
    """POSIX bash dialect: ``echo`` brackets and ``$?`` baked into the END marker.

    The default frame for SSH/telnet/local unix sessions.
    """

    type_name = "bash"
    streams_output_live = True  # echo off + single prompt: raw stream == clean output

    @override
    def handshake(self, m: SessionMarkers) -> str:
        # Silence echo so the probe command text doesn't come back, then print
        # the READY marker.
        return f"stty -echo 2>/dev/null; echo {m.ready}\n"

    @override
    def frame(self, cmd: str, m: SessionMarkers) -> str:
        # Bracket the command with echoes; the END sentinel embeds ``$?`` so the
        # exit code travels back inside the marker.
        return f'echo "{m.begin}"; {cmd}; echo "{m.end_prefix}$?__"\n'

    @override
    def recover(self, m: SessionMarkers) -> str:
        # Echo-proof liveness probe on a marker distinct from the normal
        # end-of-command sentinel: bake $? into the RECOVER marker (not
        # end_prefix), so a real shell emits `..._RECOVER__<digits>__` while an
        # echo/REPL can only reproduce the literal `$?`. Using RECOVER rather
        # than end_prefix keeps recover_pattern distinct from end_pattern, so a
        # dying command's own compound-line tail echo (`{end_prefix}<code>__`,
        # appended by `frame`) never matches this probe's reply — no collision,
        # nothing to drain.
        return f'echo "{m.recover}$?__"\n'

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
        # `|| :` — pins $? to 0. This payload also prefixes the resync probe
        #   (`echo "…RECOVER__$?__"`), and without it dash/busybox report
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
        # readonly — hence a zsh-scoped clause rather than a rewrite.
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
        # READY parse must never see.)
        return (
            "{ command export HISTFILE=/dev/null; } 2>/dev/null || :; "
            "{ command set +o history; } 2>/dev/null || :; "
            "case ${ZSH_VERSION:-} in ?*) "
            "eval 'export HISTFILE=/dev/null' 2>/dev/null || :;; esac; "
        )

    @override
    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # `__OTTO_<id>_RECOVER__<code>__` — digits prove real execution; the
        # RECOVER marker keeps this disjoint from end_pattern.
        return re.compile(re.escape(m.recover) + r"(\d+)__")

    @override
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # ``__OTTO_<id>_END__<code>__`` — the digits carry the exit code.
        return re.compile(re.escape(m.end_prefix) + r"(\d+)__")

    @override
    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        # bash emits the marker on a line of its own, so equality (or suffix,
        # for a marker printed after same-line text) is the right test — and it
        # avoids a false hit on an echoed wrapped command.
        stripped = data.rstrip("\r\n")
        return stripped == m.begin or stripped.endswith(m.begin)

    @override
    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str:
        # Find the LAST BEGIN marker — if the shell echoes the wrapped command,
        # the marker appears twice (echoed command text + actual output);
        # rfind skips the echoed copy.
        begin_idx = buffer.rfind(m.begin)
        if begin_idx != -1:
            start = begin_idx + len(m.begin)
            # Skip trailing newline(s) after the marker.
            while start < len(buffer) and buffer[start] in ("\r", "\n"):
                start += 1
        else:
            start = 0
        end_match = self.end_pattern(m).search(buffer, start)
        end = end_match.start() if end_match else len(buffer)
        # Strip carriage returns left over from PTY \r\n line endings.
        return buffer[start:end].rstrip("\r\n").replace("\r", "")

    @override
    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        match = self.end_pattern(m).search(buffer)
        if match and match.groups():
            return int(match.group(1))
        return -1


# Terminal control sequences (colored prompt, cursor moves). otto is not a
# terminal — strip them before parsing the Zephyr shell stream.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class ZephyrFrame(CommandFrame):
    """Stock Zephyr RTOS shell dialect (3.7 / 4.4 LTS).

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
    yields the output without ever reading the prompt text. ANSI escapes (the
    colored prompt) are stripped first.
    """

    type_name = "zephyr"

    @override
    def handshake(self, m: SessionMarkers) -> str:
        # An unknown token: the shell's error handler echoes it back, which is
        # all the readiness probe needs. No `stty`, no `echo`.
        return f"{m.ready}\n"

    @override
    def frame(self, cmd: str, m: SessionMarkers) -> str:
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
    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        # The Zephyr shell rejects the BEGIN token as `<token>: command not
        # found`, so the marker is a substring of the line, not the whole line.
        return m.begin in data

    @override
    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        """Recover the exit code from ``retval``'s output.

        ``retval`` prints a bare signed integer on its own line just before the
        END marker. Take the last such line in the region preceding END.
        Returns ``-1`` if no integer is found.
        """
        for line in reversed(self._region_before_end(buffer, m)):
            stripped = line.strip()
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped)
        return -1

    @override
    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str:
        """Extract the command's output from the framed response, positionally.

        Between the BEGIN error line and ``retval``'s integer line the shell
        emitted ``[prompt, <command output...>, prompt]`` (a prompt after each
        of the two executed lines: the rejected BEGIN, then the command). Drop
        the bracketing prompt lines.
        """
        lines = self._region_before_end(buffer, m)

        # The BEGIN block: BEGIN is an unknown command, so the shell emits
        # `<token>: command not found`. The last line carrying the token is it.
        begin_line = -1
        for i, line in enumerate(lines):
            if m.begin in line:
                begin_line = i

        # The retcode: `retval`'s output — the last bare integer line.
        retcode_line = len(lines)
        for i in range(len(lines) - 1, begin_line, -1):
            if re.fullmatch(r"-?\d+", lines[i].strip()):
                retcode_line = i
                break

        # [prompt, <command output...>, prompt] — drop the bracketing prompts.
        block = lines[begin_line + 1 : retcode_line]
        output = block[1:-1] if len(block) >= 2 else []  # noqa: PLR2004 — block needs ≥2 lines for [1:-1] slice to yield non-empty output
        return "\n".join(output).strip()

    def _region_before_end(self, buffer: str, m: SessionMarkers) -> list[str]:
        r"""Return the buffer's lines up to (not including) the END token.

        ANSI terminal sequences and the carriage returns from telnet ``\r\n``
        line endings are stripped from each line. The END token's own line and
        anything after it are excluded.
        """
        clean = _ANSI_RE.sub("", buffer)
        end_idx = clean.find(m.end_prefix)
        region = clean if end_idx == -1 else clean[:end_idx]
        return [line.replace("\r", "") for line in region.split("\n")]


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
    unaffected. Mirrors ``repo1's ZephyrInlineRetcodeFrame`` for 2.7.
    """

    type_name = "zephyr-serial"

    @override
    def handshake(self, m: SessionMarkers) -> str:
        return f"shell echo off\r{m.ready}\n"


def history_prefix(frame: CommandFrame | None, shell_history: bool) -> str:
    """Prefix that suppresses history recording, or ``""`` when history is kept.

    *frame* None means bash, matching :class:`~otto.host.session.ShellSession`'s
    own ``command_frame or BashFrame()`` default — so a caller holding a host's
    unresolved ``command_frame`` field doesn't have to repeat it.

    ``shell_history`` carries one meaning everywhere it appears — "are otto's
    commands recorded in this host's shell history" — and is threaded through
    unchanged from :attr:`~otto.host.unix_host.UnixHost.shell_history`. The
    guard below is deliberately the *only* negation in the feature; resist
    introducing a second, inverted flag name alongside it.
    """
    if shell_history:
        return ""
    return (frame or BashFrame()).quiet_history()


# Registry of dialect name -> frame class, mirroring
# ``embedded_filesystem.FILESYSTEM_CLASSES``. Seeded empty here and populated
# by ``_register_builtin_frames()`` at module end, so otto's own built-ins
# travel the same ``register_command_frame`` path third parties use.
FRAME_CLASSES: Registry[type[CommandFrame]] = Registry(
    "command frame", register_hint="otto.host.command_frame.register_command_frame()"
)


def register_command_frame(
    type_name: str, cls: type[CommandFrame], *, overwrite: bool = False
) -> None:
    """Make a custom :class:`CommandFrame` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.embedded_filesystem.register_filesystem` follows.
    Once registered, lab-data entries can reference the subclass by *type_name*
    in the ``command_frame`` field.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.

    Raises
    ------
    ValueError
        If *type_name* doesn't match ``cls.type_name`` (the registry key and
        the class constant should agree).
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_command_frame: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    FRAME_CLASSES.register(type_name, cls, overwrite=overwrite, origin=caller_module())


def build_command_frame(type_name: str) -> CommandFrame:
    """Construct the :class:`CommandFrame` registered under *type_name*.

    Raises
    ------
    ValueError
        If *type_name* is not registered. The error lists the registered names
        so a typo is diagnosable from the message alone.
    """
    return FRAME_CLASSES.get(type_name)()


def _register_builtin_frames() -> None:
    """Register otto's built-in frames through the public path.

    Ensures first-party and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``).
    """
    register_command_frame(BashFrame.type_name, BashFrame)
    register_command_frame(ZephyrFrame.type_name, ZephyrFrame)
    register_command_frame(ZephyrSerialFrame.type_name, ZephyrSerialFrame)


_register_builtin_frames()
