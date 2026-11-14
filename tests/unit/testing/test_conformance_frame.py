"""``assert_command_frame_conforms`` proves a frame reports a command's exact output."""

import pytest

from otto.host.command_frame import AshFrame, BashFrame
from otto.host.limitation import Limitation
from otto.testing import EXACT_OUTPUT_CASES, assert_command_frame_conforms
from tests._fixtures.shell_transcripts import bash_transcript


def test_the_bash_frame_passes_the_kit():
    assert_command_frame_conforms(BashFrame, transcript=bash_transcript)


def test_the_ash_frame_passes_the_kit():
    assert_command_frame_conforms(AshFrame, transcript=bash_transcript)


def test_the_kit_reports_a_frame_that_strips():
    class Stripping(BashFrame):
        type_name = "stripping"

        def parse_output(self, buffer, *, cmd, m):
            return super().parse_output(buffer, cmd=cmd, m=m).strip()

    with pytest.raises(AssertionError) as caught:
        assert_command_frame_conforms(Stripping, transcript=bash_transcript)
    assert repr("printf ' \\n'") in str(caught.value)


def test_the_kit_reports_a_wrong_retcode():
    class Wrong(BashFrame):
        type_name = "wrong"

        def extract_retcode(self, buffer, *, m):
            return 7

    with pytest.raises(AssertionError) as caught:
        assert_command_frame_conforms(Wrong, transcript=bash_transcript)
    assert repr("true") in str(caught.value)


def _trimmed(text: str) -> str:
    """What a console that trims each answer would hold instead of ``text``."""
    return text.strip()


class _Stripping(BashFrame):
    type_name = "declared-stripping"
    exact_output = Limitation(
        affects="exact output",
        kind="device",
        reason="the console trims each answer",
        measured_on=["a device"],
    )

    def parse_output(self, buffer, *, cmd, m):
        return super().parse_output(buffer, cmd=cmd, m=m).strip()


class BashFrameDeclaringNothingReal(BashFrame):
    type_name = "declaring-nothing-real"
    exact_output = Limitation(affects="a promise", kind="device", reason="r", measured_on=["x"])


def test_a_declared_limitation_needs_holds_instead_and_a_witness():
    with pytest.raises(AssertionError, match="holds_instead"):
        assert_command_frame_conforms(_Stripping, transcript=bash_transcript)
    # What the frame holds instead, and a witness among the cases: it passes.
    assert_command_frame_conforms(
        _Stripping, transcript=bash_transcript, holds_instead=lambda case: _trimmed(case.value)
    )
    # A limitation every case contradicts: no case differs, so it is refused.
    with pytest.raises(AssertionError, match="witness"):
        assert_command_frame_conforms(
            BashFrameDeclaringNothingReal,
            transcript=bash_transcript,
            holds_instead=lambda case: case.value,
        )


def test_holds_instead_is_refused_for_a_frame_without_a_limitation():
    with pytest.raises(AssertionError, match="holds_instead"):
        assert_command_frame_conforms(
            BashFrame, transcript=bash_transcript, holds_instead=lambda case: case.value
        )


def test_the_table_has_a_case_the_stripping_frame_breaks():
    assert any(case.value != _trimmed(case.value) for case in EXACT_OUTPUT_CASES)
