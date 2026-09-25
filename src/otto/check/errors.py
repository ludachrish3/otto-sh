"""Errors a check raises when it cannot even ask a host (not a verdict)."""

from ..errors import OttoError


class CheckHostUnreachableError(OttoError, RuntimeError):
    """A host a check needed did not answer (transport error or timeout)."""


class CheckCommandFailedError(OttoError, RuntimeError):
    """A host answered, but a read-only probe a check depends on failed."""
