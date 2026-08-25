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

from collections.abc import Callable

import pytest

from otto.utils import Status
from tests.conformance._framing import assert_single_line_answer, framing_leak
from tests.conformance._resolved import ResolvedCell
from tests.conformance._vocabulary import OTTO_SENTINEL_PREFIX

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


@pytest.mark.observable(
    "otto's CommandResult.retcode, .exit_code and .status for "
    "`{words.failing_command}` (must report {words.failing_code}) and "
    "`{words.succeeding_command}` (must report 0)"
)
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


@pytest.mark.observable(
    "the framed stdout of `{words.multiline_command}`: more than one line, and none of "
    "otto's `__OTTO_` scaffolding, `retval` line, ANSI escape or echoed command line "
    "surviving into it"
)
async def test_exec_frames_output_without_prompt_noise(
    resolved_cell: ResolvedCell, note_observable: "Callable[[str], None]"
) -> None:
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

    THE THIRD ASSERTION'S PRESENCE IS ALSO WHAT THIS CELL'S SUPPORT-MATRIX
    OBSERVABLE IS, which is why :func:`~tests.conformance._observable.note_observable`
    is called from the body rather than left to the marker's template. The
    marker declares the floor every cell gets; only the running test knows
    whether this userland could afford exact equality, and a matrix cell that
    published the strong observable for a cell that only got the weak one would
    overstate its own evidence -- §5's shell-history example, in this tree.
    """
    words = resolved_cell.vocabulary
    note_observable(
        f"the framed stdout of `{words.multiline_command}`: more than one line, no otto "
        f"`__OTTO_` scaffolding, `retval` line, ANSI escape or echoed command line, and "
        + (
            f"exact equality against the {len(words.multiline_expected.splitlines())} lines "
            f"the tester chose"
            if words.multiline_expected is not None
            else "NO exact comparison -- the stimulus is a stock builtin whose text belongs "
            "to the firmware, so there is nothing to compare against"
        ),
    )
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


@pytest.mark.observable(
    "the aggregate Results of a sequence whose middle command "
    "`{words.sequence_failing_command}` exits {words.sequence_failing_code}: its is_ok, "
    "its status, and which command first_failure names"
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


# ==========================================================================
# POSITIVE CONTROLS -- proof that each observable above CAN GO RED on this cell
# ==========================================================================
# Spec 2026-08-22 s5 refuses a `measured-ok` matrix cell that does not name
# one of these. Each asserts THE INSTRUMENT rather than the product: the
# contract says the host answered correctly, and its control says this cell's
# answer would have been REJECTED had it been wrong. They are parametrized
# over the same drawn cells as the contracts they vouch for -- this module
# declares no `applicable_cell`, so that is every drawn cell -- because a
# control that passed on `gnu` says nothing about `busybox-1.16.1`.
#
# The whole rationale, and why the marker rather than the signature separates
# a control from a contract, is in `tests/conformance/_controls.py`.


@pytest.mark.positive_control("exec-exit-code")
async def test_control_the_exit_code_channel_reports_more_than_one_code(
    resolved_cell: ResolvedCell,
) -> None:
    """The exit-code assertions above are falsifiable HERE: the channel MOVES.

    The contract pins two equalities -- ``failed.retcode == failing_code`` and
    ``succeeded.retcode == 0`` -- and each of them is satisfied by a backend
    that answers its own constant to everything. This runs both stimuli and
    asserts the CROSS pairs, which is the half a constant cannot satisfy: the
    succeeding command's code is not ``failing_code``, and the failing
    command's is not 0. So a backend collapsing every reply to one value
    fails at least one of the contract's equalities, which is what "the
    contract can go red on this cell" means.

    **THE PLAN'S SKETCH FOR THIS SURFACE IS FALSE ON A ZEPHYR CELL, AND IT
    WAS MEASURED RATHER THAN ASSUMED.** It asked for "the cell's vocabulary
    yields two distinct known FAILURE codes". A POSIX vocabulary has two (42
    and 5); a Zephyr one has ONE -- ``failing_code`` and
    ``sequence_failing_code`` are both ``-8``, which
    ``tests/conformance/_vocabulary.py`` records and
    ``tests/unit/test_conformance_bed.py`` pins. Nor is there a stable third:
    MEASURED 2026-08-24 across all seven bed guests, ``fs`` (a subcommand
    group invoked bare) answers ``1`` on the four guests that HAVE a
    filesystem and ``-8`` -- indistinguishable from ``failing_command`` -- on
    ``zephyr37_nofs``, ``zephyr37_llext`` and ``zephyr44_llext``, which do
    not. A control keyed on it would have been strong on four cells, VACUOUS
    on three, and identical-looking on all seven. The two codes every
    vocabulary really does have are 0 and ``failing_code``, so those are the
    two this uses.

    Nothing here is written back to the host, so there is nothing to restore.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        failed = (await host.run(words.failing_command)).only
        succeeded = (await host.run(words.succeeding_command)).only

    cell = resolved_cell.cell
    assert failed.retcode != succeeded.retcode, (
        f"{cell}: `{words.failing_command}` and `{words.succeeding_command}` both "
        f"reported retcode {failed.retcode}, so the exit-code channel carries ONE "
        f"value here and the contract's equalities cannot go red on this cell"
    )
    assert succeeded.retcode != words.failing_code, (
        f"{cell}: `{words.succeeding_command}` reported {words.failing_code}, the very "
        f"code the contract asserts of a FAILING command -- the assertion is satisfied "
        f"by a backend that answers that constant to everything"
    )
    assert failed.retcode != 0, (
        f"{cell}: `{words.failing_command}` reported 0, so the contract's "
        f"`succeeded.retcode == 0` is satisfied by every reply this cell can make"
    )
    assert succeeded.status is not failed.status, (
        f"{cell}: both stimuli reported {succeeded.status!r}, so the contract's "
        f"Status.Success / Status.Failed split cannot go red here either"
    )


