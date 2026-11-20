"""The join: a lab entry plus its inventory record → one plain host dict (spec §6)."""

from dataclasses import dataclass
from typing import Any

from typing_extensions import final

from ..host.element import Element
from ..host.inventory_ref import (
    InventoryRef,
    InventoryReference,
    normalize_reference,
)
from ..models.inventory import INVENTORY_KEY_FIELDS, MERGED_INVENTORY_FIELDS
from .creds import merge_creds
from .errors import InventoryError
from .protocol import Inventory
from .view import InventoryView


@final
@dataclass(frozen=True)
class ResolvedEntry:
    """A host dict every factory function accepts, plus where its facts came from."""

    host_data: dict[str, Any]
    ref: InventoryRef


def _normalized(host_data: dict[str, Any]) -> "InventoryReference | None":
    """Return what *host_data*'s ``inventory`` references; a bad value is an ``InventoryError``."""
    try:
        return normalize_reference(value=host_data.get("inventory"))
    except ValueError as e:
        raise InventoryError(str(e)) from None


def _inline(host_data: dict[str, Any]) -> ResolvedEntry:
    """Return the inline entry *host_data* without its ``inventory`` key, with an empty ref."""
    return ResolvedEntry(
        host_data={k: v for k, v in host_data.items() if k != "inventory"},
        ref=InventoryRef(),
    )


def resolve_host_entry(
    host_data: dict[str, Any], *, inventory: "Inventory | None", element: Element
) -> ResolvedEntry:
    """Return *host_data* with its ``inventory`` reference resolved through *inventory*.

    *inventory* is the ``default`` inventory, the one a bare reference
    resolves through, or ``None`` when none is configured. The reference is
    read by :func:`~otto.host.inventory_ref.normalize_reference` and decided
    in this order:

    1. An ABSENT ``inventory`` key and a ``None`` one both mean "references
       nothing" (R7 — a host entry that never mentioned the inventory
       validates as ``{"inventory": None, ...}`` under schema-legal
       round-tripping, and :func:`otto.host.factory.reject_unresolved_reference`
       reads it the same way): the key is dropped and the entry carries an
       empty :class:`~otto.host.inventory_ref.InventoryRef`, in any project.
    2. A bare key, when *inventory* is a
       :class:`~otto.inventory.RefusedDefault` (the project refuses bare
       references), is refused by its
       :meth:`~otto.inventory.RefusedDefault.refuse` before any supplies check
       or lookup; with no inventory it raises
       :func:`~otto.inventory.no_default_message`; otherwise it is joined.
    3. A qualified ``{"name": "default", ...}`` reference resolves through the
       process ``default``, which a ``RefusedDefault`` keeps as its
       :attr:`~otto.inventory.RefusedDefault.default`.
    4. A qualified reference to any other inventory is refused: it needs a
       named-inventory view (:func:`resolve_view_entry`), and is never
       resolved against whatever inventory this call received.

    Anything else, the empty string included, is an error, not a second
    spelling of "inline". The join itself is described under
    :func:`resolve_view_entry`.

    It is :func:`resolve_view_entry` over
    :meth:`InventoryView.of_default(inventory=inventory)
    <otto.inventory.InventoryView.of_default>`, the one decision of what a
    legacy ``inventory=`` argument reaches.

    Never mutates *host_data*. Raises
    :class:`~otto.inventory.errors.InventoryError` (or its subclass
    :class:`~otto.inventory.errors.InventoryKeyError`) with the key in the
    message; the lab loader prefixes file / element / index.
    """
    return resolve_view_entry(
        host_data, view=InventoryView.of_default(inventory=inventory), element=element
    )


