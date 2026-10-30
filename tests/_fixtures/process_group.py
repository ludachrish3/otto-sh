"""Abort a hung child's process group so each process says where it was stuck.

Built on :mod:`scripts.process_group`, which decides what a group is and
whether it is alive; this adds only the diagnostic abort a test runs at its
deadline.
"""

import contextlib
import os
import signal

from otto.utils import WaitTimeoutError, wait_for
from scripts.process_group import group_members, process_state, signal_group


def abort_group_in_turn(pgid: int, *, timeout: float = 10) -> list[int]:
    """SIGABRT every process in group *pgid*, one at a time; return the pids that outlived it.

    The group is stopped first, so no process sees another die and exits on
    its own before its turn. Each is then aborted and resumed alone, and the
    next waits until it is dead (gone, or a zombie its stopped parent cannot
    reap), so dumps written to a shared file (a faulthandler dump of every
    thread, say) do not interleave.
    """
    signal_group(pgid, signal.SIGSTOP)
    survivors = []
    for pid in group_members(pgid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGABRT)
            os.kill(pid, signal.SIGCONT)
        try:
            wait_for(lambda pid=pid: process_state(pid) in (None, "Z"), timeout, on_timeout="")
        except WaitTimeoutError:
            survivors.append(pid)
    signal_group(pgid, signal.SIGCONT)
    return survivors
