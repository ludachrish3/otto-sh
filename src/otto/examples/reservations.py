"""In-memory reference reservation backend (sample).

A teaching/reference backend backed by a plain ``user -> resources`` mapping.
It inherits :class:`~otto.reservations.ReservationBackendBase` — the
recommended starting point for a backend of your own — needs no files or
network, demonstrates multi-holder ``who_reserved`` and the optional
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
>>> backend = ExampleReservationBackend()
>>> backend.backend_name()
'example'
>>> backend.who_reserved("shared")
['alice', 'bob']
>>> sorted(backend.get_reserved_resources("alice"))
['lab-a', 'shared']
>>> backend.list_usernames()
['alice', 'bob']
"""

from pathlib import Path

from typing_extensions import override

from otto.reservations import ReservationBackendBase

# A tiny built-in dataset: "shared" is held by two users to demonstrate the
# multi-holder who_reserved contract.
_DEMO_RESERVATIONS: dict[str, list[str]] = {
    "alice": ["lab-a", "shared"],
    "bob": ["lab-b", "shared"],
}


class ExampleReservationBackend(ReservationBackendBase):
    """In-memory reference backend, built on :class:`~otto.reservations.ReservationBackendBase`.

    Also implements the optional
    :class:`~otto.reservations.SupportsUsernameCompletion` capability — by
    having a ``list_usernames`` method, which is all signalling one takes.

    Parameters
    ----------
    url : str | None
        Forwarded to the base, which keeps it as ``self.url``. This in-memory
        sample never reads it.
    repo_dir : Path | None
        The SUT repo root, forwarded to the base as ``self.repo_dir``. This
        in-memory sample never reads it; a real backend would use it to anchor
        relative path-like settings of its own.
    reservations : dict[str, list[str]] | None
        Optional mapping of username to the resources they hold. Defaults to a
        small built-in demo dataset.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        repo_dir: Path | None = None,
        reservations: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(url=url, repo_dir=repo_dir)
        source = _DEMO_RESERVATIONS if reservations is None else reservations
        self._by_user: dict[str, set[str]] = {
            user: set(resources) for user, resources in source.items()
        }

    @override
    def get_reserved_resources(self, username: str) -> set[str]:
        """Return the set of resources currently held by ``username``."""
        return set(self._by_user.get(username, set()))

    @override
    def who_reserved(self, resource: str) -> list[str]:
        """Return a sorted list of users who currently hold ``resource``."""
        # Deterministic order, duplicates removed (a user holds a resource once).
        return sorted(user for user, resources in self._by_user.items() if resource in resources)

    @override
    def backend_name(self) -> str:
        """Return the registry key for this backend (``"example"``)."""
        return "example"

    def list_usernames(self) -> list[str]:
        """Return a sorted list of all known usernames in this backend."""
        return sorted(self._by_user)
