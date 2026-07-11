"""Lab reservation check logic and exceptions.

The :func:`check_reservations` function is the heart of the subsystem:
given a lab, a username, and a backend, it raises
:class:`MissingReservationError` if the user does not hold every resource
the lab needs.  The error message lists missing resources and their current
holders (via :meth:`~otto.reservations.protocol.ReservationBackend.who_reserved`) but
deliberately does NOT advertise ``--skip-reservation-check`` — that flag is surfaced only when
the backend itself is unreachable, where proceeding requires it.

:class:`ReservationGate` is the library-facing, framework-free entry point:
:meth:`ReservationGate.evaluate` honors the skip flag (returning a
plain-text warning for the caller to present however it likes) and
otherwise runs the check. It has no dependency on Typer or any other CLI
framework — the CLI adapter that presents ``evaluate()``'s output lives in
``otto.cli``.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config.lab import Lab
    from .identity import ResolvedIdentity
    from .protocol import ReservationBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReservationGateOutcome:
    """Result of :meth:`ReservationGate.evaluate`.

    ``warning`` is plain text (no rich markup) — CLI callers decide how to
    present it (e.g. wrapping it in ``[bold red]...[/bold red]``).
    """

    checked: bool
    skipped: bool
    warning: "str | None"


@dataclass(frozen=True)
class ReservationGate:
    """Per-invocation reservation gate: framework-free, callable from any Python caller.

    Typically built by :func:`~otto.reservations.build_reservation_gate` and,
    in the CLI, stashed on Typer's ``ctx.meta["otto_reservation"]`` — but
    nothing here depends on Typer or ``ctx.meta``; construct one directly and
    call :meth:`evaluate` from any script.
    """

    backend: "ReservationBackend | None" = None
    identity: "ResolvedIdentity | None" = None
    skip_check: bool = False
    # Builds the backend on demand. Set even under -R (where ``backend`` is
    # None) so reservation subcommands can construct it only when needed.
    backend_factory: "Callable[[], ReservationBackend] | None" = None

    def evaluate(self) -> ReservationGateOutcome:
        """Run the reservation check (or the skip path) and report the outcome.

        When ``skip_check`` (``-R``) is set, a loud warning is always
        produced — regardless of whether a backend was configured — and no
        check runs. Otherwise, a ``backend`` of ``None`` (no ``[reservations]``
        section resolved, or nothing to check) is a silent no-op. The active
        lab is fetched lazily so the no-op paths never require an
        :class:`~otto.context.OttoContext`.

        Raises
        ------
        MissingReservationError
            If any required resource is not held by the resolved identity.
        RuntimeError
            If a backend is configured but ``identity`` was never resolved —
            a construction invariant, not a runtime condition callers should
            handle.
        """
        from ..config import get_lab

        if self.skip_check:
            lab = get_lab()
            username = self.identity.username if self.identity is not None else "<unknown>"
            needed = required_resources(lab)
            warning = (
                f"\N{WARNING SIGN}  Reservation check SKIPPED for user {username!r} "
                f"on lab {lab.name!r}. Required resources: {sorted(needed)!r}"
            )
            logger.warning(
                "Reservation check skipped for user %r on lab %r. Required: %r",
                username,
                lab.name,
                sorted(needed),
            )
            return ReservationGateOutcome(checked=False, skipped=True, warning=warning)

        if self.backend is None:
            return ReservationGateOutcome(checked=False, skipped=False, warning=None)

        lab = get_lab()
        if self.identity is None:
            raise RuntimeError("identity must be resolved before evaluate() runs")
        check_reservations(lab, self.identity.username, self.backend)
        return ReservationGateOutcome(checked=True, skipped=False, warning=None)


class ReservationBackendError(Exception):
    """Raised by backends when a query cannot be answered.

    Network outages, DB errors, malformed data files, and authentication
    failures all surface as this exception so the CLI can translate them
    into a single fail-closed startup error.
    """


class MissingReservationError(Exception):
    """Raised when the effective user does not hold every required resource.

    The message lists the missing resources and their current holders.  It
    does not mention ``--skip-reservation-check`` — that suggestion belongs
    only in the backend-failure path, never on a legitimate contention
    failure (or the option gets abused).
    """


def required_resources(lab: "Lab") -> set[str]:
    """Return every resource identifier the lab needs.

    The union of the lab's own ``resources`` set and each host's
    ``resources`` set.  Any of these resources that are not held by the
    effective user will cause :func:`check_reservations` to raise.
    """
    needed: set[str] = set(lab.resources)
    for host in lab.hosts.values():
        needed.update(host.resources)
    return needed


def check_reservations(
    lab: "Lab",
    username: str,
    backend: "ReservationBackend",
) -> None:
    """Raise :class:`MissingReservationError` if ``username`` does not cover ``lab``.

    Parameters
    ----------
    lab : Lab
        The lab about to be used.
    username : str
        The reservation-system identity to check against.
    backend : ReservationBackend
        The configured reservation backend.

    Raises
    ------
    MissingReservationError
        If any required resource is not held by ``username``.
    ReservationBackendError
        If the backend cannot answer the query (network, file, DB failure).
    """
    # NullReservationBackend short-circuits to a no-op so teams without a
    # scheduler configured aren't blocked.  Importing here avoids a circular
    # import between this module and the null backend's factory path.
    from .null_backend import NullReservationBackend

    if isinstance(backend, NullReservationBackend):
        return

    needed = required_resources(lab)
    if not needed:
        return

    reserved = backend.get_reserved_resources(username)
    missing = needed - reserved
    if not missing:
        return

    holders: dict[str, list[str]] = {r: backend.who_reserved(r) for r in sorted(missing)}
    lines = [
        f"User {username!r} does not hold all resources required by lab {lab.name!r}. Missing:"
    ]
    for resource, who in holders.items():
        if not who:
            lines.append(f"  - {resource} (unreserved)")
        else:
            lines.append(f"  - {resource} (held by {', '.join(who)})")
    raise MissingReservationError("\n".join(lines))
