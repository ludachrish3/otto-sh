"""What every host owes a caller whose command outlives its budget.

Not "raises something". otto's documented timeout is not an exception at all --
``HostSession.run_cmd`` catches ``asyncio.TimeoutError``, attempts recovery via
Ctrl+C, and RETURNS a ``CommandResult`` whose ``timed_out`` is True. That
distinction is the point of the contract: a caller that wrapped ``run()`` in
``try/except`` and got a result object back would read a timeout as an ordinary
failure, and ``timed_out`` exists precisely so it does not have to string-match
``value`` to tell the two apart.

The second assertion is the one that earns the file. A timeout that leaves the
session dead is a DIFFERENT bug wearing the same failure -- every later command
on that host fails, and the first test to notice is whichever one happened to
run next. Per issue #260 a wedged console is a real failure mode in this
codebase, so "the session is still usable afterwards" is asserted here, on the
same session, rather than left to be discovered downstream.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: cells whose userland
has a command that can outlive a budget at all. See :func:`applicable_cell`.
"""

import pytest

from otto.utils import Status
from tests.conformance._framing import assert_single_line_answer
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

# The budget the command must outlive. The MARGIN is a discriminator, not
# padding: each vocabulary's long-running command runs for at least an order of
# magnitude longer (``tests/unit/test_conformance_bed.py`` pins that ratio over
# every vocabulary), so a backend that quietly ran the command to completion
# and reported success cannot be confused with one that timed out, on any load
# this suite runs under. Nothing asserts the elapsed time -- the per-test
# SIGALRM (180s) is what bounds a timeout that never fires at all.
#
# MEASURED against the bed's round-trip floor, because a budget this small
# against a hopped host could time out on LATENCY alone and pass for the wrong
# reason. At load 1.08 on this dev VM, `(exit 0)` answered in 0.001s on
# `test1:ssh:sftp` and 0.043s on `bb1161:telnet:shell` and `bb1350:telnet:shell`
# after the connection was warm (0.25s and 0.087s respectively on the first
# command, which carries the connect). The nearest of those is 17x under this
# budget, so the margin holds today -- and it is a measurement with a shelf
# life, not a proof.
_BUDGET_S = 0.75


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about: the ones with a stimulus that can outlive a budget.

    Read by ``tests/conformance/conftest.py``'s ``pytest_generate_tests``,
    which parametrizes ``resolved_cell`` over the drawn cells this answers
    True for. Today it narrows nothing in the hermetic venue (all 8 cells are
    a POSIX shell with ``sleep``) and excludes exactly the bed's 7
    ``bed-zephyr`` cells.

    TWO INDEPENDENT REASONS, BOTH MEASURED, AND THE SECOND IS THE ONE THAT
    MAKES THIS A NARROWING RATHER THAN A MISSING STIMULUS:

    1. **No stimulus exists.** Every Zephyr command anywhere in this suite
       (``help``, ``version``, ``kernel uptime``, an unknown one) returns
       essentially instantly. The Zephyr shell is synchronous on the shell
       thread, so a command that blocked for the budget's duration would block
       the SHELL ITSELF -- the very thing whose survival the second half of
       this contract asserts. A stimulus that made the first assertion pass
       would make the second unmeasurable.
    2. **The recovery half is the issue #260 wedge shape.** Driving a
       single-client telnet console to a timeout and then asserting that the
       session recovered is the exact sequence that has taken guests down: when
       a send fails at accept time the guest re-initialises its telnet backend
       and then refuses every connection until ``make qemu-restart``.

    HOW THIS DIFFERS FROM ``test_transfer_contract.py``'s DOMAIN, and the
    difference is worth stating because that one is the model. Its predicate
    reads OTTO'S OWN ANSWER -- ``remote_scratch`` is ``None`` for exactly the
    hosts whose filesystem reports ``supports_transfer`` False. There is no
    equivalent property here: otto does not describe a userland as "can be made
    to block", so this reads the SUITE's vocabulary
    (:attr:`~tests.conformance._vocabulary.Vocabulary.long_running_command`)
    instead. That is still a fact derived from lab data rather than an element
    sniff -- the vocabulary is selected by the userland axis ``axes_for``
    resolves off the host otto builds -- but it is this suite's answer, not
    otto's, and a reader should not assume otherwise.

    NOT A SKIP AND NOT AN EXCLUSION FROM THE SPACE. A skip inside a drawn cell
    reports success for a contract nobody ran. Dropping the cell is worse: a
    Zephyr host reports a single ``(telnet, console)`` pair, so its one cell IS
    the guest, and dropping it would take that guest's exec coverage with it.

    THE UNCOVERED HALF IS UNCOVERED, and that is stated rather than delegated.
    An earlier draft of this docstring said the depth lives per-backend in
    ``tests/integration/host/test_host_contract.py``. MEASURED, that is FALSE:
    ``grep -rn 'timed_out' tests/integration/host/`` returns ONE line, and it
    is a docstring cross-reference to a UNIT test
    (``test_hop_integration.py:528``) -- no file under
    ``tests/integration/host/`` ASSERTS ``timed_out``. That file's contracts
    are run/exec, transfer round-trip and transfer progress.
    The only bed-side timeout assertion in the repo is
    ``tests/integration/busybox_bed/test_session_frame.py``'s
    ``test_a_timed_out_command_leaves_no_orphan_process``, and its target is a
    BusyBox guest, not a Zephyr one. So what an embedded backend does with a
    command that outlives its budget is asserted NOWHERE, and pointing at a
    home that does not exist is worse than a hole nobody has closed: it stops
    anyone looking. If a Zephyr timeout contract is ever wanted, it needs a
    stimulus that does not block the shell thread and a recovery assertion that
    does not reproduce #260 -- neither of which exists today.

    Which cells this includes and excludes is pinned in
    ``tests/unit/test_conformance_bed.py``, so a change that quietly widens or
    narrows the domain fails instead of passing.
    """
    return resolved.vocabulary.long_running_command is not None


async def test_a_command_exceeding_its_budget_fails_the_documented_way(
    resolved_cell: ResolvedCell,
) -> None:
    """The documented failure, and a session that survives it.

    Both commands run inside one ``open_host()`` so the second is measured on
    the SAME session the first timed out on. Reopening the host between them
    would assert that a fresh session works, which nobody doubted.

    ``exit_code`` is asserted separately from ``retcode`` because they diverge
    here and the divergence is the documented behaviour: the command never ran
    to completion, so ``retcode`` is -1, and ``CommandResult.exit_code`` maps
    that to ssh's 255 rather than passing -1 out to a shell that would read it
    as 255 anyway by accident.

    The survival check goes through the same
    ``assert_single_line_answer`` the exec contract's sequence uses, because it
    is the same question asked at a different moment -- issue the vocabulary's
    single-line command, and get its answer back clean. One spelling so the two
    cannot drift.
    """
    words = resolved_cell.vocabulary
    assert words.long_running_command is not None, (
        f"{resolved_cell.cell}: this cell has no command that outlives a budget, so "
        f"`applicable_cell` should have kept it out of this contract's domain"
    )

    async with resolved_cell.open_host() as host:
        timed_out = (await host.run(words.long_running_command, timeout=_BUDGET_S)).only
        afterwards = (await host.run(words.single_line_command)).only

    cell = resolved_cell.cell
    assert timed_out.timed_out is True, (
        f"{cell}: `{words.long_running_command}` under a {_BUDGET_S}s budget came "
        f"back with timed_out=False -- {timed_out.status!r} {timed_out.value!r}"
    )
    assert timed_out.is_ok is False, f"{cell}: a timed-out command is not ok"
    assert timed_out.status is Status.Error, (
        f"{cell}: a timeout is Status.Error, not {timed_out.status!r} -- "
        f"Status.Failed would read as a command that ran and exited non-zero"
    )
    assert timed_out.retcode == -1, (
        f"{cell}: a command killed by its timeout never produced a return code, "
        f"so retcode stays -1; got {timed_out.retcode}"
    )
    assert timed_out.exit_code == 255, (
        f"{cell}: retcode -1 maps to ssh's 255, got {timed_out.exit_code}"
    )
    assert timed_out.value.startswith(f"Command timed out after {_BUDGET_S}s"), (
        f"{cell}: the diagnostic must name the budget it blew, got {timed_out.value!r}"
    )

    assert afterwards.is_ok, (
        f"{cell}: the session did not survive the timeout -- the next command "
        f"reported {afterwards.status!r} {afterwards.value!r}. A timeout that "
        f"wedges the session is a different bug wearing the same failure"
    )
    assert_single_line_answer(afterwards.value, words, cell, "the command after the timeout")