def resolve_view_entry(
    host_data: dict[str, Any], *, view: "InventoryView", element: Element
) -> ResolvedEntry:
    """Return *host_data* with its ``inventory`` reference resolved through *view*.

    No reference returns the entry inline (see :func:`resolve_host_entry`).
    Otherwise :meth:`InventoryView.select <otto.inventory.InventoryView.select>`
    picks the one inventory the reference consults — a bare reference in a
    project that refuses them is refused first, then a reference through a
    missing ``default`` raises :func:`~otto.inventory.no_default_message`,
    then an undeclared name is refused — and only that inventory is joined.

    The join is the partition rule (spec §2) as code: for a referenced entry
    every field in the selected inventory's ``supplies`` must be ABSENT
    inline (checked on the raw entry, before the fill, so the fill cannot
    fool it) and is copied from the record when the record STATES it. Key
    fields (``element_id``) are never copied: a record is per host and an
    element is shared, so the record's value is cross-checked against
    ``element.id`` and never fills it (spec 2026-09-05 §8.6). ``creds`` is the
    exception (``MERGED_INVENTORY_FIELDS``, spec 2026-09-06 §6.2): stated
    inline beside a reference it COMPOSES over the record's by login rather
    than colliding.

    "States it" means the record SET the field — ``exclude_unset``, keyed on
    ``model_fields_set``, not ``exclude_defaults``, which compares values and
    would drop an explicit ``is_virtual: False`` or ``creds: []`` as though the
    record had been silent. An explicit ``None`` is also not a statement
    (``exclude_none``): it is how a schema round-trip spells "unset", and the
    entry's own default applies instead. The same rule holds one level down:
    an inventory-owned field present inline as ``None`` states nothing, so it
    does not collide with the record.

    The result's :class:`~otto.host.inventory_ref.InventoryRef` records the
    inventory's name and whether the reference was qualified.

    Never mutates *host_data*. Raises
    :class:`~otto.inventory.errors.InventoryError` (or a subclass:
    :class:`~otto.inventory.errors.InventoryKeyError`,
    :class:`~otto.inventory.errors.BareReferenceRefused`) with the key in the
    message.
    """
    reference = _normalized(host_data)
    if reference is None:
        return _inline(host_data)
    inventory = view.select(reference=reference)
    return _join(host_data, reference=reference, inventory=inventory, element=element)


def _join(
    host_data: dict[str, Any],
    *,
    reference: "InventoryReference",
    inventory: "Inventory",
    element: Element,
) -> ResolvedEntry:
    """Join *host_data* with *reference*'s record in *inventory*: the one join of every resolver.

    The partition rule is described under :func:`resolve_view_entry`.
    """
    key = reference.key
    inline = sorted(
        k
        for k, v in host_data.items()
        if v is not None
        and k in inventory.supplies
        and k not in INVENTORY_KEY_FIELDS
        and k not in MERGED_INVENTORY_FIELDS
    )
    if inline:
        raise InventoryError(
            f"{inline[0]!r} is inventory-owned — it comes from inventory key {key!r}; "
            "remove it here, or drop 'inventory' and declare the host inline"
        )
    record = inventory.lookup(key)
    if "element_id" in inventory.supplies:
        theirs = record.element_id
        if theirs is not None and element.id is not None and theirs != element.id:
            raise InventoryError(
                f"element_id disagrees: the lab file says {element.id!r} for element "
                f"{element.name!r}, inventory key {key!r} says {theirs!r}"
            )
    resolved = {k: v for k, v in host_data.items() if k != "inventory"}
    # Python mode: the record's creds below are merged and built into the
    # host, and keep their ``SecretStr`` passwords all the way (a JSON dump
    # would replace each with the mask; a revealed one would leave the plain
    # password in this frame's locals for a traceback to show).
    stated = record.model_dump(exclude_none=True, exclude_unset=True)
    for field_name in sorted(inventory.supplies - INVENTORY_KEY_FIELDS - MERGED_INVENTORY_FIELDS):
        if field_name in stated:
            resolved[field_name] = stated[field_name]
    if "creds" in inventory.supplies:
        # The set has one member; a second one needs its own compose step here.
        inline_creds = host_data.get("creds")
        if isinstance(inline_creds, list):
            # Spec 2026-09-06 §6.2: the lab file is the highest layer and leads.
            resolved["creds"] = merge_creds(
                lower=stated.get("creds", []),
                higher=inline_creds,
                key=key,
                lower_name="inventory record",
                higher_name="lab file",
            )
        elif inline_creds is None and "creds" in stated:
            resolved["creds"] = stated["creds"]
        # any other inline shape (the legacy dict) stays for the host spec's own error
    return ResolvedEntry(
        host_data=resolved,
        ref=InventoryRef(
            key=key,
            backend=inventory.label,
            extra=dict(record.extra),
            name=reference.name,
            qualified=reference.qualified,
        ),
    )
