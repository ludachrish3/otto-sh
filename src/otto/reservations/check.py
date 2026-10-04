"""Lab reservation check logic and exceptions.

The decision lives in :func:`~otto.reservations.report.build_report`;
:func:`check_reservations` is its raising wrapper:
given a lab, a username, and a backend, it raises
:class:`MissingReservationError` if the user does not hold every resource
the lab needs.  The error message lists missing resources and their current
holders (via :meth:`~otto.reservations.protocol.SupportsResourceHolders.holders`) but
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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, get_args

from ..errors import OttoError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ..config.lab import Lab
    from .identity import ReservationIdentity, ResolvedIdentity
    from .protocol import Reservation, ReservationBackend
    from .report import ReservationReport

logger = logging.getLogger(__name__)

EXPIRY_WARNING_WINDOW = timedelta(minutes=5)
"""How far ahead to warn about a reservation that is about to end.

A constant, not a setting: it is a nudge, and a configurable nudge is a
support question with no right answer.
"""

_warned_expiring: "set[tuple[str, datetime]]" = set()
"""``(resource, end)`` pairs already announced by :func:`announce_expiring`.

Process-wide, because the same lab-level booking is visible to more than one
call site in a single run — the preamble gate and the out-of-fleet named-host
check both hold the lab's own requirement — and a user who is told twice that
one rack is lapsing learns nothing the second time.
"""


def reset_expiry_warnings() -> None:
    """Forget every expiry warning already announced this process.

    Exists for tests: the suppression set is module state, so a test that
    warns leaks into the next one unless it is cleared between them.
    """
    _warned_expiring.clear()


def _announce_expiry(resource: str, end: datetime, reference: datetime) -> None:
    """Log one expiry nudge, unless this ``(resource, end)`` was already announced."""
    if (resource, end) in _warned_expiring:
        return
    _warned_expiring.add((resource, end))
    minutes = max(0, int((end - reference).total_seconds() // 60))
    logger.warning(
        "\N{WARNING SIGN}  Reservation for %r expires in %d minute(s) (at %s).",
        resource,
        minutes,
        # In the READER's zone, not the backend's. The JSON backend
        # normalises every `expires` to UTC, and other schedulers answer in
        # whatever zone they were configured with, so an unconverted clock
        # time tells a user in CET their booking lapses an hour before it
        # does — and they hand back hardware they still hold. `%H:%M` alone
        # carries no offset to disambiguate it, so the conversion is the
        # only thing that makes the string true.
        end.astimezone().strftime("%H:%M"),
    )


def announce_expiring(report: "ReservationReport", *, now: "datetime | None" = None) -> None:
    """Log the expiry nudge for each of *report*'s expiring bookings, once per process.

    Never queries a backend: *report* already holds the rows. Callers choose
    the moment. The gate announces after a pass, so a refusal is never trailed
    by a nudge. ``otto reservation check`` announces after its table and before
    its verdict, because a status report says everything it knows.
    """
    reference = now if now is not None else datetime.now(tz=timezone.utc)
    for booking in report.expiring:
        if booking.end is not None:
            _announce_expiry(booking.resource, booking.end, reference)


@dataclass(frozen=True)
class ReservationGateResult:
    """Result of :meth:`ReservationGate.evaluate`.

    ``warning`` is plain text (no rich markup) — CLI callers decide how to
    present it (e.g. wrapping it in ``[bold red]...[/bold red]``).
    """

    checked: bool
    skipped: bool
    warning: "str | None"
    report: "ReservationReport | None" = None
    """The report the check read; ``None`` when no check ran."""


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

    def _backend_for_report(self) -> "ReservationBackend | None":
        """Return the gate's backend, or build one now (under ``-R`` there is none yet)."""
        if self.backend is not None:
            return self.backend
        return self.backend_factory() if self.backend_factory is not None else None

    @staticmethod
    def _lab_and_hosts(
        lab: "Lab | None", host_ids: "Iterable[str] | None"
    ) -> "tuple[Lab, Iterable[str] | None]":
        """Use an explicit lab as given; otherwise the active lab and its hosts in play."""
        if lab is not None:
            return lab, host_ids
        from ..config import get_lab
        from ..config.fleet import get_hosts_in_play

        return get_lab(), (get_hosts_in_play() if host_ids is None else host_ids)

    def report(self, lab: "Lab", host_ids: "Iterable[str] | None" = None) -> "ReservationReport":
        """Report what this gate's user needs from *lab* and holds.

        A status report ignores ``-R``: the backend is built now if the gate
        skipped building it.

        Raises
        ------
        RuntimeError
            No identity was resolved, or there is no backend and no factory.
        ReservationBackendError
            The backend cannot be built or cannot answer.
        """
        from .report import build_report

        if self.identity is None:
            raise RuntimeError("identity must be resolved before report() runs")
        backend = self._backend_for_report()
        if backend is None:
            raise RuntimeError("the gate has no backend and no backend_factory")
        return build_report(lab, self.identity.username, backend, host_ids=host_ids)

    def evaluate(
        self, lab: "Lab | None" = None, host_ids: "Iterable[str] | None" = None
    ) -> ReservationGateResult:
        """Run the reservation check (or the skip path) and report the outcome.

        When ``skip_check`` (``-R``) is set, a loud warning is always
        produced — regardless of whether a backend was configured — and no
        check runs. Otherwise, a ``backend`` of ``None`` (no ``[reservations]``
        section resolved, or nothing to check) is a silent no-op. An explicit
        *lab* needs no :class:`~otto.context.OttoContext`; *host_ids* then
        defaults to every host of that lab. Without one, the active lab is
        fetched lazily so the no-op paths never require a context.

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
        from .report import build_report

        if self.skip_check:
            lab, host_ids = self._lab_and_hosts(lab, host_ids)
            username = self.identity.username if self.identity is not None else "<unknown>"
            needed = required_resources(lab, host_ids=host_ids)
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

        lab, host_ids = self._lab_and_hosts(lab, host_ids)
        if self.identity is None:
            raise RuntimeError("identity must be resolved before evaluate() runs")
        report = build_report(lab, self.identity.username, self.backend, host_ids=host_ids)
        if not report.covered:
            raise MissingReservationError.from_report(report)
        # After the verdict, so a refusal is never trailed by a nudge.
        announce_expiring(report)
        return ReservationGateResult(checked=True, skipped=False, warning=None, report=report)

    def check_hosts(self, lab: "Lab", hosts: "Iterable[object]") -> "ReservationReport | None":
        """Require the OWN slots of the named hosts the fleet left out.

        Returns ``None`` when there is nothing to check. Needs an installed
        :class:`~otto.context.OttoContext`, because it reads the hosts in play
        through :func:`~otto.config.fleet.get_hosts_in_play`.

        ``otto host <id> --hop <id>`` is deliberately unscoped (explicit
        targeting beats scoping) while :meth:`evaluate` requires only the
        fleet of interest. Element- and host-level slots make that gap
        reachable: a project scoped to ``slot1`` would pass the gate holding
        ``slot-1`` and then touch ``slot2``. *hosts* is every host the caller
        names, a jump host included: reaching a fleet host through an
        unreserved jump box is still using the jump box.

        ``-R`` and a gate with no backend are no-ops, and the null backend
        answers inside the report. Short-circuits, in this order:

        * The built-in ``local`` host is skipped, as it is never a host in
          play: naming the machine otto is running on must never need a slot.
          A lab that declares its OWN ``local`` entry is not this host and is
          not skipped.
        * A host the fleet already covers is skipped; :meth:`evaluate` asked
          the backend for exactly that set.
        * A remaining host that declares neither ``resources`` nor element
          resources adds nothing beyond the lab-level set the gate already
          checked. With none that declare any, there is no query at all. The
          read is local (two frozensets on a built host), never the backend.

        One report covers every remaining host, so a run short of two slots is
        told about both at once.

        Raises
        ------
        MissingReservationError
            A remaining host's resources are not all held.
        """
        from ..config.fleet import get_hosts_in_play, is_builtin_host
        from .report import build_report

        # No identity is a quiet no-op here, unlike evaluate()/report(): a gate with
        # nothing to check as has nothing to refuse.
        if self.skip_check or self.backend is None or self.identity is None:
            return None
        fleet = get_hosts_in_play()
        outside = [
            host
            for host in hosts
            if getattr(host, "id", None) not in fleet and not is_builtin_host(host)
        ]

        def _declares(host: object) -> bool:
            element = getattr(host, "element", None)
            return bool(getattr(host, "resources", ())) or bool(
                element.resources if element is not None else ()
            )

        if not any(_declares(host) for host in outside):
            return None
        report = build_report(
            lab,
            self.identity.username,
            self.backend,
            host_ids={getattr(host, "id", "") for host in outside},
        )
        if not report.covered:
            raise MissingReservationError.from_report(report)
        announce_expiring(report)
        return report

    def identity_report(self) -> "ReservationIdentity":
        """Who this gate checks as, and its backend's name; builds the backend under ``-R``.

        Raises
        ------
        RuntimeError
            No identity was resolved.
        ReservationBackendError
            The backend cannot be built.
        """
        from .identity import ReservationIdentity

        if self.identity is None:
            raise RuntimeError("identity must be resolved before identity_report() runs")
        backend = self._backend_for_report()
        return ReservationIdentity(
            username=self.identity.username,
            source=self.identity.source,
            backend_name=backend.backend_name() if backend is not None else "<none>",
        )


class ReservationBackendError(OttoError):
    """Raised by backends when a query cannot be answered.

    Network outages, DB errors, malformed data files, and authentication
    failures all surface as this exception so the CLI can translate them
    into a single fail-closed startup error.
    """


def active_reservations(backend: "ReservationBackend") -> "list[Reservation]":
    """Return *backend*'s cached active rows.

    The ``ReservationBackend`` Protocol deliberately declares only methods:
    ``isinstance`` against a ``runtime_checkable`` Protocol evaluates
    non-method members, so declaring ``reservations`` there would turn every
    type check into a live scheduler query. This accessor is the one place
    that gap is bridged, and it converts a backend missing the member from a
    bare ``AttributeError`` at an arbitrary call site into a named error.
    """
    rows = getattr(backend, "reservations", None)
    if rows is None:
        raise ReservationBackendError(
            f"Reservation backend {type(backend).__name__!r} has no 'reservations' "
            "attribute; inherit ReservationBackendBase or provide it (see "
            "docs/cookbook/extending/reservation-backends.md)."
        )
    return rows


class MissingReservationError(OttoError):
    """Raised when the effective user does not hold every required resource.

    The message names each missing resource's origin — the level
    (``lab``/``element``/``host``) and owner that declared it, via
    :func:`required_resource_origins` — alongside its current holders. It
    does not mention ``--skip-reservation-check`` — that suggestion belongs
    only in the backend-failure path, never on a legitimate contention
    failure (or the option gets abused). ``report`` is the
    :class:`~otto.reservations.report.ReservationReport` it was raised from,
    when there is one.
    """

    def __init__(self, message: str, *, report: "ReservationReport | None" = None) -> None:
        super().__init__(message)
        self.report = report

    @classmethod
    def from_report(cls, report: "ReservationReport") -> "MissingReservationError":
        """Raise-ready refusal for *report*, worded by :func:`refusal_message`."""
        return cls(refusal_message(report), report=report)


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
    """The lab name, the element's slug, or the host id."""


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
        # The ``element`` object is ``None`` on a container and on ``local``,
        # and otto.reservations may not import otto.host (tach.toml) — so this
        # duck-types rather than importing the class just to narrow the type.
        # An ``Element`` always has a non-empty name (its own validator refuses
        # one that slugs to nothing), so the owner label is never blank.
        element = getattr(host, "element", None)
        if element is not None and element.resources:
            owner = element.slug
            origins.update(ResourceOrigin(r, "element", owner) for r in element.resources)
        origins.update(ResourceOrigin(r, "host", host.id) for r in host.resources)
    return sorted(origins, key=lambda o: (o.resource, _LEVEL_ORDER[o.level], o.owner))


