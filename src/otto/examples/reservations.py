"""In-memory reference :class:`~otto.reservations.protocol.ReservationBackend` (sample).

A teaching/reference reservation backend backed by a plain ``user -> resources``
mapping. It needs no files or network, demonstrates multi-holder
``who_reserved`` and the optional
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

# A tiny built-in dataset: "shared" is held by two users to demonstrate the
# multi-holder who_reserved contract.
_DEMO_RESERVATIONS: dict[str, list[str]] = {
    "alice": ["lab-a", "shared"],
    "bob": ["lab-b", "shared"],
}


class ExampleReservationBackend:
    """In-memory :class:`~otto.reservations.protocol.ReservationBackend` reference backend.

    Also implements the optional
    :class:`~otto.reservations.SupportsUsernameCompletion` capability.

    Parameters
    ----------
    url : str | None
        Accepted for factory uniformity — :func:`otto.reservations.build_backend`
        may call ``cls(url=url, ...)``. This in-memory sample ignores it.
    reservations : dict[str, list[str]] | None
        Optional mapping of username to the resources they hold. Defaults to a
        small built-in demo dataset.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        reservations: dict[str, list[str]] | None = None,
    ) -> None:
        source = _DEMO_RESERVATIONS if reservations is None else reservations
        self._by_user: dict[str, set[str]] = {
            user: set(resources) for user, resources in source.items()
        }

    def get_reserved_resources(self, username: str) -> set[str]:
        return set(self._by_user.get(username, set()))

    def who_reserved(self, resource: str) -> list[str]:
        # Deterministic order, duplicates removed (a user holds a resource once).
        return sorted(user for user, resources in self._by_user.items() if resource in resources)

    def backend_name(self) -> str:
        return "example"

    def list_usernames(self) -> list[str]:
        return sorted(self._by_user)
