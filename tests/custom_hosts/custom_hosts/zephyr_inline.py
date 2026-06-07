"""
A custom :class:`~otto.host.command_frame.CommandFrame` for Zephyr **2.7**
targets — the worked example of registering a custom shell dialect from an
out-of-tree module (the extension path otto's ``register_command_frame`` exists
for). It lives in the shared ``custom_hosts`` package rather than any one SUT
repo, so every repo whose lab includes a 2.7 host can depend on it the way a
product depends on a third-party library (see this package's ``__init__`` and
``tests/custom_hosts/README.md``).

Why 2.7 needs its own frame
---------------------------
otto's stock :class:`~otto.host.command_frame.ZephyrFrame` reads a command's
exit code by appending the shell's ``retval`` builtin. Zephyr 2.7 predates that
command (and its shell core doesn't even track a last return value), so there
is nothing for otto to read. Rather than patch a return-value *command* into
2.7 — which would mean patching the shell core — otto's 2.7 firmware carries a
one-line source patch (``tests/firmware/zephyr/patches/v2_7-shell-retcode.patch``)
that prints ``retCode = <n>`` after *every* command. This frame parses that
line in place of ``retval``.

Differences from the stock Zephyr frame
---------------------------------------
- **Framing** drops the ``retval`` line: every command already self-reports, so
  the frame is just ``BEGIN / cmd / END``.
- **Retcode** comes from the last ``retCode = <n>`` line before the END marker
  (the command's own; the rejected BEGIN marker emits an earlier ``-8``).
- **Output** is the text between the BEGIN marker's ``retCode`` line and the
  command's ``retCode`` line, with the single bracketing prompt dropped
  positionally (the prompt text is never matched — same principle as the stock
  frame).

Everything else — the readiness handshake, recovery, begin-marker detection,
ANSI stripping, the END pattern — is inherited from :class:`ZephyrFrame`
unchanged, because the transport (the Zephyr telnet shell) is identical.
"""

from __future__ import annotations

import re

from otto.host.command_frame import SessionMarkers, ZephyrFrame

# The inline return-code line the 2.7 firmware patch emits after every command.
# Tolerant of surrounding whitespace; the signed integer is captured.
_RETCODE_RE = re.compile(r"retCode\s*=\s*(-?\d+)")


class ZephyrInlineRetcodeFrame(ZephyrFrame):
    """Zephyr 2.7 dialect: exit code read from an inline ``retCode = <n>`` line
    instead of the ``retval`` builtin (which 2.7 lacks).
    """

    type_name = "zephyr-inline"

    def handshake(self, m: SessionMarkers) -> str:
        # Unlike 3.7+, the Zephyr 2.7 telnet shell echoes input by default.
        # Left on, the echoed END marker would match otto's read loop *before*
        # the command's real output arrives, desyncing every command by one.
        # `shell echo off` is a stock 2.7 builtin; disable echo once, up front,
        # so the session then behaves like the non-echoing 3.7 shell the parser
        # assumes. The readiness marker (rejected as an unknown command) still
        # echoes back via the shell's error handler — that is shell *output*,
        # not input echo, so the handshake probe is unaffected.
        return f"shell echo off\r{m.ready}\n"

    def frame(self, cmd: str, m: SessionMarkers) -> str:
        # Three CR-separated lines — no `retval`. Every command self-reports its
        # code via the firmware patch, so the bracketing markers are enough:
        #   BEGIN  -> rejected, emits `<token>: command not found` + retCode=-8
        #   cmd    -> output + retCode=<n>   (the code we want)
        #   END    -> rejected (its retCode lands after the END token, ignored)
        return f"{m.begin}\r{cmd}\r{m.end_prefix}\r"

    def _begin_line(self, lines: list[str], m: SessionMarkers) -> int:
        """Index of the BEGIN-marker line, or ``-1``.

        Uses the *last* occurrence: residue from the readiness handshake (the
        rejected ready marker's ``command not found`` + ``retCode = -8`` +
        prompt) can sit ahead of the marker in the buffer, so anchoring on the
        marker — not the first ``retCode`` line — keeps that residue out of the
        parse.
        """
        return max((i for i, ln in enumerate(lines) if m.begin in ln), default=-1)

    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        """Return the command's exit code: the last ``retCode = <n>`` after the
        BEGIN marker.

        The rejected BEGIN marker emits its own ``retCode = -8`` first, so the
        *last* match is the real command's. ``-1`` if none is found (e.g. an
        unpatched 2.7 build — a clear signal the firmware patch is missing).
        """
        lines = self._region_before_end(buffer, m)
        begin = self._begin_line(lines, m)
        for ln in reversed(lines[begin + 1:]):
            match = _RETCODE_RE.search(ln)
            if match:
                return int(match.group(1))
        return -1

    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str:
        """Extract the command's output from the framed response.

        Region layout after the BEGIN marker (prompt printed after each
        executed line; echo is disabled in the handshake)::

            <BEGIN>: command not found
            retCode = -8          <- BEGIN's code (first retCode after BEGIN)
            <prompt>
            <command output...>
            retCode = <n>         <- command's code (last retCode after BEGIN)
            <prompt>

        The output is the slice between those two ``retCode`` lines, with the
        single leading prompt dropped positionally — the prompt is structurally
        the first line the shell prints after BEGIN's code, so this never reads
        or hard-codes the prompt text.
        """
        lines = self._region_before_end(buffer, m)
        begin = self._begin_line(lines, m)
        rc_idx = [
            i for i in range(begin + 1, len(lines)) if _RETCODE_RE.search(lines[i])
        ]
        if len(rc_idx) < 2:
            # Need both BEGIN's and the command's code to bracket the output.
            return ""
        begin_rc, cmd_rc = rc_idx[0], rc_idx[-1]
        # Between the two codes: [prompt, <output...>] once empties are dropped.
        block = [ln for ln in lines[begin_rc + 1:cmd_rc] if ln.strip()]
        output = block[1:] if block else []  # drop the bracketing prompt
        return "\n".join(output).strip()
