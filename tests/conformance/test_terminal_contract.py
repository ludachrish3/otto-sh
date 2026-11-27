"""What the ``terminal`` switch owes its caller: the mode it asked for, or a refusal by name.

``run`` and ``exec`` both take ``terminal``. Left unset, a command gets the
session the entry takes by default; ``terminal=True`` asks for a session with
a terminal, ``terminal=False`` for one without. Where the host cannot give
the mode asked for, otto refuses the value with
:class:`~otto.host.errors.UnsupportedOptionError`, naming it, and the host
answers the next command.

The expected outcome of each call is read off the cell's facts
(:func:`expected_mode`), never off otto: the unset call's mode is
:func:`~tests.conformance._entry.has_terminal`'s, and the explicit values
follow :func:`~tests.conformance._entry.offers_no_terminal` and the family.
What the command saw is read by the command itself: the vocabulary's
``terminal_read_command`` answers ``unread`` where there is no controlling
terminal, and the terminal's settings where there is one. A vocabulary with
no such command (the Zephyr shell) sends ``single_line_command`` instead, so
what is checked there is that the call answered or was refused as expected.
"""

import re
from collections.abc import Callable
from typing import Literal, NamedTuple

import pytest

from otto.host.errors import UnsupportedOptionError
from otto.result import CommandResult
from otto.utils import Status
from tests.conformance._entry import (
    LOCAL_FAMILY,
    has_terminal,
    issue,
    offers_no_terminal,
    opened,
)
from tests.conformance._framing import assert_single_line_answer
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

Mode = Literal["terminal", "none", "refused"]
"""What one call is expected to give: a terminal, no terminal, or a refusal by name."""

UNREAD = "unread"
"""What ``terminal_read_command`` answers where the command has no controlling terminal."""

_SETTINGS = re.compile(r"(no)?echo (no)?icanon")
"""The shape of ``terminal_read_command``'s answer where it read a terminal."""

TERMINAL_VALUES: "tuple[bool | None, ...]" = (None, True, False)
"""The three calls, in the order the contract sends them."""

BOTH_MODES = "terminal=True: a terminal; terminal=False: no terminal"
"""The two explicit calls' fragments on a host that gives both modes, joined as the
observable joins them; the support matrix page tells that arm apart by this text."""

_TRUE_TERMINAL, _FALSE_NONE = BOTH_MODES.split("; ")

FRAGMENTS: "dict[tuple[bool | None, str], str]" = {
    (None, "terminal"): "unset: a terminal",
    (None, "none"): "unset: no terminal",
    (None, "answered"): "unset: answered",
    (True, "terminal"): _TRUE_TERMINAL,
    (True, "refused"): "terminal=True: refused by name",
    (True, "answered"): "terminal=True: answered",
    (False, "none"): _FALSE_NONE,
    (False, "refused"): "terminal=False: refused by name",
    (False, "answered"): "terminal=False: answered",
}
"""What the observable says of each call, by its value and what it saw, spelled out
whole: the support matrix page tells the arms apart by these exact words."""


def expected_mode(resolved: ResolvedCell, terminal: "bool | None") -> Mode:
    """What a call with *terminal* owes on *resolved*, from the cell's facts only.

    Unset: the default session's mode (:func:`has_terminal`), never a refusal.
    ``True``: a terminal, except on the local host, which has none to give.
    ``False``: no terminal where the host can open a session without one
    (:func:`offers_no_terminal`), and a refusal everywhere else.
    """
    if terminal is None:
        return "terminal" if has_terminal(resolved) else "none"
    if terminal:
        return "refused" if resolved.family == LOCAL_FAMILY else "terminal"
    return "none" if offers_no_terminal(resolved) else "refused"


class Observed(NamedTuple):
    """One call's outcome: its result, or otto's refusal."""

    result: "CommandResult | None"
    refused: "UnsupportedOptionError | None"


def _probe(resolved: ResolvedCell) -> str:
    words = resolved.vocabulary
    return words.terminal_read_command or words.single_line_command


async def observe(host, resolved: ResolvedCell, terminal: "bool | None") -> Observed:
    """Send the probe with *terminal* (unset when ``None``) and keep what came back.

    Only :exc:`~otto.host.errors.UnsupportedOptionError` is caught; any other
    failure is the cell's, raised as it came.
    """
    kwargs = {} if terminal is None else {"terminal": terminal}
    try:
        result = await issue(host, resolved, _probe(resolved), **kwargs)
    except UnsupportedOptionError as refused:
        return Observed(None, refused)
    return Observed(result, None)


