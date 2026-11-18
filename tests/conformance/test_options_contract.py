"""What every entry owes an option of ``run`` and ``exec``: the same effect, or a refusal by name.

``expects=``, ``sudo=`` and ``user=``, their combinations, a
:class:`~otto.host.host.ShellCommand`'s own options against the call's, and an
``as_user()`` scope have the same effect on every entry and backend
(``docs/extend/custom-host-classes.md``).
Where a family declares it cannot honour an option
(:attr:`~otto.host.capability_grid.HostCapabilities.sudo`,
:attr:`~otto.host.capability_grid.HostCapabilities.expects`,
:attr:`~otto.host.capability_grid.HostCapabilities.user_refusal`), the call
raises :exc:`~otto.host.errors.UnsupportedOptionError` and types nothing.

The arms are :func:`tests.conformance._options.arms`'s table, built from the
opened host's declaration and the cell's vocabulary and users; each is issued
through the cell's entry, its route asserted as a call carrying an option
(:func:`~tests.conformance._entry.issue`), and compared by
:func:`~tests.conformance._options.arm_mismatch`, which the control shares.

Two things are not arms here. Which timeout wins, a
:class:`~otto.host.host.ShellCommand`'s or the call's, does not show on a
result: it is ``tests/unit/host/test_host_exec_core.py``'s
``TestOneCommandDifferential``. ``sudo=True`` on a compound command is a
known open difference, so every sudo arm issues one simple command.
"""

import dataclasses
from collections.abc import Callable

import pytest

from otto.host.errors import UnsupportedOptionError
from otto.host.host import ShellCommand
from otto.logger.mode import LogMode
from tests.conformance._entry import opened
from tests.conformance._options import (
    Answer,
    OptionArm,
    arm_mismatch,
    arms,
    fresh_token,
    measured_login,
    run_arm,
    unrun_notes,
)
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.observable(
    "the value of a command issued with expects=, sudo=, user=, their combinations, a "
    "ShellCommand's own expects and log against the call's, and inside and after as_user(); "
    "or, where the host declares an option refused, UnsupportedOptionError with no output "
    "logged and the next plain command's value unchanged"
)
async def test_an_option_has_the_same_effect_everywhere_or_is_refused_by_name(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Each arm's value ends with the word its options decide, or the call is refused by name."""
    async with opened(resolved_cell) as host:
        login = await measured_login(host, resolved_cell)
        table = arms(resolved_cell, host, login=login)
        notes = unrun_notes(resolved_cell, host, login=login)
        note_observable(
            f"arms run: {', '.join(arm.name for arm in table) or 'none'}"
            + "".join(f"; {note}" for note in notes)
        )
        wrong = [
            why
            for arm in table
            if (why := arm_mismatch(arm, await run_arm(host, resolved_cell, arm))) is not None
        ]
    assert table, f"{cell_label(resolved_cell)}: no arm has a stimulus here"
    assert not wrong, f"{cell_label(resolved_cell)}: {wrong}"


# ==========================================================================
# POSITIVE CONTROL -- proof that this observable CAN GO RED on this cell
# ==========================================================================
# See `tests/conformance/_controls.py`. Same `entries` marker as the contract:
# the collator judges a control's cells by the control's own marker.


def _twisted(arm: OptionArm) -> OptionArm:
    """*arm* made wrong: otto types another token, or a refusal is expected to answer."""
    if isinstance(arm.expected, Answer):
        prompt, _ = arm.kwargs["expects"][0]
        return dataclasses.replace(
            arm, kwargs={**arm.kwargs, "expects": [(prompt, f"{fresh_token()}\n")]}
        )
    return dataclasses.replace(arm, expected=Answer(fresh_token()))


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.positive_control("exec-options")
async def test_control_an_answer_otto_did_not_type_is_refused(resolved_cell: ResolvedCell) -> None:
    """The expects arm with another answer typed, or a refusal expected to answer, is refused.

    Where the cell has a ``ShellCommand`` arm, the same command logging under
    ``NORMAL`` must be refused for what it logged, so the arm's "nothing
    logged" half can see a line on this entry.

    The control requires one of those two arms, and requires the arm as built
    to be accepted first, so it ran a discriminating arm: one the comparison
    accepts as built and refuses made wrong.
    """
    label = cell_label(resolved_cell)
    async with opened(resolved_cell) as host:
        table = arms(resolved_cell, host, login=await measured_login(host, resolved_cell))
        chosen = next((arm for arm in table if arm.name == "expects"), None) or next(
            (arm for arm in table if arm.expected is UnsupportedOptionError), None
        )
        assert chosen is not None, f"{label}: no expects arm and no refusal arm to twist"
        honest = arm_mismatch(chosen, await run_arm(host, resolved_cell, chosen))
        assert honest is None, f"{label}: the {chosen.name} arm as built is refused: {honest}"
        twisted = _twisted(chosen)
        why = arm_mismatch(twisted, await run_arm(host, resolved_cell, twisted))
        logged = next((arm for arm in table if arm.name == "ShellCommand"), None)
        heard = None
        if logged is not None:
            # The log half of the ShellCommand arm: with the command logging,
            # the comparison must see its lines on this entry.
            assert isinstance(logged.command, ShellCommand)
            loud = dataclasses.replace(
                logged, command=dataclasses.replace(logged.command, log=LogMode.NORMAL)
            )
            heard = arm_mismatch(loud, await run_arm(host, resolved_cell, loud))
    assert why is not None, (
        f"{label}: the comparison accepted the {chosen.name} arm made wrong ({twisted!r})"
    )
    if logged is not None:
        assert heard is not None, (
            f"{label}: a ShellCommand logging under NORMAL was accepted as logging nothing"
        )
        assert "logged" in heard, f"{label}: refused for another reason: {heard}"
