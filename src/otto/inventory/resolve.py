"""The join: a lab entry plus its inventory record → one plain host dict (spec §6)."""

from dataclasses import dataclass
from typing import Any

from ..host.inventory_ref import InventoryRef
from ..models.inventory import INVENTORY_KEY_FIELDS
from .errors import InventoryError
from .protocol import Inventory

_NO_INVENTORY = (
    "references inventory key {key!r} but no inventory is configured; declare "
    "[inventory] in ~/.otto/settings.toml or in the project's .otto/settings.toml"
)


@dataclass(frozen=True)
class ResolvedEntry:
    """A host dict every factory function accepts, plus where its facts came from."""

    host_data: dict[str, Any]
    ref: InventoryRef


def resolve_host_entry(host_data: dict[str, Any], inventory: "Inventory | None") -> ResolvedEntry:
    """Return *host_data* with its ``inventory`` reference resolved, or a copy unchanged.

    The partition rule (spec §2) as code: for a referenced entry every field in
    ``inventory.supplies`` must be ABSENT inline (checked on the raw entry,
    before the fill, so the fill cannot fool it) and is copied from the record
    when the record STATES it. Key fields (``element_id``) are never copied:
    when the inventory supplies one and both sides state it, they must agree.

    "States it" means the record SET the field — ``exclude_unset``, keyed on
    ``model_fields_set``, not ``exclude_defaults``, which compares values and
    would drop an explicit ``is_virtual: False`` or ``creds: []`` as though the
    record had been silent. An explicit ``None`` is also not a statement
    (``exclude_none``): it is how a schema round-trip spells "unset", and the
    entry's own default applies instead.

    An ABSENT ``inventory`` key and a ``None`` one both mean "references
    nothing" (R7 — a host entry that never mentioned the inventory validates
    as ``{"inventory": None, ...}`` under schema-legal round-tripping, and
    :func:`otto.host.factory.reject_unresolved_reference` reads it the same
    way); the key is dropped and the entry carries an empty
    :class:`~otto.host.inventory_ref.InventoryRef`. Any other non-string — the
    empty string included — is an error, not a second spelling of "inline".
    The same rule holds one level down: an inventory-owned field present inline
    as ``None`` states nothing, so it does not collide with the record.

    Never mutates *host_data*. Raises
    :class:`~otto.inventory.errors.InventoryError` (or its subclass
    :class:`~otto.inventory.errors.InventoryKeyError`) with the key in the
    message; the lab loader prefixes file / element / index.
    """
    if host_data.get("inventory") is None:
        return ResolvedEntry(
            host_data={k: v for k, v in host_data.items() if k != "inventory"},
            ref=InventoryRef(),
        )
    key = host_data["inventory"]
    if not isinstance(key, str) or not key:
        raise InventoryError(f"'inventory' must name a key (a non-empty string), got {key!r}")
    if inventory is None:
        raise InventoryError(_NO_INVENTORY.format(key=key))
    inline = sorted(
        k
        for k, v in host_data.items()
        if v is not None and k in inventory.supplies and k not in INVENTORY_KEY_FIELDS
    )
    if inline:
        raise InventoryError(
            f"{inline[0]!r} is inventory-owned — it comes from inventory key {key!r}; "
            "remove it here, or drop 'inventory' and declare the host inline"
        )
    record = inventory.lookup(key)
    for name in sorted(INVENTORY_KEY_FIELDS & inventory.supplies):
        theirs = getattr(record, name)
        mine = host_data.get(name)
        if theirs is not None and mine is not None and theirs != mine:
            raise InventoryError(
                f"{name} disagrees: the lab file says {mine!r}, "
                f"inventory key {key!r} says {theirs!r}"
            )
    resolved = {k: v for k, v in host_data.items() if k != "inventory"}
    stated = record.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    for name in sorted(inventory.supplies - INVENTORY_KEY_FIELDS):
        if name in stated:
            resolved[name] = stated[name]
    return ResolvedEntry(
        host_data=resolved,
        ref=InventoryRef(key=key, backend=inventory.label, extra=dict(record.extra)),
    )