def judge(resolved: ResolvedCell, terminal: "bool | None", observed: Observed) -> str:
    """Assert *observed* is what :func:`expected_mode` owes; return the observable fragment."""
    expected = expected_mode(resolved, terminal)
    label = "unset" if terminal is None else f"terminal={terminal}"
    words = resolved.vocabulary
    if expected == "refused":
        assert observed.refused is not None, (
            f"{resolved.cell}: {label} was expected to be refused by name, and the command "
            f"answered {observed.result!r}"
        )
        assert re.search(rf"terminal={terminal}\b", str(observed.refused)), (
            f"{resolved.cell}: {label} was refused, but not by name: {observed.refused}"
        )
        return FRAGMENTS[terminal, "refused"]
    assert observed.refused is None, (
        f"{resolved.cell}: {label} was expected to run with "
        f"{'a terminal' if expected == 'terminal' else 'no terminal'}, and otto refused it: "
        f"{observed.refused}"
    )
    assert observed.result is not None
    answer = observed.result.value
    if words.terminal_read_command is None:
        assert_single_line_answer(answer, words, resolved.cell, f"the command sent with {label}")
        return FRAGMENTS[terminal, "answered"]
    seen = "none" if answer == UNREAD else "terminal" if _SETTINGS.fullmatch(answer) else None
    assert seen is not None, (
        f"{resolved.cell}: with {label} `{words.terminal_read_command}` answered {answer!r}, "
        f"which reads neither a terminal nor {UNREAD!r}"
    )
    assert seen == expected, (
        f"{resolved.cell}: with {label} the command saw "
        f"{'a terminal' if seen == 'terminal' else 'no terminal'} ({answer!r}), expected "
        f"{'a terminal' if expected == 'terminal' else 'no terminal'}"
    )
    return FRAGMENTS[terminal, seen]


@pytest.mark.entries("run", "exec")
@pytest.mark.observable(
    "for a command sent with `terminal` unset, then `terminal=True`, then `terminal=False`: "
    "whether `{words.terminal_read_command}` reads a terminal, "
    "or otto's refusal of the value by name"
)
async def test_a_command_sees_the_terminal_mode_it_asked_for(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Each value of ``terminal`` gives the command its mode, or is refused by name."""
    async with opened(resolved_cell) as host:
        fragments = [
            judge(resolved_cell, terminal, await observe(host, resolved_cell, terminal))
            for terminal in TERMINAL_VALUES
        ]
        # The default session keeps the mode it had, also after an explicit
        # value opened another one: the unset call again, judged the same way.
        judge(resolved_cell, None, await observe(host, resolved_cell, None))
    note_observable("; ".join(fragments))


def _answered(value: str, resolved: ResolvedCell) -> CommandResult:
    """A successful answer *value* to the probe: the wrong answer the control hands the judge."""
    return CommandResult(status=Status.Success, value=value, command=_probe(resolved), retcode=0)


@pytest.mark.entries("run", "exec")
@pytest.mark.positive_control("terminal-switch")
async def test_control_the_mode_reading_tells_a_terminal_from_none(
    resolved_cell: ResolvedCell,
) -> None:
    """The judge passes what each call really answered, and fails every wrong answer.

    On the host: each call this cell is expected to run is judged against
    what it answered, so a host whose probe answers one mode whatever was
    asked fails here. Then the judge itself is handed the wrong answer for
    each expectation: ``unread`` where a terminal is owed, ``noecho icanon``
    where none is, and a successful answer where a refusal is.
    """
    async with opened(resolved_cell) as host:
        for terminal in TERMINAL_VALUES:
            if expected_mode(resolved_cell, terminal) != "refused":
                judge(resolved_cell, terminal, await observe(host, resolved_cell, terminal))
    for terminal in TERMINAL_VALUES:
        expected = expected_mode(resolved_cell, terminal)
        if expected == "refused":
            wrong = _answered("otto-cf-answered", resolved_cell)
        elif resolved_cell.vocabulary.terminal_read_command is None:
            continue
        else:
            value = UNREAD if expected == "terminal" else "noecho icanon"
            wrong = _answered(value, resolved_cell)
        with pytest.raises(AssertionError):
            judge(resolved_cell, terminal, Observed(wrong, None))
