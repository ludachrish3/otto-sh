"""What a typed command owes its caller: it reaches the shell byte for byte, or is refused whole.

``run`` and ``exec`` both TYPE a command into a shell session. A command
holding printable ASCII punctuation, a TAB and UTF-8 text reaches the shell
byte for byte, on every session, terminal or not. A command holding a control
character a terminal acts on (BEL here, ``\\x07``) is a different case by the
session's mode, and the expected outcome is selected by it
(:func:`~tests.conformance._entry.has_terminal`, read off the cell's facts,
never the route): where the session reads a pipe the byte arrives intact;
where it reads a terminal otto refuses the command with
:class:`~otto.host.errors.UntypableCommandError` before typing anything, and
the host answers the next command.

What the shell received is read back by the shell itself: the vocabulary's
``typed_checksum_template`` prints the MD5 of its argument, which this
contract compares with the MD5 of the argument it sent, so the comparison
never trusts how otto reads the output back.

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: see
:func:`applicable_cell`.
"""

import hashlib
import shlex
import string
from collections.abc import Callable

import pytest

from otto.host.errors import UntypableCommandError
from otto.result import CommandResult
from tests.conformance._bed import BED_BUSYBOX
from tests.conformance._entry import has_terminal, issue, opened
from tests.conformance._framing import assert_single_line_answer
from tests.conformance._resolved import ResolvedCell
from tests.conformance._witness import scratch_file
from tests.conformance.test_dry_run_contract import mark_answer

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

PRINTABLE_ARGUMENT = f"{string.punctuation}\tx\théllo wörld ✓ 日本語"
"""Every printable ASCII punctuation character, two TABs and UTF-8 text (two-, three-byte)."""

BEL_ARGUMENT = "a\x07b"
"""An argument holding BEL, a control character a terminal acts on and a pipe passes."""

BANG_WORD = "!word"
"""A word typed UNQUOTED: a history event to interactive bash or zsh, literal in a script."""

COMMENT = "# it's a comment"
"""A comment typed after the command: an ordinary word to an interactive zsh, whose
apostrophe would open a quote (#1346). In a script it is ignored."""


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about.

    A vocabulary with no ``typed_checksum_template`` has no command that reads
    its own argument back as data: the Zephyr shell quotes an unknown command
    back through its line editor, which also takes a TAB as completion, so no
    firmware command can show which bytes arrived.
    """
    return resolved.vocabulary.typed_checksum_template is not None


# ==========================================================================
# A REGISTERED, MEASURED-BROKEN CELL -- a typed TAB on a BusyBox console
# ==========================================================================
# MEASURED 2026-11-25 by `make conformance-bed`: both tests here fail on
# `bed-busybox[bb1350:console]`, for `run` and `exec`, and pass on every other
# cell, including ssh and telnet to the same guest and the GNU console
# (`bed-unix[test2:console]`). otto types a TAB as Ctrl-V then TAB
# (`BashFrame.typed`); that console's tty has `-iexten`, so `lnext` is off and
# the shell receives `0x16 0x09`. The `userland.GAPS` record
# `typed-tab-literal-next` is the user-facing account; the fix is #1321.
#
# Keyed on resolver facts, not a guest's name: kind `bed-busybox` and term
# `console`. No hermetic cell is kind `bed-busybox`, so the hermetic venue is
# never declared.
_BUSYBOX_CONSOLE_TYPED_TAB = (
    "a typed TAB reaches the shell with a Ctrl-V byte (0x16) before it, because otto "
    "types TAB as Ctrl-V + TAB and a BusyBox console tty without iexten has no lnext; "
    "measured 2026-11-25 on bed-busybox[bb1350:console]; #1321"
)


def expected_failure(resolved: ResolvedCell) -> "str | None":
    """The reason this cell's typed-input contract is KNOWN to fail, or None.

    Read by ``tests/conformance/conftest.py``'s ``pytest_generate_tests``, which
    turns a reason into ``xfail(strict=True)`` on that cell's items. Both tests
    of this module fail on the declared cell, so a per-cell declaration matches
    the measurement exactly. See the banner above.
    """
    if resolved.kind == BED_BUSYBOX and resolved.cell.term == "console":
        return _BUSYBOX_CONSOLE_TYPED_TAB
    return None


def checksum_command(resolved: ResolvedCell, argument: str) -> str:
    """The vocabulary's command printing the MD5 of *argument*, shell-quoted."""
    template = resolved.vocabulary.typed_checksum_template
    assert template is not None, f"{resolved.cell}: the domain admitted a cell with no checksum"
    return template.format(arg=shlex.quote(argument))


def md5_of(argument: str) -> str:
    """The MD5 of *argument*'s UTF-8 bytes, as ``md5sum`` prints it."""
    return hashlib.md5(argument.encode("utf-8"), usedforsecurity=False).hexdigest()


def checksum_answer(result: CommandResult) -> str:
    """The first field of *result*'s value: the checksum, without ``md5sum``'s file name."""
    fields = result.value.split()
    return fields[0] if fields else ""


