"""The ``Inventory`` protocol (spec 2026-08-28 host-inventory §10).

A backend answers "what is true about the machine with this key" and declares,
once, WHICH record fields it supplies — that declaration is the partition
between inventory-owned and lab-file-owned fields (spec §2), fixed at
construction from configuration and never discovered from records.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..models.inventory import FILLABLE_INVENTORY_FIELDS, INVENTORY_KEY_FIELDS, InventoryRecord
from .errors import InventoryError

if TYPE_CHECKING:
    from pathlib import Path


@runtime_checkable
class Inventory(Protocol):
    """Tool-agnostic host facts, keyed by an opaque inventory key."""

    # Read-only on the protocol: a backend sets these once in its own ``__init__``,
    # and a wrapper (``CredsOverlay``) derives them lazily from its inner backend.
    @property
    def label(self) -> str:
        """Name this inventory in provenance and errors, e.g. ``json:/home/me/inventory.json``."""
        ...

    @property
    def supplies(self) -> frozenset[str]:
        """Record fields this instance supplies; always contains ``"ip"``.

        Validated by :func:`check_supplies`.
        """
        ...

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*.

        Raises :class:`~otto.inventory.errors.InventoryKeyError` when this
        inventory does not hold *key*.
        """
        ...

    def list_keys(self) -> list[str]:
        """Return every key this inventory holds, sorted."""
        ...

    def fingerprint(self) -> "str | None":
        """Return a value that changes whenever the records may have.

        ``None`` means "not cacheable".
        """
        ...


@runtime_checkable
class SupportsStatPaths(Protocol):
    """Optional capability: name the files whose stat IS this backend's freshness.

    The completion shim (``otto._shim_complete``) revalidates the cache by
    ``os.stat`` alone. A backend whose :meth:`Inventory.fingerprint` is
    derived from files it can name implements this and returns them; one
    whose fingerprint is a content hash (the snapshot cache) returns ``None``
    and the shim hands over.
    """

    def stat_paths(self) -> "list[Path] | None":
        """Return the files whose stat decides this backend's freshness, or ``None``."""
        ...


def check_supplies(supplies: "Iterable[str] | None") -> frozenset[str]:
    """Validate a ``supplies`` declaration; ``None`` means every fillable field.

    Raises :class:`~otto.inventory.errors.InventoryError` when ``"ip"`` is
    missing (a reference that yields no address is pointless) or a name is not
    a record field. Key fields (``element_id``) are allowed: listing one means
    "this deployment asserts the key and wants it cross-checked".
    """
    if supplies is None:
        return FILLABLE_INVENTORY_FIELDS
    declared = frozenset(supplies)
    unknown = sorted(declared - FILLABLE_INVENTORY_FIELDS - INVENTORY_KEY_FIELDS)
    if unknown:
        raise InventoryError(
            f"supplies names a field that is not a record field: {unknown[0]!r} "
            f"(record fields: {sorted(FILLABLE_INVENTORY_FIELDS | INVENTORY_KEY_FIELDS)})"
        )
    if "ip" not in declared:
        raise InventoryError(
            "supplies must include 'ip': a reference that yields no address is pointless"
        )
    return declared
