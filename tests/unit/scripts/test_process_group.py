"""scripts/process_group.py: what a child's process group holds, and whether it is gone."""

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from otto.utils import wait_for
from scripts import process_group
from scripts.process_group import group_alive, group_members, process_state, signal_group


def test_group_members_reads_the_proc_field() -> None:
    """The /proc field arithmetic finds this very process in its own group."""
    assert os.getpid() in group_members(os.getpgid(0))


def test_a_comm_with_parens_and_spaces_still_parses(tmp_path: Path) -> None:
    """comm is cut at the LAST ')', so one holding ') ' does not shift the fields."""
    # This interpreter under the name, not `sleep`: a multi-call coreutils (uutils
    # on Ubuntu 26.04) dispatches on argv[0] and refuses a name it does not know.
    sleeper = tmp_path / "a) b"
    sleeper.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(sleeper), "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        wait_for(lambda: process_state(proc.pid) == "S", 10, on_timeout="it never slept")
        assert Path(f"/proc/{proc.pid}/comm").read_text() == "a) b\n"
        assert proc.pid in group_members(proc.pid)
    finally:
        signal_group(proc.pid)
        proc.wait()


def test_a_process_reaped_mid_read_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """ESRCH from reading a reaped pid's stat is 'gone', not an error."""

    def reaped(self: Path) -> str:
        raise ProcessLookupError

    monkeypatch.setattr(process_group.Path, "read_text", reaped)
    assert process_state(os.getpid()) is None


def test_a_group_is_alive_until_its_last_member_is_reaped() -> None:
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    assert group_alive(proc.pid)
    signal_group(proc.pid, signal.SIGTERM)
    wait_for(lambda: process_state(proc.pid) == "Z", 10, on_timeout="sleep never died")
    assert group_alive(proc.pid)  # a zombie not yet reaped is still a member
    proc.wait()
    assert not group_alive(proc.pid)
    signal_group(proc.pid)  # a group already gone is no error


def test_a_member_not_ours_to_signal_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane that escalated through sudo leaves a member we may not signal: alive, not gone."""

    def denied(pgid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(process_group.os, "killpg", denied)
    assert group_alive(12345)


def test_an_entry_proc_will_not_show_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A /proc entry that refuses the read (hidepid, say) is not a member, and no error."""

    def refused(pid: int) -> list[str] | None:
        raise PermissionError

    monkeypatch.setattr(process_group, "proc_stat", refused)
    assert group_members(os.getpgid(0)) == []


def test_a_stat_line_cut_short_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_group.Path, "read_text", lambda self: "1 (x) S")
    assert process_state(os.getpid()) is None


def test_process_start_is_the_kernel_stamp_and_none_once_gone() -> None:
    """The start time is the 22nd stat field; a pid that is gone has none."""
    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    assert process_group.process_start(os.getpid()) == int(fields[19])
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert process_group.process_start(proc.pid) is None


def test_ancestors_walks_the_ppid_chain_without_self() -> None:
    chain = process_group.ancestors(os.getpid())
    assert chain[0] == os.getppid()
    assert os.getpid() not in chain
    assert chain == list(dict.fromkeys(chain)), "no cycle, no repeat"
    assert 1 in chain or chain[-1] > 1, chain


def test_ancestors_of_a_gone_pid_is_empty() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert process_group.ancestors(proc.pid) == []


# ---------------------------------------------------------------------------
# run_awaiting_descendants: the lane runner both gates share
# ---------------------------------------------------------------------------
#
# A Python spawner, not ``sh -c "sleep & wait"``: a non-interactive shell starts
# ``&`` jobs with SIGINT ignored (POSIX async-list rule), so such a grandchild
# could never be interrupted. Real lane descendants are spawned the Python way.

_SPAWNER = (
    "import subprocess, pathlib, sys; p = subprocess.Popen(['sleep', '30']); "
    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); p.wait()"
)
_SPAWNER_IGNORING_SIGINT = (
    "import subprocess, pathlib, sys, signal; "
    "p = subprocess.Popen([sys.executable, '-c', "
    "'import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)']); "
    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
    "signal.signal(signal.SIGINT, signal.SIG_IGN); p.wait()"
)


def _interrupt_waits(monkeypatch, pid_file: Path, times: int) -> list:
    """Make the first *times* ``Popen.wait`` calls raise KeyboardInterrupt (once the pid exists)."""
    real_wait = subprocess.Popen.wait
    raised: list[int] = []

    def wait_then_interrupt(self, timeout=None):
        if len(raised) < times:
            raised.append(self.pid)
            wait_for(pid_file.exists, 10.0, interval=0.02, on_timeout="no grandchild pid")
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait_then_interrupt)
    return raised


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return process_group.process_state(pid) == "Z"


