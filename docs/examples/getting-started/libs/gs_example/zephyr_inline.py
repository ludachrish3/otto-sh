"""A custom :class:`~otto.host.command_frame.CommandFrame` for Zephyr 2.7 targets.

Zephyr 2.7 predates the ``retval`` shell builtin otto's stock
:class:`~otto.host.command_frame.ZephyrFrame` reads the exit code from. otto's
2.7 firmware carries a one-line source patch that prints ``retCode = <n>``
after every command; this frame parses that line instead.

otto's own test bed runs this exact frame, and a guard in otto's test suite
keeps the two copies identical — so this file may be copied into a project as
it stands.
"""

import re

from otto.host.command_frame import SessionMarkers, ZephyrFrame

# The inline return-code line the 2.7 firmware patch emits after every command.
# Tolerant of surrounding whitespace; the signed integer is captured.
_RETCODE_RE = re.compile(r"retCode\s*=\s*(-?\d+)")


class ZephyrInlineRetcodeFrame(ZephyrFrame):
    """Zephyr 2.7 dialect: exit code read from an inline ``retCode = <n>`` line.

    Used instead of the ``retval`` builtin, which 2.7 lacks.
    """

    type_name = "zephyr-inline"
    shell_lines_after_begin = 3  # the BEGIN rejection, its retCode line, the prompt

    def handshake(self, m: SessionMarkers, *, prefix: str = "") -> str:
        """Disable shell echo, then emit the readiness probe.

        Unlike 3.7+, the Zephyr 2.7 telnet shell echoes input by default. Left
        on, the echoed END marker would match otto's read loop *before* the
        command's real output arrives, desyncing every command by one.
        ``shell echo off`` is a stock 2.7 builtin; disable echo once, up
        front, so the session then behaves like the non-echoing 3.7 shell the
        parser assumes. The readiness marker (rejected as an unknown command)
        still echoes back via the shell's error handler — that is shell
        *output*, not input echo, so the handshake probe is unaffected.
        """
        return f"{prefix}shell echo off\r{m.ready}\n"

    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
        """Bracket ``cmd`` with the BEGIN/END sentinels; no ``retval`` line.

        Three CR-separated lines — every command self-reports its code via
        the firmware patch, so the bracketing markers are enough: BEGIN ->
        rejected, emits ``<token>: command not found`` + ``retCode=-8``; cmd
        -> output + ``retCode=<n>`` (the code we want); END -> rejected (its
        retCode lands after the END token, ignored).
        """
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
        """Return the command's exit code.

        The last ``retCode = <n>`` line after the BEGIN marker. The rejected
        BEGIN marker emits its own ``retCode = -8`` first, so the *last* match
        is the real command's. ``-1`` if none is found (e.g. an unpatched 2.7
        build — a clear signal the firmware patch is missing).
        """
        lines = self.region_before_end(buffer, m=m)
        begin = self._begin_line(lines, m)
        for ln in reversed(lines[begin + 1 :]):
            match = _RETCODE_RE.search(ln)
            if match:
                return int(match.group(1))
        return -1

    def parse_output(
        self,
        buffer: str,
        *,
        cmd: str,  # noqa: ARG002 -- override signature; the AST guard forbids a rename
        m: SessionMarkers,
    ) -> str:
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
        if len(codes) < 2:  # noqa: PLR2004 -- arity check; the AST guard forbids a named constant
            # Need both BEGIN's and the command's code to bracket the output.
            return ""
        # The command's own code ends its output, even mid-line.
        cmd_rc, code = codes[-1]
        region = "\r\n".join([*lines[:cmd_rc], lines[cmd_rc][: code.start()]])
        return self.output_value(region, m=m, lines_after_begin=self.shell_lines_after_begin)
