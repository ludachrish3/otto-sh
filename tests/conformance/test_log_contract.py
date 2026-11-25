"""What every host owes a reader of its log: the lines of a command's ``value``.

otto's rule (``docs/architecture/utilities/results.md``, "What the host log
shows of a command's output"): for a command that completed, the host log
receives :func:`~otto.logger.relay.log_lines` of its ``value``, one record
per line, under ``log=LogMode.NORMAL`` and ``log=LogMode.QUIET`` alike, each
stamped with its mode (a quiet record is still written; the console filter
reads the stamp to decide where it shows), and nothing at all under
``log=LogMode.NEVER``: no record of otto's, at any level, carries a NEVER
command or its output.

This contract issues every output case the output contract issues
(:func:`~tests.conformance.test_output_contract.arm_cases`, so the two
contracts never choose differently) through the cell's entry under each log
mode, and compares the host's output records
(:func:`~tests.conformance._log.logged_lines`) with the lines of the
``value`` that call returned. The value's own correctness is the output
contract's; here it is the reference the log is held to.

The comparison is :func:`log_mismatch`, shared by the contract and its
control so the control proves the very comparison the contract makes.
"""

import contextlib
import secrets
from collections.abc import Callable, Iterator

import pytest

from otto.host.host import BaseHost
from otto.logger.mode import LogMode
from otto.logger.relay import log_lines
from otto.testing import OutputCase
from tests.conformance._entry import issue, opened
from tests.conformance._log import OutputRecord, logged_output, records_containing
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance.test_output_contract import arm_cases

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

LOGGING_MODES = [LogMode.NORMAL, LogMode.QUIET]
"""The modes under which a completed command's lines reach the host log."""


def log_mismatch(
    case: OutputCase, mode: LogMode, logged: "list[OutputRecord]", value: str
) -> "str | None":
    """Why *logged* is not what *mode* owes for a call that returned *value*; ``None`` if it is.

    Under :data:`LogMode.NEVER <otto.logger.mode.LogMode.NEVER>` nothing is
    owed; under every other mode, :func:`~otto.logger.relay.log_lines` of
    *value*, record for record, each stamped with *mode* (the stamp the
    console filter reads, so a QUIET line logged at NORMAL is refused).
    """
    expected = [] if mode is LogMode.NEVER else log_lines(value)
    lines = [found.text for found in logged]
    stamps = sorted({found.log_mode.name for found in logged if found.log_mode is not mode})
    if lines == expected and not stamps:
        return None
    if lines != expected:
        return f"{case.id} under log={mode.name}: logged {lines!r}, expected {expected!r}"
    return f"{case.id} under log={mode.name}: records stamped {stamps}, not {mode.name}"


async def logged_mismatches(
    host: BaseHost, resolved: ResolvedCell, cases: "list[OutputCase]"
) -> "list[str]":
    """Every case of *cases* under every log mode, issued once each; one line per mismatch."""
    wrong: "list[str]" = []
    for case in cases:
        for mode in [*LOGGING_MODES, LogMode.NEVER]:
            with logged_output(host) as logged:
                result = await issue(host, resolved, case.command, log=mode)
            if (why := log_mismatch(case, mode, list(logged), result.value)) is not None:
                wrong.append(why)
    return wrong


async def never_leaks(host: BaseHost, resolved: ResolvedCell) -> "list[str]":
    """Every record that carries a ``log=NEVER`` command's token, at any level; ``[]`` if none.

    The command prints a fresh token
    (:attr:`~tests.conformance._vocabulary.Vocabulary.distinct_answer_template`),
    so its line and its output both carry it, and no record of otto's
    loggers, DEBUG included, may: not the command, not its output, not a
    transport's trace of either. The value must hold the token, so an empty
    answer cannot pass for a redacted one.
    """
    token = f"otto-never-{secrets.token_hex(4)}"
    command = resolved.vocabulary.distinct_answer_template.format(token=token)
    with records_containing(token) as leaked:
        result = await issue(host, resolved, command, log=LogMode.NEVER)
    assert token in result.value, (
        f"{cell_label(resolved)}: `{command}` returned {result.value!r}, without its token"
    )
    return list(leaked)


@pytest.mark.entries("run", "exec")
@pytest.mark.observable(
    "the records the host log receives for every output case this userland can produce, "
    "against the lines of `value` and the stamp of the mode, under each log mode; and every "
    "record of otto's loggers, at any level, against a log=NEVER command's token"
)
async def test_the_log_receives_the_lines_of_value(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Each case's output records are the lines of its ``value``, stamped with its mode.

    None under ``NEVER``, and nothing of a NEVER command in any record of otto's, at any level.
    """
    async with opened(resolved_cell) as host:
        cases = arm_cases(resolved_cell.vocabulary, host).cases
        note_observable(
            f"the records the host log receives for {len(cases)} output cases, against the "
            f"lines of `value` and their mode stamps, under log=NORMAL, QUIET and NEVER; and "
            f"every record of otto's loggers, DEBUG included, for a log=NEVER command's token"
        )
        wrong = await logged_mismatches(host, resolved_cell, cases)
        leaked = await never_leaks(host, resolved_cell)
    assert not wrong, f"{cell_label(resolved_cell)}: {wrong[:5]}"
    assert not leaked, f"{cell_label(resolved_cell)}: a log=NEVER command reached {leaked[:5]}"


# ==========================================================================
# POSITIVE CONTROL -- proof that this observable CAN GO RED on this cell
# ==========================================================================
# See `tests/conformance/_controls.py`. Same `entries` marker as the contract:
# the collator judges a control's cells by the control's own marker.


@contextlib.contextmanager
def blank_lines_dropped() -> Iterator[None]:
    """Every host's output seam drops blank lines while the block runs.

    Patched on the class, before the host is opened, because every route
    takes the seam as a bound method when its session is built.
    """
    honest = BaseHost._log_output

    def dropping(self: BaseHost, output: str, mode: LogMode = LogMode.NORMAL) -> None:
        kept = "\n".join(line for line in output.split("\n") if line)
        if kept:
            honest(self, kept, mode)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BaseHost, "_log_output", dropping)
        yield


def _has_blank_line(case: OutputCase) -> bool:
    return "" in log_lines(case.value)


@pytest.mark.entries("run", "exec")
@pytest.mark.positive_control("exec-log")
async def test_control_a_log_that_drops_blank_lines_is_refused(resolved_cell: ResolvedCell) -> None:
    """With the output seam dropping blank lines, the comparison refuses a case that has one.

    The control requires such a case in the selection, so it cannot pass on
    a selection where dropping blank lines changes nothing.
    """
    label = cell_label(resolved_cell)
    with blank_lines_dropped():
        async with opened(resolved_cell) as host:
            blank = [
                case
                for case in arm_cases(resolved_cell.vocabulary, host).cases
                if _has_blank_line(case)
            ]
            assert blank, (
                f"{label}: no output case has a blank line, so the control has nothing to drop"
            )
            wrong = await logged_mismatches(host, resolved_cell, blank)
    # Refused for its lines, the observable the twist moves: a wrong mode
    # stamp alone would refuse a case for another reason.
    refused = {
        case.id
        for case in blank
        if any(why.startswith(f"{case.id} under") and ": logged " in why for why in wrong)
    }
    assert refused == {case.id for case in blank}, (
        f"{label}: with blank lines dropped from the log, only {sorted(refused)} of "
        f"{sorted(case.id for case in blank)} were refused, so the contract cannot see a "
        f"dropped blank line on this cell"
    )
