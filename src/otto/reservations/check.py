"""Lab reservation check logic and exceptions.

The :func:`check_reservations` function is the heart of the subsystem:
given a lab, a username, and a backend, it raises
:class:`MissingReservationError` if the user does not hold every resource
the lab needs.  The error message lists missing resources and their current
holders (via :meth:`~otto.reservations.protocol.ReservationBackend.who_reserved`) but
deliberately does NOT advertise ``--skip-reservation-check`` — that flag is surfaced only when
the backend itself is unreachable, where proceeding requires it.

:func:`gate` is the subcommand-facing entry point that wires the check into
the CLI: it reads the per-invocation reservation state from Typer's
``ctx.meta["otto_reservation"]``, honors the top-level skip flag, emits the
bold-red skip warning when used, and otherwise runs the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

from ..logger import get_otto_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..configmodule.lab import Lab
    from .identity import ResolvedIdentity
    from .protocol import ReservationBackend

logger = get_otto_logger()


@dataclass(frozen=True)
class ReservationState:
    backend: "ReservationBackend | None" = None
    identity: "ResolvedIdentity | None" = None
    skip_check: bool = False
    # Builds the backend on demand. Set even under -R (where ``backend`` is
    # None) so reservation subcommands can construct it only when needed.
    backend_factory: "Callable[[], ReservationBackend] | None" = None


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


def required_resources(lab: Lab) -> set[str]:
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
    lab: Lab,
    username: str,
    backend: ReservationBackend,
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


def gate(ctx: typer.Context) -> None:
    """Run the reservation check for this invocation, reading state from ctx.meta.

    When ``-R`` (skip_check) is set, a loud warning is emitted regardless of
    whether a backend was configured. No-ops when no reservation state is
    present (e.g. unit tests invoking a subcommand app directly) or, after the
    skip-warning path, when no backend is configured. The active lab is fetched
    lazily so the no-op paths never require an OttoContext.
    """
    res = ctx.meta.get("otto_reservation")
    if res is None:
        return

    from ..configmodule import get_lab

    # A skipped check (-R) must be loud and is independent of whether a backend
    # was constructed — under -R no backend is built (backend is None).
    if res.skip_check:
        lab = get_lab()
        username = res.identity.username if res.identity is not None else "<unknown>"
        needed = required_resources(lab)
        from rich import print as rprint

        rprint(
            f"[bold red]\N{WARNING SIGN}  Reservation check SKIPPED for user "
            f"{username!r} on lab {lab.name!r}. Required resources: {sorted(needed)!r}[/bold red]"
        )
        logger.warning(
            "Reservation check skipped for user %r on lab %r. Required: %r",
            username,
            lab.name,
            sorted(needed),
        )
        return

    if res.backend is None:
        return

    lab = get_lab()
    if res.identity is None:
        raise RuntimeError("identity must be resolved before gate() runs")
    check_reservations(lab, res.identity.username, res.backend)
