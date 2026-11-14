"""One stop for a process-route command: local exec and the ssh exec channel.

The escalation (first signal, ``KILL`` only for a leader that outlives it),
local exec's process-group delivery (M14: before, a background member of the
command's group received neither signal), stdin at end of file, one bounded
drain-and-wait (#1063), and a stop that never replaces the error or the
cancellation that unwound the exec (M10).
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import asyncssh
import pytest

from otto.host import process_stop
from otto.host.local_host import LocalHost
from otto.host.process_stop import LocalStoppable, StopSignal, stop_process
from otto.logger.mode import LogMode
from otto.utils import wait_for_async
from scripts.process_group import group_members, signal_group
from tests._fixtures.liveness import pid_running
from tests.unit.host._fake_channel import FakeChannelProcess, FakeConn, channel_manager

pytestmark = [pytest.mark.asyncio]


class FakeStoppable:
    """A :class:`~otto.host.process_stop.StoppableProcess` that exits on a signal in *obeys*."""

    def __init__(self, *, obeys: "set[str]") -> None:
        self.sent: list[str] = []
        self._obeys = obeys
        self._exited = asyncio.Event()

    def send(self, sig: "StopSignal | str") -> None:
        name = sig if isinstance(sig, str) else sig.value
        if self._exited.is_set():
            raise ProcessLookupError
        self.sent.append(name)
        if name in self._obeys or name == "KILL":
            self._exited.set()

    async def wait(self) -> None:
        await self._exited.wait()


async def _written(path: Path, text: str = "") -> None:
    """Wait until *path* holds *text* (any content when empty)."""
    await wait_for_async(
        lambda: path.exists() and text in path.read_text() and path.read_text().strip() != "",
        10.0,
        interval=0.01,
        on_timeout=f"{path} never held {text!r}",
    )


def _kill_if_running(path: Path) -> None:
    """SIGKILL the pid recorded in *path*, if it is recorded and still there."""
    if not path.exists() or not path.read_text().strip():
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(int(path.read_text()), signal.SIGKILL)


def _signal_logger(log: Path, child: Path, leader: Path) -> str:
    """M14's witness: a background member of the group records each signal it receives.

    The member's own output goes to ``/dev/null``: a shell reports a waited
    job that the group signal killed ("Terminated", "Hangup") on stderr, and
    once otto has closed the exec's pipe that write raises ``SIGPIPE`` and
    kills the member before its trap logs the signal.
    """
    return (
        f'sh -c \'trap "echo HUP >> {log}; exit" HUP; trap "echo TERM >> {log}; exit" TERM; '
        f"echo $$ > {child}; echo ready >> {log}; while :; do sleep 1 & wait $!; done' "
        f">/dev/null 2>&1 & "
        f"echo $$ > {leader}; exec sleep 30"
    )


async def test_stop_escalates_to_kill_only_when_the_leader_outlives_the_first_signal(monkeypatch):
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0)
    p = FakeStoppable(obeys=set())
    await stop_process(p, first=StopSignal.TERMINATE)
    assert p.sent == ["TERM", "KILL"]


@pytest.mark.parametrize(
    ("first", "name"), [(StopSignal.TERMINATE, "TERM"), (StopSignal.HANG_UP, "HUP")]
)
async def test_a_leader_that_obeys_ends_the_escalation(first, name):
    p = FakeStoppable(obeys={name})
    await stop_process(p, first=first)
    assert p.sent == [name]


async def test_a_process_already_gone_is_not_signalled_again():
    p = FakeStoppable(obeys={"TERM"})
    p._exited.set()
    await stop_process(p, first=StopSignal.TERMINATE)
    assert p.sent == []


async def test_a_reaped_leader_is_never_signalled_even_if_its_group_remains():
    proc = await asyncio.create_subprocess_shell("sleep 30 & exit 0", start_new_session=True)
    await proc.wait()  # deterministic fast exit: reaped before the stop
    try:
        # No ProcessLookupError, no killpg.
        exited = asyncio.Event()
        exited.set()
        await stop_process(LocalStoppable(proc, exited), first=StopSignal.TERMINATE)
        assert group_members(proc.pid) != []  # the background sleep was not signalled
    finally:
        signal_group(proc.pid)


async def test_local_cancel_hangs_up_the_whole_group_first(tmp_path):
    log, child, leader = tmp_path / "log", tmp_path / "child", tmp_path / "leader"
    task = asyncio.ensure_future(LocalHost().exec(_signal_logger(log, child, leader)))
    try:
        await _written(log, "ready")
        await _written(leader)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _written(log, "HUP")  # before: 'ready' only (M14)
        assert log.read_text().splitlines() == ["ready", "HUP"]
        assert not pid_running(int(leader.read_text()))
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_local_timeout_terminates_the_whole_group_first(tmp_path, monkeypatch):
    log, child, leader = tmp_path / "log", tmp_path / "child", tmp_path / "leader"
    real = process_stop.stop_process

    async def gated(p, *, first):
        # The timeout fires first and drives the real stop; TERM is sent only
        # once the child has installed its traps, so the budget is no race.
        await _written(log, "ready")
        await real(p, first=first)

    monkeypatch.setattr(process_stop, "stop_process", gated)
    r = await LocalHost().exec(_signal_logger(log, child, leader), timeout=0.5)
    try:
        await _written(log, "TERM")  # before: 'ready' only (M14)
        assert r.timed_out
        assert log.read_text().splitlines() == ["ready", "TERM"]
        assert not pid_running(int(leader.read_text()))
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_local_cancel_leaves_a_nohup_child_running(tmp_path):
    # Survivor coverage, not the group witness.
    leader, child = tmp_path / "leader", tmp_path / "child"
    task = asyncio.ensure_future(
        LocalHost().exec(
            f"nohup sh -c 'echo $$ > {child}; exec sleep 30' >/dev/null 2>&1 & "
            f"echo $$ > {leader}; exec sleep 30"
        )
    )
    try:
        await _written(leader)
        await _written(child)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not pid_running(int(leader.read_text()))
        assert pid_running(int(child.read_text()))
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_local_exec_gives_the_command_end_of_file_on_stdin(monkeypatch):
    seen: dict = {}
    loop = asyncio.get_running_loop()
    real = loop.subprocess_shell

    async def spy(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(loop, "subprocess_shell", spy)
    r = await LocalHost().exec("cat; echo otto-cf-eof", timeout=5)
    assert (r.timed_out, r.value) == (False, "otto-cf-eof")
    assert seen["stdin"] is asyncio.subprocess.DEVNULL
    assert seen["start_new_session"] is True


async def test_local_exec_keeps_a_line_longer_than_64_kib():
    # Regression only: pull request 1 fixed it.
    r = await LocalHost().exec("head -c 200000 /dev/zero | tr '\\0' a")
    assert r.is_ok
    assert r.value == "a" * 200000


async def test_local_eof_before_exit_still_times_out_inside_the_budget():
    r = await LocalHost().exec("exec >/dev/null 2>&1; sleep 10", timeout=0.5)
    assert r.timed_out  # before: returned after 10 s, timed_out=False
    assert r.value == "Command timed out after 0.5s"


async def test_the_ssh_channel_bounds_the_wait_after_the_drain():
    proc = FakeChannelProcess(
        [], returncode=None, exit_status=None, eof=True, obeys=frozenset({"TERM"})
    )
    proc.exits_on_eof = False  # drains to EOF, never exits by itself
    r = await channel_manager().exec_on(
        conn=FakeConn(proc), cmd="x", timeout=0.05, log=LogMode.NEVER
    )
    assert r.timed_out
    assert proc.signals[0] == "TERM"


async def test_a_cancel_landing_during_the_timeout_stop_is_held_until_the_stop_finishes(
    monkeypatch,
):
    gate, stopping = asyncio.Event(), asyncio.Event()
    firsts: "list[StopSignal]" = []
    finished: "list[bool]" = []

    async def slow_stop(_stoppable, *, first):  # gets a ChannelStoppable: touch only `first`
        firsts.append(first)
        stopping.set()
        await gate.wait()
        finished.append(True)

    # The one seam: session.py calls process_stop.stop_process.
    monkeypatch.setattr(process_stop, "stop_process", slow_stop)
    proc = FakeChannelProcess([], returncode=None, exit_status=None, eof=False)
    task = asyncio.ensure_future(
        channel_manager().exec_on(conn=FakeConn(proc), cmd="x", timeout=0, log=LogMode.NEVER)
    )
    await stopping.wait()
    task.cancel()
    # One loop turn, so the cancellation reaches compensate while the stop is
    # still blocked on the gate; nothing in the stop can observe it being held.
    await asyncio.sleep(0)
    assert not task.done()  # held: the cancel did not end the exec
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (firsts, finished) == ([StopSignal.TERMINATE], [True])  # the stop ran to its end


async def test_an_interrupted_channel_exec_hangs_up_and_keeps_the_cancellation_when_the_stop_fails(
    monkeypatch,
):
    firsts: "list[StopSignal]" = []

    async def failing_stop(_proc, *, first):
        firsts.append(first)
        raise OSError("channel gone")

    monkeypatch.setattr(process_stop, "stop_process", failing_stop)
    proc = FakeChannelProcess([], returncode=None, exit_status=None, eof=False)
    task = asyncio.ensure_future(
        channel_manager().exec_on(conn=FakeConn(proc), cmd="x", timeout=60, log=LogMode.NEVER)
    )
    await proc.reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # not OSError (M10)
        await task
    assert firsts == [StopSignal.HANG_UP]


async def test_a_failing_stop_never_replaces_a_non_cancellation_error(monkeypatch):
    async def failing_stop(_proc, *, first):
        raise OSError("channel gone")

    monkeypatch.setattr(process_stop, "stop_process", failing_stop)
    proc = FakeChannelProcess([], returncode=None, exit_status=None, eof=False)
    proc.stdout.fail_with = asyncssh.ConnectionLost("lost")  # the drain itself raises
    with pytest.raises(asyncssh.ConnectionLost):
        await channel_manager().exec_on(conn=FakeConn(proc), cmd="x", timeout=60, log=LogMode.NEVER)


async def test_local_stop_ends_when_the_leader_exits_though_a_survivor_holds_its_output(
    tmp_path, monkeypatch
):
    # A member that ignores HUP keeps the exec's stdout open after the leader
    # obeys. From Python 3.11.x on, Process.wait() waits for every pipe to
    # close as well, so a stop that awaited it waited out the whole reap bound
    # and then sent KILL to a leader long gone. The stop observes the leader's
    # exit itself; the survivor is left alone (it is not ours to kill).
    leader, child = tmp_path / "leader", tmp_path / "child"
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 60)
    monkeypatch.setattr(process_stop, "EXEC_CANCEL_REAP_BOUND", 120)
    sent: "list[str]" = []
    real_send = LocalStoppable.send

    def spy(self, sig):
        sent.append(sig if isinstance(sig, str) else sig.value)
        real_send(self, sig)

    waited: "list[bool]" = []
    real_wait = LocalStoppable.wait

    async def wait_spy(self):
        await real_wait(self)
        waited.append(True)

    monkeypatch.setattr(LocalStoppable, "send", spy)
    monkeypatch.setattr(LocalStoppable, "wait", wait_spy)
    task = asyncio.ensure_future(
        LocalHost().exec(
            f"sh -c 'trap \"\" HUP; echo $$ > {child}; exec sleep 30' & "
            f"echo $$ > {leader}; exec sleep 30"
        )
    )
    try:
        await _written(leader)
        await _written(child)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # A bound that only turns a hang into a failure, never a pass.
            await asyncio.wait_for(task, 30)
        # Before (3.11 on): the leader's exit was never seen; the teardown
        # deadline abandoned the stop at 10 s, waited == [].
        assert (sent, waited) == (["HUP"], [True])
        assert not pid_running(int(leader.read_text()))
        assert pid_running(int(child.read_text()))  # the survivor policy holds
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_a_timed_out_exec_whose_stop_fails_still_returns_the_timed_out_result(monkeypatch):
    # The stop's own failure (the channel already gone) is logged, as on the
    # interrupt arm: the command did time out, and that is the result.
    async def failing_stop(_proc, *, first):
        raise OSError("channel gone")

    monkeypatch.setattr(process_stop, "stop_process", failing_stop)
    proc = FakeChannelProcess([], returncode=None, exit_status=None, eof=False)
    r = await channel_manager().exec_on(conn=FakeConn(proc), cmd="x", timeout=0, log=LogMode.NEVER)
    assert r.timed_out  # before: OSError raised out of exec_on
