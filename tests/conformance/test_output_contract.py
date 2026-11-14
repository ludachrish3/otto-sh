"""What every host owes a caller reading a command's output: the exact bytes.

otto's rule (``docs/architecture/utilities/results.md``, "What a command's
``value`` holds"): a command's ``value`` is its output with exactly one
trailing newline removed, and nothing else changed. This contract issues
every output case the cell's userland can produce
(:attr:`~tests.conformance._vocabulary.Vocabulary.output_cases`) through the
cell's entry and compares ``value`` and ``retcode`` for equality.

TWO ARMS, ONE RULE. On a POSIX shell the cases are
:data:`otto.testing.EXACT_OUTPUT_CASES`, programs whose bytes the tester
chose -- blank lines, trailing spaces, a tab, ``\\r``, invalid UTF-8, no
trailing newline. A Zephyr shell has no ``printf`` and no ``echo``, so its
cases are the stock commands whose output the firmware fixes: the same rule,
weaker evidence. Which arm ran is what the cell's observable names.

The comparison is :func:`mismatches`, shared by the contract and its control
so the control proves the very comparison the contract makes.
"""

import dataclasses
from collections.abc import Callable

import pytest

from otto.host.limitation import Limitation
from otto.host.options import UNESCAPED_IAC
from otto.result import CommandResult
from otto.testing import OutputCase
from tests.conformance._entry import issue, opened
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance._vocabulary import OutputArm, Vocabulary

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


def mismatches(got: "list[CommandResult]", cases: "list[OutputCase]") -> "list[str]":
    """One line per case whose result's ``(value, retcode)`` is not the case's."""
    if len(got) != len(cases):
        return [f"{len(got)} results for {len(cases)} cases"]
    return [
        f"{case.id}: got ({result.value!r}, {result.retcode}), "
        f"expected ({case.value!r}, {case.retcode})"
        for result, case in zip(got, cases, strict=True)
        if (result.value, result.retcode) != (case.value, case.retcode)
    ]


_IAC = 0xFF


def carried_unescaped(writes: bytes) -> bytes:
    """*writes* as a console whose server does not escape 0xFF delivers it.

    What :data:`otto.host.options.UNESCAPED_IAC` declares, as replayed
    through telnetlib3 5.0.1 and otto's reader
    (``tests/unit/host/test_console_unescaped_iac.py`` pins this model
    against those replays, case by case). A 0xFF is read as the start of a
    telnet command and lost with that command's bytes:

    - followed by 0xFF: one 0xFF arrives;
    - followed by 0xFB-0xFE (WILL/WONT/DO/DONT): those two bytes and the
      option byte after them are lost;
    - followed by 0xF1-0xF9: those two bytes are lost;
    - followed by any other byte except 0xFA (0xF0 included): only the 0xFF
      is lost.

    Raises:
        ValueError: a 0xFF followed by 0xFA (subnegotiation: everything after
            it is swallowed until a 0xFF 0xF0, so what arrives depends on the
            rest of the stream, the prompt included), or a 0xFF as the last
            byte (what follows it is not in *writes*).
    """
    out = bytearray()
    i = 0
    while i < len(writes):
        byte = writes[i]
        if byte != _IAC:
            out.append(byte)
            i += 1
            continue
        if i + 1 == len(writes):
            raise ValueError("0xFF as the last byte: what arrives depends on the bytes after it")
        verb = writes[i + 1]
        if verb == _IAC:
            out.append(_IAC)
            i += 2
        elif verb == 0xFA:
            raise ValueError(
                "0xFF then 0xFA: all output after it is swallowed until a 0xFF 0xF0 arrives"
            )
        elif 0xFB <= verb <= 0xFE:
            i += 3
        elif 0xF1 <= verb <= 0xF9:
            i += 2
        else:
            i += 1
    return bytes(out)


def declared_exact_output(host: object) -> "Limitation | None":
    """The output limitation *host*'s console declares, when its term is ``console``.

    The one lookup is :attr:`ConsoleOptions.exact_output
    <otto.host.options.ConsoleOptions.exact_output>`; a host on another term
    reaches no console server, and a host with no console options has none.
    """
    if getattr(host, "term", None) != "console":
        return None
    options = getattr(host, "console_options", None)
    return None if options is None else options.exact_output


@dataclasses.dataclass(frozen=True)
class ArmCases:
    """The output arm one opened host runs, and the cases it expects there."""

    arm: OutputArm
    cases: "list[OutputCase]"


