"""The measured exec-frame payloads behave as DEF-TAIL and DEF-REC say.

The module under test is ``tests/_fixtures/exec_frame_payloads.py``.

Hostless: each payload is fed to real shells on a pipe (bash, dash, BusyBox
``sh`` where installed). The product rendering's byte equality with these
functions is the product task's own test; this pins the fixture itself.
"""

import re
import shutil
import subprocess

import pytest

from otto.host.command_frame import SessionMarkers
from tests._fixtures import exec_frame_payloads as payloads

M = SessionMarkers.for_session(session_id="a2fixture")

SHELLS = [
    argv
    for argv in (["bash", "--norc", "--noprofile"], ["dash"], ["busybox", "sh"])
    if shutil.which(argv[0])
]
SHELL_IDS = [argv[0] for argv in SHELLS]

RECORD = re.compile(re.escape(M.end_prefix) + r"([BN]?)(\d+)__")


def _sh(argv: "list[str]", script: str) -> str:
    return subprocess.run(
        argv, input=script, capture_output=True, text=True, timeout=10, check=False
    ).stdout


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
@pytest.mark.parametrize(
    ("setup", "flag", "code", "printed", "opts"),
    [
        ("", "N", "0", True, ""),
        ("set -e", "N", "1", False, "e"),
        ("set -eu", "N", "1", False, "eu"),
        ("__otto_base=; set -eu", "B", "1", False, "eu"),
    ],
)
def test_shape_c_keeps_errexit_inside_and_restores_it_outside(
    argv, setup, flag, code, printed, opts
):
    script = (
        f"{setup}\n"
        + payloads.shape_c("false; printf continued", m=M, stdin_eof=True, start_var=None)
        + "echo opts:$-\n"
    )
    out = _sh(argv, script)
    record = RECORD.search(out)
    assert record is not None, out
    assert (record.group(1), record.group(2)) == (flag, code)
    assert ("continued" in out) is printed
    after = re.search(r"opts:(\S*)", out)
    assert after is not None, out
    assert all(letter in after.group(1) for letter in opts)


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
def test_stdin_is_at_end_of_file_only_when_asked(argv):
    eof = _sh(
        argv, payloads.shape_c("cat; echo eof", m=M, stdin_eof=True, start_var=None) + "leftover\n"
    )
    assert "eof" in eof
    assert "leftover" not in eof.split(M.end_prefix)[0]


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
@pytest.mark.parametrize("setup", ["", "set -u", "set -eu"])
def test_the_restore_line_answers_with_the_variables_unset(argv, setup):
    out = _sh(argv, f"{setup}\n{payloads.restore_line(payloads.BASELINE_VAR, M)}echo opts:$-\n")
    assert payloads.restored_marker(M) in out
    assert "opts:" in out


def test_each_half_appears_verbatim_in_both_compositions():
    for var in (payloads.BASELINE_VAR, payloads.START_VAR):
        line = payloads.restore_line(var, M)
        frame = payloads.shape_c(
            "c", m=M, stdin_eof=True, start_var=None if var == payloads.BASELINE_VAR else var
        )
        for half in (payloads.terminal_restore(var), payloads.errexit_restore()):
            assert half in line
            assert half in frame
        assert f"{payloads.terminal_restore(var)}; {payloads.errexit_restore()}; " in line
    start = payloads.shape_c("c", m=M, stdin_eof=False, start_var=payloads.START_VAR)
    assert start.startswith(payloads.saved_into(payloads.START_VAR) + "; ")
    assert "3<" not in start
    assert ") </dev/null;" in payloads.shape_c("c", m=M, stdin_eof=True, start_var=None)
    kept = payloads.shape_c("c", m=M, stdin_eof=False, start_var=None, credential=True)
    assert " 3<&0;" in kept
    assert " 3<&0 </dev/null;" in payloads.shape_c(
        "c", m=M, stdin_eof=True, start_var=None, credential=True
    )


def test_the_restore_line_is_quote_free_and_anchored():
    line = payloads.restore_line(payloads.START_VAR, M)
    assert "'" not in line
    assert '"' not in line
    assert payloads.restored_marker(M) not in line
    assert payloads.restored_marker(M) == M.recover.removesuffix("RECOVER__") + "RESTORED__"
