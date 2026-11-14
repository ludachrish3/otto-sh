"""What a caller owes nothing for: the shell state one command leaves, and who sees it.

Two promises, one per entry. ``run`` keeps one shell, so what one ``run``
does to it (a ``cd``, an ``export``, a ``set``) the next ``run`` sees. An
``exec`` keeps nothing: it starts from the state its session was set up with,
and what it changes (an ``exit`` included) reaches neither the next ``exec``
nor a ``run`` -- in all three directions (``run`` -> ``exec``, ``exec`` ->
``exec``, ``exec`` -> ``run``). Where ``exec`` is a process of its own that
holds by construction; where it is typed into a pooled shell session, the
exec frame runs it in a subshell of its own; on a console, whose one shell
``run`` also uses, in a clean login shell started from it, which runs as
whoever that shell is (:func:`_console_exec`).

The stimulus and the read are the cell's vocabulary
(``run_state_set_command``, ``exec_state_set_command``,
``state_read_command``, ``exit_session_command``). The read prints the
working directory, the two state variables, the session-setup hook's mark
(``OTTO_CF_HOOK``, set by ``tests/conformance/_entry.py``'s marking hook on
every session of an ``exec-shell`` host), the options ``f``, ``C``, ``e`` and
``u``, and the user, separated by ``|``.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: see
:func:`applicable_cell`.
"""

from collections.abc import Callable
from typing import NamedTuple

import pytest

