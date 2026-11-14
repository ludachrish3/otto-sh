"""One exit-status rule for the routes that run a command as a process.

A process route -- the ssh exec channel, local ``exec`` and the interactive
ssh bridge -- learns how a command ended from a return code in asyncio's
convention: the exit status, or ``-N`` when signal ``N`` ended the process, or
``None`` when neither was reported. A shell reports the same death as
``128 + N``, so a caller would otherwise see the same command end two ways
depending on the route. :func:`exit_status_of` is that translation, in one
place, and :func:`no_return_code_result` is the one result a process route
gives when there is no status to translate.
"""

import signal

from ..result import CommandResult
from ..utils import Status
from .exec_route import ExecRoute

NO_RETURN_CODE = "Process did not provide a return code"
"""The ``value`` of :func:`no_return_code_result`."""


def _signalled(n: int) -> int:
    """Return the exit status a POSIX shell reports for a command killed by signal *n*."""
    return 128 + n


def exit_status_of(returncode: "int | None") -> "int | None":
    """Return a process route's return code as the exit status a shell would report.

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


def no_return_code_result(*, cmd: str, route: ExecRoute) -> CommandResult:
    """Build the one result of a process that ended with neither an exit status nor a signal."""
    return CommandResult(
        status=Status.Error,
        value=NO_RETURN_CODE,
        command=cmd,
        retcode=-1,
        route=route.value,
    )