def arm_cases(words: Vocabulary, host: object) -> ArmCases:
    """The arm *host* runs and the cases it expects there.

    A console declaring :data:`~otto.host.options.UNESCAPED_IAC` runs the
    shipped table with each 0xFF row expected as that console carries it,
    and every other row exactly as written. A declaration that touches none
    of the cases (a Zephyr shell's) leaves the vocabulary's own arm.
    """
    cases = words.output_cases
    if declared_exact_output(host) is not UNESCAPED_IAC or words.output_arm is not (
        OutputArm.SHIPPED_TABLE
    ):
        return ArmCases(arm=words.output_arm, cases=cases)
    carried = [dataclasses.replace(case, writes=carried_unescaped(case.writes)) for case in cases]
    return ArmCases(arm=OutputArm.UNESCAPED_IAC, cases=carried)


def control_case(cases: "list[OutputCase]") -> OutputCase:
    """The first of *cases* whose ``value`` has a line with whitespace between two words.

    The positive control's case, chosen by that property rather than by
    position: an empty ``value`` (``true``, the table's first row) would let
    the honest baseline pass for a backend that returns nothing, and a value
    with no whitespace inside a line could not show that the comparison sees
    a space. Every arm holds one -- ``printf 'a\\tb\\n'`` in the table (no
    0xFF, so a console that does not escape 0xFF carries it unchanged),
    ``kernel version`` on a Zephyr firmware.

    Raises:
        LookupError: no case has such a value, so the control cannot run.
    """
    for case in cases:
        if any(len(line.split()) > 1 for line in case.value.splitlines()):
            return case
    raise LookupError(f"no output case has whitespace inside a line: {[c.id for c in cases]}")


def _arm(arm: OutputArm, cases: "list[OutputCase]") -> str:
    """The observable *arm* offers: the shipped table, as carried, or a firmware's output."""
    if arm is OutputArm.SHIPPED_TABLE:
        return f"every row of the table `otto.testing.EXACT_OUTPUT_CASES` ({len(cases)} cases)"
    if arm is OutputArm.UNESCAPED_IAC:
        return (
            f"the table `otto.testing.EXACT_OUTPUT_CASES` ({len(cases)} cases) "
            "as a console that does not escape 0xFF carries them"
        )
    return (
        f"the stock commands whose output this firmware fixes ({len(cases)}: "
        f"{', '.join(f'`{case.command}`' for case in cases)})"
    )


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.observable(
    "`value` of every output case this userland can produce, compared for equality"
)
async def test_a_commands_value_is_its_exact_output(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Every case's ``value`` and ``retcode`` are exactly the case's own.

    On a console declaring :data:`~otto.host.options.UNESCAPED_IAC`, a row
    holding 0xFF is expected as the declaration says it arrives, so the day
    the server starts escaping the byte this goes red and the declaration
    must go.
    """
    async with opened(resolved_cell) as host:
        chosen = arm_cases(resolved_cell.vocabulary, host)
        cases = chosen.cases
        note_observable(f"`value` and retcode of {_arm(chosen.arm, cases)}, compared for equality")
        got = [await issue(host, resolved_cell, case.command) for case in cases]
    wrong = mismatches(got, cases)
    assert not wrong, f"{cell_label(resolved_cell)}: {wrong[:5]}"


# ==========================================================================
# POSITIVE CONTROL -- proof that this observable CAN GO RED on this cell
# ==========================================================================
# See `tests/conformance/_controls.py`. Same `entries` marker as the contract:
# the collator judges a control's cells by the control's own marker.


@pytest.mark.entries("run", "exec", "exec-shell")
@pytest.mark.positive_control("exec-output-exact")
async def test_the_output_comparison_sees_one_added_space(resolved_cell: ResolvedCell) -> None:
    """A real result against an expectation one space, or one exit code, wrong must be refused.

    The honest baseline first, so a comparison that refused everything
    could not pass here. Then each half of the compared pair is doctored
    alone, so a comparison that ignored either ``value`` or ``retcode`` would
    go green here. Nothing is written on the far side.
    """
    async with opened(resolved_cell) as host:
        case = control_case(arm_cases(resolved_cell.vocabulary, host).cases)
        got = [await issue(host, resolved_cell, case.command)]
    label = cell_label(resolved_cell)
    assert mismatches(got, [case]) == [], f"{label}: the honest baseline did not match"
    doctored = dataclasses.replace(case, writes=case.writes.rstrip(b"\n") + b" \n")
    assert mismatches(got, [doctored]) != [], (
        f"{label}: `{case.command}` against an expectation with one added space was "
        f"accepted, so the contract's equality cannot go red on this cell"
    )
    wrong_code = dataclasses.replace(case, retcode=case.retcode + 1)
    assert mismatches(got, [wrong_code]) != [], (
        f"{label}: `{case.command}` against an expectation with exit code "
        f"{wrong_code.retcode} was accepted, so the contract cannot see a wrong exit code "
        f"on this cell"
    )
