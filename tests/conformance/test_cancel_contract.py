"""Cancelling an ``exec`` stops its command before the cancel returns, and the host stays usable.

A caller that cancels an ``exec`` (a ``wait_for`` around it, a task group that
lost a sibling) gets the cancellation back, and by then the command otto
started is stopped: on a shell route the interrupt and the shell's
confirmation (``ShellSession._stop_under_bound``), on a process route the
hang-up of the process (``otto.host.process_stop.stop_interrupted``). The
host then answers the next command.

The command is cancelled once the route under test has relayed its start
token (:func:`~tests.conformance._started.output_seen`), so the cancel lands
on a command that is running, not on one still being written. Whether it
still runs is read off the route under test by the witness
(:func:`~tests.conformance._witness.witness`). Each call goes through
``issue``, which asserts the route the cell's entry must take.

WHAT THIS CONTRACT IS ABOUT: see :func:`applicable_cell`.
"""

import asyncio
import dataclasses
from asyncio import FIRST_COMPLETED
from contextlib import suppress

import pytest

from otto.host import process_stop
from otto.host.exec_route import ExecRoute
from otto.host.host import _EXEC_REAP_TIMEOUT, EXEC_CANCEL_REAP_BOUND
from otto.host.session import ShellSession
from otto.utils import WaitTimeoutError, wait_for_async
from tests._fixtures import witness_shell
from tests._fixtures.session_stop import old_mark_only
from tests._fixtures.witness_shell import STARTED_TOKEN
from tests.conformance._entry import expected_route, issue, on_console, opened
from tests.conformance._framing import assert_single_line_answer
from tests.conformance._resolved import ResolvedCell
from tests.conformance._started import output_seen
from tests.conformance._vocabulary import Vocabulary
from tests.conformance._witness import Witness, witness

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

_START_WAIT_S = 10.0
"""How long the contract waits for the start token: a runaway bound, never a discriminator."""


@dataclasses.dataclass(frozen=True, slots=True)
class CancelForm:
    """How one route's witnessed stimulus is spelled around the vocabulary's command."""

    around: str
    """A ``str.format`` template over ``{command}``."""

    ignores: str = ""
    """The signals the witnessed shell itself ignores (:func:`witness_shell.ignoring`), if any."""


CANCEL_FORMS: "dict[str, CancelForm]" = {
    # The stop is a Ctrl-C on the session's terminal; the release that competes
    # with it is the hang-up of a session the stop left dead, when it is
    # closed. A witnessed process that ignores HUP and dies on INT meets the
    # stop alone. The witnessed shell sets the ignore itself: older BusyBox
    # shells drop one they inherit (`witness_shell.ignoring`). The subshell
    # keeps the `exec` off the session's own shell.
    ExecRoute.POOLED_SHELL.value: CancelForm(around="({command})", ignores="HUP"),
    # The stop signals the command's process group (HUP, then KILL); the
    # release that competes with it is asyncio's kill of the LEADER alone when
    # the transport is closed. A witnessed group member that is not the
    # leader, with HUP at its default, meets the stop alone.
    ExecRoute.LOCAL_SUBPROCESS.value: CancelForm(around="({command})"),
    # The stop signals the command through the channel (HUP, then KILL);
    # closing a pty-less channel signals nothing. The command as it is.
    ExecRoute.SSH_CHANNEL.value: CancelForm(around="{command}"),
}
"""The witnessed stimulus, by the route that runs it: the form ONLY that route's cancel
stop can end before the witness looks, whatever else happens after a cancel. Each
wraps the vocabulary's ``exec_pid_long_running_template``; what is asserted is the
same on every route."""


