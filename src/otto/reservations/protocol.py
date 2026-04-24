"""Protocol contract for pluggable lab-reservation backends.

A reservation backend answers two questions for otto:

- "What resources does user X currently have reserved?"
- "Who, if anyone, currently holds resource Y?"

Otto is strictly a consumer — it never creates, modifies, or releases
reservations.  The scheduler (Jira, a booking tool, a shared JSON file,
anything) remains authoritative.

Implementers
------------
Third-party backends implement the :class:`ReservationBackend` protocol and
are selected in the repo's ``.otto/settings.toml``:

.. code-block:: toml

    [reservations]
    backend = "mypkg.mybackend:MyBackend"
    url = "https://scheduler.example.com"

    [reservations.mypkg-mybackend]
    api_key_env = "SCHEDULER_API_KEY"

The ``url`` key and any ``[reservations.<backend>]`` sub-table are passed to
the backend's ``__init__`` as keyword arguments.  ``url`` is optional on both
sides: implementers may accept and use it, or hardcode their own endpoint —
whichever fits the deployment.

All failure modes that prevent answering a query (network down, database
unreachable, credentials rejected, file corrupt) **must** be raised as
:class:`otto.reservations.check.ReservationBackendError` so the CLI can
translate them into a fail-closed startup error with a clear hint about the
``--skip-reservation-check`` escape hatch.
"""

from typing import (
    Protocol,
    runtime_checkable,
)


@runtime_checkable
class ReservationBackend(Protocol):
    """Read-only view over a reservation scheduler."""

    def get_reserved_resources(self,
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
            byte-for-byte the values in ``RemoteHost.resources`` and
            ``Lab.resources`` — any necessary normalization is the backend's
            job.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer (network error,
            file I/O error, DB error, credential rejection, malformed data).
        """
        ...

    def who_reserved(self,
        resource: str,
    ) -> str | None:
        """Return the username currently holding ``resource``.

        Used for error messages when a reservation check fails
        (e.g. ``"rack3-psu is held by alice"``) so the caller knows who to
        talk to.

        Parameters
        ----------
        resource : str
            Resource identifier to look up.

        Returns
        -------
        str | None
            The holder's username, or ``None`` if no one currently holds the
            resource.

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
