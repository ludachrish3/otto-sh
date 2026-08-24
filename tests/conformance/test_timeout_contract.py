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
"""

import pytest

from otto.utils import Status
from tests.conformance._cells import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

# The budget, and a command that outlives it by more than an order of
# magnitude. The margin is a discriminator, not padding: at 10s against 0.75s,
# a backend that quietly ran the command to completion and reported success
# cannot be confused with one that timed out, on any load this suite runs
# under. Nothing asserts the elapsed time -- the per-test SIGALRM (180s) is
# what bounds a timeout that never fires at all.
_BUDGET_S = 0.75
_SLEEP_S = 10

_ALIVE_TOKEN = "otto-conformance-still-alive"


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
    """
    async with resolved_cell.open_host() as host:
        timed_out = (await host.run(f"sleep {_SLEEP_S}", timeout=_BUDGET_S)).only
        afterwards = (await host.run(f"printf '%s\\n' {_ALIVE_TOKEN}")).only

    cell = resolved_cell.cell
    assert timed_out.timed_out is True, (
        f"{cell}: `sleep {_SLEEP_S}` under a {_BUDGET_S}s budget came back with "
        f"timed_out=False -- {timed_out.status!r} {timed_out.value!r}"
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
    assert afterwards.value.strip() == _ALIVE_TOKEN, (
        f"{cell}: the command after the timeout returned {afterwards.value!r}; "
        f"anything else is the killed command's output still in the buffer"
    )
