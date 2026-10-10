"""Inventory errors (spec 2026-08-28 host-inventory §10)."""

from ..errors import OttoError


class InventoryError(OttoError):
    """An inventory backend could not answer: I/O, parse, network, auth, or a bad record."""


class InventoryKeyError(InventoryError):
    """A host entry references a key the inventory does not hold."""

    def __init__(self, key: str, label: str) -> None:
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
