"""What every host owes a caller of ``run()``: the exit code, and the framing.

Three contracts, and the third is the one that makes the first two mean
anything. A suite that only checks the happy path passes unchanged against a
backend that hardcodes ``Status.Success`` and returns the empty string, so the
failing-command contract is not an extra case -- it is the discriminator the
others are measured against.

THE STIMULUS COMES FROM THE CELL; THE ASSERTED PROPERTY DOES NOT VARY. Nothing
below spells a command. Each cell carries its userland's
:class:`~tests.conformance._vocabulary.Vocabulary` -- ``(exit 42)`` on a POSIX
shell, ``definitely_not_a_zephyr_command`` on a Zephyr one -- and the
assertions run identically over every cell that reaches them. The bash
spellings that used to be written here were universally true of the HERMETIC
venue and false the moment the bed venue reached a Zephyr shell (measured:
``printf: command not found``, retcode ``-8``), which is what a contract's
portability being tested only by the venue it runs in looks like.

Assertions are equality or ``startswith``, never ``in``. ``Results.value`` is
the LIST of per-command results and stringifies to ``[CommandResult(...)]``,
which CONTAINS the output a reader meant to check; an ``in`` assertion against
the wrong attribute therefore passes silently. The right read is ``.only`` for
a single command, and this rule is written down because it has already cost
this workstream one bug.
"""

import pytest

from otto.utils import Status
from tests.conformance._framing import assert_single_line_answer, framing_leak
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


