"""Error contract for the host-source (``LabRepository``) backend interface.

Mirrors the reservation backend's error contract
(:class:`~otto.reservations.check.ReservationBackendError`): a backend signals
trouble through these types so callers and the conformance suite can rely on a
stable surface instead of backend-specific exceptions.
"""


class LabRepositoryError(Exception):
    """A host-source backend failed to satisfy a query.

    Raised for I/O, network, parse, or credential failures while loading or
    listing labs — anything other than "the named lab does not exist", which
    raises the more specific :class:`LabNotFoundError`.
    """


class LabNotFoundError(LabRepositoryError):
    """``load_lab`` was asked for a lab name the backend does not know.

    A missing lab must raise this — not return ``None`` or raise a bare
    ``KeyError`` / ``FileNotFoundError`` — so callers can distinguish "unknown
    lab" from "backend is broken".
    """
