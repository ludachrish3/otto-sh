"""The bash/ash frame holds in every shell otto frames for (#678).

The frame types ``echo BEGIN; { <cmd>`` and closes the group on the next line
with ``}; echo END``, so a command may end in ``&`` or a ``# comment`` and the
END marker still prints. That is only safe if every shell behind a
:class:`BashFrame` or :class:`AshFrame` session parses it the same way, and
prints its continuation prompt before BEGIN rather than inside the output.
otto's remote sessions are interactive pty shells (bash over SSH, dash or
busybox ``sh`` in a container, busybox ``ash`` over telnet); the local session
is non-interactive ``bash`` on pipes. Each one runs here, for real, on the
framed text otto would type, with stderr folded into stdout as a pty folds it,
so a prompt that leaked into the output would be seen.

The busybox here is the dev VM's; the five busybox versions the conformance
matrix carries are measured on the bed by
``tests/integration/busybox_bed/test_session_frame.py``.
"""

import re
import shutil
import subprocess
from typing import NamedTuple

import pytest

from otto.host.command_frame import AshFrame, BashFrame
from otto.host.session import SessionMarkers
from tests.conformance._vocabulary import POSIX

pytestmark = pytest.mark.interpreter_agnostic

M = SessionMarkers.for_session(session_id="abcd1234")

# Always present wherever otto's own suite runs; a missing one is a broken
# runner and fails.
_REQUIRED = [
    pytest.param(["bash", "-i"], BashFrame(), id="bash-interactive"),
    pytest.param(["bash"], BashFrame(), id="bash-local-session"),
    pytest.param(["sh", "-i"], BashFrame(), id="sh-interactive"),
]

# Tested when installed, as in test_history_suppression_portability: CI's
# general lanes have no busybox (only its busybox lane installs one), and a
# runner with more shells simply gets more coverage. Which ones ran is visible
# in the test ids.
_OPTIONAL = [
    pytest.param(["dash", "-i"], BashFrame(), id="dash-interactive"),
    pytest.param(["busybox", "sh", "-i"], AshFrame(), id="busybox-ash-interactive"),
]

SHELLS = _REQUIRED + [p for p in _OPTIONAL if shutil.which(p.values[0][0]) is not None]


class FrameCase(NamedTuple):
    """A command whose shape once broke the frame (#678), and what it must report."""

    text: str
    expected: str
    code: int


FRAME_EDGE_CASES: list[FrameCase] = [
    FrameCase("sleep 0 >/dev/null 2>&1 &", "", 0),  # a trailing `&` spliced into `&;`
    FrameCase("echo hi # a note", "hi", 0),  # a trailing comment hid the END echo
    FrameCase("# just a note", "", 0),  # an empty group is a syntax error
    FrameCase("echo a \\", "a", 0),  # a trailing backslash joined the closing line
    FrameCase("""echo "it's" 'a "test"' $((1 + 2))""", 'it\'s a "test" 3', 0),
    FrameCase("echo one\necho two", "one\ntwo", 0),
    FrameCase("cat <<EOF\nhere\nEOF", "here", 0),
]
"""The frame's edge cases, shared by every frame that types a command into a shell
(``BashFrame.frame`` here, ``BashFrame.frame_exec`` in ``test_exec_frame.py``)."""

_JOB_ANNOUNCEMENT = re.compile(r"\A\[\d+\] \d+(\n|\Z)")
"""What an interactive shell with job control prints for a job it backgrounds."""


def _run(
    argv: "list[str]", frame: BashFrame, *cmds: str, new_session: bool = False
) -> "list[tuple[str, int]]":
    """Type each framed command into one shell; return each one's (output, exit code).

    *new_session* starts the shell with no controlling terminal, so a runner
    launched from a terminal does not hand its ``/dev/tty`` to the commands.
    """
    if shutil.which(argv[0]) is None:
        pytest.fail(f"{argv[0]} is not installed on this machine")
    script = "".join(frame.frame(c, m=M) for c in cmds)
    done = subprocess.run(
        argv,
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "PS1": "PROMPT$ ", "PS2": "MORE> ", "HOME": "/tmp"},
        check=False,
        start_new_session=new_session,
    )
    out = done.stdout
    results = []
    for _ in cmds:
        end = frame.end_pattern(M).search(out)
        assert end is not None, f"no END marker in:\n{out}"
        chunk, out = out[: end.end()], out[end.end() :]
        results.append((frame.parse_output(chunk, cmd="", m=M), frame.extract_retcode(chunk, m=M)))
    return results


@pytest.mark.parametrize("case", FRAME_EDGE_CASES)
@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_every_frame_edge_case_frames(argv, frame, case):
    """A command ending in ``&`` or a comment, a comment only, a trailing backslash,
    quotes, several lines, a heredoc: each left the call waiting out its timeout or
    corrupted its output once. Each is followed by a command, so a group that never
    closed would swallow it."""
    edge, after = _run(argv, frame, case.text, "echo next")
    # An interactive shell with job control announces a job it backgrounds
    # (`[1] 4242`), as it would to anyone typing the command; nothing else may appear.
    assert (_JOB_ANNOUNCEMENT.sub("", edge[0]), edge[1]) == (case.expected, case.code)
    assert after == ("next", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_state_survives_the_frame(argv, frame):
    cd, where = _run(argv, frame, "cd /tmp && X=kept", 'pwd; echo "$X"')
    assert cd[1] == 0
    assert where == ("/tmp\nkept", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_the_exit_code_is_the_command_s_own(argv, frame):
    fails, sets, empty = _run(argv, frame, "false", "sh -c 'exit 7'", "")
    assert fails[1] == 1
    assert sets[1] == 7
    assert empty == ("", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_vocabulary_stimuli_behave_as_documented(argv, frame):
    """The spellings the conformance vocabulary types are measured through the frame."""
    killed, after = _run(argv, frame, POSIX.signal_kill_command, "printf ok")
    assert killed[1] == POSIX.signal_exit_code
    assert after == ("ok", 0)

    (lookalike,) = _run(argv, frame, POSIX.signal_lookalike_command)
    assert lookalike[1] == POSIX.signal_exit_code

    (state,) = _run(argv, frame, POSIX.state_read_command)
    assert state[1] == 0
    assert len(state[0].split("|")) == 6, state

    (terminal,) = _run(argv, frame, POSIX.terminal_read_command, new_session=True)
    assert terminal == ("unread", 0)
