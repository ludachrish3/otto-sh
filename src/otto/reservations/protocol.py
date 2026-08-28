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
from datetime import datetime
from typing import (
    Protocol,
    runtime_checkable,
)


@runtime_checkable
class ReservationBackend(Protocol):
    """Read-only view over a reservation scheduler."""

    def get_reserved_resources(
        self,
        username: str,
    ) -> set[str]:
        """Return the set of resource identifiers currently reserved by ``username``.

        Parameters
        ----------
        username : str
            The reservation-system identity to query.  Case sensitivity and
            any other normalization rules are the backend's responsibility;
            otto passes the username through unchanged.

        Returns
        -------
        set[str]
            Resource identifiers the user currently holds.  Empty set if the
            user has no active reservations.  Resource strings must match
            byte-for-byte the values in ``Lab.resources`` (the lab's declared
            reservation identifiers) — any necessary normalization is the
            backend's job.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer (network error,
            file I/O error, DB error, credential rejection, malformed data).
        """
        ...

    def who_reserved(
        self,
        resource: str,
    ) -> list[str]:
        """Return the usernames currently holding ``resource``.

        Used for error messages when a reservation check fails
        (e.g. ``"shared-lab is held by alice, bob"``) so the caller knows who
        to talk to.

        Parameters
        ----------
        resource : str
            Resource identifier to look up.

        Returns
        -------
        list[str]
            The usernames holding the resource, in a deterministic order with
            duplicates removed.  An **empty list** means no one currently holds
            it (there is no ``None`` sentinel — a resource can have any number
            of concurrent holders).

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer.
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
    """Optional capability: enumerate usernames for ``--as-user`` completion.

    A backend that can list its users implements ``list_usernames``; otto
    detects it structurally (``isinstance(backend, SupportsUsernameCompletion)``)
    and feeds the values into ``--as-user`` tab-completion (cached, see
    ``otto.config.completion_cache.collect_reservation_usernames``).
    Backends that cannot enumerate users simply omit it.
    """

    def list_usernames(self) -> list[str]:
        """Return all usernames the backend knows about, for completion."""
        ...


@dataclass(frozen=True)
class ReservationWindow:
    """One reservation a backend knows about: *resource* held over ``[start, end]``.

    ``start`` and ``end`` must be timezone-aware.  A backend that does not
    know when a booking began uses the epoch for ``start``; an open-ended
    booking uses a far-future ``end``.
    """

    resource: str
    start: datetime
    end: datetime


@runtime_checkable
class SupportsReservationWindows(Protocol):
    """Optional capability: report reservation windows (start/end per resource).

    Implemented by backends that can say *when* a booking starts and ends,
    not just whether it is currently held.  Otto detects it structurally
    (``isinstance(backend, SupportsReservationWindows)``) and uses the window
    edges to invalidate the remote-path completion cache the moment a booking
    boundary passes (see ``otto.config.remote_completion_cache``).  Backends
    that cannot report windows simply omit it and fall back to a flat-TTL
    cache of
    :meth:`ReservationBackend.get_reserved_resources
    <otto.reservations.protocol.ReservationBackend.get_reserved_resources>`.
    """

    def get_reservation_windows(self, username: str) -> "list[ReservationWindow]":
        """Return every active window ``username`` holds (expired entries omitted)."""
        ...
