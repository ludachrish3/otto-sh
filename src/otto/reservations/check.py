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
from typing import TYPE_CHECKING, Literal, get_args

from ..errors import OttoError
from ..models.lab import ElementKey

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ..config.lab import Lab
    from .identity import ResolvedIdentity
    from .protocol import ReservationBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReservationGateResult:
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

    def evaluate(self) -> ReservationGateResult:
        """Run the reservation check (or the skip path) and report the outcome.

        When ``skip_check`` (``-R``) is set, a loud warning is always
        produced — regardless of whether a backend was configured — and no
        check runs. Otherwise, a ``backend`` of ``None`` (no ``[reservations]``
        section resolved, or nothing to check) is a silent no-op. The active
        lab is fetched lazily so the no-op paths never require an
        :class:`~otto.context.OttoContext`.

        The requirement is computed over the fleet of interest —
        :meth:`~otto.context.OttoContext.admissible_ids` (spec 2026-08-28
        three-level-reservations §5), reached through
        :func:`~otto.config.fleet.get_hosts_in_play` because ``tach.toml``
        does not allow this package the context module. An empty declared
        fleet is ZERO hosts in play, so the requirement narrows to the
        lab-level set and the gate reaches a verdict rather than aborting.
        That refusal belongs to the walk: a run that then walks its fleet
        still refuses it with the same fleet-shaped message, which is where it
        was before this gate existed.

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
        from ..config.fleet import get_hosts_in_play

        if self.skip_check:
            lab = get_lab()
            username = self.identity.username if self.identity is not None else "<unknown>"
            needed = required_resources(lab, host_ids=get_hosts_in_play())
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
            return ReservationGateResult(checked=False, skipped=True, warning=warning)

        if self.backend is None:
            return ReservationGateResult(checked=False, skipped=False, warning=None)

        lab = get_lab()
        if self.identity is None:
            raise RuntimeError("identity must be resolved before evaluate() runs")
        check_reservations(lab, self.identity.username, self.backend, host_ids=get_hosts_in_play())
        return ReservationGateResult(checked=True, skipped=False, warning=None)


class ReservationBackendError(OttoError):
    """Raised by backends when a query cannot be answered.

    Network outages, DB errors, malformed data files, and authentication
    failures all surface as this exception so the CLI can translate them
    into a single fail-closed startup error.
    """


class MissingReservationError(OttoError):
    """Raised when the effective user does not hold every required resource.

    The message names each missing resource's origin — the level
    (``lab``/``element``/``host``) and owner that declared it, via
    :func:`required_resource_origins` — alongside its current holders. It
    does not mention ``--skip-reservation-check`` — that suggestion belongs
    only in the backend-failure path, never on a legitimate contention
    failure (or the option gets abused).
    """


ResourceLevel = Literal["lab", "element", "host"]
"""The reservation level a resource identifier can be declared at."""

_LEVEL_ORDER = {level: i for i, level in enumerate(get_args(ResourceLevel))}
"""Sort priority for :func:`required_resource_origins`, derived from
:data:`ResourceLevel` itself so the set of levels has exactly one source of
truth rather than two lists that could drift apart."""


@dataclass(frozen=True)
class ResourceOrigin:
    """One reason a resource is required: the level that declared it and who at that level."""

    resource: str
    """The resource identifier, exactly as declared — opaque to otto, matched byte-for-byte."""

    level: ResourceLevel
    """The declaring point — the lab, an element, or a host — that named this resource."""

    owner: str
    """The lab name, the element rendered as ``('chassis', 1)``, or the host id."""


def required_resource_origins(
    lab: "Lab", *, host_ids: "Iterable[str] | None" = None
) -> list[ResourceOrigin]:
    """Every (resource, level, owner) the run needs, sorted by resource → level → owner.

    ``host_ids`` selects the hosts IN PLAY (spec 2026-08-28 three-level-
    reservations §4): the lab's own set always counts; each selected host
    contributes its element's set and its own. ``None`` means every host in
    the lab — the conservative reading for a caller with no fleet in hand.
    An id the lab does not contain is a ``ValueError``: the caller passed a
    fleet from a different lab, which is a bug, not a condition to skip past.
    """
    if host_ids is None:
        selected = list(lab.hosts.values())
    else:
        wanted = list(host_ids)
        unknown = sorted(set(wanted) - set(lab.hosts))
        if unknown:
            raise ValueError(f"host_ids names host(s) not in lab {lab.name!r}: {unknown}")
        selected = [lab.hosts[host_id] for host_id in wanted]
    origins = {ResourceOrigin(r, "lab", lab.name) for r in lab.resources}
    for host in selected:
        # ``element``/``element_id`` live on RemoteHost, not the base Host
        # protocol (otto.host.host.Host) — and otto.reservations may not
        # import otto.host (tach.toml). element_resources is only ever
        # non-empty on a RemoteHost (the loader stamps it from the host's
        # element), so this duck-types rather than importing the class just
        # to narrow the type.
        if host.element_resources:
            element = getattr(host, "element", None)
            # ``not element``, not ``is None``: an empty name is no more of an
            # identity than a missing one, and it would render the owner as
            # ``('', None)`` — the plausible-looking output this raise exists
            # to prevent. A factory-built host cannot reach either (the spec
            # validator refuses a name that slugs to nothing).
            if not element:
                raise RuntimeError(
                    f"host {host.id!r} carries element resources but no element identity"
                )
            owner = str(ElementKey(element, getattr(host, "element_id", None)))
            origins.update(ResourceOrigin(r, "element", owner) for r in host.element_resources)
        origins.update(ResourceOrigin(r, "host", host.id) for r in host.resources)
    return sorted(origins, key=lambda o: (o.resource, _LEVEL_ORDER[o.level], o.owner))


def required_resources(lab: "Lab", *, host_ids: "Iterable[str] | None" = None) -> set[str]:
    """Every resource identifier the run needs — derived from :func:`required_resource_origins`.

    ``host_ids`` selects the hosts in play (``None`` means every host in the
    lab); see :func:`required_resource_origins` for the exact rule.
    """
    return {o.resource for o in required_resource_origins(lab, host_ids=host_ids)}


def check_reservations(
    lab: "Lab",
    username: str,
    backend: "ReservationBackend",
    *,
    host_ids: "Iterable[str] | None" = None,
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
    host_ids : Iterable[str] | None
        The hosts actually in play; forwarded to
        :func:`required_resource_origins`. ``None`` (the default) means every
        host in the lab.

    Raises
    ------
    MissingReservationError
        If any required resource is not held by ``username``.
    ReservationBackendError
        If the backend cannot answer the query (network, file, DB failure).
    """
    # The null backend short-circuits to a no-op so teams without a scheduler
    # configured aren't blocked.  Importing here avoids a circular import
    # between this module and the null backend's factory path. The PREDICATE,
    # not the class: ``otto reservation check`` has to reach the same verdict
    # to know the held column is unanswerable, and two isinstance checks are
    # how those two answers drift apart.
    from .null_backend import is_null_backend

    # AFTER the walk, not before it. The walk raises on two BUGS — an unknown
    # id in ``host_ids`` (spec §4) and a host carrying element resources with
    # no element identity (R17) — and neither is a user condition the backend
    # has any say in. Under ``backend = "none"`` there is no scheduler to
    # notice the lab file is wrong, which is exactly where a short-circuit
    # above this line would let it sit longest. The backend is still never
    # queried: every return below this point precedes the first call on it,
    # and ``otto reservation check`` computes its origins first for the same
    # reason.
    needed_origins = required_resource_origins(lab, host_ids=host_ids)
    if is_null_backend(backend):
        return

    needed = {o.resource for o in needed_origins}
    if not needed:
        return

    reserved = backend.get_reserved_resources(username)
    missing = needed - reserved
    if not missing:
        return

    width = max(len(r) for r in missing)
    lines = [
        f"User {username!r} does not hold all resources required by lab {lab.name!r}. Missing:"
    ]
    for resource in sorted(missing):
        who = backend.who_reserved(resource)
        held = ", ".join(who) if who else "nobody"
        lines.extend(
            f"  {resource:<{width}}  {origin.level} {origin.owner}  (held by: {held})"
            for origin in needed_origins
            if origin.resource == resource
        )
    raise MissingReservationError("\n".join(lines))
