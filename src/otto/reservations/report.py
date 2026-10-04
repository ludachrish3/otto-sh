"""The reservation report: every fact a reservation check reads, built in one place.

:func:`build_report` is the only function that decides when a backend is
queried. The one thing before it is
:meth:`~otto.reservations.check.ReservationGate.check_hosts`, which narrows the
named hosts first and skips the query when none of them declare a resource.
``otto reservation check`` renders its report, and the gate's
:meth:`~otto.reservations.check.ReservationGate.evaluate`, that
``check_hosts`` and :func:`~otto.reservations.check.check_reservations` read
theirs. None of them re-derives the short-circuits below, so they cannot
disagree about when a scheduler outage can fail a run.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .check import (
    EXPIRY_WARNING_WINDOW,
    ResourceLevel,
    active_reservations,
    required_resource_origins,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..config.lab import Lab
    from .protocol import Reservation, ReservationBackend


@dataclass(frozen=True)
class ReservationRow:
    """One required resource at one declaring point, and whether the user holds it."""

    resource: str
    """The resource identifier, exactly as declared."""

    level: ResourceLevel
    """The lab, element or host that declared it."""

    owner: str
    """The lab name, the element's slug, or the host id."""

    held: "bool | None"
    """Whether the user holds it; ``None`` under the ``none`` backend, which cannot answer."""


@dataclass(frozen=True)
class MissingResource:
    """A required resource the user does not hold, and who does."""

    resource: str
    """The resource identifier."""

    holders: "list[Reservation] | None"
    """Its current holders; empty means nobody, ``None`` means the backend cannot say."""


@dataclass(frozen=True)
class ReservationReport:
    """What a run needs, what the user holds, and the verdict.

    Built only by :func:`build_report`. ``rows`` keeps
    :func:`~otto.reservations.check.required_resource_origins` order.
    """

    lab: str
    username: str
    in_play: list[str]
    rows: list[ReservationRow]
    null_backend: bool
    expiring: "list[Reservation]"
    missing: list[MissingResource]

    @property
    def covered(self) -> bool:
        """Whether nothing required is missing."""
        return not self.missing


def build_report(
    lab: "Lab",
    username: str,
    backend: "ReservationBackend",
    *,
    host_ids: "Iterable[str] | None" = None,
    now: "datetime | None" = None,
) -> ReservationReport:
    """Report what *username* needs from *lab* over *host_ids*, and what it holds.

    The backend is queried at most once for the user's rows, and only when
    something is required and the backend is not the ``none`` backend.
    ``holders()`` is asked only about missing resources, and only when the
    backend has that capability. The requirement is walked first, so a lab
    file naming an unknown host still raises under ``none``.

    Raises
    ------
    ValueError
        *host_ids* names a host the lab does not contain.
    RuntimeError
        The backend was built for a different user than *username*.
    ReservationBackendError
        The backend cannot answer.
    """
    from .null_backend import is_null_backend
    from .protocol import SupportsResourceHolders

    ids = list(lab.hosts) if host_ids is None else list(host_ids)
    origins = required_resource_origins(lab, host_ids=ids)
    null = is_null_backend(backend)
    if null or not origins:
        return ReservationReport(
            lab=lab.name,
            username=username,
            in_play=sorted(set(ids)),
            rows=[ReservationRow(o.resource, o.level, o.owner, None) for o in origins],
            null_backend=null,
            expiring=[],
            missing=[],
        )

    # The backend caches rows for the user it was built for. If that disagrees
    # with *username*, every resource would read missing, blaming a user whose
    # reservations were never fetched. Fail loudly instead.
    backend_user = getattr(backend, "username", None)
    if backend_user is not None and backend_user != username:
        raise RuntimeError(
            f"backend was built for {backend_user!r} but the check is for "
            f"{username!r}; these must agree"
        )

    own = [r for r in active_reservations(backend) if r.user == username]
    held = {r.resource for r in own}
    needed = {o.resource for o in origins}
    reference = now if now is not None else datetime.now(tz=timezone.utc)
    expiring = sorted(
        (
            r
            for r in own
            if r.end is not None
            and r.resource in needed
            and r.expires_within(EXPIRY_WARNING_WINDOW, now=reference)
        ),
        key=lambda r: (r.end, r.resource),
    )
    # Absence of the capability is checked BEFORE the call, never inferred from
    # an empty result: empty means "nobody holds it", and printing that for a
    # backend that cannot tell would mislead a user who is locked out.
    namer = backend if isinstance(backend, SupportsResourceHolders) else None
    missing = [
        MissingResource(resource, namer.holders(resource) if namer is not None else None)
        for resource in sorted(needed - held)
    ]
    return ReservationReport(
        lab=lab.name,
        username=username,
        in_play=sorted(set(ids)),
        rows=[ReservationRow(o.resource, o.level, o.owner, o.resource in held) for o in origins],
        null_backend=False,
        expiring=expiring,
        missing=missing,
    )
