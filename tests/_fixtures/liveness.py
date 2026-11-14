"""Whether a process is running, the one test-side answer.

A forked child that has exited but is not yet reaped still answers
``kill(pid, 0)``; its ``/proc/<pid>/stat`` state is ``Z``. So "running" is a
process whose state is known and is not ``Z``, read through
:mod:`scripts.process_group`, the repository's one ``/proc`` parser.
"""

from scripts.process_group import process_state


def pid_running(pid: int) -> bool:
    """Whether *pid* names a live process: it exists and is not a zombie."""
    return process_state(pid) not in (None, "Z")
