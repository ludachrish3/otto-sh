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