@pytest.mark.positive_control("exec-framing")
async def test_control_the_framing_check_sees_planted_pollution(
    resolved_cell: ResolvedCell,
) -> None:
    """Plant otto's own sentinel prefix in the OUTPUT, and require the leak detector to fire.

    The exemplar's shape (``tests/e2e/host/test_shell_history_e2e.py``'s
    ``test_opting_in_still_records``): create the condition the real assertion
    looks for, prove the instrument DETECTS it. A framing check that cannot
    see planted pollution proves nothing about real pollution -- and this
    contract's universal half is exactly such a check, ``framing_leak``, whose
    ``None`` on a clean reply is otherwise indistinguishable from a ``None``
    it would return for anything.

    THE PLANT IS A LOOKALIKE, NEVER A REAL FRAME. otto's markers carry a
    per-session id (``SessionMarkers.for_session`` -> ``__OTTO_<id>_BEGIN__``)
    and both frames parse for the escaped id, so a literal carrying the prefix
    alone cannot be mistaken for a frame by otto while being exactly what
    ``framing_leak`` scans for. Verified on the bed before this was written:
    the reply came back whole, on every guest.

    THE SECOND HALF CONTROLS THE OTHER UNIVERSAL ASSERTION. The contract also
    requires more than one line back, and that too is satisfied by any
    talkative backend; so the vocabulary's SINGLE-line command is run and
    required to produce exactly one line -- the reply the contract's
    ``len(...) > 1`` must refuse.

    Read-only on the far side: one unknown command on a Zephyr shell, one
    ``printf`` on a POSIX one. Nothing to restore.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        planted = (await host.run(words.sentinel_plant_command)).only
        single = (await host.run(words.single_line_command)).only

    cell = resolved_cell.cell
    body = planted.value.strip()
    assert OTTO_SENTINEL_PREFIX in body, (
        f"{cell}: `{words.sentinel_plant_command}` was supposed to put "
        f"{OTTO_SENTINEL_PREFIX!r} into its own output and the reply was {planted.value!r} "
        f"-- the plant did not land, so nothing about the leak detector is proved here"
    )
    leak = framing_leak(body, words.sentinel_plant_command)
    assert leak is not None, (
        f"{cell}: framing_leak() reported NOTHING for output that carries "
        f"{OTTO_SENTINEL_PREFIX!r} -- the contract's `leak is None` assertion cannot "
        f"go red on this cell, so its green says nothing"
    )
    assert OTTO_SENTINEL_PREFIX in leak, (
        f"{cell}: framing_leak() fired, but for {leak!r} rather than the planted "
        f"sentinel -- a detector that answers for the wrong reason is not evidence "
        f"that it would answer for the right one"
    )

    assert single.is_ok, (
        f"{cell}: `{words.single_line_command}` failed outright -- "
        f"{single.status!r} {single.value!r}"
    )
    assert len(single.value.strip().splitlines()) == 1, (
        f"{cell}: `{words.single_line_command}` came back as "
        f"{len(single.value.strip().splitlines())} lines, so the contract's "
        f"`more than one line` assertion has nothing on this cell that it refuses"
    )


@pytest.mark.positive_control("exec-failure-in-sequence")
async def test_control_a_succeeding_sequence_is_not_reported_as_failed(
    resolved_cell: ResolvedCell,
) -> None:
    """A sequence that genuinely succeeds must NOT arrive as a failure.

    The contract asserts a sequence containing a failing command is reported
    failed; every one of its assertions is satisfied by an aggregate that
    reports failure unconditionally -- ``is_ok False``, falsy, ``Status.Failed``,
    a ``first_failure`` that is not None. So this runs the same shape with NO
    failing command in it and requires the opposite of each, which is the
    reply that aggregate could never produce.

    The middle entry is the vocabulary's single-line command rather than a
    third copy of the succeeding one, so the sequence still exercises three
    DISTINCT commands the way the contract's does.

    Nothing is written on the far side; nothing to restore.
    """
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        results = await host.run(
            [words.succeeding_command, words.single_line_command, words.succeeding_command]
        )

    cell = resolved_cell.cell
    assert results.is_ok is True, (
        f"{cell}: a sequence of three commands that all exit 0 reported is_ok=False "
        f"-- {[(e.command, e.retcode) for e in results]}. The contract's "
        f"`is_ok is False` assertion is then true of every sequence this cell can run"
    )
    assert bool(results) is True, f"{cell}: Results truthiness follows is_ok, so this is truthy"
    assert results.status is Status.Success, (
        f"{cell}: the aggregate of three succeeding commands is Status.Success, "
        f"not {results.status!r}"
    )
    assert results.exit_code == 0, (
        f"{cell}: the aggregate exit_code of a succeeding sequence is 0, got "
        f"{results.exit_code} -- the contract's `== sequence_failing_code` cannot "
        f"discriminate on a cell that answers it either way"
    )
    assert results.first_failure is None, (
        f"{cell}: first_failure named {results.first_failure!r} in a sequence with no "
        f"failing command, so the contract's `is not None` proves nothing here"
    )
    assert [entry.retcode for entry in results] == [0, 0, 0], (
        f"{cell}: per-command retcodes were {[e.retcode for e in results]}"
    )
    assert_single_line_answer(
        results[1].value, words, cell, "the middle command of a clean sequence"
    )
