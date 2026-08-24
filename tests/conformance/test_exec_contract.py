"""What every host owes a caller of ``run()``: the exit code, and the framing.

Three contracts, and the third is the one that makes the first two mean
anything. A suite that only checks the happy path passes unchanged against a
backend that hardcodes ``Status.Success`` and returns the empty string, so the
failing-command contract is not an extra case -- it is the discriminator the
others are measured against.

Assertions are equality or ``startswith``, never ``in``. ``Results.value`` is
the LIST of per-command results and stringifies to ``[CommandResult(...)]``,
which CONTAINS the output a reader meant to check; an ``in`` assertion against
the wrong attribute therefore passes silently. The right read is ``.only`` for
a single command, and this rule is written down because it has already cost
this workstream one bug.
"""

import pytest

from otto.utils import Status
from tests.conformance._cells import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

# Deliberately neither 0 nor 1 nor 255. A backend that collapses every failure
# to "1", or that reports the ssh-style "the command never ran" 255, is
# indistinguishable from a correct one under `false`; it is not under this.
_DISTINCT_CODE = 42

# The code the sequence contract fails on. Different from _DISTINCT_CODE so a
# cross-wired constant cannot make one contract pass on the other's evidence.
_SEQUENCE_CODE = 5

# Two lines, so the framing contract measures line structure and not just
# "some text came back". The token is not a substring of any prompt, sentinel
# or shell builtin, so a leak of any of those breaks the equality below.
_FRAME_TOKEN = "otto-conformance-frame"
_TAIL_TOKEN = "otto-conformance-tail"


async def test_exec_reports_the_documented_exit_code(resolved_cell: ResolvedCell) -> None:
    """A command's exit code reaches the caller unchanged.

    Both directions in one test, because "unchanged" is a claim about a
    mapping and a single sample cannot falsify it: a backend that answers 42 to
    everything passes the failing half alone, and one that answers 0 to
    everything passes the succeeding half alone.

    ``(exit N)`` rather than ``sh -c 'exit N'``: ``run()`` drives a PERSISTENT
    shell session, so a bare ``exit`` would take the session down and the next
    contract's failure would be this one's fault. The subshell is also free of
    nested quoting, which is what a shell-dialect difference would bite first.

    ``retcode`` and ``exit_code`` are separate assertions because they are
    separate documented claims -- ``CommandResult.exit_code`` is the ssh-like
    CLI code and is only equal to ``retcode`` when the command actually ran and
    exited non-zero. A timeout, which never ran, answers 255 to the same
    question.
    """
    async with resolved_cell.open_host() as host:
        failed = (await host.run(f"(exit {_DISTINCT_CODE})")).only
        succeeded = (await host.run("(exit 0)")).only

    cell = resolved_cell.cell
    assert failed.retcode == _DISTINCT_CODE, (
        f"{cell}: `(exit {_DISTINCT_CODE})` reported retcode {failed.retcode}, "
        f"not the code the command exited with"
    )
    assert failed.exit_code == _DISTINCT_CODE, (
        f"{cell}: `(exit {_DISTINCT_CODE})` reported exit_code {failed.exit_code}; "
        f"a command that ran and exited non-zero carries its own retcode"
    )
    assert failed.status is Status.Failed, (
        f"{cell}: a non-zero exit is Status.Failed, not {failed.status!r}"
    )
    assert failed.timed_out is False, (
        f"{cell}: `(exit {_DISTINCT_CODE})` exited on its own; timed_out must stay False"
    )

    assert succeeded.retcode == 0, f"{cell}: `(exit 0)` reported retcode {succeeded.retcode}, not 0"
    assert succeeded.status is Status.Success, (
        f"{cell}: a zero exit is Status.Success, not {succeeded.status!r}"
    )


async def test_exec_frames_output_without_prompt_noise(resolved_cell: ResolvedCell) -> None:
    """Stdout carries the command's output and nothing the shell added.

    Equality, not ``in``. ``in`` is satisfied by output that also carries the
    echoed command line, a sentinel, or a shell prompt -- and a prompt IS what
    leaks here when framing breaks: the timeout contract's recovered partial on
    a loopback-ssh cell ends in a literal ``vagrant@otto:~$``, measured in this
    worktree. Only an equality rejects that.

    ``.strip()`` and no more. Whether the framing keeps the payload's trailing
    newline is not something otto documents -- ``docs/library/sessions.md``
    reads every example's ``value`` through ``.strip()`` -- so pinning it here
    would be locking in observed behaviour, which this suite is not for. What
    IS the contract is that nothing the shell added survives, and stripping
    whitespace cannot hide a prompt, an echo or a sentinel: none of them are
    whitespace.
    """
    async with resolved_cell.open_host() as host:
        framed = (await host.run(f"printf '%s\\n%s\\n' {_FRAME_TOKEN}-1 {_FRAME_TOKEN}-2")).only

    cell = resolved_cell.cell
    assert framed.is_ok, f"{cell}: printf failed outright -- {framed.status!r} {framed.value!r}"
    assert framed.value.strip() == f"{_FRAME_TOKEN}-1\n{_FRAME_TOKEN}-2", (
        f"{cell}: framed output was {framed.value!r}; anything beyond the two "
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

    Commands after a failure still run; that is documented (``run()`` budgets a
    sequence by time, not by success) and is asserted here so a future
    fail-fast would be a red rather than a silent narrowing.
    """
    async with resolved_cell.open_host() as host:
        results = await host.run(
            ["(exit 0)", f"(exit {_SEQUENCE_CODE})", f"printf '%s\\n' {_TAIL_TOKEN}"]
        )

    cell = resolved_cell.cell
    assert results.is_ok is False, (
        f"{cell}: a sequence containing `(exit {_SEQUENCE_CODE})` reported is_ok=True"
    )
    assert bool(results) is False, (
        f"{cell}: Results truthiness follows is_ok, so a failing sequence is falsy"
    )
    assert results.status is Status.Failed, (
        f"{cell}: the aggregate takes the first non-ok entry's status, not {results.status!r}"
    )
    assert results.exit_code == _SEQUENCE_CODE, (
        f"{cell}: the aggregate exit_code is the first failing command's, got {results.exit_code}"
    )

    failure = results.first_failure
    assert failure is not None, f"{cell}: first_failure is None on a sequence that failed"
    assert failure.command == f"(exit {_SEQUENCE_CODE})", (
        f"{cell}: first_failure names {failure.command!r}, not the command that failed"
    )

    assert [entry.retcode for entry in results] == [0, _SEQUENCE_CODE, 0], (
        f"{cell}: per-command retcodes were {[e.retcode for e in results]}; the "
        f"command after the failure is documented to still run"
    )
    assert results[2].value.strip() == _TAIL_TOKEN, (
        f"{cell}: the command after the failure produced {results[2].value!r}"
    )
