"""
Zephyr RTOS shell session.

:class:`ZephyrSession` runs commands over the telnet shell of a Zephyr target.
It reuses :class:`~otto.host.session.TelnetSession`'s I/O primitives and the
:class:`~otto.host.session.ShellSession` engine (read loop, expect handling,
timeout recovery) unchanged, and overrides only the *framing seam* — the few
hooks that assume a POSIX bash shell.

Why Zephyr needs its own framing
--------------------------------
The stock Zephyr shell is not bash: no command substitution, no ``$?``, no
generic ``echo``/``printf``. otto cannot ``echo`` a sentinel or read ``$?``.
But the shell's *error handler* stands in for ``echo``: an unrecognized
command produces a line ``<token>: command not found`` that echoes the token
verbatim. The token is otto-controlled, so otto frames each command as four
CR-separated lines::

    __OTTO_<id>_BEGIN__   rejected -> shell emits `<token>: command not found`
    <the real command>    real output appears here
    retval                stock Zephyr command: prints <cmd>'s exit code
    __OTTO_<id>_END__     rejected -> shell emits `<token>: command not found`

otto never depends on the ``uart:~$`` prompt and never modifies the firmware —
``retval`` is a stock built-in and the markers are just rejected input.

The four-line order is load-bearing
-----------------------------------
Every command — including an unrecognized one — overwrites the shell's stored
return value (an unknown command sets it to ``-8``, ``-ENOEXEC``). So BEGIN
before ``<cmd>`` is harmless (``<cmd>`` overwrites BEGIN's ``-8``) and END
after ``retval`` is harmless (already captured), but a marker placed *between*
``<cmd>`` and ``retval`` would clobber the value. ``retval`` must run
immediately after ``<cmd>``.

What the live shell actually does (verified 2026-05-22 on Zephyr 3.7.2)
----------------------------------------------------------------------
- **Input is not echoed.** Only the *results* of the four framed lines come
  back — there is no echoed copy of the command text.
- **A prompt follows every executed line.** Each of the four framed lines
  produces ``\\r\\n<result>\\r\\n<prompt>``, so the response is four
  ``<result>`` blocks each trailed by the shell prompt. Parsing handles the
  prompt *positionally* — between the BEGIN error line and ``retval``'s
  integer line the slice is exactly ``[prompt, <output...>, prompt]``, so
  dropping the first and last line yields the output. The prompt text is
  never read, matched, or hard-coded.
- ``retval`` prints a bare signed integer on its own line; codes are signed
  errno-style values (``0`` success, negative failure, ``-8`` = unknown cmd).
- The shell wraps the prompt in ANSI color codes; otto strips ANSI before
  parsing (it is not a terminal).

Parsing keys only on the two unique per-session marker tokens and the integer
``retval`` line — never on the literal word ``retval`` or on the prompt text.
"""

import re
from typing import Any

from .session import TelnetSession

# Terminal control sequences (colored prompt, cursor moves). otto is not a
# terminal — strip them before parsing.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


class ZephyrSession(TelnetSession):
    """Persistent shell session for a Zephyr RTOS target, reached over telnet.

    Substitutable for :class:`TelnetSession` — it shares the
    ``(reader, writer, _owned_client)`` constructor — so an embedded host can
    pass it to :class:`~otto.host.session.SessionManager` as
    ``telnet_session_cls``.
    """

    # The Zephyr telnet shell under QEMU can take a few seconds after the TCP
    # connection opens before it starts reading input. Give the readiness
    # handshake a generous ceiling (the bash default is 3 s).
    _init_timeout: float = 15.0

    def __init__(self, reader: Any, writer: Any, _owned_client: Any = None) -> None:
        super().__init__(reader, writer, _owned_client)
        # The Zephyr END marker carries no exit code — `retval` reports it on
        # its own line — so the end pattern is the bare token, not the bash
        # form's `__OTTO_<id>_END__(\d+)__`.
        self._end_pattern = re.compile(rf"__OTTO_{self._session_id}_END__")

    # --- Framing seam overrides ---

    def _handshake_command(self) -> str:
        # An unknown token: the shell's error handler echoes it back, which is
        # all the readiness probe needs. No `stty`, no `echo`.
        return f"{self._ready_marker}\n"

    def _recover_command(self) -> str:
        # Same trick for the post-timeout re-sync sentinel.
        return f"{self._recover_marker}\n"

    def _frame_command(self, cmd: str) -> str:
        # Four CR-separated lines (see the module docstring). The order
        # BEGIN / cmd / retval / END is load-bearing — `retval` must run
        # directly after `cmd`, with no marker in between to clobber it.
        return (
            f"{self._begin_marker}\r"
            f"{cmd}\r"
            f"retval\r"
            f"{self._end_marker_prefix}\r"
        )

    def _marks_begin(self, data: str) -> bool:
        # The Zephyr shell rejects the BEGIN token as `<token>: command not
        # found`, so the marker is a substring of the line, not the whole line.
        return self._begin_marker in data

    def _extract_retcode(self, buffer: str) -> int:
        """Recover the exit code from ``retval``'s output.

        ``retval`` prints a bare signed integer on its own line just before
        the END marker. Take the last such line in the region preceding the
        END token. Returns ``-1`` if no integer is found (e.g. a target whose
        shell lacks ``retval`` — unusual, it is a default-on built-in).
        """
        for line in reversed(self._region_before_end(buffer)):
            stripped = line.strip()
            if re.fullmatch(r'-?\d+', stripped):
                return int(stripped)
        return -1

    def _parse_output(self, buffer: str, cmd: str) -> str:
        """Extract the command's output from the framed response.

        The Zephyr telnet shell does not echo input, but it prints its prompt
        after *every* line it executes. So between the BEGIN error line and
        ``retval``'s integer line the shell emitted exactly two blocks: the
        command's output, and a prompt after each of the two executed lines
        (the rejected BEGIN, then the command). The slice is therefore
        ``[prompt, <command output...>, prompt]``.

        Parsing is purely **positional** — drop the first and last line of
        that slice. They are *structurally* the prompt (the shell prints it
        once per executed line), so this never reads, matches, or hard-codes
        the prompt text. The only assumption is that the shell prints a prompt
        at all, which a stock Zephyr shell does.
        """
        lines = self._region_before_end(buffer)

        # The BEGIN block: BEGIN is an unknown command, so the shell emits
        # `<token>: command not found`. The last line carrying the token is it.
        begin_line = -1
        for i, line in enumerate(lines):
            if self._begin_marker in line:
                begin_line = i

        # The retcode: `retval`'s output — the last bare integer line.
        retcode_line = len(lines)
        for i in range(len(lines) - 1, begin_line, -1):
            if re.fullmatch(r'-?\d+', lines[i].strip()):
                retcode_line = i
                break

        # [prompt, <command output...>, prompt] — drop the bracketing prompts.
        block = lines[begin_line + 1:retcode_line]
        output = block[1:-1] if len(block) >= 2 else []
        return '\n'.join(output).strip()

    def _region_before_end(self, buffer: str) -> list[str]:
        r"""Return the buffer's lines up to (not including) the END token.

        ANSI terminal sequences (the colored prompt, cursor moves) and the
        carriage returns from telnet ``\r\n`` line endings are stripped from
        each line. The END token's own line and anything after it (the shell's
        error echo, the next prompt) are excluded.
        """
        clean = _ANSI_RE.sub('', buffer)
        end_idx = clean.find(self._end_marker_prefix)
        region = clean if end_idx == -1 else clean[:end_idx]
        return [line.replace('\r', '') for line in region.split('\n')]
