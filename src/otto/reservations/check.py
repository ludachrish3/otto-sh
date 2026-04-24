"""Lab reservation check logic and exceptions.

The :func:`check_reservations` function is the heart of the subsystem:
given a lab, a username, and a backend, it raises
:class:`MissingReservationError` if the user does not hold every resource
the lab needs.  The error message lists missing resources and their current
holders (via :meth:`ReservationBackend.who_reserved`) but deliberately does
NOT advertise ``--skip-reservation-check`` — that flag is surfaced only when
the backend itself is unreachable, where proceeding requires it.

:func:`gate` is the subcommand-facing entry point that wires the check into
the CLI: it reads state from the configmodule, honors the top-level skip
flag, emits the bold-red skip warning when used, and otherwise runs the
check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logger import getOttoLogger

if TYPE_CHECKING:
    from ..configmodule.configmodule import ConfigModule
    from ..configmodule.lab import Lab
    from .protocol import ReservationBackend

logger = getOttoLogger()


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

    holders: dict[str, str | None] = {r: backend.who_reserved(r) for r in sorted(missing)}
    lines = [
        f"User {username!r} does not hold all resources required by lab "
        f"{lab.name!r}. Missing:"
    ]
    for resource, holder in holders.items():
        if holder is None:
            lines.append(f"  - {resource} (unreserved)")
        else:
            lines.append(f"  - {resource} (held by {holder})")
    raise MissingReservationError("\n".join(lines))


def gate(cm: ConfigModule | None) -> None:
    """Run the reservation check for the current invocation, if applicable.

    Called from each live-lab subcommand callback (``run``, ``test``,
    ``host``, ``monitor``) after the configmodule has been populated by the
    top-level Typer callback.  Not called from ``cov report``, which is
    offline.

    Behavior:

    - If ``cm`` is ``None`` (the configmodule singleton was never set up —
      e.g. a unit test invoking a subcommand app directly), the gate is a
      no-op.
    - If ``cm.skip_reservation_check`` is set, logs a bold-red WARNING and
      returns without querying the backend.
    - Otherwise, calls :func:`check_reservations`.  Lets the raised
      exception propagate so Typer renders it with the normal error path.
    """
    if cm is None or cm.reservation_backend is None:
        # build_backend was never called or returned None — no check configured.
        return

    if cm.skip_reservation_check:
        username = cm.identity.username if cm.identity is not None else "<unknown>"
        needed = required_resources(cm.lab)
        from rich import print as rprint
        rprint(
            f"[bold red]\N{WARNING SIGN}  Reservation check SKIPPED "
            f"for user {username!r} on lab {cm.lab.name!r}. "
            f"Required resources: {sorted(needed)!r}[/bold red]"
        )
        logger.warning(
            "Reservation check skipped for user %r on lab %r. Required: %r",
            username, cm.lab.name, sorted(needed),
        )
        return

    assert cm.identity is not None, "identity must be resolved before gate() runs"
    check_reservations(cm.lab, cm.identity.username, cm.reservation_backend)
