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

import pytest

from otto.host.command_frame import AshFrame, BashFrame
from otto.host.session import SessionMarkers

pytestmark = pytest.mark.interpreter_agnostic

M = SessionMarkers.for_session("abcd1234")

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


def _run(argv: "list[str]", frame: BashFrame, *cmds: str) -> "list[tuple[str, int]]":
    """Type each framed command into one shell; return each one's (output, exit code)."""
    if shutil.which(argv[0]) is None:
        pytest.fail(f"{argv[0]} is not installed on this machine")
    script = "".join(frame.frame(c, M) for c in cmds)
    done = subprocess.run(
        argv,
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "PS1": "PROMPT$ ", "PS2": "MORE> ", "HOME": "/tmp"},
        check=False,
    )
    out = done.stdout
    results = []
    for _ in cmds:
        end = frame.end_pattern(M).search(out)
        assert end is not None, f"no END marker in:\n{out}"
        chunk, out = out[: end.end()], out[end.end() :]
        results.append((frame.parse_output(chunk, "", M), frame.extract_retcode(chunk, M)))
    return results


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_a_backgrounded_command_and_a_trailing_comment_both_frame(argv, frame):
    bg, comment = _run(argv, frame, "sleep 0 >/dev/null 2>&1 &", "echo hi # a note")
    # An interactive shell with job control announces the job (`[1] 4242`),
    # as it would to anyone typing the command; nothing else may appear.
    assert re.fullmatch(r"(\[\d+\] \d+)?", bg[0]), bg
    assert bg[1] == 0
    assert comment == ("hi", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_quotes_state_and_multiple_lines_survive_the_frame(argv, frame):
    quoted, cd, where, lines = _run(
        argv,
        frame,
        """echo "it's" 'a "test"' $((1 + 2))""",
        "cd /tmp && X=kept",
        'pwd; echo "$X"',
        "echo one\necho two",
    )
    assert quoted == ('it\'s a "test" 3', 0)
    assert cd[1] == 0
    assert where == ("/tmp\nkept", 0)
    assert lines == ("one\ntwo", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_the_exit_code_is_the_command_s_own(argv, frame):
    fails, sets, empty = _run(argv, frame, "false", "sh -c 'exit 7'", "")
    assert fails[1] == 1
    assert sets[1] == 7
    assert empty == ("", 0)


@pytest.mark.parametrize(("argv", "frame"), SHELLS)
def test_a_comment_only_command_and_a_trailing_backslash_both_frame(argv, frame):
    """A group holding only a comment is empty, which every shell rejects; a
    trailing backslash joins the closing brace onto the command's line, so the
    group never closes. Both left the call waiting out its timeout."""
    note, continued, after = _run(argv, frame, "# just a note", "echo a \\", "echo next")
    assert note == ("", 0)
    assert continued == ("a", 0)
    assert after == ("next", 0)