async def test_exec_reports_the_documented_exit_code(resolved_cell: ResolvedCell) -> None:
    """A command's exit code reaches the caller unchanged.

    Both directions in one test, because "unchanged" is a claim about a
    mapping and a single sample cannot falsify it: a backend that answers 42 to
    everything passes the failing half alone, and one that answers 0 to
    everything passes the succeeding half alone.

    THE EXPECTED CODE IS PINNED EXACTLY, never "any non-zero", and that is a
    hazard rather than fussiness: ``retcode == -1`` is otto's reserved "the
    command never ran" sentinel (mapped to ssh's 255 by
    ``CommandResult.exit_code``), and Zephyr's signed-errno convention makes
    ``-1`` a genuinely reachable value (``-EPERM``) -- a command returning it
    would be reported as a connection failure, indistinguishable from one.
    Each vocabulary names a code that is not ``-1``, and
    ``tests/unit/test_conformance_bed.py`` refuses one that is.

    ``retcode`` and ``exit_code`` are separate assertions because they are
    separate documented claims -- ``CommandResult.exit_code`` is the ssh-like
    CLI code and is only equal to ``retcode`` when the command actually ran and
    exited non-zero. A timeout, which never ran, answers 255 to the same
    question. NO ASSERTION HERE CHANGED for the Zephyr vocabulary:
    ``exit_code`` (``src/otto/result.py``) returns any non-zero, non-``-1``
    retcode verbatim, so ``-8`` satisfies both of these as written.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        failed = (await host.run(words.failing_command)).only
        succeeded = (await host.run(words.succeeding_command)).only

    cell = resolved_cell.cell
    assert failed.retcode == words.failing_code, (
        f"{cell}: `{words.failing_command}` reported retcode {failed.retcode}, "
        f"not the {words.failing_code} the command exited with"
    )
    assert failed.exit_code == words.failing_code, (
        f"{cell}: `{words.failing_command}` reported exit_code {failed.exit_code}; "
        f"a command that ran and exited non-zero carries its own retcode"
    )
    assert failed.status is Status.Failed, (
        f"{cell}: a non-zero exit is Status.Failed, not {failed.status!r}"
    )
    assert failed.timed_out is False, (
        f"{cell}: `{words.failing_command}` exited on its own; timed_out must stay False"
    )

    assert succeeded.retcode == 0, (
        f"{cell}: `{words.succeeding_command}` reported retcode {succeeded.retcode}, not 0"
    )
    assert succeeded.status is Status.Success, (
        f"{cell}: a zero exit is Status.Success, not {succeeded.status!r}"
    )


async def test_exec_frames_output_without_prompt_noise(resolved_cell: ResolvedCell) -> None:
    """Stdout carries the command's output and nothing the shell added.

    Three assertions, and the first two are the ones every cell gets: the
    output is more than one line (so this measures LINE STRUCTURE and not just
    "some text came back"), and none of otto's own framing scaffolding reached
    the caller (``tests/conformance/_framing.py``'s ``framing_leak``).

    THE THIRD IS AVAILABLE ONLY WHERE THE TESTER CHOSE THE OUTPUT, and its
    absence is a real fidelity loss rather than a tidier spelling. ``printf``
    lets a POSIX cell assert exact equality, which also catches truncation,
    reordering, interleaving and a leaked prompt. A Zephyr shell has no
    ``printf`` and no ``echo``; the stimulus is the stock ``help`` builtin,
    whose text belongs to the firmware, so there is nothing to compare
    against. Stated here, at the assertion, rather than left for a reader to
    infer from a ``None``.

    ``.strip()`` and no more. Whether the framing keeps the payload's trailing
    newline is not something otto documents -- ``docs/library/sessions.md``
    reads every example's ``value`` through ``.strip()`` -- so pinning it here
    would be locking in observed behaviour, which this suite is not for. What
    IS the contract is that nothing the shell added survives, and stripping
    whitespace cannot hide a prompt, an echo or a sentinel: none of them are
    whitespace.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        framed = (await host.run(words.multiline_command)).only

    cell = resolved_cell.cell
    assert framed.is_ok, (
        f"{cell}: `{words.multiline_command}` failed outright -- {framed.status!r} {framed.value!r}"
    )
    body = framed.value.strip()
    assert len(body.splitlines()) > 1, (
        f"{cell}: `{words.multiline_command}` is a multi-line command, so a single "
        f"line back means the framing collapsed or truncated it -- {framed.value!r}"
    )
    leak = framing_leak(body, words.multiline_command)
    assert leak is None, f"{cell}: {leak} -- {framed.value!r}"

    if words.multiline_expected is not None:
        assert body == words.multiline_expected, (
            f"{cell}: framed output was {framed.value!r}; anything beyond the "
            f"printed lines is noise the session was supposed to strip"
        )


async def test_a_failing_command_is_not_reported_as_success(resolved_cell: ResolvedCell) -> None:
    """The discriminating half, stated over a SEQUENCE.

    A contract that only checks the happy path passes against a backend that
    hardcodes ``Status.Success``. So does one that checks a single failing
    command but reads only its ``retcode`` -- the aggregate is a separate
    computation (``Results.collect`` walks the entries for the first non-ok
    one), and a sequence is the shape where it can be wrong: the failure is in
    the MIDDLE, with a successful command after it, so an aggregate that
    reports the LAST entry's status, or the first's, reads Success.

    THE SEQUENCE IS OTTO'S, NOT THE SHELL'S -- ``run()`` takes a list and
    issues the commands itself, so no ``&&`` and no chaining vocabulary is
    involved and this contract needed no shell feature a Zephyr shell lacks.

    THE CROSS-WIRED-CONSTANT DISCRIMINATOR IS WEAKER ON A ZEPHYR SHELL, and
    that is measured. The failing command here is deliberately different from
    the one the exit-code contract uses so a cross-wired constant cannot make
    one contract pass on the other's evidence -- on a POSIX cell the CODES
    differ too (5 against 42). A Zephyr shell has exactly one failure code
    that is stable across this lab's LTS releases (``-8`` from an unknown
    command): measured, the obvious second one, ``kernel uptime extra arg``,
    answers ``-22`` on 3.7 and 4.4 and ``0`` on 2.7. So on those cells the two
    contracts share a code and only the command NAMES differ -- which
    ``first_failure.command`` below still tells apart, while the aggregate
    ``exit_code`` assertion no longer can.

    Commands after a failure still run; that is documented (``run()`` budgets a
    sequence by time, not by success) and is asserted here so a future
    fail-fast would be a red rather than a silent narrowing.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        results = await host.run(
            [words.succeeding_command, words.sequence_failing_command, words.single_line_command]
        )

    cell = resolved_cell.cell
    assert results.is_ok is False, (
        f"{cell}: a sequence containing `{words.sequence_failing_command}` reported is_ok=True"
    )
    assert bool(results) is False, (
        f"{cell}: Results truthiness follows is_ok, so a failing sequence is falsy"
    )
    assert results.status is Status.Failed, (
        f"{cell}: the aggregate takes the first non-ok entry's status, not {results.status!r}"
    )
    assert results.exit_code == words.sequence_failing_code, (
        f"{cell}: the aggregate exit_code is the first failing command's "
        f"({words.sequence_failing_code}), got {results.exit_code}"
    )

    failure = results.first_failure
    assert failure is not None, f"{cell}: first_failure is None on a sequence that failed"
    assert failure.command == words.sequence_failing_command, (
        f"{cell}: first_failure names {failure.command!r}, not the command that failed"
    )

    assert [entry.retcode for entry in results] == [0, words.sequence_failing_code, 0], (
        f"{cell}: per-command retcodes were {[e.retcode for e in results]}; the "
        f"command after the failure is documented to still run"
    )
    assert_single_line_answer(results[2].value, words, cell, "the command after the failure")
