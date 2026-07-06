"""
Unit tests for the :class:`~otto.host.command_frame.CommandFrame` value
objects — bash and Zephyr dialects — and the registry.

These test the frames *standalone* (no session), with a fixed
:class:`SessionMarkers` so expected strings are concrete. End-to-end coverage
(frame driven by a real session's read loop) lives in test_session.py (bash)
and test_zephyr.py (Zephyr).
"""

import dataclasses
import re

import pytest

from otto.host.command_frame import (
    BashFrame,
    CommandFrame,
    SessionMarkers,
    ZephyrFrame,
    ZephyrSerialFrame,
    build_command_frame,
    register_command_frame,
)

M = SessionMarkers.for_session("cafef00d")


class TestSessionMarkers:
    def test_for_session_derives_all_four_tokens(self):
        m = SessionMarkers.for_session("abc123")
        assert m.begin == "__OTTO_abc123_BEGIN__"
        assert m.end_prefix == "__OTTO_abc123_END__"
        assert m.ready == "__OTTO_abc123_READY__"
        assert m.recover == "__OTTO_abc123_RECOVER__"

    def test_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            M.begin = "nope"  # type: ignore[misc]


class TestBashFrame:
    frame = BashFrame()

    def test_type_name(self):
        assert BashFrame.type_name == "bash"

    def test_handshake_silences_echo_and_prints_ready(self):
        assert self.frame.handshake(M) == f"stty -echo 2>/dev/null; echo {M.ready}\n"

    def test_frame_brackets_command_and_bakes_retcode_into_end(self):
        assert self.frame.frame("ls /tmp", M) == (
            f'echo "{M.begin}"; ls /tmp; echo "{M.end_prefix}$?__"\n'
        )

    def test_recover_is_exit_code_probe(self):
        assert self.frame.recover(M) == f'echo "{M.recover}$?__"\n'

    def test_end_pattern_captures_retcode_digits(self):
        pat = self.frame.end_pattern(M)
        assert pat.search(f"{M.end_prefix}127__").group(1) == "127"

    def test_extract_retcode_reads_end_marker_digits(self):
        buf = f"{M.begin}\nhi\n{M.end_prefix}0__\n"
        assert self.frame.extract_retcode(buf, M) == 0
        buf_fail = f"{M.begin}\nboom\n{M.end_prefix}127__\n"
        assert self.frame.extract_retcode(buf_fail, M) == 127

    def test_extract_retcode_missing_marker_is_minus_one(self):
        assert self.frame.extract_retcode("no markers here", M) == -1

    def test_marks_begin_on_own_line_or_suffix(self):
        assert self.frame.marks_begin(f"{M.begin}\r\n", M)
        assert self.frame.marks_begin(f"$ {M.begin}", M)  # prompt prefix
        assert not self.frame.marks_begin("unrelated output", M)

    def test_parse_output_slices_between_markers(self):
        buf = f"{M.begin}\nline one\nline two\n{M.end_prefix}0__\n"
        assert self.frame.parse_output(buf, "cmd", M) == "line one\nline two"

    def test_parse_output_uses_last_begin_marker(self):
        # Echoed wrapped command means the marker appears twice; rfind wins.
        buf = (
            f'echo "{M.begin}"; cmd\n'  # echoed command line (false marker)
            f"{M.begin}\nreal output\n{M.end_prefix}0__\n"
        )
        assert self.frame.parse_output(buf, "cmd", M) == "real output"

    def test_streams_output_live_is_true(self):
        # Bash with echo off emits a clean line-by-line stream, so it streams live.
        assert BashFrame.streams_output_live is True


class TestZephyrFrame:
    frame = ZephyrFrame()

    def test_type_name(self):
        assert ZephyrFrame.type_name == "zephyr"

    def test_handshake_is_bare_ready_marker(self):
        assert self.frame.handshake(M) == f"{M.ready}\n"

    def test_frame_is_four_cr_separated_lines_with_retval(self):
        assert self.frame.frame("kernel version", M) == (
            f"{M.begin}\rkernel version\rretval\r{M.end_prefix}\r"
        )

    def test_end_pattern_is_bare_token_no_group(self):
        assert self.frame.end_pattern(M).pattern == re.escape(M.end_prefix)

    def test_marks_begin_is_substring(self):
        assert self.frame.marks_begin(f"{M.begin}: command not found", M)
        assert not self.frame.marks_begin("nope", M)

    def test_streams_output_live_is_false(self):
        # The Zephyr stream interleaves prompts + a `retval` line, so it must be
        # buffered and emitted via parse_output rather than streamed raw.
        assert ZephyrFrame.streams_output_live is False

    def test_extract_retcode_reads_retval_integer_line(self):
        # Modelled on the real shell: prompt after each executed line.
        buf = (
            f"\r\n{M.begin}: command not found\r\n~$ "
            f"\r\nZephyr version 3.7.2\r\n~$ "
            f"\r\n-8\r\n~$ "
            f"\r\n{M.end_prefix}: command not found\r\n~$ "
        )
        assert self.frame.extract_retcode(buf, M) == -8

    def test_parse_output_drops_bracketing_prompts(self):
        buf = (
            f"\r\n{M.begin}: command not found\r\n~$ "
            f"\r\nZephyr version 3.7.2\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{M.end_prefix}: command not found\r\n~$ "
        )
        assert self.frame.parse_output(buf, "version", M) == "Zephyr version 3.7.2"


