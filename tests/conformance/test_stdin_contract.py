"""What an ``exec``'s command reads on stdin: end of file, unless the caller answers prompts.

An ``exec`` without ``expects=`` gives its command nothing to read, so a
command that reads stdin (``cat``) gets end of file at once instead of waiting
on a terminal nobody types into. That holds also under ``sudo=True`` after a
password prompt otto answers itself: the password is read from the caller's
terminal on its own descriptor, and the elevated command still reads end of
file.

The stimulus is the cell's ``stdin_eof_command``. The elevated arm runs only
on a cell whose sudo asks a password otto answers
(:attr:`~tests.conformance._resolved.ResolvedCell.password_sudo`); no bed or
hermetic cell is one (``tests/conformance/_bed.py``,
``BED_SUDO_ASKS_A_PASSWORD``), so that arm is proved by
``tests/unit/host/test_exec_stdin.py`` with a stand-in sudo, not on a device.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: see
:func:`applicable_cell`.
"""

from collections.abc import Callable

import pytest

from tests.conformance._entry import EXEC, has_terminal, issue, opened
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

_STDIN_BUDGET_S = 5.0
"""Seconds a command that reads stdin may take: end of file arrives at once, a terminal never."""


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    A vocabulary with no ``stdin_eof_command`` has no command that reads stdin
    (the Zephyr shell runs no process with a stdin to read).
    """
    return resolved.vocabulary.stdin_eof_command is not None


def _stdin_words(resolved: ResolvedCell) -> str:
    """The cell's stdin command (the domain admitted the cell)."""
    cmd = resolved.vocabulary.stdin_eof_command
    assert cmd is not None, f"{resolved.cell}: the domain admitted a cell with no stdin command"
    return cmd


def piped(stdin_command: str) -> str:
    """*stdin_command* with one line, ``x``, piped to it: the control's stimulus."""
    return "printf 'x\\n' | { " + stdin_command + "; }"


@pytest.mark.entries("exec")
@pytest.mark.observable(
    "value and timed_out of `{words.stdin_eof_command}` under a 5s budget"
    "; no password prompt on this cell"
)
async def test_an_exec_without_expects_reads_end_of_file(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """A command that reads stdin gets end of file, also after otto answers sudo's password."""
    cmd = _stdin_words(resolved_cell)
    async with opened(resolved_cell) as host:
        plain = await issue(host, resolved_cell, cmd, timeout=_STDIN_BUDGET_S)
        assert (plain.timed_out, plain.value, plain.retcode) == (False, "otto-cf-eof", 0)
        if resolved_cell.password_sudo and resolved_cell.entry == EXEC:
            # Arm: after otto's own password prompt. The prompt otto answered is
            # the terminal's output, ahead of the command's.
            elevated = await host.exec(cmd, timeout=_STDIN_BUDGET_S, sudo=True)
            assert (elevated.timed_out, elevated.value.endswith("otto-cf-eof")) == (False, True)
            # A sudo exec needs a terminal for the prompt: read the session's own.
            assert has_terminal(resolved_cell, needs_shell=True)
            read = resolved_cell.vocabulary.terminal_read_command
            if read is not None:
                tty = await host.exec(read, timeout=_STDIN_BUDGET_S, sudo=True)
                assert not tty.value.endswith("unread"), (
                    f"{resolved_cell.cell}: a sudo exec read no terminal -- {tty.value!r}"
                )
            note_observable(
                f"value and timed_out of `{cmd}` under a 5s budget, plain and under "
                "sudo=True after otto answers the password"
            )


@pytest.mark.entries("exec")
@pytest.mark.positive_control("exec-stdin")
async def test_control_input_given_to_the_command_is_read(resolved_cell: ResolvedCell) -> None:
    """Input the command is given is read: fails a ``cat`` that never reads."""
    piped_cmd = piped(_stdin_words(resolved_cell))
    async with opened(resolved_cell) as host:
        given = await issue(host, resolved_cell, piped_cmd)
        assert given.value == "x\notto-cf-eof"  # fails a cat that never reads
        if resolved_cell.password_sudo and resolved_cell.entry == EXEC:
            # The elevated arm, on the same cells as the contract's.
            elevated = await host.exec(piped_cmd, timeout=_STDIN_BUDGET_S, sudo=True)
            # Fails an elevation that swallows the command's input.
            assert elevated.value.endswith("x\notto-cf-eof")
