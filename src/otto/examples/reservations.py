"""In-memory reference reservation backend (sample).

A teaching/reference backend backed by a plain ``user -> resources`` mapping.
It inherits :class:`~otto.reservations.ReservationBackendBase` — the
recommended starting point for a backend of your own — needs no files or
network, demonstrates the optional
:class:`~otto.reservations.SupportsResourceHolders` capability with a
multi-holder ``holders`` and the optional
:class:`~otto.reservations.SupportsUsernameCompletion` capability, and is
conformance-verified in otto's own suite.

Register it from an ``init`` module and select it by name::

    from otto.reservations import register_reservation_backend
    from otto.examples.reservations import ExampleReservationBackend

    register_reservation_backend("example", ExampleReservationBackend)

then in ``.otto/settings.toml``::

    [reservations]
    backend = "example"

Direct usage:

>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend(username="alice")
>>> backend.backend_name()
'example'
>>> [r.resource for r in backend.reservations]
['lab-a', 'shared']
>>> sorted(h.user for h in backend.holders("shared"))
['alice', 'bob']
>>> backend.list_usernames()
['alice', 'bob']
"""

from datetime import datetime
from pathlib import Path

from typing_extensions import override

from otto.reservations import Reservation, ReservationBackendBase

# A tiny built-in dataset: "shared" is held by two users to demonstrate the
# multi-holder holders() contract.
_DEMO_RESERVATIONS: dict[str, list[str]] = {
    "alice": ["lab-a", "shared"],
    "bob": ["lab-b", "shared"],
}


class ExampleReservationBackend(ReservationBackendBase):
    """In-memory reference backend, built on :class:`~otto.reservations.ReservationBackendBase`.

    Also implements both optional capabilities —
    :class:`~otto.reservations.SupportsResourceHolders` and
    :class:`~otto.reservations.SupportsUsernameCompletion` — by having a
    ``holders`` and a ``list_usernames`` method, which is all signalling one
    takes.

    Every booking here is open-ended: this dataset records no times, so each
    :class:`~otto.reservations.protocol.Reservation` carries ``start=None`` and
    ``end=None`` — never a sentinel date.

    Parameters
    ----------
    url : str | None
        Forwarded to the base, which keeps it as ``self.url``. This in-memory
        sample never reads it.
    repo_dir : Path | None
        The SUT repo root, forwarded to the base as ``self.repo_dir``. This
        in-memory sample never reads it; a real backend would use it to anchor
        relative path-like settings of its own.
    username : str | None
        The identity otto resolved, forwarded to the base. It is whose rows
        ``self.reservations`` returns.
    reservations : dict[str, list[str]] | None
        Optional mapping of username to the resources they hold. Defaults to a
        small built-in demo dataset.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        repo_dir: Path | None = None,
        username: str | None = None,
        reservations: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(url=url, repo_dir=repo_dir, username=username)
        source = _DEMO_RESERVATIONS if reservations is None else reservations
        self._by_user: dict[str, set[str]] = {
            user: set(resources) for user, resources in source.items()
        }

    @override
    def fetch_reservations(
        self,
        username: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Reservation]:
        """Return ``username``'s reservations, sorted by resource.

        The window is ignored: every booking in this dataset is open-ended, so
        it overlaps any window a caller can ask for.
        """
        return [
            Reservation(user=username, resource=resource)
            for resource in sorted(self._by_user.get(username, ()))
        ]

    def holders(self, resource: str) -> list[Reservation]:
        """Return every reservation covering ``resource``, sorted by user."""
        # Deterministic order, duplicates removed (a user holds a resource once).
        return [
            Reservation(user=user, resource=resource)
            for user in sorted(self._by_user)
            if resource in self._by_user[user]
        ]

    @override
    def backend_name(self) -> str:
        """Return the registry key for this backend (``"example"``)."""
        return "example"

    def list_usernames(self) -> list[str]:
        """Return a sorted list of all known usernames in this backend."""
        return sorted(self._by_user)