def cancel_stimulus(resolved: ResolvedCell, path: str) -> str:
    """The witnessed command for *resolved*'s route, writing its witness to *path*."""
    template = resolved.vocabulary.exec_pid_long_running_template
    assert template is not None
    form = CANCEL_FORMS[expected_route(resolved)]
    command = template.format(path=path)
    if form.ignores:
        command = witness_shell.ignoring(command, form.ignores)
    return form.around.format(command=command)


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    Two kinds are outside it. A vocabulary with no
    ``exec_pid_long_running_template`` has no command that keeps running
    until it is cancelled: a Zephyr board's firmware has none that blocks
    (#1066), and a Zephyr guest is never driven to a cancellation on the bed
    (#260); the embedded stop is checked by a unit test
    (``tests/unit/host/test_embedded_cancel.py``). And on a ``term`` of
    ``console`` otto cannot watch the command from a second connection,
    since the console takes one client, so the stop is checked by a unit test
    that models the console (``tests/unit/host/test_session_stop.py``).
    """
    return resolved.vocabulary.exec_pid_long_running_template is not None and not on_console(
        resolved
    )


_STOPPED_WITHIN_S = _EXEC_REAP_TIMEOUT
"""How long the witness may take to read a cancelled command stopped: the wait otto's
stop gives a signalled command to exit (``process_stop.stop_process``), a rainy-day
bound only. It sits well inside the witnessed sleeper's own life, so a command nothing
stops is still running at the bound (:func:`_seen_stopped` asserts the margin)."""


async def _seen_stopped(w: Witness, words: Vocabulary) -> bool:
    """Whether the witness reads the cancelled command stopped within :data:`_STOPPED_WITHIN_S`.

    The one judgement the contract and its control share. Nothing more is
    done to the host meanwhile; the first read that sees it stopped answers,
    so a command the cancel already stopped costs one read. A bound at all
    because on a local process the cancel returns once the leader is ended
    and its process group hung up, and a member hung up may still be exiting
    then (the exec page's known gaps).
    """
    assert words.long_running_seconds is not None
    assert words.long_running_seconds >= 2 * _STOPPED_WITHIN_S, (
        f"the witnessed command sleeps {words.long_running_seconds}s, too close to the "
        f"{_STOPPED_WITHIN_S}s bound to tell a stopped command from one that ended by itself"
    )

    async def stopped() -> bool:
        return await w.state() == "stopped"

    try:
        await wait_for_async(
            stopped, _STOPPED_WITHIN_S, on_timeout="the cancelled command still runs"
        )
    except WaitTimeoutError:
        return False
    return True


async def _cancel_once_started(host, resolved: ResolvedCell, cmd: str) -> None:
    """Issue *cmd*, cancel it once its start token arrived, and require the cancellation back.

    A call that ends before its token arrives (a route refused by ``issue``,
    a command that returned) re-raises or fails here at once rather than
    waiting out the bound.
    """
    with output_seen(host, STARTED_TOKEN) as started:
        task = asyncio.ensure_future(issue(host, resolved, cmd))
        seen = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait({task, seen}, timeout=_START_WAIT_S, return_when=FIRST_COMPLETED)
            if task.done():
                returned = task.result()
                raise AssertionError(
                    f"{resolved.cell}: the command returned before it was cancelled -- "
                    f"{returned.status!r} {returned.value!r}"
                )
            assert seen.done(), (
                f"{resolved.cell}: the command printed no {STARTED_TOKEN!r} in {_START_WAIT_S}s"
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            seen.cancel()
            # Inside the host's lifetime, so a command that never started is not left behind.
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


@pytest.mark.entries("exec", "exec-shell")
@pytest.mark.observable(
    "after cancelling a witnessed `{words.long_running_command}`, in the form only its "
    "route's cancel stop can end, once the route under test relayed its start token: "
    "whether the process it recorded (its pid and start time) is read as stopped, read "
    "again, if it is not yet, for as long as otto's stop waits for a signalled command to "
    "exit, and whether the same host answers "
    "`{words.single_line_command}` afterwards"
)
async def test_cancelling_an_exec_stops_its_command_and_leaves_the_host_usable(
    resolved_cell: ResolvedCell,
) -> None:
    """The cancel returns with the command stopped, and the same host answers the next command."""
    words = resolved_cell.vocabulary
    async with opened(resolved_cell) as host:
        async with witness(resolved_cell, host, purpose="cancel") as w:
            await _cancel_once_started(host, resolved_cell, cancel_stimulus(resolved_cell, w.path))
            assert await _seen_stopped(w, words), (
                f"{resolved_cell.cell}: the cancelled command is still running "
                f"{_STOPPED_WITHIN_S}s after the cancel returned"
            )
        afterwards = await issue(host, resolved_cell, words.single_line_command)
    assert afterwards.is_ok, (
        f"{resolved_cell.cell}: the host did not answer after the cancel -- "
        f"{afterwards.status!r} {afterwards.value!r}"
    )
    assert_single_line_answer(
        afterwards.value, words, resolved_cell.cell, "the command after the cancel"
    )


class _StopsNothing:
    """A process route's ``stop_interrupted`` that signals nothing and keeps what it was handed.

    :meth:`released` then waits for those processes to end once the
    witness's cleanup has killed what they run, so a process the disabled
    stop would have reaped is reaped before the test ends, not collected
    still running.
    """

    def __init__(self) -> None:
        self._handed: list[process_stop.StoppableProcess] = []

    async def __call__(
        self, proc: "process_stop.StoppableProcess", _exc: BaseException, *, what: str
    ) -> None:
        self._handed.append(proc)

    async def released(self) -> None:
        """Wait, bounded as the real stop is, for every process this was handed to end."""
        for proc in self._handed:
            await asyncio.wait_for(proc.wait(), EXEC_CANCEL_REAP_BOUND)


# ==========================================================================
# POSITIVE CONTROL -- proof that this observable CAN GO RED on this cell
# ==========================================================================


@pytest.mark.entries("exec", "exec-shell")
@pytest.mark.positive_control("exec-cancel")
async def test_control_with_the_stop_disabled_the_command_is_seen_running(
    resolved_cell: ResolvedCell,
) -> None:
    """With the route's stop disabled, the cancelled command reads as running.

    The one seam each route's cancel stop goes through is replaced: on a
    shell route ``ShellSession._stop_interrupted_command`` by the cancel arm
    that only marked the session for its next use, on a process route
    ``process_stop.stop_interrupted`` by a stop that stops nothing. The
    witness must then see the command running, so a host whose cancel leaves
    the command behind fails the contract. The witness context's cleanup
    kills it, and the host must still answer the next command, through the
    same route check as the contract's.

    The stimulus is the contract's own (:func:`cancel_stimulus`), so the
    control and the contract differ only in the disabled seam.
    """
    words = resolved_cell.vocabulary
    stops_nothing = _StopsNothing()
    with pytest.MonkeyPatch.context() as patch:
        if expected_route(resolved_cell) == ExecRoute.POOLED_SHELL.value:
            patch.setattr(ShellSession, "_stop_interrupted_command", old_mark_only)
        else:
            patch.setattr(process_stop, "stop_interrupted", stops_nothing)
        async with opened(resolved_cell) as host:
            async with witness(resolved_cell, host, purpose="cancel") as w:
                cmd = cancel_stimulus(resolved_cell, w.path)
                await _cancel_once_started(host, resolved_cell, cmd)
                assert not await _seen_stopped(w, words), (
                    f"{resolved_cell.cell}: with the stop disabled the cancelled command "
                    f"reads as stopped, so the contract's `stopped` says nothing about the stop"
                )
            await stops_nothing.released()
            await issue(host, resolved_cell, words.single_line_command)
