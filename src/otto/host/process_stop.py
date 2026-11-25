"""One stop sequence for a command otto started as a process: a local shell's group, an ssh channel.

A timed-out command is asked to stop with ``TERM`` ("you took too long"); an
interrupted one is hung up with ``HUP`` ("the terminal went away", the end a
shell-routed command meets when its pty closes, which spares a deliberately
``nohup``'d child the hang-up itself). Either way the command can ignore the
first signal, so the stop is bounded by ``_EXEC_REAP_TIMEOUT`` (the grace): a
command that OUTLIVES the first signal by the grace is killed, and on a route
that sees the command's process group, so is whatever is left in the group
once the grace is over (a member that ignores the signal, a ``nohup`` child).
A group that is empty when the command exits ends the stop at once. A process
that left the group (``setsid``) is never reached.

Where the signal goes is each route's adapter:

* :class:`LocalStoppable` signals the command's process group (local exec
  starts the command in a new session, so the group id is the leader's pid),
  and never once the leader is known to be reaped (asyncio has set its
  ``returncode``); see :class:`LocalStoppable` for the window before that.
* :class:`ChannelStoppable` asks the ssh channel; delivery to the remote
  command (sshd signals its process group) is the server's, not observed by
  otto.

Callers reach :func:`stop_process` through this module (``process_stop.
stop_process``), never a name imported into their own namespace, so one seam
replaces it on both routes.

A REMOTE shell otto cannot signal as a group (no terminal to hang up, a server
that signals one process, a ``docker exec`` whose client went away) is stopped
by MARK instead: it is started with a token in its environment
(:func:`marked_shell_command`, or the caller's own ``-e``), every process it
starts inherits the token, and :func:`reap_marked_command` kills each process
that carries it.
"""

import asyncio
import contextlib
import enum
import logging
import os
import shlex
import signal
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypeVar

from typing_extensions import override

from .command_frame import SESSION_SHELL
from .host import _EXEC_REAP_TIMEOUT, EXEC_CANCEL_REAP_BOUND

logger = logging.getLogger(__name__)

SESSION_MARK = "OTTO_SESSION"
"""The environment variable a shell session without a terminal is started with.

Its value is the session's token; every process the shell starts inherits
it, and that is how a command otto gives up on is found and stopped
(:func:`reap_marked_command`). The stop ends the session, so it reaches
everything the session started and is still running, a background job from
an earlier command included. A command can read the variable; a process
started without it is not stopped, which is how a daemon otto launches
(:func:`otto.host.daemon.launch_command`) outlives the stop."""


def reap_marked_script(variable: str) -> str:
    """Return the ``sh`` script that kills every process started with ``variable=$1`` set.

    It reads ``/proc/<pid>/environ``, which holds the environment a process
    was STARTED with: the marked shell itself, each subshell it forks and
    each command it runs, however deep, and whatever outlived its parent.

    Every marked process is frozen (``STOP``, two sweeps) before any is
    killed. A sweep visits ``/proc`` in name order, so it can reach a
    command before the shell that waits for it; killed first, the command
    would release that shell to run its next statement before the sweep
    reached it. A frozen shell runs nothing. The ``KILL`` sweep is then
    repeated until one finds nothing (ten at most), so a process started
    mid-sweep is caught by the next. The script's own
    processes do not carry the token and are never matched. Only what
    BusyBox ``sh`` and ``grep`` have; where there is no ``/proc`` it matches
    nothing and ends. It reaches what the user running it may read: another
    user's process (after ``su`` or ``sudo``) and a process that rewrote its
    environment area are not matched. Two narrow gaps are accepted: a bash
    that turned job control on (``set -m``) is handed control when its
    command is frozen, and may run on if the sweep reaches the command
    first; and a process forked by one that was itself forked during the
    two ``STOP`` sweeps is killed without having been frozen.
    """
    return (
        "t=$1; s() { f=; for e in /proc/[0-9]*/environ; do "
        f'if grep -q "{variable}=$t" "$e" 2>/dev/null; then '
        'p=${e#/proc/}; kill "$1" "${p%/environ}" 2>/dev/null && f=1; fi; done; }; '
        "s -STOP; s -STOP; "
        'n=0; while [ $n -lt 10 ]; do s -KILL; [ -n "$f" ] || break; n=$((n + 1)); done; :'
    )


