r"""Each frame's transcript model against the recorded bed, and the Zephyr frames against the kit.

``shell_transcripts/*.json`` holds what each bed shell really sent for each
framed command (``make shell-transcripts``).
``tests/_fixtures/shell_transcripts.py`` loads it and models it per frame; the
kit (:func:`~otto.testing.assert_command_frame_conforms`) drives the frames
through those models.
"""

import pytest

from otto.host.binary_loader import LlextHexLoader
from otto.host.command_frame import (
    BashFrame,
    SessionMarkers,
    ZephyrFrame,
    ZephyrSerialFrame,
    command_value,
    strip_zephyr_levels,
)
from otto.testing import OutputCase, assert_command_frame_conforms
from tests._fixtures.paths import ensure_custom_hosts_on_path
from tests._fixtures.shell_transcripts import (
    ZEPHYR_EXTRA_COMMANDS,
    ZEPHYR_MODELS,
    bash_recorded,
    recording_markers,
    recordings,
    zephyr_inline_transcript,
    zephyr_output,
    zephyr_recorded_output,
    zephyr_serial_transcript,
    zephyr_transcript,
)

ensure_custom_hosts_on_path()
from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

M = SessionMarkers.for_session(session_id="5e55a1e0")

_ZEPHYR_FRAMES = {
    "zephyr": ZephyrFrame,
    "zephyr-serial": ZephyrSerialFrame,
    "zephyr-inline": ZephyrInlineRetcodeFrame,
}


@pytest.mark.parametrize("stem", sorted(recordings()))
def test_each_model_reproduces_its_recorded_transcript(stem):
    r"""The frame's model, given a recorded command's output and exit code, rebuilds its buffer.

    What is circular: no recording states the program's output on its own (the
    recorded ``value`` is what otto returned then, trimmed), so it is derived
    from the buffer, positionally — between BEGIN's lines and the model's tail
    — and that derivation shares the model's view of where the output starts.

    What is not: the model then rebuilds every byte of the buffer, head and
    tail included (the previous END's rejection, each prompt, BEGIN's
    rejection, ``retval`` / ``retCode``, the colours, END), from the output,
    the exit code and the markers alone, for every recorded command; the
    derived output must have the README's shape (every line ending ``\r\n``,
    no other ``\r``, the error wrapper only around error output); and the
    frame must parse each recorded buffer to the derived output's value and
    the recorded exit code.
    """
    data = recordings()[stem]
    m = recording_markers(data)
    assert data["markers"] == {"begin": m.begin, "end_prefix": m.end_prefix}
    assert data["commands"], data["element"]
    for i, entry in enumerate(data["commands"]):
        buffer, rc = entry["buffer"], entry["retcode"]
        if data["frame"] == "bash":
            begin = buffer.index(m.begin)
            start = begin + len(m.begin) + 1
            end = buffer.index(m.end_prefix, start)
            output = buffer[start:end]
            head = buffer[:begin].removesuffix("> ")
            ps1 = head.removeprefix("\n")
            assert bash_recorded(output, retcode=rc, m=m, ps1=ps1, first=i == 0) == buffer
            frame = BashFrame()
            assert frame.parse_output(buffer, cmd=entry["cmd"], m=m) == command_value(output)
            assert command_value(output) == entry["value"]
        else:
            model = ZEPHYR_MODELS[data["frame"]]
            recorded = zephyr_recorded_output(data, entry)
            text = recorded.text
            rebuilt = model.build(recorded.sent, retcode=rc, m=m, prompt=model.prompt)
            assert rebuilt == buffer, entry["cmd"]
            if not recorded.levels:
                assert recorded.sent == zephyr_output(text, error=False), entry["cmd"]
            frame = _ZEPHYR_FRAMES[data["frame"]]()
            assert frame.parse_output(buffer, cmd=entry["cmd"], m=m) == command_value(text)
        assert frame.extract_retcode(buffer, m=m) == rc
        assert buffer == entry["raw"] or data["terminal_mapping"] == "onlcr"


def test_the_zephyr_frame_passes_the_kit():
    assert_command_frame_conforms(ZephyrFrame, transcript=zephyr_transcript)


def test_the_zephyr_serial_frame_passes_the_kit():
    assert_command_frame_conforms(ZephyrSerialFrame, transcript=zephyr_serial_transcript)


