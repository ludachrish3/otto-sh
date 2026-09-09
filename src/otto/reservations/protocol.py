"""Protocol contract for pluggable lab-reservation backends.

A reservation backend answers two questions for otto:

- "What resources does user X currently have reserved?"
- "Who, if anyone, currently holds resource Y?"

Otto is strictly a consumer — it never creates, modifies, or releases
reservations.  The scheduler (Jira, a booking tool, a shared JSON file,
anything) remains authoritative.

Implementers
------------
Third-party backends implement the :class:`ReservationBackend` protocol, register under
a bare name via ``register_reservation_backend("my-team-jira", MyBackend)`` from an
``init`` module, and are selected in the repo's ``.otto/settings.toml``:

.. code-block:: toml

    [reservations]
    backend = "my-team-jira"
    url = "https://scheduler.example.com"

    [reservations.my-team-jira]
    api_key_env = "SCHEDULER_API_KEY"

The ``url`` key and any ``[reservations.<name>]`` sub-table are passed to
the backend's ``__init__`` as keyword arguments.  ``url`` is optional on both
sides: implementers may accept and use it, or hardcode their own endpoint —
whichever fits the deployment.

All failure modes that prevent answering a query (network down, database
unreachable, credentials rejected, file corrupt) **must** be raised as
:class:`otto.reservations.check.ReservationBackendError` so the CLI can
translate them into a fail-closed startup error with a clear hint about the
``--skip-reservation-check`` escape hatch.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Protocol,
    runtime_checkable,
)


@runtime_checkable
class ReservationBackend(Protocol):
    """Read-only view over a reservation scheduler."""

    # `reservations` is deliberately absent: isinstance() against a
    # runtime_checkable Protocol EVALUATES non-method members, so declaring
    # the cached_property here would make the factory's and is_null_backend's
    # type checks silently query the scheduler. It is guaranteed by
    # ReservationBackendBase and enforced by the conformance helper instead.

    def fetch_reservations(
        self,
        username: str,
        start: "datetime | None" = None,
        end: "datetime | None" = None,
    ) -> "list[Reservation]":
        """Return *username*'s reservations overlapping ``[start, end]``.

        Both bounds default to **this instant**, so the unbounded call returns
        what the user holds right now — the tightest query, not the widest.

        The window predicate is **overlap**, not containment: a booking that
        began before *start* and ends after *end* is active during the window
        and MUST be returned.  Reading it as "contained in" or "beginning
        within" makes otto's gate fail open, admitting a second user onto held
        hardware.

        Parameters
        ----------
        username : str
            The reservation-system identity to query.  Case sensitivity and
            any other normalization rules are the backend's responsibility;
            otto passes the username through unchanged.
        start : datetime | None
            Start of the window of interest; ``None`` means this instant.
        end : datetime | None
            End of the window of interest; ``None`` means this instant.

        Returns
        -------
        list[Reservation]
            One row per ``(user, resource)`` window — a booking covering three
            racks yields three :class:`Reservation` objects.  Windows that
            ended at or before now are omitted.  Order is not significant;
            otto sorts where it displays.  Resource strings must match
            byte-for-byte the identifiers ``required_resources`` computes —
            lab, element and host levels alike (spec 2026-08-28
            three-level-reservations §9) — any necessary normalization is the
            backend's job.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer (network error,
            file I/O error, DB error, credential rejection, malformed data).
            Never swallow and return an empty list: the CLI turns this
            exception into a fail-closed startup error, while an empty list is
            a refusal that blames the user.
        """
        ...

    def backend_name(self) -> str:
        """Return a short human-readable identifier for this backend.

        Used in diagnostic output and error messages (e.g. ``"json"``,
        ``"my-team-jira"``).  Should be stable across runs.
        """
        ...


@runtime_checkable
class SupportsUsernameCompletion(Protocol):
    """Optional capability: enumerate usernames for ``--holder`` completion.

    A backend that can list its users implements ``list_usernames``; otto
    detects it structurally (``isinstance(backend, SupportsUsernameCompletion)``)
    and feeds the values into ``--holder`` tab-completion (cached, see
    ``otto.config.completion_cache.collect_reservation_usernames``).
    Backends that cannot enumerate users simply omit it.
    """

    def list_usernames(self) -> list[str]:
        """Return all usernames the backend knows about, for completion."""
        ...


@dataclass(frozen=True)
class Reservation:
    """One booking: *user* holds *resource* over ``[start, end]``.

    ``start`` and ``end`` are timezone-aware when present. ``start is None``
    means the backend does not know when the booking began; ``end is None``
    means it is open-ended and never expires. There are no sentinel dates —
    a backend that does not know a bound reports ``None`` rather than the
    epoch or a far-future year.

    Parameters
    ----------
    user : str
        The holder's reservation-system identity.
    resource : str
        The resource identifier, byte-for-byte as the lab file declares it.
    start : datetime | None
        When the booking began, or ``None`` if unknown.
    end : datetime | None
        When the booking ends, or ``None`` if open-ended.
    """

    user: str
    resource: str
    start: "datetime | None" = None
    end: "datetime | None" = None

    def expires_within(self, delta: "timedelta", *, now: "datetime | None" = None) -> bool:
        """Whether this booking ends within *delta* from *now*.

        Always ``False`` for an open-ended booking (``end is None``). A booking
        whose ``end`` has already passed reports ``True`` — it is the most
        urgent case there is, not an expired one to ignore.

        Parameters
        ----------
        delta : timedelta
            The look-ahead window.
        now : datetime | None
            The instant to measure from; defaults to ``datetime.now(timezone.utc)``.
            Present so callers' tests can freeze the clock without patching
            module globals.
        """
        if self.end is None:
            return False
        reference = now if now is not None else datetime.now(timezone.utc)
        return self.end - reference <= delta

    def is_active(self, *, now: "datetime | None" = None) -> bool:
        """Whether this booking covers *now* — the "is it held right now?" rule.

        Both bounds are **inclusive**: a booking is active at the instant it
        starts and at the instant it ends. Absent bounds are infinite —
        ``start is None`` reads as "since before this backend knows",
        ``end is None`` as "never expires" — so a row with neither bound is
        always active rather than an unusable one.

        This is a question about a single *instant*, and it is deliberately
        not the window predicate
        :meth:`~otto.reservations.protocol.ReservationBackend.fetch_reservations`
        applies: that one asks whether a booking overlaps a *range*, and its
        end clause is ``row.end is None or row.end > start`` — strictly
        greater, because a row ending at the window's start contributes zero
        overlap. The exact and only divergence is a row whose ``end`` is the
        very instant being asked about: the query drops it, ``is_active`` still
        holds it. That is deliberate, and it fails closed. The two rules must
        not be collapsed into one.

        Parameters
        ----------
        now : datetime | None
            The instant to test; defaults to ``datetime.now(timezone.utc)``.
            Present so callers' tests can freeze the clock without patching
            module globals.
        """
        reference = now if now is not None else datetime.now(timezone.utc)
        return (self.start is None or self.start <= reference) and (
            self.end is None or reference <= self.end
        )


@runtime_checkable
class SupportsResourceHolders(Protocol):
    """Optional capability: answer the inverted query about a resource.

    A backend that can report who holds a given resource — by enumerating its
    schedule or by a targeted lookup, whichever it supports — implements
    ``holders``. Otto detects it structurally
    (``isinstance(backend, SupportsResourceHolders)``) and uses it to name the
    current holders, and when each booking frees up, in the refusal message
    raised by :func:`~otto.reservations.check.check_reservations`.

    A backend whose scheduler answers only per-user queries simply omits the
    method; the refusal then says the holders are unknown rather than naming
    them. Nothing else degrades.
    """

    def holders(self, resource: str) -> "list[Reservation]":
        """Return every reservation currently covering *resource*, any user.

        An **empty list** means nobody holds it. Rows obey the same rules as
        :meth:`~otto.reservations.protocol.ReservationBackend.fetch_reservations`
        results.
        """
        ...
