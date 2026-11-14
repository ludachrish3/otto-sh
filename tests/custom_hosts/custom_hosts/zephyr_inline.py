"""
A custom :class:`~otto.host.command_frame.CommandFrame` for Zephyr **2.7**
targets — the worked example of registering a custom shell dialect from an
out-of-tree module (the extension path otto's ``register_command_frame`` exists
for). It lives in the shared ``custom_hosts`` package rather than any one
project, so every project whose lab includes a 2.7 host can depend on it the way a
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
- **Output** is the text between the prompt after the BEGIN marker's
  ``retCode`` line and the command's ``retCode``, found positionally (the
  prompt text is never matched — same principle as the stock frame), and
  reported exactly: only what the shell adds is removed.

Everything else — the readiness handshake, recovery, begin-marker detection,
the END pattern, the removal of the shell's own bytes — is inherited from :class:`ZephyrFrame`
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

    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        # Unlike 3.7+, the Zephyr 2.7 telnet shell echoes input by default.
        # Left on, the echoed END marker would match otto's read loop *before*
        # the command's real output arrives, desyncing every command by one.
        # `shell echo off` is a stock 2.7 builtin; disable echo once, up front,
        # so the session then behaves like the non-echoing 3.7 shell the parser
        # assumes. The readiness marker (rejected as an unknown command) still
        # echoes back via the shell's error handler — that is shell *output*,
        # not input echo, so the handshake probe is unaffected.
        return f"{prefix}shell echo off\r{m.ready}\n"

    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
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

    def extract_retcode(self, buffer: str, *, m: SessionMarkers) -> int:
        """Return the command's exit code: the last ``retCode = <n>`` after the
        BEGIN marker.

        The rejected BEGIN marker emits its own ``retCode = -8`` first, so the
        *last* match is the real command's. ``-1`` if none is found (e.g. an
        unpatched 2.7 build — a clear signal the firmware patch is missing).
        """
        lines = self.region_before_end(buffer, m=m)
        begin = self._begin_line(lines, m)
        for ln in reversed(lines[begin + 1 :]):
            match = _RETCODE_RE.search(ln)
            if match:
                return int(match.group(1))
        return -1

    def parse_output(self, buffer: str, *, cmd: str, m: SessionMarkers) -> str:
        r"""Return the command's exact output.

        Region layout after the BEGIN marker (prompt printed after each
        executed line; echo is disabled in the handshake; every line ends
        ``\r\n``)::

            <BEGIN>: command not found
            retCode = -8          <- BEGIN's code (first retCode after BEGIN)
            <prompt>
            <command output...>
            retCode = <n>         <- command's code (last retCode after BEGIN)
            <prompt>

        The output is everything between the prompt after BEGIN's code and
        the command's ``retCode``: the three shell lines after BEGIN are
        skipped positionally, so the prompt text is never read or hard-coded.
        :meth:`~otto.host.command_frame.ZephyrFrame.output_value` then removes
        only what the shell adds; blank lines and escapes the command wrote
        are kept.
        """
        lines = self.region_before_end(buffer, m=m)
        begin = self._begin_line(lines, m)
        codes = [
            (i, code)
            for i in range(begin + 1, len(lines))
            if (code := _RETCODE_RE.search(lines[i])) is not None
        ]
        if len(codes) < 2:
            # Need both BEGIN's and the command's code to bracket the output.
            return ""
        # The command's own code ends its output, even mid-line.
        cmd_rc, code = codes[-1]
        region = "\r\n".join([*lines[:cmd_rc], lines[cmd_rc][: code.start()]])
        return self.output_value(region, m=m, lines_after_begin=3)
