"""The root of otto's exception hierarchy.

Every exception otto raises subclasses :class:`OttoError` in addition to the
stdlib type it already carried, so callers can catch "anything otto raised"
with one clause while every existing ``except ValueError`` / ``except
RuntimeError`` handler keeps working unchanged. Place ``except OttoError``
before any broad ``except ValueError`` / ``except RuntimeError`` clause in
the same ``try`` — the first lexical match wins.

The one deliberate exception is
:class:`~otto.lifecycle.SyncPhaseInterrupt`: it is a ``KeyboardInterrupt``,
and any ``Exception``-rooted base would make it catchable by ``except
Exception`` — which is exactly what its signal contract forbids.
"""


class OttoError(Exception):
    """Base class for every exception otto raises."""
