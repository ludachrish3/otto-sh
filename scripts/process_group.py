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

# Fields of /proc/<pid>/stat after the `pid (comm) ` prefix: state is 0, pgrp is 2.
_STATE = 0
_PGRP = 2


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
    return fields if len(fields) > _PGRP else None


def process_state(pid: int) -> str | None:
    """Return *pid*'s one-letter state (``S`` sleeping, ``Z`` zombie, ...); None once it is gone."""
    fields = proc_stat(pid)
    return None if fields is None else fields[_STATE]


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
