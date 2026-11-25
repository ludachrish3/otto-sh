"""One stop for a local shell's process group, and a local exec on its session.

The escalation (first signal, ``KILL`` for a leader that outlives it and for
what is left of a local group once the grace is over), the group delivery
(M14: before, a background member of the command's group received neither
signal), stdin at end of file, and a timeout that bounds the command, not
only its output (#1063).
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from otto.host import process_stop
from otto.host.local_host import LocalHost
from otto.host.process_stop import LocalStoppable, stop_process
from otto.utils import wait_for_async
from scripts.process_group import group_members, signal_group
from tests._fixtures import witness_shell as wit
from tests._fixtures.liveness import pid_running

pytestmark = [pytest.mark.asyncio]


class FakeStoppable:
    """A :class:`~otto.host.process_stop.StoppableProcess` that exits on a signal in *obeys*."""

    def __init__(self, *, obeys: "set[str]", group_left: bool = False) -> None:
        self.sent: list[str] = []
        self._obeys = obeys
        self._exited = asyncio.Event()
        self._group_left = group_left
        self.group_probes = 0
        self.group_killed = False

    def send(self, sig: str) -> None:
        if self._exited.is_set():
            raise ProcessLookupError
        self.sent.append(sig)
        if sig in self._obeys or sig == "KILL":
            self._exited.set()

    async def wait(self) -> None:
        await self._exited.wait()

    def group_gone(self) -> bool:
        self.group_probes += 1
        return not self._group_left

    def kill_group(self) -> None:
        self.group_killed = True


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
    await stop_process(p)
    assert p.sent == ["TERM", "KILL"]


async def test_a_leader_that_obeys_ends_the_escalation():
    p = FakeStoppable(obeys={"TERM"})
    await stop_process(p)
    assert p.sent == ["TERM"]


async def test_a_process_already_gone_is_not_signalled_again():
    p = FakeStoppable(obeys={"TERM"})
    p._exited.set()
    await stop_process(p)
    assert p.sent == []


async def test_a_reaped_leader_is_never_signalled_even_if_its_group_remains():
    proc = await asyncio.create_subprocess_shell("sleep 30 & exit 0", start_new_session=True)
    await proc.wait()  # deterministic fast exit: reaped before the stop
    try:
        # No ProcessLookupError, no killpg.
        exited = asyncio.Event()
        exited.set()
        await stop_process(LocalStoppable(proc, exited))
        assert group_members(proc.pid) != []  # the background sleep was not signalled
    finally:
        signal_group(proc.pid)


async def test_local_cancel_terminates_the_whole_group_first(tmp_path):
    """A cancelled local exec closes its session, whose close sends ``TERM`` to the group.

    ``$$`` is the session's shell, the group's leader, which the command
    replaces with its ``sleep``.
    """
    log, child, leader = tmp_path / "log", tmp_path / "child", tmp_path / "leader"
    task = asyncio.ensure_future(LocalHost().exec(_signal_logger(log, child, leader)))
    try:
        await _written(log, "ready")
        await _written(leader)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _written(log, "TERM")  # before the session: 'HUP'; before M14: 'ready' only
        assert log.read_text().splitlines() == ["ready", "TERM"]
        assert not pid_running(int(leader.read_text()))
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_local_timeout_terminates_the_whole_group_first(tmp_path, monkeypatch):
    log, child, leader = tmp_path / "log", tmp_path / "child", tmp_path / "leader"
    real = process_stop.stop_process

    async def gated(p):
        # The timeout fires first and drives the real stop; TERM is sent only
        # once the child has installed its traps, so the budget is no race.
        await _written(log, "ready")
        await real(p)

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


def _gate_the_stop_on(monkeypatch, ready: Path) -> None:
    """Send the timed-out stop only once *ready* is written, so the budget is no race."""
    real = process_stop.stop_process

    async def gated(p):
        await _written(ready)
        await real(p)

    monkeypatch.setattr(process_stop, "stop_process", gated)


async def test_local_timeout_signals_a_foreground_member_that_is_not_the_leader(
    tmp_path, monkeypatch
):
    # The declared member case: the leader is the exec's /bin/sh, the witnessed
    # process its foreground child. The call returns on the leader's reap; the
    # member has been sent TERM, but whether it has finished exiting is not
    # promised, so only the signal is asserted (its trap's marker).
    witness, marker = tmp_path / "witness", tmp_path / "marker"
    write = wit.witness_write_template(str(witness))
    member = (
        f'sh -c \'trap "echo TERM > {marker}; exit" TERM; {write}; '
        f"while :; do sleep 1 & wait $!; done' >/dev/null 2>&1; :"
    )
    _gate_the_stop_on(monkeypatch, witness)
    try:
        r = await LocalHost().exec(member, timeout=0.5)
        assert r.timed_out
        await _written(marker, "TERM")
    finally:
        wit.clean(witness)


async def _gone(witness: Path) -> None:
    """Wait, bounded, until the witnessed process is no longer running."""
    await wait_for_async(
        lambda: wit.observe(witness) == wit.STOPPED,
        10.0,
        interval=0.01,
        on_timeout=f"the process witnessed in {witness} is still running",
    )


async def _timed_out_with_a_short_grace(monkeypatch, witness: Path, cmd: str) -> None:
    """Run *cmd* to its timeout on the local host, the stop gated on *witness*."""
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0.5)
    _gate_the_stop_on(monkeypatch, witness)
    r = await LocalHost().exec(cmd, timeout=0.5)
    assert r.timed_out


async def test_local_timeout_kills_a_foreground_command_that_ignores_term(tmp_path, monkeypatch):
    # The session's shell exits on TERM at once; the command that ignores it
    # is what is left of the group after the grace, and is killed.
    witness = tmp_path / "witness"
    cmd = wit.ignoring(wit.long_running_template(str(witness), seconds=30), "TERM")
    try:
        await _timed_out_with_a_short_grace(monkeypatch, witness, cmd)
        await _gone(witness)  # before: still running, its group's leader long gone
    finally:
        wit.clean(witness)


async def test_local_timeout_kills_a_background_member_that_ignores_term(tmp_path, monkeypatch):
    witness = tmp_path / "witness"
    member = wit.ignoring(wit.long_running_template(str(witness), seconds=30), "TERM")
    try:
        await _timed_out_with_a_short_grace(
            monkeypatch, witness, f"{member} >/dev/null 2>&1 & exec sleep 30"
        )
        await _gone(witness)
    finally:
        wit.clean(witness)


async def test_local_timeout_leaves_a_process_that_left_the_group_running(tmp_path, monkeypatch):
    witness = tmp_path / "witness"
    detached = wit.long_running_template(str(witness), seconds=30)
    try:
        await _timed_out_with_a_short_grace(
            monkeypatch,
            witness,
            f"setsid {detached} </dev/null >/dev/null 2>&1 & exec sleep 30",
        )
        assert wit.observe(witness) == wit.RUNNING
    finally:
        wit.clean(witness)


async def test_a_local_stop_whose_group_empties_with_its_shell_kills_nothing(monkeypatch):
    # A grace far longer than the bound below: a stop that waited it out
    # fails the bound, and one that killed a group sends KILL.
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 60)
    monkeypatch.setattr(process_stop, "EXEC_CANCEL_REAP_BOUND", 120)
    killed: "list[bool]" = []
    monkeypatch.setattr(LocalStoppable, "kill_group", lambda _self: killed.append(True))
    sent: "list[str]" = []
    real_send = LocalStoppable.send

    def spy(self, sig):
        sent.append(sig if isinstance(sig, str) else sig.value)
        real_send(self, sig)

    monkeypatch.setattr(LocalStoppable, "send", spy)
    # A bound that only turns a hang into a failure, never a pass.
    r = await asyncio.wait_for(LocalHost().exec("exec sleep 30", timeout=0.5), 30)
    assert r.timed_out
    assert (sent, killed) == (["TERM"], [])


async def test_stop_kills_what_is_left_of_the_group_once_the_grace_is_over(monkeypatch):
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0.05)
    p = FakeStoppable(obeys={"TERM"}, group_left=True)
    await stop_process(p)
    assert (p.sent, p.group_killed) == (["TERM"], True)


async def test_a_group_empty_when_its_leader_exits_is_probed_once_and_not_killed():
    p = FakeStoppable(obeys={"TERM"})
    await stop_process(p)
    assert (p.sent, p.group_probes, p.group_killed) == (["TERM"], 1, False)


async def test_local_cancel_stops_a_nohup_child_too(tmp_path):
    # The session's close sends TERM to the group, which nohup does not
    # ignore: before the session, the cancel sent HUP and this child survived.
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
        await wait_for_async(
            lambda: not pid_running(int(child.read_text())),
            10.0,
            interval=0.01,
            on_timeout="the nohup child survived the group's TERM",
        )
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)


async def test_local_exec_gives_the_command_end_of_file_on_stdin():
    r = await LocalHost().exec("cat; echo otto-cf-eof", timeout=5)
    assert (r.timed_out, r.value) == (False, "otto-cf-eof")


async def test_local_exec_keeps_a_line_longer_than_64_kib():
    # Regression only: pull request 1 fixed it.
    r = await LocalHost().exec("head -c 200000 /dev/zero | tr '\\0' a")
    assert r.is_ok
    assert r.value == "a" * 200000


async def test_local_eof_before_exit_still_times_out_inside_the_budget():
    r = await LocalHost().exec("exec >/dev/null 2>&1; sleep 10", timeout=0.5)
    assert r.timed_out  # before: returned after 10 s, timed_out=False
    assert r.value == "Command timed out after 0.5s"


async def test_local_stop_ends_when_the_leader_exits_though_a_survivor_holds_its_output(
    tmp_path, monkeypatch
):
    # A member that ignores TERM keeps the exec's stdout open after the leader
    # (the session's shell, replaced by the command's sleep) obeys the close's
    # TERM. From Python 3.11.x on, Process.wait() waits for every pipe to
    # close as well, so a stop that awaited it waited out the whole reap bound
    # and then sent KILL to a leader long gone. The stop observes the leader's
    # exit itself; the survivor is killed once the grace is over.
    leader, child = tmp_path / "leader", tmp_path / "child"
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 2)
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
            f"sh -c 'trap \"\" TERM; echo $$ > {child}; exec sleep 30' & "
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
        assert (sent, waited) == (["TERM"], [True])
        assert not pid_running(int(leader.read_text()))
        # Killed as what is left of the group after the grace, not by `send`.
        await wait_for_async(
            lambda: not pid_running(int(child.read_text())),
            10.0,
            interval=0.01,
            on_timeout="the member that ignores TERM outlived the grace",
        )
    finally:
        _kill_if_running(child)
        _kill_if_running(leader)
