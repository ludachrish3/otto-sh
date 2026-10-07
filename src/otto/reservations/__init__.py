"""Lab reservation / scheduler-check subsystem.

See :mod:`otto.reservations.protocol` for the backend contract and
``docs/cli/reservation/`` for the end-user docs and
``docs/cookbook/extending/reservation-backends.md`` for the implementer contract.

Every name is exported lazily (PEP 562): ``otto reservation --help`` and the
lab session import the names they use, and neither pays for a backend it
never builds.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ReservationBackendBase as ReservationBackendBase
    from .check import MissingReservationError as MissingReservationError
    from .check import ReservationBackendError as ReservationBackendError
    from .check import ReservationGate as ReservationGate
    from .check import ReservationGateResult as ReservationGateResult
    from .check import ResourceLevel as ResourceLevel
    from .check import ResourceOrigin as ResourceOrigin
    from .check import active_reservations as active_reservations
    from .check import announce_expiring as announce_expiring
    from .check import check_reservations as check_reservations
    from .check import required_resource_origins as required_resource_origins
    from .check import required_resources as required_resources
    from .check import reset_expiry_warnings as reset_expiry_warnings
    from .factory import build_backend as build_backend
    from .factory import build_reservation_gate as build_reservation_gate
    from .factory import gate_from_settings as gate_from_settings
    from .identity import ReservationIdentity as ReservationIdentity
    from .identity import ResolvedIdentity as ResolvedIdentity
    from .identity import resolve_username as resolve_username
    from .json_backend import JsonReservationBackend as JsonReservationBackend
    from .null_backend import NullReservationBackend as NullReservationBackend
    from .null_backend import is_null_backend as is_null_backend
    from .protocol import Reservation as Reservation
    from .protocol import ReservationBackend as ReservationBackend
    from .protocol import SupportsResourceHolders as SupportsResourceHolders
    from .protocol import SupportsUsernameCompletion as SupportsUsernameCompletion
    from .registry import RESERVATION_BACKENDS as RESERVATION_BACKENDS
    from .registry import register_reservation_backend as register_reservation_backend
    from .report import MissingResource as MissingResource
    from .report import ReservationReport as ReservationReport
    from .report import ReservationRow as ReservationRow
    from .report import build_report as build_report

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "ReservationBackendBase": "otto.reservations.base",
    "MissingReservationError": "otto.reservations.check",
    "ReservationBackendError": "otto.reservations.check",
    "ReservationGate": "otto.reservations.check",
    "ReservationGateResult": "otto.reservations.check",
    "ResourceLevel": "otto.reservations.check",
    "ResourceOrigin": "otto.reservations.check",
    "active_reservations": "otto.reservations.check",
    "announce_expiring": "otto.reservations.check",
    "check_reservations": "otto.reservations.check",
    "required_resource_origins": "otto.reservations.check",
    "required_resources": "otto.reservations.check",
    "reset_expiry_warnings": "otto.reservations.check",
    "build_backend": "otto.reservations.factory",
    "build_reservation_gate": "otto.reservations.factory",
    "gate_from_settings": "otto.reservations.factory",
    "ReservationIdentity": "otto.reservations.identity",
    "ResolvedIdentity": "otto.reservations.identity",
    "resolve_username": "otto.reservations.identity",
    "JsonReservationBackend": "otto.reservations.json_backend",
    "NullReservationBackend": "otto.reservations.null_backend",
    "is_null_backend": "otto.reservations.null_backend",
    "Reservation": "otto.reservations.protocol",
    "ReservationBackend": "otto.reservations.protocol",
    "SupportsResourceHolders": "otto.reservations.protocol",
    "SupportsUsernameCompletion": "otto.reservations.protocol",
    "RESERVATION_BACKENDS": "otto.reservations.registry",
    "register_reservation_backend": "otto.reservations.registry",
    "MissingResource": "otto.reservations.report",
    "ReservationReport": "otto.reservations.report",
    "ReservationRow": "otto.reservations.report",
    "build_report": "otto.reservations.report",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.reservations' public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "RESERVATION_BACKENDS",
    "JsonReservationBackend",
    "MissingReservationError",
    "MissingResource",
    "NullReservationBackend",
    "Reservation",
    "ReservationBackend",
    "ReservationBackendBase",
    "ReservationBackendError",
    "ReservationGate",
    "ReservationGateResult",
    "ReservationIdentity",
    "ReservationReport",
    "ReservationRow",
    "ResolvedIdentity",
    "ResourceLevel",
    "ResourceOrigin",
    "SupportsResourceHolders",
    "SupportsUsernameCompletion",
    "active_reservations",
    "announce_expiring",
    "build_backend",
    "build_report",
    "build_reservation_gate",
    "check_reservations",
    "gate_from_settings",
    "is_null_backend",
    "register_reservation_backend",
    "required_resource_origins",
    "required_resources",
    "reset_expiry_warnings",
    "resolve_username",
]
