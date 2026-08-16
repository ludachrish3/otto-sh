"""The two ways a host command fails, as separate types.

Every package that drives a host through :meth:`~otto.host.host.Host.exec` had
one ``RuntimeError`` for both, and the difference is the one a caller acts on:
a host that cannot be reached (or never answered) is an infrastructure problem
to report and move past, while a command that RAN and failed is a result about
the system under test.

There is a THIRD outcome that is not defined here: ``CommandNotRunError``, for
a command a dry run declined to issue, lives in ``otto.result`` next to the
only thing that raises it (``NotRunResult.value``). It cannot live here —
``otto.result`` would have to import this module to raise it, and this module
imports ``CommandResult`` from ``otto.result``, so the edge is a circular
import at runtime and a layering inversion in ``tach.toml`` (``otto.host``
depends on ``otto.result``, never the reverse).

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

from typing import TYPE_CHECKING, Any

from ..errors import OttoError
from ..logger.mode import LogMode
from ..result import CommandResult

if TYPE_CHECKING:
    # Typing only, and deliberately so: `otto.host.userland` imports THIS
    # module at runtime to raise the class below, so a runtime import back
    # would be a cycle. `UnsupportedOnUserlandError.for_gap` reads four
    # attributes off the record and never constructs one.
    from .userland import Gap


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

    THE REGISTRY HAS LANDED, and this class stayed put. An earlier version of
    this note said the class was EXPECTED TO MOVE, next to the userland gap
    registry described in
    ``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md``. What
    the registry actually needed was a RENDERING SURFACE, which is
    :meth:`for_gap` below, and moving the class to get one would have dragged
    every ``except`` clause and every import in this package along with it for
    no gain. So the dependency runs the other way: ``otto.host.userland``
    imports this class to raise it, and this class knows a
    :class:`~otto.host.userland.Gap` only as a shape it can format.

    Both ways of raising it are legitimate and they answer different
    questions. :meth:`for_gap` renders what otto MEASURED about a whole class
    of userland ("BusyBox has no ``shutdown`` applet"). A caller's own
    f-string renders what THIS host answered a probe ("resolved
    ``elevation='none'``") — see ``PosixPrivilege._elevate`` and
    ``ShellFileTransfer._run_put``, which are probe-driven and stay that way.
    A registry record cannot say the second thing and a probe cannot say the
    first.
    """

    @classmethod
    def for_gap(
        cls, gap: "Gap", *, host: str = "", attempted: str = "", observed: str = ""
    ) -> "UnsupportedOnUserlandError":
        """Build the refusal for a declared *gap*, rendered from the record.

        The spec's first consumer of the registry: "a named
        ``UnsupportedOnUserlandError`` renders its message from the record
        (surface, why, docs anchor) instead of surfacing a bare ``sudo: not
        found``".

        Everything in the message comes from the record except *host*,
        *attempted* and *observed*, which the caller supplies because the
        record cannot know them. That is the point — an operator who hits this
        gets the same four facts (what broke, why, what proved it, where to
        read more) wherever it fires, and a docs page rendered from the same
        record cannot disagree with it.

        ``Nothing was attempted`` leads, because it is the one thing this
        exception means that the other two host errors do not: no command was
        sent, so nothing was learned about the system under test.

        *observed* REPLACES THAT LEAD, and exists because one caller cannot
        say it. ``otto.host.transfer.sftp.open_sftp_or_attribute`` translates a
        failure that has ALREADY HAPPENED — whether the device serves sftp is
        answerable only by opening the subsystem, so there is no fact to
        pre-check and the operation is its own probe. For that site "nothing
        was attempted" would be false in the message's first clause, so the
        caller passes what it watched instead and the record supplies the rest
        unchanged. Empty for every other caller, which is why every existing
        message is byte-identical to what it was before this parameter
        existed. *attempted* is dropped when *observed* is given: the two
        answer the same question (what otto was doing) and *observed* answers
        it with an outcome attached.

        TAKES A ``measured-broken`` RECORD ONLY, and does not check. The
        message says "otto has measured ``<surface>`` as broken" and prints
        ``MEASURED: <measured_on>``, both of which are false for an
        ``untested`` record -- whose ``measured_on`` is empty by the
        :class:`~otto.host.userland.Gap` invariant, so the rendering would read
        ``MEASURED: .`` The firing rule lives in
        :func:`~otto.host.userland.refuse_if_gapped`, which is what decides
        that a record refuses at all, and every raise today goes through it, so
        this is a precondition on direct callers rather than a reachable bug.
        A caller doing its own dispatch must check
        :attr:`~otto.host.userland.Gap.refuses` first.
        """
        who = f"{host}: " if host else ""
        what = f" ({attempted})" if attempted else ""
        lead = observed or f"nothing was attempted{what}"
        return cls(
            f"{who}{lead} — otto has measured `{gap.surface}` as "
            f"broken on this class of userland. {gap.reason}. MEASURED: {gap.measured_on}. "
            f"QUEUED FOR: {gap.queued_for}. See {gap.docs_anchor}."
        )


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