def required_resources(lab: "Lab", *, host_ids: "Iterable[str] | None" = None) -> set[str]:
    """Every resource identifier the run needs — derived from :func:`required_resource_origins`.

    ``host_ids`` selects the hosts in play (``None`` means every host in the
    lab); see :func:`required_resource_origins` for the exact rule.
    """
    return {o.resource for o in required_resource_origins(lab, host_ids=host_ids)}


def _describe_holders(holders: "list[Reservation]") -> str:
    """Render holders for a refusal: who, and when each frees up.

    An empty list is ``"nobody"`` — a definite answer from a backend that
    looked. Rows without an ``end`` name only the holder, because the booking
    is open-ended and there is no release time to promise.
    """
    if not holders:
        return "nobody"
    parts = []
    for h in sorted(holders, key=lambda r: (r.user, r.resource)):
        if h.end is None:
            parts.append(h.user)
        else:
            # `.astimezone()` for the same reason the expiry warning converts:
            # the refusal tells a locked-out engineer when to come back, and a
            # time in the backend's zone sends them back at the wrong hour.
            parts.append(f"{h.user} until {h.end.astimezone():%H:%M}")
    return ", ".join(parts)


def refusal_message(report: "ReservationReport") -> str:
    """Word the refusal for *report*: each missing resource, where it is declared, who holds it.

    Meant for a report with missing resources; an empty one yields only the header.
    """
    width = max((len(m.resource) for m in report.missing), default=0)
    header = (
        f"User {report.username!r} does not hold all resources required by lab "
        f"{report.lab!r}. Missing:"
    )
    lines = [header]
    for missing in report.missing:
        held = (
            "unknown \N{EM DASH} this backend cannot report other users"
            if missing.holders is None
            else _describe_holders(missing.holders)
        )
        lines.extend(
            f"  {missing.resource:<{width}}  {row.level} {row.owner}  (held by: {held})"
            for row in report.rows
            if row.resource == missing.resource
        )
    return "\n".join(lines)


def check_reservations(
    lab: "Lab",
    username: str,
    backend: "ReservationBackend",
    *,
    host_ids: "Iterable[str] | None" = None,
) -> "ReservationReport":
    """Raise :class:`MissingReservationError` if ``username`` does not cover ``lab``.

    Returns the :class:`~otto.reservations.report.ReservationReport` when it does.

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

    Returns
    -------
    ReservationReport
        The report the verdict was read from.

    Raises
    ------
    MissingReservationError
        If any required resource is not held by ``username``.
    ReservationBackendError
        If the backend cannot answer the query (network, file, DB failure).
    """
    from .report import build_report

    report = build_report(lab, username, backend, host_ids=host_ids)
    if not report.covered:
        raise MissingReservationError.from_report(report)
    return report
