"""Creds-store errors (spec 2026-09-06 creds-store §5.1)."""

from ..errors import OttoError


class CredsError(OttoError):
    """A creds store could not answer: I/O, parse, network, auth, or a bad entry."""
