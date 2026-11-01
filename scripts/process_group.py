"""The process group of a child started with ``start_new_session=True`` (Linux).

Such a child leads a group of its own (pgid == pid), and everything it starts
stays in that group unless it leaves on purpose. Signalling the group reaches
the grandchildren a signal to the child alone would miss: an xdist worker, a
make recipe's commands, otto's own children. Stdlib only, so the gate scripts
and the test suite share it.
"""

import contextlib
import os
import signal
from pathlib import Path

# Fields of /proc/<pid>/stat after the `pid (comm) ` prefix: state is 0, ppid is
# 1, pgrp is 2, starttime (clock ticks since boot, the kernel's own stamp of the
# process, so a reused pid is told apart) is 19.
_STATE = 0
_PPID = 1
_PGRP = 2
_STARTTIME = 19


def proc_stat(pid: int) -> list[str] | None:
    """Return the fields of ``/proc/<pid>/stat`` after its ``pid (comm)``; None once *pid* is gone.

    ``comm`` may hold spaces and parentheses, so the split is after the LAST
    ``)``. A process reaped between the open and the read raises ESRCH, which
    is gone too, and so is a line cut short before the fields read here.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields if len(fields) > _STARTTIME else None


def process_state(pid: int) -> str | None:
    """Return *pid*'s one-letter state (``S`` sleeping, ``Z`` zombie, ...); None once it is gone."""
    fields = proc_stat(pid)
    return None if fields is None else fields[_STATE]


def process_start(pid: int) -> int | None:
    """Return *pid*'s kernel start time (ticks since boot); None once it is gone.

    With the pid it names a process uniquely for the life of the machine:
    a record that holds both tells a reused pid from the one it recorded.
    """
    fields = proc_stat(pid)
    return None if fields is None else int(fields[_STARTTIME])


def ancestors(pid: int) -> list[int]:
    """Return *pid*'s parent, grandparent, ... up to (not including) pid 0.

    Read from ``/proc`` at call time: a process reparented by its parent's
    death answers with its new parent. *pid* itself is not in the list; a
    *pid* that is gone answers ``[]``.
    """
    chain: list[int] = []
    current = pid
    while True:
        fields = proc_stat(current)
        if fields is None:
            return chain
        parent = int(fields[_PPID])
        if parent <= 0 or parent in chain:
            return chain
        chain.append(parent)
        current = parent


def group_members(pgid: int) -> list[int]:
    """Return the pids now in group *pgid*; empty where ``/proc`` is absent."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    members = []
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:
                fields = proc_stat(int(entry.name))
            except OSError:
                continue
            if fields is not None and fields[_PGRP] == str(pgid):
                members.append(int(entry.name))
    return members


def group_alive(pgid: int) -> bool:
    """Whether any process is left in group *pgid*.

    A zombie not yet reaped is still a member, and so is one that is not ours
    to signal (a lane that escalated through sudo): that is alive, not gone.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, sig: signal.Signals = signal.SIGKILL) -> None:
    """Send *sig* to group *pgid*; a group already gone is no error.

    Reaping the child that leads it is the caller's: ``wait()`` or
    ``communicate()``, whichever it reads output with.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)
