"""
Unit tests for the shared ``custom_hosts`` module's ``ZephyrInlineRetcodeFrame``
— the Zephyr 2.7 dialect that reads the exit code from an inline ``retCode = <n>``
line (emitted by the 2.7 firmware patch) instead of the ``retval`` builtin.

These model the patched shell's stream so the parser can be validated without
a live 2.7 target; the live matrix verifies the model after the 2.7 firmware is
rebuilt with ``tests/firmware/zephyr/patches/v2_7-shell-retcode.patch``.
"""

import re
import sys
from pathlib import Path

import pytest

from otto.host.command_frame import SessionMarkers

# The shared custom_hosts module carries the frame (a third-party-style package,
# not otto core; see tests/custom_hosts/README.md).
_CUSTOM_HOSTS = Path(__file__).resolve().parents[2] / "custom_hosts"
if str(_CUSTOM_HOSTS) not in sys.path:
    sys.path.insert(0, str(_CUSTOM_HOSTS))
from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame  # noqa: E402

M = SessionMarkers.for_session("2700beef")
FRAME = ZephyrInlineRetcodeFrame()


def inline_response(output: str, retcode: int, prompt: str = "~$ ") -> str:
    """Model the patched 2.7 shell's reply to a BEGIN / cmd / END frame.

    Each executed line yields ``\\r\\n<result>\\r\\nretCode = <n>\\r\\n<prompt>``
    (input not echoed; the firmware patch prints the code before the prompt).
    The two markers are rejected as unknown commands (``retCode = -8``); the
    middle line is the real command.
    """
    body = "".join(f"{ln}\r\n" for ln in output.split("\n")) if output else ""
    return (
        f"\r\n{M.begin}: command not found\r\nretCode = -8\r\n{prompt}"
        f"\r\n{body}retCode = {retcode}\r\n{prompt}"
        f"\r\n{M.end_prefix}: command not found\r\nretCode = -8\r\n{prompt}"
    )


class TestFraming:

    def test_frame_drops_retval_line(self):
        # Three CR-separated lines: BEGIN / cmd / END — no `retval`.
        assert FRAME.frame("version", M) == f"{M.begin}\rversion\r{M.end_prefix}\r"

    def test_type_name(self):
        assert ZephyrInlineRetcodeFrame.type_name == "zephyr-inline"

    def test_handshake_disables_echo(self):
        # 2.7's telnet shell echoes input (3.7+ doesn't); the handshake must
        # turn echo off so the echoed END marker can't desync the read loop.
        assert FRAME.handshake(M) == f"shell echo off\r{M.ready}\n"

    def test_inherits_zephyr_end_pattern(self):
        assert FRAME.end_pattern(M).pattern == re.escape(M.end_prefix)


class TestRetcode:

    def test_success(self):
        buf = inline_response("Zephyr version 2.7.6", 0)
        assert FRAME.extract_retcode(buf, M) == 0

    def test_negative_errno(self):
        buf = inline_response("usage: ...", -22)
        assert FRAME.extract_retcode(buf, M) == -22

    def test_unknown_command_minus_eight(self):
        buf = inline_response("bogus: command not found", -8)
        assert FRAME.extract_retcode(buf, M) == -8

    def test_takes_commands_code_not_begin_markers(self):
        # BEGIN emits retCode = -8; the command's own code must win.
        buf = inline_response("ok", 3)
        assert FRAME.extract_retcode(buf, M) == 3

    def test_missing_retcode_is_minus_one(self):
        # An unpatched 2.7 build emits no retCode line at all.
        buf = (
            f"\r\n{M.begin}: command not found\r\n~$ "
            f"\r\nsome output\r\n~$ "
            f"\r\n{M.end_prefix}: command not found\r\n~$ "
        )
        assert FRAME.extract_retcode(buf, M) == -1


class TestOutput:

    def test_single_line(self):
        buf = inline_response("Zephyr version 2.7.6", 0)
        assert FRAME.parse_output(buf, "version", M) == "Zephyr version 2.7.6"

    def test_multiline(self):
        buf = inline_response("line one\nline two\nline three", 0)
        assert FRAME.parse_output(buf, "x", M) == "line one\nline two\nline three"

    def test_empty_output(self):
        buf = inline_response("", 0)
        assert FRAME.parse_output(buf, "kernel reboot", M) == ""

    def test_integer_output_not_swallowed(self):
        # A bare integer in the output must survive — only `retCode = N` lines
        # are treated as codes.
        buf = inline_response("123456", 0)
        assert FRAME.parse_output(buf, "kernel uptime", M) == "123456"

    def test_ansi_prompt_stripped(self):
        buf = inline_response("Zephyr version 2.7.6", 0, prompt="\x1b[1;32m~$ \x1b[m")
        assert FRAME.parse_output(buf, "version", M) == "Zephyr version 2.7.6"

    def test_no_marker_or_retcode_noise_leaks(self):
        buf = inline_response("clean line", 0)
        out = FRAME.parse_output(buf, "x", M)
        assert "__OTTO_" not in out
        assert "retCode" not in out
        assert "\x1b[" not in out

    def test_handshake_residue_before_begin_is_ignored(self):
        # Live-observed (Zephyr 2.7): the readiness handshake leaves the ready
        # marker's rejection — `: command not found` / `retCode = -8` / prompt —
        # ahead of the BEGIN marker in the first command's buffer. Anchoring on
        # the BEGIN marker (not the first retCode line) must keep it out of both
        # the parsed output and the retcode.
        residue = "\r\n: command not found\r\nretCode = -8\r\n~$ "
        buf = residue + inline_response("Zephyr version 2.7.6", 0)
        assert FRAME.parse_output(buf, "version", M) == "Zephyr version 2.7.6"
        assert FRAME.extract_retcode(buf, M) == 0
