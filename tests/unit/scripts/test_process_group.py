"""scripts/process_group.py: what a child's process group holds, and whether it is gone."""

import os
import shutil
import signal
import subprocess
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
    sleeper = tmp_path / "a) b"
    sleeper.symlink_to(shutil.which("sleep") or "/bin/sleep")
    proc = subprocess.Popen([str(sleeper), "30"], start_new_session=True)
    try:
        assert proc.pid in group_members(proc.pid)
        wait_for(lambda: process_state(proc.pid) == "S", 10, on_timeout="sleep never slept")
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
