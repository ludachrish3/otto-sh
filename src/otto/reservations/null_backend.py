"""Null reservation backend used when no scheduler is configured.

Selected by setting ``backend = "none"`` in the repo's ``[reservations]``
TOML section.  :func:`otto.reservations.check.check_reservations` recognizes
it through :func:`is_null_backend` and becomes a no-op, so teams that haven't
set up a scheduler yet aren't blocked.
"""

from typing import TYPE_CHECKING

from pydantic import ConfigDict
from typing_extensions import override

from ..models.base import OttoModel
from .base import ReservationBackendBase

if TYPE_CHECKING:
    from datetime import datetime

    from ..registry import Configured
    from .protocol import Reservation, ReservationBackend
    from .registry import ReservationEnv


class NoneReservationConfig(OttoModel):
    """The ``none`` backend's sub-table: it takes no keys."""

    model_config = ConfigDict(frozen=True)


class NullReservationBackend(ReservationBackendBase):
    """Always returns "no reservations known" — the check is a no-op."""

    @override
    def fetch_reservations(
        self,
        username: str,
        start: "datetime | None" = None,
        end: "datetime | None" = None,
    ) -> "list[Reservation]":
        """Return an empty list — this backend tracks no reservations."""
        return []

    def holders(self, resource: str) -> "list[Reservation]":  # noqa: ARG002 — capability signature: nothing is ever held, so the resource is not consulted
        """Return an empty list — this backend tracks no reservations."""
        return []

    @override
    def backend_name(self) -> str:
        """Return the registry key for this backend (``"none"``)."""
        return "none"


def _none_reservations(
    c: "Configured[NoneReservationConfig, ReservationEnv]",
) -> NullReservationBackend:
    """Build the ``none`` backend for the invoking user."""
    return NullReservationBackend(username=c.env.username)


def is_null_backend(backend: "ReservationBackend") -> bool:
    """Whether *backend* is the no-op ``"none"`` backend, which is never queried.

    It reserves nothing, so asking it what a user holds is not a question with
    a meaningful answer.

    ONE predicate, because more than one place has to make this decision and
    they must agree. :func:`~otto.reservations.check.check_reservations`
    short-circuits on it, and ``otto reservation check`` reads it to decide
    whether querying the backend for the held set is worth doing at all —
    :meth:`NullReservationBackend.fetch_reservations` answers ``[]``, so
    a caller that queried it anyway would render every requirement as unheld
    directly above an OK verdict. A second ``isinstance`` at the second site is
    exactly how those two answers drift apart.
    """
    return isinstance(backend, NullReservationBackend)
