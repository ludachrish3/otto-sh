"""One exit-status rule: a signal death is 128+N, and one reader of "no exit status".

The ssh exec channel used to report a command killed by a signal as ``0``
(``exit_status or 0``) and local ``exec`` as ``-N`` (asyncio's convention);
a shell reports the same death as ``128 + N``, and so does
:func:`~otto.host.exit_status.exit_status_of`, which the interactive ssh bridge
reads its status through. ``run`` is not translated at all: its status is
what the shell printed, and a shell can print 256+N for a function's own
return, so a magnitude rule there would rewrite a real status.
"""

import signal
import subprocess

import pytest
import pytest_asyncio

from otto.host.command_frame import BashFrame
from otto.host.exit_status import (
    exit_status_of,
    gave_no_exit_status,
)
from otto.host.local_host import LocalHost
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host.test_frame_shell_portability import M


@pytest.mark.parametrize(
    ("rc", "expected"),
    [
        (0, 0),
        (42, 42),
        (-15, 143),
        (-9, 137),
        (-35, 163),  # a real-time signal: no Signals member, still a signal (before: None)
        (-(signal.NSIG - 1), 128 + signal.NSIG - 1),
        (-signal.NSIG, None),
        (-99, None),
        (None, None),
    ],
)
def test_exit_status_of_maps_a_signal_to_128_plus_n(rc: "int | None", expected: "int | None"):
    assert exit_status_of(rc) == expected


@pytest.mark.parametrize("sig", ["TERM", "35"], ids=["term", "real-time"])
@pytest.mark.asyncio
async def test_a_local_exec_that_kills_its_own_shell_ends_the_session(sig: str):
    """``$$`` is the session's shell: killing it ends the session, not the command alone.

    A child the command starts and a signal kills still reports 128+N:
    ``test_local_host.py::test_a_command_killed_by_a_signal_reports_128_plus_it_and_exec_goes_on``.
    """
    host = LocalHost()
    try:
        r = await host.exec(f"kill -{sig} $$")
        assert (r.status, r.retcode) == (Status.Error, -1)
        assert r.value.startswith("Session died unexpectedly"), r.value
        assert (await host.exec("echo ok")).value == "ok", "the next exec has a shell of its own"
    finally:
        await host.close()


def test_run_s_brace_frame_status_is_what_the_shell_reports():
    # dash, ksh and zsh report a genuine 271 from a function; run must not rewrite it.
    framed = BashFrame().frame("f() { return 271; }; f", m=M)
    out = subprocess.run(["dash"], check=False, input=framed, capture_output=True, text=True).stdout
    assert BashFrame().extract_retcode(out, m=M) == 271


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(
            CommandResult(command="c", value="", status=Status.Success, retcode=0),
            False,
            id="exit-0",
        ),
        pytest.param(
            CommandResult(command="c", value="no", status=Status.Failed, retcode=1),
            False,
            id="exit-1",
        ),
        pytest.param(
            CommandResult(command="c", value="", status=Status.Failed, retcode=137),
            False,
            id="killed-by-a-signal",
        ),
        pytest.param(
            CommandResult(command="c", value="", status=Status.Error, retcode=-1),
            True,
            id="never-ran",
        ),
        pytest.param(
            CommandResult(command="c", value="", status=Status.Error, retcode=124, timed_out=True),
            True,
            id="timed-out",
        ),
    ],
)
def test_gave_no_exit_status(result: CommandResult, expected: bool):
    assert gave_no_exit_status(result) is expected


@pytest_asyncio.fixture(params=["ssh", "local"])
async def piped_exec_host(request, piped):
    """A host whose ``exec`` types into a shell that reads a pipe: an ssh host's, the local one."""
    if request.param == "ssh":
        yield piped[0]
        return
    host = LocalHost()
    yield host
    await host.close()


@pytest.mark.asyncio
async def test_an_exec_that_kills_its_own_shell_ends_the_session_on_either_host(piped_exec_host):
    r = await piped_exec_host.exec("kill $$")
    assert (r.status, r.retcode) == (Status.Error, -1)
    assert r.value.startswith("Session died unexpectedly"), r.value
    assert (await piped_exec_host.exec("echo ok")).value == "ok", "the next exec gets a new shell"


@pytest.mark.asyncio
async def test_an_invalid_command_ends_the_session_without_the_shells_diagnostic(piped_exec_host):
    # Typed bare into the session's shell, a syntax error ends that shell: the
    # first statement ran, the diagnostic went with the shell. Declared.
    r = await piped_exec_host.exec("echo a; echo )")
    assert (r.status, r.retcode) == (Status.Error, -1)
    assert r.value.startswith("Session died unexpectedly"), r.value
    assert (await piped_exec_host.exec("echo ok")).value == "ok", "the next exec gets a new shell"


@pytest.mark.asyncio
async def test_an_incomplete_command_waits_out_its_timeout(piped_exec_host):
    # An unclosed quote swallows the frame's tail, so no end is ever seen. Declared.
    r = await piped_exec_host.exec("echo 'oops", timeout=1)
    assert r.timed_out
    assert (await piped_exec_host.exec("echo ok")).value == "ok", "the next exec gets a new shell"
