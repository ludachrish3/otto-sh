"""A TAB in a command reaches the shell as a TAB, in every shell otto types into (#1224).

otto's remote sessions type a command into an interactive shell on a
terminal. A shell with a line editor takes TAB as its completion key: bash
dropped a TAB between two words and replaced ``/us<TAB>`` with ``/usr/``, so
a ``docker inspect --format '{{.Id}}<TAB>{{json .Mounts}}'`` ran without its
separator. Typed the way the frame spells it for a terminal
(:meth:`~otto.host.command_frame.CommandFrame.typed`), a TAB must arrive as
written, on the command line and in a here-document's body. Each shell runs
here for real, on its own controlling pty
(``tests/_fixtures/interactive_shell.py``).
"""

import re

import pytest

from otto.host.command_frame import BashFrame
from tests._fixtures.interactive_shell import (
    RC_EDITING,
    READY_TIMEOUT,
    SHELLS,
    TAIL_MARKERS,
    InteractiveShell,
    interactive_shell,
    probe_answer,
    render_probe,
)

pytestmark = pytest.mark.interpreter_agnostic

INTERACTIVE = [name for name, _ in SHELLS]

LINE_EDITING = [name for name in INTERACTIVE if name in ("bash", "zsh", "mksh")]
"""The shells of the matrix whose line editor takes a bare TAB as its completion key."""

_OD = re.compile(r"^(?:> )*((?: +\S+)+) *\r*$", re.MULTILINE)
"""An ``od -An -c`` row, after any continuation prompts a here-document left on its line."""


def _handshaken(shell: InteractiveShell) -> None:
    """Type the session's whole first line; wait for its answer and the prompt after it."""
    shell.type_line(
        render_probe("handshake", TAIL_MARKERS),
        probe_answer("handshake", TAIL_MARKERS),
        READY_TIMEOUT,
    )


def _bytes_seen(printed: str) -> "list[str]":
    """The tokens of the last ``od -An -c`` row in *printed* (every case here prints one row).

    The last, because a shell whose editor echoes the typed lines (mksh)
    prints the here-document's own body before the command's output.
    """
    rows = _OD.findall(printed)
    return rows[-1].split() if rows else []


@pytest.mark.parametrize("name", INTERACTIVE)
@pytest.mark.parametrize(
    ("typed", "want"),
    [
        pytest.param("a\tb", ["a", "\\t", "b"], id="between-words"),
        pytest.param("/us\tb", ["/", "u", "s", "\\t", "b"], id="after-a-completable-path"),
        pytest.param("\t\tx", ["\\t", "\\t", "x"], id="two-at-the-start"),
    ],
)
def test_a_tab_on_the_command_line_arrives_as_a_tab(name: str, typed: str, want: "list[str]"):
    """A quoted TAB in a typed command is one TAB byte in what the command receives."""
    with interactive_shell(name) as shell:
        _handshaken(shell)
        printed = shell.run(BashFrame().typed(cmd=f"printf '%s' '{typed}' | od -An -c"))
    assert _bytes_seen(printed) == want, f"{name}: {printed!r}"


@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tab_in_a_here_document_arrives_as_a_tab(name: str):
    """A here-document's body, typed line by line, keeps its TABs."""
    # In a group, so the here-document is read to its end before anything runs
    # and the output starts on a line of its own.
    script = "{ od -An -c <<'OTTO_EOF'\n\tx\ty\nOTTO_EOF\n}"
    with interactive_shell(name) as shell:
        _handshaken(shell)
        printed = shell.run(BashFrame().typed(cmd=script))
    assert _bytes_seen(printed) == ["\\t", "x", "\\t", "y", "\\n"], f"{name}: {printed!r}"


@pytest.mark.parametrize("name", LINE_EDITING)
def test_a_bare_tab_is_taken_as_a_completion_key_without_the_handshake(name: str):
    """The control: before otto's first line, a line-editing shell does not deliver a bare TAB."""
    with interactive_shell(name) as shell:
        printed = shell.run("printf '%s' '/us\tb' | od -An -c")
    seen = _bytes_seen(printed)
    assert seen, f"{name}: no od row in {printed!r}"
    assert "\\t" not in seen, f"{name}: {printed!r}"


@pytest.mark.parametrize(
    ("name", "editing"),
    [(name, "") for name in INTERACTIVE]
    + [(n, m) for n, m in (*RC_EDITING, ("zsh", "bindkey -v")) if n in INTERACTIVE],
)
def test_after_the_handshake_a_bare_tab_arrives_as_a_tab(name: str, editing: str):
    """The handshake makes TAB a plain character in each line editor (#1356).

    A line typed while the previous command runs meets the terminal's line
    discipline, which consumes the Ctrl-V the frame puts before a TAB; what
    the editor then reads is this bare TAB. In vi editing mode too, and
    whatever completion the user's rc file bound to TAB.
    """
    with interactive_shell(name) as shell:
        if editing:  # the user's rc file, before otto's first line
            shell.run(editing)
        _handshaken(shell)
        printed = shell.run("printf '%s' '/us\tb' | od -An -c")
    assert _bytes_seen(printed) == ["/", "u", "s", "\\t", "b"], f"{name}: {printed!r}"
