"""Error contract for the host-source (``LabRepository``) backend interface.

Mirrors the reservation backend's error contract
(:class:`~otto.reservations.check.ReservationBackendError`): a backend signals
trouble through these types so callers and the conformance suite can rely on a
stable surface instead of backend-specific exceptions.
"""

from typing import TYPE_CHECKING

from ..errors import OttoError

if TYPE_CHECKING:
    from .drops import Diagnostic


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


class LabUnavailableError(LabRepositoryError):
    """A lab is declared (or may be) but cannot be loaded now.

    Two causes, told apart by :attr:`declared`. The lab's last declaring
    source marks it unavailable (a ``labs`` entry's ``"unavailable"``
    reason), or a source whose declared scope contains the name failed, so
    whatever it would have contributed is unknown. It is not a
    :class:`LabNotFoundError`: the name is not unknown, it is out of reach.
    """

    def __init__(self, *, name: str, diagnostic: "Diagnostic", declared: bool) -> None:
        self.name = name
        """The lab that cannot be loaded."""
        self.diagnostic = diagnostic
        """Where and why, as the declaration states it or as the failed source reported it."""
        self.declared = declared
        """``True`` when a declaration marks the lab unavailable, ``False`` when a source failed."""
        if declared:
            message = f"Lab {name!r} is unavailable: {diagnostic.reason} ({diagnostic.where})"
        else:
            message = (
                f"Lab {name!r} is unavailable: source {diagnostic.where} failed: "
                f"{diagnostic.reason}"
            )
        super().__init__(message)


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
