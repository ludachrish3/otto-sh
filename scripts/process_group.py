"""The process group of a child started with ``start_new_session=True`` (Linux).

Such a child leads a group of its own (pgid == pid), and everything it starts
stays in that group unless it leaves on purpose. Signalling the group reaches
the grandchildren a signal to the child alone would miss: an xdist worker, a
make recipe's commands, otto's own children. Stdlib at import (the gate
scripts and the test suite share it); the post-kill drain borrows
``otto.utils.wait_for`` lazily, the repo's one poll-until-deadline.
"""

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
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


def alive(pid: int, started: int) -> bool:
    """Whether *pid* is still the very process that began at *started*, and running.

    The one reading of "the same process, still alive" the gate scripts share: the
    start time tells a reused pid from the process a record named, and a zombie
    has run its course. A pid that is gone answers False.
    """
    return process_start(pid) == started and process_state(pid) != "Z"


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


# --- running a lane and outliving nothing of it -------------------------------

POST_KILL_DRAIN_S = 30.0
"""How long a group is waited for after SIGKILL before it is called undrained.

After SIGKILL only a zombie under a non-reaping init or a process the user
cannot signal survives, and waiting longer does not change that.
"""
_LINGER_REPORT_INTERVAL_S = 10.0


class UndrainedError(RuntimeError):
    """A SIGKILLed group that still had members at the post-kill budget."""

    def __init__(self, pgid: int, pids: list[int]) -> None:
        self.pgid = pgid
        self.pids = pids
        super().__init__(
            f"process group {pgid} still has members after SIGKILL and {POST_KILL_DRAIN_S:g}s: "
            f"pids {' '.join(map(str, pids)) or 'unknown'}"
        )


@dataclass(frozen=True)
class RunResult:
    """What a run saw: the direct child's exit status and its captured output ("" uncaptured)."""

    status: int
    output: str


def wait_for_group(pgid: int, *, label: str) -> None:
    """Block until NO process is left in process group *pgid*.

    Polling :func:`group_alive` is the whole mechanism. No deadline and no
    kill on purpose: the caller's job here is to never start disposing of a
    worktree, a lease or a bed while anything the lanes spawned is still
    alive, and a wait that gives up after N seconds would just move the
    overlap to N seconds. A process that never exits holds the caller
    visibly, naming itself below (as *label*), instead of racing the cleanup
    silently.

    Assumes a reaping init: a zombie is still a group member, so under a
    PID 1 that never reaps (a bare ``tail -f`` container init) an orphaned
    descendant would hold this forever. The dev VM (systemd) and CI
    (``ubuntu-latest`` VMs) both reap.
    """
    reported_at = time.monotonic()
    while group_alive(pgid):
        now = time.monotonic()
        if now - reported_at >= _LINGER_REPORT_INTERVAL_S:
            reported_at = now
            pids = group_members(pgid)
            print(
                f"{label}: the lanes returned but processes they spawned are still "
                f"running; waiting for them before touching anything of theirs "
                f"(pids: {pids or 'unknown'})",
                flush=True,
            )
        time.sleep(0.1)


def _drain_after_kill(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL *proc*'s group and wait :data:`POST_KILL_DRAIN_S` for it to empty.

    One deadline covers reaping the direct child (killed, it is a zombie until
    its ``wait``, and a zombie is still a member of its group) and the group
    draining. Everything that stops this from confirming an empty group is the
    undrained verdict, never another exception: a member we may not signal (a
    lane that escalated through sudo), the child not reaped at the budget, or
    more interrupts while waiting (they are ignored until the deadline). Raises
    :class:`UndrainedError` naming the members still there.
    """
    from otto.utils import WaitTimeoutError, wait_for  # the one poll-until-deadline spelling

    pgid = proc.pid
    deadline = time.monotonic() + POST_KILL_DRAIN_S

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def empty() -> bool:
        """One attempt: True when the group is confirmed empty, False to try again."""
        try:
            proc.wait(timeout=remaining())
            wait_for(lambda: not group_alive(pgid), remaining(), on_timeout="group not empty")
        except (subprocess.TimeoutExpired, WaitTimeoutError):
            raise UndrainedError(pgid, group_members(pgid)) from None
        except KeyboardInterrupt:
            if remaining() == 0.0:
                raise UndrainedError(pgid, group_members(pgid)) from None
            return False
        return True

    try:
        signal_group(pgid, signal.SIGKILL)
    except PermissionError:
        raise UndrainedError(pgid, group_members(pgid)) from None
    while not empty():
        pass


def run_awaiting_descendants(
    args: "list[str] | str",
    *,
    cwd: Path,
    env: "dict[str, str] | None" = None,
    shell: bool = False,
    capture: bool = False,
    label: str = "gate",
) -> RunResult:
    """Run *args* in ITS OWN process group and return only when the group is empty.

    Why a group and not ``subprocess.run`` alone: ``run`` returns when the
    direct child exits, and ``make`` exits the moment its recipe does, but
    the recipe's own children (xdist workers, a test's subprocesses, an
    ``e2e`` daemon under teardown) can outlive it by a moment, still holding
    the worktree as their cwd or still writing ``.coverage.*``/``reports/``
    into it, or still on the bed a lease is about to free. That moment is
    exactly when ``git worktree remove`` used to run, and a removal that
    races a writer fails (it blocked a push from ``main`` on 2026-09-03).
    ``start_new_session`` puts the child and every descendant that does not
    ``setsid`` itself into one group; :func:`wait_for_group` then holds
    until it is empty.

    The interrupt relay: the new session took the lanes out of the
    terminal's foreground group, so the Ctrl+C that reached the caller never
    reached them. The first :class:`KeyboardInterrupt` forwards SIGINT to the
    whole group and waits, unbounded, for it to empty (pytest's single-Ctrl-C
    unwind runs the fixtures' teardown), then re-raises. A second interrupt
    during that wait means the operator has decided the run is dead: the
    group is SIGKILLed and waited for again, up to :data:`POST_KILL_DRAIN_S`;
    a group still non-empty then raises :class:`UndrainedError` naming the pids,
    and the caller decides what it may still touch (nothing of the group's).

    *capture* reads the child's stdout and stderr into one text (the
    :class:`RunResult`'s output); without it the child inherits the caller's
    stdio and the output is ``""``. *shell* runs *args* as one shell line.
    """
    stdio: dict = {}
    if capture:
        stdio = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True}
    # Not a `with`: Popen's exit would wait once more, unbounded, after an
    # undrained verdict; the pipes are closed here instead.
    proc = subprocess.Popen(  # noqa: S603 -- the gates' own lanes
        args, cwd=cwd, env=env, shell=shell, start_new_session=True, **stdio
    )
    output = ""

    def finish() -> None:
        nonlocal output
        if capture:
            output = proc.communicate()[0] or output
        else:
            proc.wait()
        wait_for_group(proc.pid, label=label)

    try:
        finish()
    except KeyboardInterrupt:
        signal_group(proc.pid, signal.SIGINT)
        try:
            finish()
        except KeyboardInterrupt:
            _drain_after_kill(proc)
            with contextlib.suppress(KeyboardInterrupt):
                finish()  # the group is empty: only the captured output is left to read
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    return RunResult(proc.returncode, output)