from otto.host.exec_route import ExecRoute
from tests.conformance._entry import EXEC_SHELL, UNIX, expected_route, issue, opened
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    A vocabulary with no ``state_read_command`` has no way to read the shell's
    state (the Zephyr shell has no working directory, variables or options a
    command can print).
    """
    return resolved.vocabulary.state_read_command is not None


class StateRead(NamedTuple):
    """One answer of ``state_read_command``, field by field."""

    cwd: str
    run: str
    exec_: str
    hook: str
    opts: str
    user: str


def _fields(value: str) -> StateRead:
    """Split *value*, one answer of ``state_read_command``, into its fields.

    A value that is not such an answer (a shell's error text, say) fails as
    an assertion that shows it.
    """
    parts = value.split("|")
    assert len(parts) == len(StateRead._fields), (
        f"the state read answered {value!r}, not {len(StateRead._fields)} '|'-separated fields"
    )
    return StateRead(*parts)


def _console_exec(resolved: ResolvedCell) -> bool:
    """Whether *resolved* is a unix console, whose ``exec`` runs in a clean login shell.

    A console has one shell for ``run`` and ``exec``, so its ``exec`` starts
    a clean login shell from it: the login's directory, environment and
    options, as the user that shell is at that moment. The contract checks
    that user too, the console's declared difference.
    """
    return resolved.cell.term == "console" and resolved.family == UNIX


def _terminal_arm(resolved: ResolvedCell) -> bool:
    """Whether *resolved*'s ``exec`` is typed into a terminal whose settings this contract reads.

    A pooled shell session and the default session read a terminal, and the
    exec frame puts it back after each ``exec``. A local ``exec`` and the ssh
    channel have none, and a Zephyr shell is outside this contract's domain.
    """
    return (
        resolved.family == UNIX
        and resolved.vocabulary.terminal_change_command is not None
        and expected_route(resolved)
        in {ExecRoute.POOLED_SHELL.value, ExecRoute.DEFAULT_SESSION.value}
    )


class _TerminalWords(NamedTuple):
    read: str
    change: str
    baseline: str


def _terminal_words(resolved: ResolvedCell) -> _TerminalWords:
    """The cell's terminal words, every one present (:func:`_terminal_arm` admitted the cell)."""
    words = resolved.vocabulary
    read, change = words.terminal_read_command, words.terminal_change_command
    baseline = words.terminal_baseline_answer
    for word in (read, change, baseline):
        assert word is not None, f"{resolved.cell}: the terminal arm ran with no terminal words"
    return _TerminalWords(read, change, baseline)


_LONG_EXEC = "printf '%s\\n' " + "a" * 1400 + " >/dev/null; echo otto-cf-long"
"""A 1,400-character exec: past ash's echoing line limit, where an echoing terminal shows."""


class _StateWords(NamedTuple):
    read: str
    run_set: str
    exec_set: str
    leave: str


def _words(resolved: ResolvedCell) -> _StateWords:
    """The cell's state words, every one present (the domain admitted the cell)."""
    words = resolved.vocabulary
    read, run_set = words.state_read_command, words.run_state_set_command
    exec_set, leave = words.exec_state_set_command, words.exit_session_command
    for word in (read, run_set, exec_set, leave):
        assert word is not None, f"{resolved.cell}: the domain admitted a cell with no state words"
    return _StateWords(read, run_set, exec_set, leave)


@pytest.mark.entries("exec", "exec-shell")
@pytest.mark.observable(
    "`{words.state_read_command}` by exec before and after run's state, exec's state and "
    "`exit`, and by run afterwards"
)
async def test_an_exec_starts_from_its_own_state_and_leaves_none(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Nothing reaches an ``exec`` from a ``run`` or another ``exec``, and nothing leaves it.

    Where the ``exec`` is typed into a terminal (:func:`_terminal_arm`), that
    includes the terminal's settings: an ``exec`` that changes them, and a
    long one after it, leave the next ``exec`` reading the session's own.
    """
    read, run_set, exec_set, leave = _words(resolved_cell)
    terminal = _terminal_words(resolved_cell) if _terminal_arm(resolved_cell) else None
    async with opened(resolved_cell) as host:
        baseline = _fields((await issue(host, resolved_cell, read)).value)
        await host.run(run_set)
        run_read = _fields((await host.run(read)).only.value)
        after_run = _fields((await issue(host, resolved_cell, read)).value)  # run -> exec
        await issue(host, resolved_cell, exec_set)
        # Read before the `exit` too: a shell the `exit` ended is replaced by a
        # fresh one, which would hide what the state words left behind.
        after_set = _fields((await issue(host, resolved_cell, read)).value)  # exec -> exec
        await issue(host, resolved_cell, leave)
        after_exec = _fields((await issue(host, resolved_cell, read)).value)  # exec -> exec
        run_after = _fields((await host.run(read)).only.value)  # exec -> run
        terminal_reads: "list[str]" = []
        if terminal is not None:
            terminal_reads.append((await issue(host, resolved_cell, terminal.read)).value)
            await issue(host, resolved_cell, terminal.change)
            terminal_reads.append((await issue(host, resolved_cell, _LONG_EXEC)).value)
            terminal_reads.append((await issue(host, resolved_cell, terminal.read)).value)
    assert baseline.hook == ("ran" if resolved_cell.entry == EXEC_SHELL else "unset")
    assert (run_read.cwd, run_read.run, "f" in run_read.opts) == ("/dev", "run", True)
    assert run_after == run_read  # exec -> run: nothing
    if terminal is not None:
        # exec -> exec, the terminal: a change and a long exec after it leave the baseline
        assert terminal_reads == [terminal.baseline, "otto-cf-long", terminal.baseline]
    assert after_set == baseline  # exec -> exec: nothing, before the exit
    assert after_exec == baseline  # exec -> exec: nothing, after it
    assert after_run == baseline  # run -> exec: nothing
    state = (
        f"`{read}` by exec before and after run's state, exec's state and `exit`, "
        "and by run afterwards"
    )
    if _console_exec(resolved_cell):
        # The declared difference: the clean login shell runs as run's user.
        assert baseline.user == run_read.user
    if terminal is None:
        note_observable(f"{state}; no terminal on this route")
        return
    tty = f"`{terminal.read}` by exec after `{terminal.change}`"
    if _console_exec(resolved_cell):
        note_observable(
            f"{state}; {tty} and a 1,400-character exec: the terminal put back as it was, "
            "in a clean login shell as run's user"
        )
    else:
        note_observable(f"{state}; {tty} and a 1,400-character exec: the terminal kept")


@pytest.mark.entries("exec", "exec-shell")
@pytest.mark.positive_control("exec-isolated")
async def test_control_state_set_inside_one_exec_is_seen_by_that_exec(
    resolved_cell: ResolvedCell,
) -> None:
    """The state an ``exec`` sets is there for the rest of that ``exec``, and only there.

    Where the contract reads the terminal, a terminal change is seen by the
    rest of the ``exec`` that made it, too.
    """
    read, _run_set, exec_set, _leave = _words(resolved_cell)
    terminal = _terminal_words(resolved_cell) if _terminal_arm(resolved_cell) else None
    async with opened(resolved_cell) as host:
        inside = _fields((await issue(host, resolved_cell, f"{exec_set}; {read}")).value)
        plain = _fields((await issue(host, resolved_cell, read)).value)
        changed = (
            (await issue(host, resolved_cell, f"{terminal.change}; {terminal.read}")).value
            if terminal is not None
            else None
        )
    assert (inside.exec_, "C" in inside.opts) == (
        "exec",
        True,
    )  # fails a read that answers nothing set
    assert inside != plain  # fails an implementation answering one value always
    if terminal is not None:
        assert changed == "echo noicanon"  # fails a read that answers the baseline regardless


@pytest.mark.entries("run")
@pytest.mark.observable(
    "`{words.state_read_command}` after `{words.run_state_set_command}` on the same run session"
)
async def test_what_one_run_does_to_its_session_the_next_run_sees(
    resolved_cell: ResolvedCell,
) -> None:
    """A ``run``'s ``cd``, ``export`` and ``set`` are what the next ``run`` starts from."""
    read, run_set, _exec_set, _leave = _words(resolved_cell)
    async with opened(resolved_cell) as host:
        before = _fields((await issue(host, resolved_cell, read)).value)
        await issue(host, resolved_cell, run_set)
        after = _fields((await issue(host, resolved_cell, read)).value)
    assert (before.cwd, before.run) != ("/dev", "run")
    assert (after.cwd, after.run, "f" in after.opts) == ("/dev", "run", True)


@pytest.mark.entries("run")
@pytest.mark.positive_control("run-keeps-state")
async def test_control_a_run_that_sets_nothing_reads_the_same_and_one_that_sets_reads_it(
    resolved_cell: ResolvedCell,
) -> None:
    """Two reads with nothing between them agree; a read after the state is set differs."""
    read, run_set, _exec_set, _leave = _words(resolved_cell)
    async with opened(resolved_cell) as host:
        a = (await issue(host, resolved_cell, read)).value
        b = (await issue(host, resolved_cell, read)).value
        c = (await issue(host, resolved_cell, f"{run_set}; {read}")).value
    assert a == b
    assert c != a
