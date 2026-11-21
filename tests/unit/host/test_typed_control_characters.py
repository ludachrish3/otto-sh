"""A control character in a typed command is refused by name, never typed (#1224).

A session on a terminal types the command into the shell's line reader. Bash
was measured through ``run()``: Ctrl-C, Ctrl-R, Ctrl-U and ESC hung the
command until its timeout, Ctrl-W, DEL and backspace erased what came before
them, Ctrl-D and Ctrl-V vanished and a carriage return became a newline. A
keystroke belongs in ``send()``; a byte meant as data is spelled in the
command. A newline ends a line and a TAB is typed literally
(``test_typed_tab.py``), so neither is refused.
"""

import pytest

from otto.host.command_frame import LITERAL_NEXT, AshFrame, BashFrame, ZephyrFrame
from otto.host.errors import UntypableCommandError
from otto.host.session import SessionMarkers, refuse_if_terminal_would_act_on, render_payload
from tests.unit.host.test_run_line_length import _manager

M = SessionMarkers.for_session(session_id="abcd1234")

_KEYS = [
    pytest.param("\x03", "Ctrl-C (0x03)", id="ctrl-c"),
    pytest.param("\x1b", "ESC (0x1b)", id="esc"),
    pytest.param("\x7f", "DEL (0x7f)", id="del"),
    pytest.param("\r", "a carriage return (0x0d)", id="cr"),
]


@pytest.mark.parametrize(("key", "name"), _KEYS)
def test_a_control_character_is_refused_by_name(key: str, name: str):
    """The refusal names the host, the character, ``send()`` and the spelled form."""
    with pytest.raises(UntypableCommandError) as refused:
        refuse_if_terminal_would_act_on(AshFrame(), f"echo a{key}b", terminal=True, host="bb1")
    message = str(refused.value)
    assert message.startswith("bb1: ")
    assert name in message
    assert "`send()`" in message
    assert f"printf '\\{ord(key):03o}'" in message


def test_every_character_found_is_named_once():
    """Two different keys, one of them twice, are named once each, in code order."""
    with pytest.raises(UntypableCommandError) as refused:
        refuse_if_terminal_would_act_on(BashFrame(), "a\x1bb\x03c\x1b", terminal=True)
    assert "Ctrl-C (0x03), ESC (0x1b)" in str(refused.value)


@pytest.mark.parametrize(
    "cmd",
    ["echo plain", "printf 'a\tb'", "cat <<'E'\n\tline\nE", "echo é", "echo a\r\necho b\r\n"],
    ids=["plain", "tab", "newlines-and-tabs", "utf8", "crlf-line-endings"],
)
def test_a_command_without_such_a_character_is_not_refused(cmd: str):
    """A newline, a TAB, non-ASCII text and CRLF line endings are all typed."""
    refuse_if_terminal_would_act_on(BashFrame(), cmd, terminal=True)


def test_a_session_on_a_pipe_is_not_refused():
    """A pipe delivers every byte, so there is nothing to refuse."""
    refuse_if_terminal_would_act_on(BashFrame(), "echo a\x03b", terminal=False)


def test_another_dialect_is_not_refused():
    """A Zephyr shell's console is not a POSIX line reader and is not measured."""
    refuse_if_terminal_would_act_on(ZephyrFrame(), "echo a\x03b", terminal=True)


def test_a_tab_is_typed_after_literal_next_at_a_terminal():
    """At a terminal each TAB of the command is typed after Ctrl-V."""
    typed = render_payload(BashFrame(), "printf 'a\tb\tc'", m=M, exec_input=None, terminal=True)
    assert f"printf 'a{LITERAL_NEXT}\tb{LITERAL_NEXT}\tc'" in typed


def test_a_tab_is_written_bare_to_a_pipe():
    """A pipe has no line reader: the command's bytes are written as they are."""
    written = render_payload(BashFrame(), "printf 'a\tb'", m=M, exec_input=None, terminal=False)
    assert "printf 'a\tb'" in written
    assert LITERAL_NEXT not in written


def test_a_zephyr_command_is_typed_as_written():
    """Only the POSIX dialects spell a TAB; another dialect's command is unchanged."""
    typed = render_payload(ZephyrFrame(), "echo a\tb", m=M, exec_input=None, terminal=True)
    assert LITERAL_NEXT not in typed


@pytest.mark.asyncio
async def test_run_refuses_before_the_session_is_asked_to_run_anything():
    """Through ``run_cmd``: the refusal names the host and the session runs nothing."""
    mgr, sessions = _manager(BashFrame(), name="bb1")
    with pytest.raises(UntypableCommandError, match=r"^bb1: .*Ctrl-C \(0x03\)"):
        await mgr.run_cmd(cmd="echo a\x03b")
    assert [session.ran for session in sessions] == [[]]


def test_a_comment_indented_with_a_tab_is_framed_as_a_comment():
    """The frame reads the command as written, so a TAB-led comment still gets its placeholder."""
    bare = render_payload(BashFrame(), "\t# note", m=M, exec_input=None, terminal=False)
    typed = render_payload(BashFrame(), "\t# note", m=M, exec_input=None, terminal=True)
    assert typed == bare.replace("\t", LITERAL_NEXT + "\t")
