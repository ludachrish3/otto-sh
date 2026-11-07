"""Abort a hung child's process group so each process says where it was stuck.

Built on :mod:`scripts.process_group`, which decides what a group is and
whether it is alive; this adds only the child a test can abort that way and
the diagnostic abort it runs at its deadline.
"""

import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import BinaryIO

from otto.utils import WaitTimeoutError, wait_for
from scripts.process_group import ancestors, group_members, process_state, signal_group


def start_dumping_group(
    cmd: list[str], *, env: dict[str, str], cwd: Path, sink: BinaryIO
) -> subprocess.Popen[bytes]:
    """Start *cmd* leading a process group of its own, every process in it able to dump.

    PYTHONFAULTHANDLER keeps faulthandler armed in every Python process of the
    group, for the whole life of the interpreter: pytest's own faulthandler
    plugin disarms at unconfigure, before the teardown a session can hang in.
    PYTHONUNBUFFERED keeps what was printed before an abort. Output, the dumps
    included, goes to *sink*; the caller kills what is left of the group.
    """
    return subprocess.Popen(
        cmd,
        env={**env, "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1"},
        cwd=cwd,
        stdout=sink,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def abort_group_in_turn(pgid: int, *, timeout: float = 10) -> list[int]:
    """SIGABRT every process in group *pgid*, one at a time; return the pids that outlived it.

    The group is stopped first, so no process sees another die and exits on
    its own before its turn. Each is then aborted and resumed alone, and the
    next waits until it is dead (gone, or a zombie its stopped parent cannot
    reap), so dumps written to a shared file (a faulthandler dump of every
    thread, say) do not interleave.

    The deepest go first: a child is aborted while its parent still lives, so
    no pipe from the parent closes under it mid-dump. A parent aborted first
    wakes the child's thread blocked on that pipe (an xdist worker's execnet
    receiver), which then runs while faulthandler walks its frames, and the
    dump can segfault cut short. A parent's own dump can still be woken that
    way by its dead child, so the deepest frame is the one a caller can rely on.
    """
    signal_group(pgid, signal.SIGSTOP)
    members = group_members(pgid)
    in_group = set(members)
    depth = {pid: sum(a in in_group for a in ancestors(pid)) for pid in members}
    survivors = []
    for pid in sorted(members, key=depth.__getitem__, reverse=True):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGABRT)
            os.kill(pid, signal.SIGCONT)
        try:
            wait_for(lambda pid=pid: process_state(pid) in (None, "Z"), timeout, on_timeout="")
        except WaitTimeoutError:
            survivors.append(pid)
    signal_group(pgid, signal.SIGCONT)
    return survivors