def test_the_custom_zephyr_frame_passes_the_kit():
    assert_command_frame_conforms(ZephyrInlineRetcodeFrame, transcript=zephyr_inline_transcript)


def test_a_blank_line_and_trailing_spaces_survive():
    buf = zephyr_transcript(OutputCase(id="x", command="x", writes=b"\na  \n\n"), M)
    assert ZephyrFrame().parse_output(buf, cmd="x", m=M) == "\na  \n"


def test_the_cursor_moves_in_help_survive():
    case = OutputCase(id="help", command="help", writes=b"  devmem : x\n\x1b[17CUsage:\n")
    buf = zephyr_transcript(case, M)
    assert ZephyrFrame().parse_output(buf, cmd="help", m=M) == "  devmem : x\n\x1b[17CUsage:"


#: Each message-level shape the bed sent, and the value the README's rule gives
#: it: every colour and reset the shell adds removed wherever it falls, ``\r\n``
#: read as ``\n``, then one final newline dropped. Written from the recorded
#: ``raw`` bytes by hand, never from a frame.
_LEVEL_VALUES = {
    "fs cat /nonexistent /nonexistent2": (
        "Failed to obtain file /nonexistent (err: -2)\n"
        "Failed to obtain file /nonexistent2 (err: -2)"
    ),
    "log enable inf otto_nope1 otto_nope2": (
        "otto_nope1: unknown source name.\notto_nope2: unknown source name."
    ),
    "dynamic show": "Please add some commands first.",
    "llext call_fn otto_nope otto_nope": "No such extension otto_nope",
}

_WARN_THEN_ERROR = (
    "This command is deprecated as it is a duplicate. "
    "Use 'log enable' command from logging commands set.\n"
    "Unable to find log source: otto_nope"
)


def _recorded_extra_entries() -> "list[pytest.ParameterSet]":
    return [
        pytest.param(stem, entry["cmd"], id=f"{stem}-{entry['cmd']}")
        for stem, data in recordings().items()
        if data["frame"] in ZEPHYR_MODELS
        for entry in data["commands"]
        if entry["cmd"] in ZEPHYR_EXTRA_COMMANDS
    ]


@pytest.mark.parametrize(("stem", "cmd"), _recorded_extra_entries())
def test_every_message_level_wrapper_is_removed_wherever_it_falls(stem, cmd):
    data = recordings()[stem]
    entry = next(one for one in data["commands"] if one["cmd"] == cmd)
    m = recording_markers(data)
    value = _ZEPHYR_FRAMES[data["frame"]]().parse_output(entry["buffer"], cmd=cmd, m=m)
    for colour in ["\x1b[1;31m", "\x1b[1;33m", "\x1b[m"]:
        assert colour not in value
    if cmd in _LEVEL_VALUES:
        assert value == _LEVEL_VALUES[cmd]
    elif cmd == "kernel log_level otto_nope 1" and data["release"] == "zephyr-4.4":
        assert value == _WARN_THEN_ERROR
    elif cmd == "devmem" and data["release"] != "zephyr-2.7":
        # An error line, then the command's help: the formatter's cursor moves stay.
        assert value.startswith(
            "devmem: wrong parameter count\ndevmem - Read/write physical memory\n\x1b[9CUsage:\n"
        )


def test_the_absent_extension_line_after_other_output_reports_not_loaded():
    """The consumer reads ``No such extension`` wherever the line falls in a value."""
    recorded = next(
        entry["value"]
        for data in recordings().values()
        for entry in data["commands"]
        if entry["cmd"] == "llext call_fn otto_nope otto_nope"
    )
    loader = LlextHexLoader()
    assert loader.reports_not_loaded(f"ok\n{recorded}", "otto_nope")
    # A build that prints the line through `shell_error`: the frame, not the
    # loader, removes the colouring, so the loader reads the frame's value.
    coloured = f"ok\r\n\x1b[1;31m{recorded}\r\n\x1b[m"
    assert not loader.reports_not_loaded(coloured, "otto_nope")
    assert loader.reports_not_loaded(strip_zephyr_levels(coloured), "otto_nope")
    assert not loader.reports_not_loaded(f"ok\n{recorded}", "otto_no")
    assert not loader.reports_not_loaded(f"ok\n{recorded}_two", "otto_nope")
