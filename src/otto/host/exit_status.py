"""One exit-status rule for a command run as a process, and one reader of "no status".

The interactive ssh bridge learns how a command ended from a return code in
asyncio's convention: the exit status, or ``-N`` when signal ``N`` ended the
process, or ``None`` when neither was reported. A shell reports the same
death as ``128 + N``, so a caller would otherwise see the same command end
two ways. :func:`exit_status_of` is that translation, in one place, and
:func:`subshell_status_of` gives a ksh93 subshell's signal death the same
shape.
:func:`gave_no_exit_status` is the one reader of a result that carries no
exit status, on any host.
"""

import signal

from ..result import CommandResult


def _signalled(n: int) -> int:
    """Return the exit status a POSIX shell reports for a command killed by signal *n*."""
    return 128 + n


def subshell_status_of(reported: int) -> int:
    """Return the status a subshell reported as the exit status a shell reports for its command.

    Only ksh93 reports a command a signal killed inside a subshell as
    ``256 + N``; every other shell, and every other ksh93 status, is already
    ``0..255``. A status above 255 is therefore ksh93's signal death and
    becomes ``128 + N``; any other is unchanged.
    """
    if reported > 255:  # noqa: PLR2004 — the largest exit status a shell reports
        return _signalled(reported - 256)
    return reported


def exit_status_of(returncode: "int | None") -> "int | None":
    """Return a process's return code as the exit status a shell would report.

    ``-N`` for any signal number this platform has (``1 <= N < NSIG``,
    real-time signals included: most have no :class:`signal.Signals` member,
    and they are signals all the same) becomes ``128 + N``. A number outside
    that range -- asyncssh answers ``-99`` for a signal name its table lacks
    -- is no signal, so it is ``None`` rather than a guess, as is ``None``
    itself. Every other value is the exit status, unchanged.
    """
    if returncode is None or returncode >= 0:
        return returncode
    if 1 <= -returncode < signal.NSIG:
        return _signalled(-returncode)
    return None


def gave_no_exit_status(result: CommandResult) -> bool:
    """Return whether *result* is of a command that gave no exit status to read.

    Either its timeout killed it, or it never ran. "Never ran" is
    ``retcode == -1``, the field's documented meaning
    (:attr:`~otto.result.CommandResult.retcode`; a shell cannot exit -1),
    which otto's own result for a session that died mid-command carries. The
    ``value`` of such a result is otto's text, never the command's answer,
    so a caller that parses an answer asks this first.
    """
    return result.timed_out or result.retcode == -1
