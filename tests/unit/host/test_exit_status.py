"""One exit-status rule on the process routes: a signal death is 128+N, no status is one error.

The ssh exec channel used to report a command killed by a signal as ``0``
(``exit_status or 0``) and local ``exec`` as ``-N`` (asyncio's convention);
a shell reports the same death as ``128 + N``. ``run`` is not translated at
all: its status is what the shell printed, and a shell can print 256+N for a
function's own return, so a magnitude rule there would rewrite a real status.
"""

import asyncio
import signal
import subprocess
from typing import Any

import pytest

from otto.host.command_frame import BashFrame
from otto.host.exit_status import NO_RETURN_CODE, exit_status_of
from otto.host.local_host import LocalHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host._fake_channel import FakeChannelProcess, FakeConn, channel_manager
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


@pytest.mark.asyncio
async def test_local_exec_reports_a_signal_death_as_128_plus_n():
    host = LocalHost()
    try:
        assert (await host.exec("kill -TERM $$")).retcode == 143
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_local_exec_reports_a_real_time_signal_death_as_128_plus_n():
    host = LocalHost()
    try:
        r = await host.exec("kill -35 $$")
        assert (r.status, r.retcode) == (Status.Failed, 163)  # before: Error, -1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_ssh_channel_reports_a_signal_death_as_128_plus_n():
    # asyncssh's completed process for a signal death: exit_status -1, returncode -N.
    process = FakeChannelProcess([], returncode=-15, exit_status=-1)
    r = await channel_manager().exec_on(
        conn=FakeConn(process), cmd="x", timeout=5, log=LogMode.NEVER
    )
    assert (r.retcode, r.status) == (143, Status.Failed)


class _NoReturnCodeTransport:
    """A subprocess transport that reaps its child but reports no return code."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)

    def get_returncode(self) -> None:
        return None

    async def _wait(self) -> None:
        await self._transport._wait()


async def _local_without_returncode(monkeypatch: pytest.MonkeyPatch) -> CommandResult:
    """Local ``exec`` of a process whose transport gives no return code after EOF and wait."""
    loop = asyncio.get_running_loop()
    real = loop.subprocess_shell

    async def spy(*args: Any, **kwargs: Any) -> Any:
        transport, protocol = await real(*args, **kwargs)
        return _NoReturnCodeTransport(transport), protocol

    monkeypatch.setattr(loop, "subprocess_shell", spy)
    host = LocalHost()
    try:
        return await host.exec("true")
    finally:
        await host.close()


@pytest.mark.parametrize("route", ["local", "channel"])
@pytest.mark.asyncio
async def test_a_process_with_no_status_and_no_signal_is_one_error_result(
    route: str, monkeypatch: pytest.MonkeyPatch
):
    if route == "local":
        r = await _local_without_returncode(monkeypatch)
    else:
        process = FakeChannelProcess([], returncode=None, exit_status=None)
        r = await channel_manager().exec_on(
            conn=FakeConn(process), cmd="x", timeout=5, log=LogMode.NEVER
        )
    assert (r.status, r.retcode, r.value) == (Status.Error, -1, NO_RETURN_CODE)


def test_run_s_brace_frame_status_is_what_the_shell_reports():
    # dash, ksh and zsh report a genuine 271 from a function; run must not rewrite it.
    framed = BashFrame().frame("f() { return 271; }; f", m=M)
    out = subprocess.run(["dash"], check=False, input=framed, capture_output=True, text=True).stdout
    assert BashFrame().extract_retcode(out, m=M) == 271
