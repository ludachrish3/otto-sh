"""Name → class registry for reservation backends.

Mirrors otto's other extension registries (``register_term_backend`` /
``register_transfer_backend`` / ``register_host_class``): a custom backend
registers a bare name from an ``init`` module, and ``[reservations] backend =
"<name>"`` selects it. Built-ins ``none`` and ``json`` are pre-registered at
import so they resolve through the same path.
"""

from ..registry import Registry, caller_module

# Name -> ReservationBackend-compatible class. ``build_backend`` constructs the
# resolved class (built-ins keep their bespoke construction; custom backends get
# url= + their ``[reservations.<name>]`` kwargs).
RESERVATION_BACKENDS: Registry[type] = Registry(
    "reservation backend", register_hint="otto.reservations.registry.register_reservation_backend()"
)


def register_reservation_backend(name: str, cls: type, *, overwrite: bool = False) -> None:
    """Make a custom reservation backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
    protocol.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    RESERVATION_BACKENDS.register(name, cls, overwrite=overwrite, origin=caller_module())


def get_reservation_backend_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    return RESERVATION_BACKENDS.get(name)


def _register_builtins() -> None:
    """Register the built-in reservation backends through the public path."""
    from .json_backend import JsonReservationBackend
    from .null_backend import NullReservationBackend

    register_reservation_backend("none", NullReservationBackend)
    register_reservation_backend("json", JsonReservationBackend)


_register_builtins()