def unquoted_checksum_command(resolved: ResolvedCell, word: str, tail: str = "") -> str:
    """The vocabulary's checksum of *word* typed unquoted, with *tail* after it.

    For the words a shell reads as a script reads them only when it treats a
    typed line as a script line: *word* must hold no character a script
    would expand or split.
    """
    template = resolved.vocabulary.typed_checksum_template
    assert template is not None, f"{resolved.cell}: the domain admitted a cell with no checksum"
    return f"{template.format(arg=word)} {tail}".rstrip()


async def _arrived(
    host, resolved: ResolvedCell, argument: str, command: "str | None" = None
) -> str:
    """The checksum the shell computed over *argument*, as typed through *resolved*'s entry.

    *command* is the line typed, when it is not the argument's quoted checksum.
    """
    typed = command if command is not None else checksum_command(resolved, argument)
    result = await issue(host, resolved, typed)
    assert result.is_ok, f"{resolved.cell}: the checksum command failed -- {result!r}"
    return checksum_answer(result)


@pytest.mark.entries("run", "exec")
@pytest.mark.observable(
    "the MD5 the shell computes over an argument of ASCII punctuation, TABs and UTF-8 text, "
    "over an unquoted `!word`, over a word with a `#` comment after the command, "
    "then over one holding BEL (or otto's refusal of it, and the absence of the mark the "
    "refused command would leave), then whether "
    "`{words.single_line_command}` answers on the same host"
)
async def test_a_typed_command_reaches_the_shell_byte_for_byte(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
    """Printable text arrives intact everywhere; BEL arrives intact or is refused, by the mode."""
    words = resolved_cell.vocabulary
    terminal = has_terminal(resolved_cell)
    async with opened(resolved_cell) as host:
        printable = await _arrived(host, resolved_cell, PRINTABLE_ARGUMENT)
        bang = await _arrived(
            host, resolved_cell, BANG_WORD, unquoted_checksum_command(resolved_cell, BANG_WORD)
        )
        commented = await _arrived(
            host, resolved_cell, "kept", unquoted_checksum_command(resolved_cell, "kept", COMMENT)
        )
        bel: "str | None" = None
        if terminal:
            # The refused command would leave a mark first: its absence afterwards
            # is what shows nothing of it was typed, not the next command answering.
            leave, left = words.leave_mark_template, words.mark_left_template
            assert leave is not None, f"{resolved_cell.cell}: no leave_mark_template"
            assert left is not None, f"{resolved_cell.cell}: no mark_left_template"
            path = scratch_file("typed-input")
            refused = f"{leave.format(path=path)}; {checksum_command(resolved_cell, BEL_ARGUMENT)}"
            with pytest.raises(UntypableCommandError):
                await issue(host, resolved_cell, refused)
            marked = mark_answer(await issue(host, resolved_cell, left.format(path=path)))
            assert marked is False, (
                f"{resolved_cell.cell}: after the refusal the mark check at {path} answered "
                f"{marked!r}, not absent: part of the refused command was typed"
            )
        else:
            bel = await _arrived(host, resolved_cell, BEL_ARGUMENT)
        afterwards = await issue(host, resolved_cell, words.single_line_command)
    assert printable == md5_of(PRINTABLE_ARGUMENT), (
        f"{resolved_cell.cell}: the shell received other bytes than the command held: "
        f"MD5 {printable!r}, sent {md5_of(PRINTABLE_ARGUMENT)!r}"
    )
    assert bang == md5_of(BANG_WORD), (
        f"{resolved_cell.cell}: an unquoted {BANG_WORD!r} was not read literally, as a "
        f"script reads it: MD5 {bang!r}, sent {md5_of(BANG_WORD)!r}"
    )
    assert commented == md5_of("kept"), (
        f"{resolved_cell.cell}: {COMMENT!r} after the command was not read as a comment: "
        f"MD5 {commented!r}, expected {md5_of('kept')!r}"
    )
    if not terminal:
        assert bel == md5_of(BEL_ARGUMENT), (
            f"{resolved_cell.cell}: a BEL typed into a session without a terminal did not "
            f"arrive intact: MD5 {bel!r}, sent {md5_of(BEL_ARGUMENT)!r}"
        )
    assert_single_line_answer(
        afterwards.value, words, resolved_cell.cell, "the command after the typed input"
    )
    mode = (
        "a terminal: a BEL refused, and the mark the refused command would leave absent"
        if terminal
        else "no terminal: a BEL arrived intact"
    )
    note_observable(
        f"the MD5 the shell computes over punctuation, TABs and UTF-8 text, an unquoted "
        f"`!word` and a word before a `#` comment; {mode}; "
        f"and `{words.single_line_command}` answered afterwards"
    )


@pytest.mark.entries("run", "exec")
@pytest.mark.positive_control("typed-input")
async def test_control_the_checksum_comparison_sees_one_changed_byte(
    resolved_cell: ResolvedCell,
) -> None:
    """The contract's checksum moves with one byte of the argument, and matches the one sent.

    Fails a host whose checksum command answers one value whatever it was
    given, on which the contract's equality could not go red.
    """
    changed = PRINTABLE_ARGUMENT[:-1] + "x"
    async with opened(resolved_cell) as host:
        sent = await _arrived(host, resolved_cell, PRINTABLE_ARGUMENT)
        other = await _arrived(host, resolved_cell, changed)
    assert sent == md5_of(PRINTABLE_ARGUMENT)
    assert other == md5_of(changed)
    assert other != sent