def reap_marked_command(variable: str, token: str) -> str:
    """Return the command that runs :func:`reap_marked_script` for *token*."""
    return f"sh -c {shlex.quote(reap_marked_script(variable))} sh {token}"


def marked_shell_command(variable: str, token: str) -> str:
    """Return the command that starts the user's login shell with ``variable=token`` set.

    For a channel opened with a command, which the server hands to the
    user's shell: that shell replaces itself with a login shell (``-l``, so
    the profile is read as a plain shell channel reads it) that reads its
    commands from the channel. The token is in the new shell's own start
    environment, where :func:`reap_marked_script` finds it.

    The login shell's stderr is joined to its stdout. A server started
    without a pty gives the command two pipes and forwards them in the order
    it finds them, not the order they were written, so output from the two
    could arrive out of order and stderr could arrive after the command's
    end marker. One pipe keeps the order written.
    """
    return f"{variable}={token} exec {SESSION_SHELL} -l 2>&1"


class StopSignal(enum.Enum):
    """The first signal a stop sends: what the stop means."""

    TERMINATE = "TERM"
    """A timeout: the ordinary ask to stop."""
    HANG_UP = "HUP"
    """An interrupt: the terminal went away."""


class StoppableProcess(Protocol):
    """A started command, as :func:`stop_process` stops it."""

    def send(self, sig: "StopSignal | Literal['KILL']") -> None:
        """Deliver *sig*; raise :class:`ProcessLookupError` when the command is gone.

        Any other failure to deliver is an :class:`OSError`, whatever the route.
        """
        ...

    async def wait(self) -> None:
        """Return once the command has exited; a route failure is an :class:`OSError`."""
        ...

    def group_gone(self) -> bool:
        """Whether nothing is left of the command's process group, as far as otto can see.

        ``True`` on a route that cannot see the group (the server's to
        signal), so the stop never waits for it there.
        """
        ...

    def kill_group(self) -> None:
        """``KILL`` what is left of the command's process group; an empty group is no error."""
        ...


async def _exited_within(proc: StoppableProcess, bound: float) -> bool:
    """Whether *proc* exits within *bound* seconds."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=bound)
    except asyncio.TimeoutError:
        return False
    return True


async def stop_process(proc: StoppableProcess, *, first: StopSignal) -> None:
    """Stop *proc*: *first*, then ``KILL`` what outlives it by ``_EXEC_REAP_TIMEOUT``.

    The command itself is killed if it has not exited by the end of the
    grace. If it has, what is left of its process group is given the rest
    of the grace to go, then killed; a group that is already empty ends the
    stop with no wait. A :class:`ProcessLookupError` from any signal means
    the command is gone and ends the escalation. Every wait is bounded, so a
    command that never reports its exit cannot hold the stop open.
    """
    loop = asyncio.get_running_loop()
    grace_ends = loop.time() + _EXEC_REAP_TIMEOUT
    try:
        proc.send(first)
    except ProcessLookupError:
        return
    if await _exited_within(proc, _EXEC_REAP_TIMEOUT):
        await _kill_group_left_after(proc, grace_ends - loop.time())
        return
    try:
        proc.send("KILL")
    except ProcessLookupError:
        return
    await _exited_within(proc, _EXEC_REAP_TIMEOUT)


async def _kill_group_left_after(proc: StoppableProcess, bound: float) -> None:
    """Give what is left of *proc*'s group *bound* seconds to go, then ``KILL`` it.

    The first probe is immediate, so an empty group costs no wait.
    """
    from ..utils import WaitTimeoutError, wait_for_async

    try:
        await wait_for_async(
            proc.group_gone,
            max(bound, 0.0),
            interval=_GROUP_POLL_INTERVAL,
            on_timeout="process group outlived the grace",
        )
    except WaitTimeoutError:
        proc.kill_group()


_GROUP_POLL_INTERVAL = 0.05
"""Seconds between looks at whether a stopped command's process group has emptied."""


