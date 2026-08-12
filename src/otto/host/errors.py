"""The two ways a host command fails, as separate types.

Every package that drives a host through :meth:`~otto.host.host.Host.exec` had
one ``RuntimeError`` for both, and the difference is the one a caller acts on:
a host that cannot be reached (or never answered) is an infrastructure problem
to report and move past, while a command that RAN and failed is a result about
the system under test.

The two classes are PEERS, not parent and child — neither implies the other,
and a caller catching one must not silently catch the other. Both keep
``RuntimeError`` so the ``except (ValueError, RuntimeError)`` clauses in
``otto.cli`` (where typer's vendored click makes ``typer.Exit`` a
``RuntimeError`` too) keep working unchanged.

:func:`exec_or_raise` is the control flow those types exist to express. It
takes the two classes as arguments rather than hard-coding them, because the
sequence is what repeats and the taxonomy is the caller's: ``otto.link`` names
its own :class:`~otto.link.manage.LinkHostUnreachableError` /
:class:`~otto.link.manage.LinkCommandFailedError` so an ``except`` around an
impair can mean "link work failed" rather than "some host call failed", and it
gets the same three checks by passing them in.
"""

from typing import Any

from ..errors import OttoError
from ..logger.mode import LogMode
from ..result import CommandResult


class HostUnreachableError(OttoError, RuntimeError):
    """Transport to the host failed, or the command never completed.

    A timeout belongs here rather than with :class:`HostCommandError`: the
    command produced no verdict, so nothing about the system under test was
    learned.
    """


class HostCommandError(OttoError, RuntimeError):
    """The command ran on the host and reported failure.

    The host was reachable and answered; the answer was a non-ok status (or
    output the caller cannot use).
    """


class UnsupportedOnUserlandError(OttoError, RuntimeError):
    """The host's userland provides no way to do what otto was asked to do.

    A THIRD outcome, and the reason it is not one of the two above: nothing was
    attempted, so there is neither a transport problem nor a verdict about the
    system under test. otto knows in advance that the command it would emit
    cannot work — a host that resolved ``elevation`` to ``"none"`` has no sudo
    and no su — and says so at the call site instead of shipping a command that
    fails somewhere less informative.

    Raising rather than degrading is the whole point on a privilege path. A
    quiet fallback to sudo on a host without sudo LOOKS like it worked: the
    wrapped command runs, the shell answers ``sudo: not found``, and the
    failure is attributed to whatever the caller was actually doing.

    ``RuntimeError``, matching :class:`HostCommandError` and
    :class:`HostUnreachableError`, so the ``except (ValueError, RuntimeError)``
    clauses that already bracket host work keep catching it.

    EXPECTED TO MOVE. This class belongs with the userland gap registry
    described in ``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md``,
    which will render its message from the registry's entry for the missing
    capability rather than from the caller's f-string. It is defined here
    because the elevation branch needs it now and the registry does not exist
    yet; when the registry lands, this becomes its rendering surface rather
    than being redefined next to it.
    """


async def exec_or_raise(
    host: Any,
    cmd: str,
    *,
    timeout: float,
    log: LogMode = LogMode.QUIET,
    unreachable: type[OttoError] = HostUnreachableError,
    failed: type[OttoError] = HostCommandError,
) -> CommandResult:
    """Run one read-only *cmd* on *host*, raising instead of returning a bad result.

    Three checks in the order the information arrives, and the order is the
    substance: the transport can fail before anything runs, the command can
    fail to finish, and only then can it finish badly. Collapsing the first two
    into the third is what produced the review's headline defect — a reachable
    host reported as unreachable.

    *unreachable* and *failed* let a package raise its OWN pair while keeping
    this sequence and these messages; the defaults are the shared ones.

    Raises:
        HostUnreachableError: the transport raised, or the command hit
            *timeout* without completing. (Or *unreachable*, if given.)
        HostCommandError: the command completed with a non-ok status. (Or
            *failed*, if given.)
    """
    try:
        result = await host.exec(cmd, timeout=timeout, log=log)
    except (OSError, ConnectionError) as e:
        raise unreachable(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    if result.timed_out:
        raise unreachable(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {timeout}s"
        )
    if not result.is_ok:
        raise failed(f"{cmd!r} failed on {host.id!r}: {result.msg or result.value}")
    return result
