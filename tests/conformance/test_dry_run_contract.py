"""What a dry run owes every entry: it reaches no device and answers one shape.

Under a dry run (``otto -n``, ``OttoContext.policy.dry_run``) ``run`` and
``exec`` decline: they contact nothing and return a
:class:`~otto.result.NotRunResult` -- ``Status.NotRun``, ``retcode == -1``,
no route, ``command`` the line a live call would type, and a ``value`` that
raises :exc:`~otto.result.CommandNotRunError`
(``docs/extend/dry-run-contract.md``).

The contract issues a command with a durable effect
(:attr:`~tests.conformance._vocabulary.Vocabulary.leave_mark_template`) on an
OPENED host, with the dry run entered around that one call only, then leaves
the dry run and asks the device, live, whether the effect is there
(:attr:`~tests.conformance._vocabulary.Vocabulary.mark_left_template`). A
warm host is the case a dry run could get wrong: its session is open and a
command written to it would run, so the contract first issues one live
command through the same entry. That a cold host opens no connection is a
unit test (``tests/unit/host/test_dry_run.py``).

The comparison is :func:`dry_run_mismatch`, shared by the contract and its
control so the control proves the very comparison the contract makes.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: see
:func:`applicable_cell`.
"""

import contextlib
import dataclasses
from collections.abc import AsyncIterator

import pytest

from otto.host.host import BaseHost
from otto.result import CommandNotRunError, CommandResult
from otto.utils import Status
from tests.conformance._entry import issue, opened
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance._witness import scratch_file
from tests.conftest import active_context

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    A vocabulary with no ``leave_mark_template`` has no stock command whose
    effect a later one reads back, so the device-side witness has no stimulus
    there. The dry run itself is otto's own code on every host; the embedded
    stand-in is ``tests/unit/host/test_dry_run.py``'s
    ``TestADryRunOnAnOpenedEmbeddedHostWritesNothing``.
    """
    return resolved.vocabulary.leave_mark_template is not None


def _mark_words(resolved: ResolvedCell, path: str) -> "list[str]":
    """The leave and check commands for a mark at *path* (the domain admitted the cell)."""
    words = resolved.vocabulary
    leave, left = words.leave_mark_template, words.mark_left_template
    why = f"{cell_label(resolved)}: the domain admitted a cell with no mark words"
    assert leave is not None, why
    assert left is not None, why
    return [leave.format(path=path), left.format(path=path)]


_PRESENT, _ABSENT = 0, 1
"""The two answers of :attr:`~tests.conformance._vocabulary.Vocabulary.mark_left_template`."""


def mark_answer(left: CommandResult) -> "bool | None":
    """Whether the live check *left* found the mark: ``True``, ``False``, or ``None`` for no answer.

    A completed check answers with its status and code: present
    (``Success``, 0) or absent (``Failed``, 1). A timeout, a transport
    error or any other code is no answer, never "absent".
    """
    if left.timed_out:
        return None
    if (left.status, left.retcode) == (Status.Success, _PRESENT):
        return True
    if (left.status, left.retcode) == (Status.Failed, _ABSENT):
        return False
    return None


def _answered(resolved: ResolvedCell, left: CommandResult) -> bool:
    """*left*'s answer, asserting it is one."""
    found = mark_answer(left)
    assert found is not None, (
        f"{cell_label(resolved)}: the mark check gave no answer: status={left.status!r} "
        f"retcode={left.retcode} timed_out={left.timed_out}"
    )
    return found


@contextlib.asynccontextmanager
async def _mark_removed(host: BaseHost, resolved: ResolvedCell, path: str) -> AsyncIterator[None]:
    """Remove a mark the body left at *path*, live, also when the body failed."""
    try:
        yield
    finally:
        left = _mark_words(resolved, path)[1]
        if _answered(resolved, await issue(host, resolved, left)):
            removed = await issue(
                host, resolved, resolved.vocabulary.remove_file_template.format(path=path)
            )
            assert removed.retcode == 0, f"{cell_label(resolved)}: {path} was not removed"


