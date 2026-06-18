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

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class SessionMarkers:
    """The unique per-session sentinel tokens a :class:`CommandFrame` renders
    into commands and keys on when parsing.

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
    """A shell's command-framing dialect: how to wrap a command for execution
    and how to parse the echoed stream back into ``(output, retcode)``.

    Concrete frames are stateless value objects. The render half
    (:meth:`handshake` / :meth:`frame` / :meth:`recover`) and the parse half
    (:meth:`end_pattern` / :meth:`marks_begin` / :meth:`parse_output` /
    :meth:`extract_retcode`) live together because they co-vary through *where
    the retcode lives* — splitting them would let mismatched halves combine.
    """

    type_name: ClassVar[str]
    """Lab-data string for this dialect (e.g. ``'bash'``). Looked up against
    :data:`_FRAME_CLASSES` by the storage factory; unique across frames."""

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


class BashFrame(CommandFrame):
    """POSIX bash dialect: ``echo`` brackets and ``$?`` baked into the END
    marker. The default frame for SSH/telnet/local unix sessions.
    """

    type_name = "bash"
    streams_output_live = True  # echo off + single prompt: raw stream == clean output

    def handshake(self, m: SessionMarkers) -> str:
        # Silence echo so the probe command text doesn't come back, then print
        # the READY marker.
        return f"stty -echo 2>/dev/null; echo {m.ready}\n"

    def frame(self, cmd: str, m: SessionMarkers) -> str:
        # Bracket the command with echoes; the END sentinel embeds ``$?`` so the
        # exit code travels back inside the marker.
        return (
            f'echo "{m.begin}"; '
            f'{cmd}; '
            f'echo "{m.end_prefix}$?__"\n'
        )

    def recover(self, m: SessionMarkers) -> str:
        return f"echo {m.recover}\n"

    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # ``__OTTO_<id>_END__<code>__`` — the digits carry the exit code.
        return re.compile(re.escape(m.end_prefix) + r"(\d+)__")

    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        # bash emits the marker on a line of its own, so equality (or suffix,
        # for a marker printed after same-line text) is the right test — and it
        # avoids a false hit on an echoed wrapped command.
        stripped = data.rstrip("\r\n")
        return stripped == m.begin or stripped.endswith(m.begin)

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

    def handshake(self, m: SessionMarkers) -> str:
        # An unknown token: the shell's error handler echoes it back, which is
        # all the readiness probe needs. No `stty`, no `echo`.
        return f"{m.ready}\n"

    def frame(self, cmd: str, m: SessionMarkers) -> str:
        # Four CR-separated lines (see class docstring). The order
        # BEGIN / cmd / retval / END is load-bearing.
        return (
            f"{m.begin}\r"
            f"{cmd}\r"
            f"retval\r"
            f"{m.end_prefix}\r"
        )

    def recover(self, m: SessionMarkers) -> str:
        return f"{m.recover}\n"

    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        # The Zephyr END marker carries no exit code — `retval` reports it on
        # its own line — so the end pattern is the bare token, not the bash
        # form's `..._END__(\d+)__`.
        return re.compile(re.escape(m.end_prefix))

    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        # The Zephyr shell rejects the BEGIN token as `<token>: command not
        # found`, so the marker is a substring of the line, not the whole line.
        return m.begin in data

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
        block = lines[begin_line + 1:retcode_line]
        output = block[1:-1] if len(block) >= 2 else []
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
    """Zephyr 3.7+ dialect for a serial/UART shell reached over a raw byte
    bridge (e.g. QEMU ``-serial telnet:<ip>:<port>,server``).

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
    unaffected. Mirrors :class:`repo1's ZephyrInlineRetcodeFrame` for 2.7.
    """

    type_name = "zephyr-serial"

    def handshake(self, m: SessionMarkers) -> str:
        return f"shell echo off\r{m.ready}\n"


# Registry of dialect name -> frame class, mirroring
# ``embedded_filesystem._FILESYSTEM_CLASSES``. Seeded empty here and populated
# by ``_register_builtin_frames()`` at module end, so otto's own built-ins
# travel the same ``register_command_frame`` path third parties use.
_FRAME_CLASSES: dict[str, type[CommandFrame]] = {}


def register_command_frame(type_name: str, cls: type[CommandFrame]) -> None:
    """Make a custom :class:`CommandFrame` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.embedded_filesystem.register_filesystem` follows.
    Once registered, lab-data entries can reference the subclass by *type_name*
    in the ``command_frame`` field.

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
    _FRAME_CLASSES[type_name] = cls


def build_command_frame(type_name: str) -> CommandFrame:
    """Construct the :class:`CommandFrame` registered under *type_name*.

    Raises
    ------
    ValueError
        If *type_name* is not registered. The error lists the registered names
        so a typo is diagnosable from the message alone.
    """
    try:
        cls = _FRAME_CLASSES[type_name]
    except KeyError:
        known = ", ".join(sorted(_FRAME_CLASSES))
        raise ValueError(
            f"Unknown command frame {type_name!r}. Registered frames: {known}. "
            f"Custom frames can be added via register_command_frame()."
        ) from None
    return cls()


def _register_builtin_frames() -> None:
    """Register otto's built-in frames through the public path, so first-party
    and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``).
    """
    register_command_frame(BashFrame.type_name, BashFrame)
    register_command_frame(ZephyrFrame.type_name, ZephyrFrame)
    register_command_frame(ZephyrSerialFrame.type_name, ZephyrSerialFrame)


_register_builtin_frames()
