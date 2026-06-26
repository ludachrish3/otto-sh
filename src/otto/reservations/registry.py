"""Name → class registry for reservation backends.

Mirrors otto's other extension registries (``register_term_backend`` /
``register_transfer_backend`` / ``register_host_class``): a custom backend
registers a bare name from an ``init`` module, and ``[reservations] backend =
"<name>"`` selects it. Built-ins ``none`` and ``json`` are pre-registered at
import so they resolve through the same path.
"""

from __future__ import annotations

# Name -> ReservationBackend-compatible class. ``build_backend`` constructs the
# resolved class (built-ins keep their bespoke construction; custom backends get
# url= + their ``[reservations.<name>]`` kwargs).
_RESERVATION_BACKENDS: dict[str, type] = {}


def register_reservation_backend(name: str, cls: type) -> None:
    """Make a custom reservation backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
    protocol.
    """
    _RESERVATION_BACKENDS[name] = cls


def get_reservation_backend_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _RESERVATION_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_RESERVATION_BACKENDS))
        raise ValueError(
            f"Unknown reservation backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_reservation_backend()."
        ) from None


def _register_builtins() -> None:
    from .json_backend import JsonReservationBackend
    from .null_backend import NullReservationBackend

    _RESERVATION_BACKENDS.setdefault("none", NullReservationBackend)
    _RESERVATION_BACKENDS.setdefault("json", JsonReservationBackend)


_register_builtins()