class TestZephyrSerialFrame:
    """The 3.7 dialect for a serial/UART shell behind a ``-serial telnet:``
    bridge: identical framing to ZephyrFrame, but the handshake disables the
    shell's input echo (the bridge swallows otto's ``IAC DONT ECHO``, so unlike
    the in-guest telnet backend the UART shell never turns echo off itself).
    """

    frame = ZephyrSerialFrame()

    def test_type_name(self):
        assert ZephyrSerialFrame.type_name == "zephyr-serial"

    def test_handshake_disables_echo_then_prints_ready(self):
        assert self.frame.handshake(M) == f"shell echo off\r{M.ready}\n"

    def test_inherits_retval_framing_from_zephyr(self):
        assert self.frame.frame("kernel version", M) == (
            f"{M.begin}\rkernel version\rretval\r{M.end_prefix}\r"
        )

    def test_inherits_retcode_parse_from_zephyr(self):
        buf = (
            f"\r\n{M.begin}: command not found\r\n~$ "
            f"\r\nZephyr version 3.7.2\r\n~$ "
            f"\r\n0\r\n~$ "
            f"\r\n{M.end_prefix}: command not found\r\n~$ "
        )
        assert self.frame.extract_retcode(buf, M) == 0

    def test_inherits_buffered_streaming(self):
        assert ZephyrSerialFrame.streams_output_live is False


class TestRecoverProbe:
    bash = BashFrame()
    zephyr = ZephyrFrame()

    def test_bash_recover_is_exit_code_probe(self):
        assert self.bash.recover(M) == f'echo "{M.recover}$?__"\n'

    def test_bash_recover_pattern_matches_digit_form(self):
        pat = self.bash.recover_pattern(M)
        assert pat.search(f"{M.recover}0__")
        assert pat.search(f"prompt$ {M.recover}130__")

    def test_bash_recover_pattern_rejects_echoed_literal_probe(self):
        # An echo/REPL reflects the probe text verbatim: literal "$?", no digits.
        pat = self.bash.recover_pattern(M)
        assert pat.search(f'echo "{M.recover}$?__"') is None

    def test_bash_recover_pattern_distinct_from_end_marker(self):
        # The whole point of the RECOVER marker: a dying command's own
        # compound-line tail echo (`{end_prefix}<code>__`) must NOT be mistaken
        # for the recovery probe's reply.
        pat = self.bash.recover_pattern(M)
        assert pat.search(f"{M.end_prefix}0__") is None

    def test_zephyr_recover_pattern_matches_bare_token(self):
        pat = self.zephyr.recover_pattern(M)
        assert pat.search(f"{M.recover}")
        assert pat.search(f"{M.recover}: command not found")

    def test_zephyr_serial_inherits_recover_pattern(self):
        assert ZephyrSerialFrame().recover_pattern(M).pattern == re.escape(M.recover)


class TestRegistry:
    def test_stock_frames_resolve_by_name(self):
        assert isinstance(build_command_frame("bash"), BashFrame)
        assert isinstance(build_command_frame("zephyr"), ZephyrFrame)
        assert isinstance(build_command_frame("zephyr-serial"), ZephyrSerialFrame)

    def test_unknown_frame_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown command frame"):
            build_command_frame("does-not-exist")

    def test_register_then_build(self):
        class CustomFrame(ZephyrFrame):
            type_name = "custom-frame-test"

        register_command_frame("custom-frame-test", CustomFrame)
        assert isinstance(build_command_frame("custom-frame-test"), CustomFrame)

    def test_register_rejects_name_mismatch(self):
        class Mismatch(ZephyrFrame):
            type_name = "right-name"

        with pytest.raises(ValueError, match="doesn't match"):
            register_command_frame("wrong-name", Mismatch)

    def test_is_a_command_frame(self):
        assert isinstance(build_command_frame("bash"), CommandFrame)


def test_builtins_registered_via_public_path():
    from otto.host import command_frame as cf

    # The seed registry starts empty and is populated by _register_builtin_frames()
    # through register_command_frame — the same path third parties use.
    assert set(cf.FRAME_CLASSES.names()) >= {"bash", "zephyr", "zephyr-serial"}
    assert cf.build_command_frame("bash").type_name == "bash"
