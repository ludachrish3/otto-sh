"""What every host owes a caller whose command is killed by a signal: 128+N, and a usable host.

A shell reports a command that signal ``N`` killed as ``128 + N``; a process
route learns the same death as asyncio's ``-N``, or (on the ssh channel) as
no exit status at all. The promise is the shell's number on every route, so a
caller reading ``retcode`` cannot tell which route ran the command.

The stimulus varies by entry, the asserted property does not: ``run`` types
the command into a shell, so the killed process must be a child of it
(``sh -c '…'``), while ``exec`` hands the command to a process of its own, so
an ``exec`` makes the killed process that process itself.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: see
:func:`applicable_cell`.
"""

import pytest

from otto.host.exec_route import ExecRoute
from otto.utils import Status
from tests.conformance._entry import RUN, expected_route, issue, opened
from tests.conformance._framing import assert_single_line_answer
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

_PROCESS_ROUTES = {ExecRoute.LOCAL_SUBPROCESS.value, ExecRoute.SSH_CHANNEL.value}


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    A vocabulary with no ``signal_kill_command`` has no command that can be
    made to die by a signal (the Zephyr shell runs its commands on the shell
    thread). On ``exec``, only the process routes are in the domain for now:
    where ``exec`` types the command into a shell session, the exec stimulus
    would end that session's shell itself until otto runs an exec frame of its
    own there.
    """
    if resolved.vocabulary.signal_kill_command is None:
        return False
    return resolved.entry == RUN or expected_route(resolved) in _PROCESS_ROUTES


@pytest.mark.entries("run", "exec")
@pytest.mark.observable(
    "retcode of the signal stimulus, then whether `{words.single_line_command}` answers "
    "on the same host"
)
async def test_a_command_killed_by_a_signal_reports_128_plus_n(resolved_cell: ResolvedCell) -> None:
    """A signal death is ``128 + N``, a failure, and the host answers the next command."""
    words = resolved_cell.vocabulary
    stimulus = (
        words.signal_kill_command if resolved_cell.entry == RUN else words.exec_signal_kill_command
    )
    assert stimulus is not None, (
        f"{resolved_cell.cell}: the domain admitted a cell with no stimulus"
    )
    async with opened(resolved_cell) as host:
        killed = await issue(host, resolved_cell, stimulus)
        afterwards = await issue(host, resolved_cell, words.single_line_command)
    assert (killed.retcode, killed.status) == (words.signal_exit_code, Status.Failed), (
        f"{resolved_cell.cell}: `{stimulus}` reported retcode {killed.retcode} "
        f"({killed.status}), not {words.signal_exit_code} and a failure"
    )
    assert_single_line_answer(
        afterwards.value, words, resolved_cell.cell, "the command after the signal"
    )


@pytest.mark.positive_control("exec-signal-exit")
@pytest.mark.entries("run", "exec")
async def test_control_143_is_reported_as_143_and_an_ordinary_command_as_success(
    resolved_cell: ResolvedCell,
) -> None:
    """A command exiting 143 with no signal reports 143; an ordinary one reports 0.

    Both halves in one control, because a surface has exactly one: the first
    fails an implementation that rewrites a genuine 143, the second one that
    answers 143 for every command.
    """
    words = resolved_cell.vocabulary
    lookalike = words.signal_lookalike_command
    assert lookalike is not None, (
        f"{resolved_cell.cell}: the domain admitted a cell with no control"
    )
    async with opened(resolved_cell) as host:
        exited = await issue(host, resolved_cell, lookalike)
        ordinary = await issue(host, resolved_cell, words.single_line_command)
    assert (exited.retcode, exited.status) == (143, Status.Failed)
    assert (ordinary.retcode, ordinary.status) == (0, Status.Success)
