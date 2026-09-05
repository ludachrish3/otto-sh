"""Null reservation backend used when no scheduler is configured.

Selected by setting ``backend = "none"`` in the repo's ``[reservations]``
TOML section.  :func:`otto.reservations.check.check_reservations` recognizes
it through :func:`is_null_backend` and becomes a no-op, so teams that haven't
set up a scheduler yet aren't blocked.
"""

from typing import TYPE_CHECKING

from typing_extensions import override

from .base import ReservationBackendBase

if TYPE_CHECKING:
    from .protocol import ReservationBackend


class NullReservationBackend(ReservationBackendBase):
    """Always returns "no reservations known" — the check is a no-op."""

    @override
    def get_reserved_resources(
        self,
        username: str,
    ) -> set[str]:
        """Return an empty set — this backend tracks no reservations."""
        return set()

    @override
    def who_reserved(
        self,
        resource: str,
    ) -> list[str]:
        """Return an empty list — this backend tracks no reservations."""
        return []

    @override
    def backend_name(self) -> str:
        """Return the registry key for this backend (``"none"``)."""
        return "none"


def is_null_backend(backend: "ReservationBackend") -> bool:
    """Whether *backend* is the no-op ``"none"`` backend, which is never queried.

    It reserves nothing, so asking it what a user holds is not a question with
    a meaningful answer.

    ONE predicate, because more than one place has to make this decision and
    they must agree. :func:`~otto.reservations.check.check_reservations`
    short-circuits on it, and ``otto reservation check`` reads it to decide
    whether querying the backend for the held set is worth doing at all —
    :meth:`NullReservationBackend.get_reserved_resources` answers ``set()``, so
    a caller that queried it anyway would render every requirement as unheld
    directly above an OK verdict. A second ``isinstance`` at the second site is
    exactly how those two answers drift apart.
    """
    return isinstance(backend, NullReservationBackend)