async def _stop_quietly(proc: StoppableProcess, *, first: StopSignal) -> None:
    """:func:`stop_process` for either arm: a failing stop is logged, never raised.

    The stop's own failure (a channel already gone, ``EPERM`` from
    ``killpg``) must not replace what the exec reports: the exception
    unwinding an interrupted exec, or a timed-out exec's timed-out result.
    The command is what the result describes; a stop that could not be
    delivered changes nothing the caller can act on.
    """
    try:
        await stop_process(proc, first=first)
    except OSError as exc:
        logger.debug("stop of %s command failed: %s", first.value, exc)


async def stop_timed_out(proc: StoppableProcess, *, what: str) -> None:
    """Stop a timed-out process-route command: ``TERM`` first, bounded, never torn.

    Run under :func:`~otto.lifecycle.compensate`, so a cancellation landing
    while the command is stopped is held until the stop finishes, and the
    stop is bounded by :data:`~otto.host.host.EXEC_CANCEL_REAP_BOUND`. A
    failing stop is logged, as on :func:`stop_interrupted`, so the caller
    still returns the timed-out result.
    """
    from ..lifecycle import compensate

    await compensate(
        _stop_quietly(proc, first=StopSignal.TERMINATE),
        timeout=EXEC_CANCEL_REAP_BOUND,
        what=what,
    )


async def stop_interrupted(proc: StoppableProcess, exc: BaseException, *, what: str) -> None:
    """Hang up a process-route command whose exec is unwinding with *exc*; never replace *exc*.

    ``HUP`` first, the end a shell-routed command meets when its pty closes,
    which a ``nohup``'d child ignores; what is still in a local process
    group after the grace is killed (see the module docstring). A failing stop is logged,
    and a cancellation *exc* is held by :func:`~otto.lifecycle.compensate`,
    so the caller's ``raise`` re-raises what unwound the exec.
    """
    from ..lifecycle import compensate, held_cancellation

    await compensate(
        _stop_quietly(proc, first=StopSignal.HANG_UP),
        timeout=EXEC_CANCEL_REAP_BOUND,
        what=what,
        held=held_cancellation(exc),
    )


_T = TypeVar("_T")


async def started_or_stopped(
    start: "Awaitable[_T]",
    stop: "Callable[[_T, BaseException], Awaitable[None]]",
    *,
    what: str,
) -> _T:
    """Await *start* (a process start) to the end, even when the caller is cancelled meanwhile.

    A process is running before the await that starts it returns, so a
    cancellation landing inside it would be handled by the library, which
    stops the leader only. Here the start runs under
    :func:`~otto.lifecycle.compensate` instead: a cancellation is held until
    it has finished (bounded by the run policy's teardown deadline once one
    is held; unbounded, as before, when nobody cancels), then *stop* is
    called with what it returned and the cancellation, and the cancellation
    is re-raised. A start that fails raises its own error; with no
    cancellation this is a plain await. A start the teardown deadline
    abandons is cancelled, and what it started is left to the library: the
    local leader killed alone, the ssh channel closed.
    """
    from ..lifecycle import compensate

    started: list[_T] = []

    async def _start() -> None:
        started.append(await start)

    try:
        await compensate(_start(), what=what)
    except asyncio.CancelledError as exc:
        if started:
            await stop(started[0], exc)
        raise
    return started[0]


_LOCAL_SIGNALS = {
    StopSignal.TERMINATE: signal.SIGTERM,
    StopSignal.HANG_UP: signal.SIGHUP,
    "KILL": signal.SIGKILL,
}


class LeaderExitProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """Local exec's subprocess protocol: also says when the LEADER has exited.

    :meth:`asyncio.subprocess.Process.wait` cannot say that on every Python
    otto supports: from 3.11.x on it returns only once every pipe has closed
    as well, so a group member that survives the leader and holds its output
    (a child ignoring ``HUP``) keeps it pending. asyncio calls
    :meth:`process_exited` when the leader is reaped, before and apart from
    pipe closure, on 3.10 through 3.14 alike; :attr:`leader_exited` is set
    there.
    """

    def __init__(self, limit: int, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(limit=limit, loop=loop)
        self.leader_exited = asyncio.Event()

    @override
    def process_exited(self) -> None:
        """Record the leader's exit, then let asyncio retire the transport as usual."""
        self.leader_exited.set()
        super().process_exited()


class LocalStoppable:
    """A local exec's process: signals go to its process group until the leader is known reaped.

    The command was started with ``start_new_session=True``, so its group id
    is the leader's pid; no lookup, which could race a fast exit.

    "Known reaped" is asyncio's ``returncode``, which lags the reap itself: on
    Python 3.10 and 3.11 the threaded child watcher can reap the leader before
    the loop sets ``returncode``. A ``killpg`` in that window signals whatever
    group members survive the leader, or fails with ``ESRCH``
    (:class:`ProcessLookupError`, which ends the stop) when none do. While a
    member survives, the pid is a live process group's id and is not handed
    to a new process; with none left the window is the few loop turns before
    ``returncode`` is set.

    The wait is on the leader alone (*leader_exited*, from
    :class:`LeaderExitProtocol`), never on the pipes: a member that outlives
    the leader is left as it is, and so is the output it holds.
    """

    def __init__(self, proc: "asyncio.subprocess.Process", leader_exited: asyncio.Event) -> None:
        self._proc = proc
        self._leader_exited = leader_exited

    def send(self, sig: "StopSignal | Literal['KILL']") -> None:
        """Signal the group; raise ProcessLookupError once the leader is known reaped or none is."""
        if self._proc.returncode is not None:
            raise ProcessLookupError(self._proc.pid)
        os.killpg(self._proc.pid, _LOCAL_SIGNALS[sig])

    async def wait(self) -> None:
        """Return once the leader has exited, whoever still holds its output."""
        await self._leader_exited.wait()

    def group_gone(self) -> bool:
        """Whether the group has no member left (``killpg`` of signal 0 finds none).

        While a member survives, the group id is not handed to a new process,
        so the probe and :meth:`kill_group` reach this command's group only.
        Where only another user's processes remain (a local ``sudo`` command)
        the probe raises :class:`PermissionError`, which the quiet stop logs,
        and that group is not killed.
        """
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def kill_group(self) -> None:
        """``KILL`` every member left in the group."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._proc.pid, signal.SIGKILL)


class ChannelStoppable:
    """An ssh exec channel's process: the channel delivers each signal to the remote command.

    An :class:`asyncssh.Error` from the channel is raised as :class:`OSError`
    (the :class:`StoppableProcess` contract), so the stop's error handling
    never imports asyncssh on local exec's path.
    """

    def __init__(self, process: Any) -> None:
        self._process = process

    def send(self, sig: "StopSignal | Literal['KILL']") -> None:
        """Ask the channel to deliver *sig*."""
        from ._asyncssh import asyncssh  # the ssh route has loaded it already

        try:
            if sig == "KILL":
                self._process.kill()
            elif sig is StopSignal.TERMINATE:
                self._process.terminate()
            else:
                self._process.send_signal(sig.value)
        except asyncssh.Error as exc:
            raise OSError(str(exc)) from exc

    async def wait(self) -> None:
        """Return once the channel reports the command's exit."""
        from ._asyncssh import asyncssh

        try:
            await self._process.wait()
        except asyncssh.Error as exc:
            raise OSError(str(exc)) from exc

    def group_gone(self) -> bool:
        """Answer ``True``: the remote group is the server's to signal, not otto's to see."""
        return True

    def kill_group(self) -> None:
        """Nothing: see :meth:`group_gone`."""