def dry_run_mismatch(result: CommandResult, left: CommandResult, command: str) -> "str | None":
    """Why *result* and the device's *left* answer are not a dry run's; ``None`` if they are.

    *result* is the call that issued *command*; *left* the live check for its
    mark afterwards.
    """
    wrong = []
    found = mark_answer(left)
    if found is None:
        wrong.append(
            f"the live check answered neither present nor absent (status={left.status!r}, "
            f"retcode={left.retcode}, timed_out={left.timed_out})"
        )
    elif found:
        wrong.append("the device carries the mark, so the command ran")
    shape = (result.status, result.retcode, result.route, result.command)
    if shape != (Status.NotRun, -1, "", command):
        wrong.append(f"the result is {shape!r}, not (NotRun, -1, '', {command!r})")
    else:
        try:
            _ = result.value
            wrong.append("reading its value did not raise CommandNotRunError")
        except CommandNotRunError:
            pass
    return "; ".join(wrong) or None


@dataclasses.dataclass(frozen=True)
class _Issued:
    """One leave-then-check round on a cell."""

    result: CommandResult
    """The call that issued :attr:`command`."""

    left: CommandResult
    """The live check for its mark, afterwards."""

    command: str
    """The leave command as issued."""


async def _issue_and_check(resolved: ResolvedCell, *, dry: bool) -> _Issued:
    """Leave a mark (under a dry run when *dry*), then ask the device, live, for it.

    The dry run covers the one leave call: the host is opened, and the mark
    checked and removed, outside it. Before it, the check is issued live
    through the same entry, its route asserted, so the session the leave
    call would use is open and writable when the dry run begins (a host's
    sessions open on first use), and the mark is asserted absent.
    """
    path = scratch_file("dry-run")
    leave, left = _mark_words(resolved, path)
    declined = active_context(dry_run=True) if dry else contextlib.nullcontext()
    async with opened(resolved) as host, _mark_removed(host, resolved, path):
        before = await issue(host, resolved, left)
        assert _answered(resolved, before) is False, (
            f"{cell_label(resolved)}: {path} was there before the command was issued"
        )
        with declined:
            result = await issue(host, resolved, leave, route_checked=not dry)
        found = await issue(host, resolved, left)
    return _Issued(result, found, leave)


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.observable(
    "CommandResult.status, .retcode, .route, .command and .value of a command that leaves "
    "a file at a fresh scratch path, issued under a dry run on an opened host, and whether "
    "that file is there when the host is asked live afterwards"
)
async def test_a_dry_run_reaches_no_device_and_returns_one_shape(
    resolved_cell: ResolvedCell,
) -> None:
    """The device never sees the command, and the result is the one dry-run shape."""
    issued = await _issue_and_check(resolved_cell, dry=True)
    why = dry_run_mismatch(issued.result, issued.left, issued.command)
    assert why is None, f"{cell_label(resolved_cell)}: {why}"


# ==========================================================================
# POSITIVE CONTROL -- proof that this observable CAN GO RED on this cell
# ==========================================================================
# See `tests/conformance/_controls.py`. Same `entries` marker as the contract:
# the collator judges a control's cells by the control's own marker.


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.positive_control("exec-dry-run")
async def test_control_a_command_issued_live_leaves_its_mark(resolved_cell: ResolvedCell) -> None:
    """Without the dry run the device carries the mark, and the comparison refuses it."""
    issued = await _issue_and_check(resolved_cell, dry=False)
    label = cell_label(resolved_cell)
    assert issued.left.retcode == 0, (
        f"{label}: a live `{issued.command}` left no mark the live check can find, so "
        f"the contract's check could never see a command that ran"
    )
    assert dry_run_mismatch(issued.result, issued.left, issued.command) is not None, (
        f"{label}: the comparison accepted a command that ran as a dry run"
    )
