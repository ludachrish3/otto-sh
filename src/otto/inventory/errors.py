"""Inventory errors (spec 2026-08-28 host-inventory §10).

Three classes decide whether an expired snapshot may stand in for a backend
that failed (lab templating [D2], decided once in
:func:`~otto.inventory.cache.stale_refusal`): a plain :class:`InventoryError`
serves the snapshot at any age, :class:`InventoryDataError` never does, and
:class:`InventoryUnreachableError` does only within its ``max_stale``.
"""

from datetime import timedelta

from ..errors import OttoError


class InventoryError(OttoError):
    """An inventory backend could not answer: I/O, parse, network, auth, or a bad record."""


class InventoryKeyError(InventoryError):
    """A host entry references a key the inventory does not hold."""

    def __init__(self, key: str, *, label: str) -> None:
        super().__init__(f"inventory key {key!r} not found in inventory {label!r}")
        self.key = key
        self.label = label


class InventoryConstructionError(InventoryError, ValueError):
    """An ``[inventory]`` table could not be prepared or built.

    Raised for every stage of turning the table into an inventory: its
    backend is not registered (lookup), its keys do not parse (parse), the
    backend's factory failed (construction), or what it built is not an
    inventory (result). The message names the stage, the backend, the module
    that registered it and the settings file that declared the table. A
    ``ValueError`` as well, because each of these is a configuration mistake;
    a backend's failures while answering stay plain :class:`InventoryError`.
    """


class InventoryDataError(InventoryError):
    """The inventory answered, but with data otto cannot use: a snapshot is never served for it.

    Raise it for an upstream DATA failure: missing output, output that does
    not parse, a record missing a field the backend needs. Serving yesterday's
    snapshot would hide a broken source behind data nobody can vouch for, so
    the snapshot cache refuses to fall back and raises a new
    :class:`InventoryDataError`, naming the snapshot's age, from this one.
    """


class InventoryUnreachableError(InventoryError):
    """The inventory could not be reached: a snapshot is served only within *max_stale*.

    Raise it for a TRANSIENT outage: a refused connection, a timeout, a
    service that is down. The snapshot cache serves its snapshot, with the
    stale notice, only when *max_stale* is set and the snapshot's total age
    (since it was fetched) is at most *max_stale*; otherwise it raises a new
    :class:`InventoryUnreachableError`, keeping *max_stale* and naming the
    snapshot's age, from this one. *max_stale* is the backend's own setting,
    read with :func:`~otto.inventory.parse_max_stale`; ``None`` (the default)
    means an expired snapshot is never served.
    """

    def __init__(self, message: str, *, max_stale: timedelta | None = None) -> None:
        super().__init__(message)
        self.max_stale = max_stale
        """How old a snapshot may be and still be served, or ``None`` for never."""


class BareReferenceRefused(InventoryError):  # noqa: N818 — interface-fixed name the plan names
    """A bare inventory reference in a project that refuses them (lab templating D1).

    A project that declares ``[[inventories]]`` without binding ``default``
    itself may not use bare ``"inventory": "<key>"`` references; the message
    quotes the qualified form. Raised only by
    :meth:`~otto.inventory.view.RefusedDefault.refuse`, which has already
    recorded the refusal as a drop, so every other drop recorder skips it.
    """
