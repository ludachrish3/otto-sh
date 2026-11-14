"""One stop sequence for a command otto started as a process: local exec and the ssh exec channel.

A timed-out command is asked to stop with ``TERM`` ("you took too long"); an
interrupted one is hung up with ``HUP`` ("the terminal went away", the end a
shell-routed command meets when its pty closes, which spares a deliberately
``nohup``'d child). Either way the command can ignore the first signal, so
the wait for it is bounded by ``_EXEC_REAP_TIMEOUT`` and only a command that
OUTLIVES that first signal is killed. A command that obeys ends the
escalation, so its group's survivors (a ``nohup`` child, a child that traps
``TERM``) are left as they are.

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
"""

import asyncio
import enum
import logging
import os
import signal
from typing import Any, Literal, Protocol

from typing_extensions import override

from .host import _EXEC_REAP_TIMEOUT, EXEC_CANCEL_REAP_BOUND

logger = logging.getLogger(__name__)


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


async def _exited_within(proc: StoppableProcess, bound: float) -> bool:
    """Whether *proc* exits within *bound* seconds."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=bound)
    except asyncio.TimeoutError:
        return False
    return True


async def stop_process(proc: StoppableProcess, *, first: StopSignal) -> None:
    """Stop *proc*: *first*, then ``KILL`` only if it outlives *first* by ``_EXEC_REAP_TIMEOUT``.

    A :class:`ProcessLookupError` from any signal means the command is gone
    and ends the escalation. Both waits are bounded, so a command that never
    reports its exit cannot hold the stop open.
    """
    try:
        proc.send(first)
    except ProcessLookupError:
        return
    if await _exited_within(proc, _EXEC_REAP_TIMEOUT):
        return
    try:
        proc.send("KILL")
    except ProcessLookupError:
        return
    await _exited_within(proc, _EXEC_REAP_TIMEOUT)


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
    so a deliberately ``nohup``'d child survives. A failing stop is logged,
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
        import asyncssh  # the ssh route has loaded it already

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
        import asyncssh

        try:
            await self._process.wait()
        except asyncssh.Error as exc:
            raise OSError(str(exc)) from exc