def test_run_awaiting_descendants_waits_for_a_backgrounded_grandchild(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    result = process_group.run_awaiting_descendants(
        ["sh", "-c", f"sleep 1 & echo $! > {pid_file}; exit 0"], cwd=tmp_path
    )
    assert result == process_group.RunResult(0, "")
    assert _gone(int(pid_file.read_text()))


def test_run_awaiting_descendants_captures_both_streams_and_the_status(tmp_path: Path):
    result = process_group.run_awaiting_descendants(  # noqa: S604 -- the gates' own lines
        "echo out; echo err >&2; exit 3",
        cwd=tmp_path,
        shell=True,
        capture=True,
    )
    assert result.status == 3
    assert sorted(result.output.splitlines()) == ["err", "out"]


def test_run_awaiting_descendants_passes_the_environment(tmp_path: Path):
    env = {"LANE_MARK": "x1", "PATH": os.environ["PATH"]}
    result = process_group.run_awaiting_descendants(
        ["sh", "-c", "echo $LANE_MARK"], cwd=tmp_path, env=env, capture=True
    )
    assert result.output == "x1\n"


@pytest.mark.timeout(30)
def test_the_first_interrupt_reaches_the_group_and_is_waited_out(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    raised = _interrupt_waits(monkeypatch, pid_file, 1)
    with pytest.raises(KeyboardInterrupt):
        process_group.run_awaiting_descendants(
            [sys.executable, "-c", _SPAWNER, str(pid_file)], cwd=tmp_path
        )
    assert raised
    assert _gone(int(pid_file.read_text()))


@pytest.mark.timeout(60)
def test_a_second_interrupt_kills_a_group_that_ignores_the_first(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    raised = _interrupt_waits(monkeypatch, pid_file, 2)
    with pytest.raises(KeyboardInterrupt):
        process_group.run_awaiting_descendants(
            [sys.executable, "-c", _SPAWNER_IGNORING_SIGINT, str(pid_file)], cwd=tmp_path
        )
    assert len(raised) == 2
    assert _gone(int(pid_file.read_text()))


@pytest.mark.timeout(60)
def test_a_group_that_outlives_the_kill_budget_is_undrained(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    _interrupt_waits(monkeypatch, pid_file, 2)
    monkeypatch.setattr(process_group, "POST_KILL_DRAIN_S", 0.3)
    monkeypatch.setattr(process_group, "group_alive", lambda pgid: True)  # it never empties
    monkeypatch.setattr(process_group, "group_members", lambda pgid: [4242])
    with pytest.raises(process_group.UndrainedError, match="pids 4242") as caught:
        process_group.run_awaiting_descendants(
            [sys.executable, "-c", _SPAWNER_IGNORING_SIGINT, str(pid_file)], cwd=tmp_path
        )
    assert caught.value.pids == [4242]
    # the kill itself happened: the real grandchild is gone
    assert _gone(int(pid_file.read_text()))


@pytest.mark.timeout(60)
@pytest.mark.filterwarnings(
    "ignore::ResourceWarning", "ignore::pytest.PytestUnraisableExceptionWarning"
)
def test_a_member_we_may_not_signal_is_the_undrained_verdict(tmp_path: Path, monkeypatch):
    """A lane that escalated through sudo: the kill is refused, and that is undrained, not EPERM.

    The direct child is still running at the verdict (nothing could kill it), so the
    runner's Popen is dropped unreaped: the ResourceWarning that follows is the point.
    """
    pid_file = tmp_path / "grandchild.pid"
    _interrupt_waits(monkeypatch, pid_file, 2)
    real_killpg = os.killpg

    def killpg(pgid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise PermissionError
        real_killpg(pgid, sig)

    monkeypatch.setattr(process_group.os, "killpg", killpg)
    with pytest.raises(process_group.UndrainedError):
        process_group.run_awaiting_descendants(
            [sys.executable, "-c", _SPAWNER_IGNORING_SIGINT, str(pid_file)], cwd=tmp_path
        )
    monkeypatch.undo()
    grandchild = int(pid_file.read_text())
    with contextlib.suppress(ProcessLookupError):  # the real kill, now that nothing refuses it
        signal_group(os.getpgid(grandchild))
    wait_for(lambda: _gone(grandchild), 10, on_timeout="the lane outlived the test")


@pytest.mark.timeout(60)
def test_more_interrupts_during_the_kill_drain_are_waited_out(tmp_path: Path, monkeypatch):
    """A third Ctrl-C does not escape as KeyboardInterrupt: the drain holds to its deadline."""
    pid_file = tmp_path / "grandchild.pid"
    _interrupt_waits(monkeypatch, pid_file, 2)
    monkeypatch.setattr(process_group, "POST_KILL_DRAIN_S", 0.5)
    polls: list[int] = []

    def alive_then_interrupt(pgid: int) -> bool:
        polls.append(pgid)
        if len(polls) == 1:
            raise KeyboardInterrupt
        return True  # it never empties

    monkeypatch.setattr(process_group, "group_alive", alive_then_interrupt)
    monkeypatch.setattr(process_group, "group_members", lambda pgid: [4242])
    with pytest.raises(process_group.UndrainedError, match="pids 4242"):
        process_group.run_awaiting_descendants(
            [sys.executable, "-c", _SPAWNER_IGNORING_SIGINT, str(pid_file)], cwd=tmp_path
        )
    assert len(polls) > 1, "the drain went on after the interrupt"
    assert _gone(int(pid_file.read_text()))
