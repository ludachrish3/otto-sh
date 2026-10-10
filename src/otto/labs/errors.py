"""Error contract for the host-source (``LabRepository``) backend interface.

Mirrors the reservation backend's error contract
(:class:`~otto.reservations.check.ReservationBackendError`): a backend signals
trouble through these types so callers and the conformance suite can rely on a
stable surface instead of backend-specific exceptions.
"""

from ..errors import OttoError


class LabRepositoryError(OttoError):
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


class LabSourceConstructionError(LabRepositoryError, ValueError):
    """A ``[[lab.sources]]`` entry could not be prepared or built.

    Raised for every stage of turning a declaration into a source: its
    backend is not registered (lookup), its options do not parse (parse), the
    backend's factory failed (construction), or what it built is not a lab
    source (result). The message names the stage, the backend, the module
    that registered it and the settings file that declared the source. A
    ``ValueError`` as well, because each of these is a configuration mistake;
    a backend's runtime failures while querying stay plain
    :class:`LabRepositoryError`.
    """
