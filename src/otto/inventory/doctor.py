"""Inventory findings for the ``otto init`` doctor (spec §11, §13).

Pure — takes an inventory and the keys the lab files reference, returns
strings — so ``otto.init`` stays the one reader of a project's files. Which
inventory a reference counts against is the view's decision
(:meth:`~otto.inventory.InventoryView.selected_name`), never restated here.
"""

import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..host.inventory_ref import InventoryReference, normalize_reference
from ..models.lab import ElementSpec
from .creds import CredsOverlay
from .errors import InventoryError
from .protocol import Inventory
from .view import InventoryView

_ORPHAN_LIST_CAP = 10


def _reference_of(host_data: "dict[str, Any]") -> "InventoryReference | None":
    """*host_data*'s reference, or ``None`` for none and for a malformed one.

    The one reading of a host entry's reference here: a missing or ``None``
    key references nothing, and a value the normalizer refuses (the empty
    string, a non-string, a misspelled qualified reference) is a malformed
    reference, a problem resolving the entry reports on its own.
    """
    try:
        return normalize_reference(value=host_data.get("inventory"))
    except ValueError:
        return None


def referenced_references(
    *, element_lists: "Iterable[list[ElementSpec]]"
) -> "list[InventoryReference]":
    """Every well-formed reference any host entry states, across all files.

    De-duplicated and sorted. A bare and a qualified reference to the same
    record stay two entries: ``qualified`` is kept, for the caller that
    applies a project's policy on bare references.
    """
    refs: set[InventoryReference] = set()
    for elements in element_lists:
        for element in elements:
            for host_data in element.hosts:
                ref = _reference_of(host_data)
                if ref is not None:
                    refs.add(ref)
    return sorted(refs)


def selected_inventory(host_data: "dict[str, Any]", *, view: InventoryView) -> "str | None":
    """Return the name of the inventory *host_data*'s reference selects in *view*, or ``None``.

    ``None`` when the entry references nothing (no ``inventory`` key, or
    ``None``) or its reference is malformed, read by
    :func:`~otto.host.inventory_ref.normalize_reference` as
    :func:`referenced_references` reads it, and when *view* selects no inventory for
    it: a bare reference its project refuses (D1), or a name none
    configures. Those are problems of the entry itself, which resolving it
    reports. Selection only: nothing is built or looked up beyond what
    :meth:`~otto.inventory.InventoryView.selected_name` reads.
    """
    reference = _reference_of(host_data)
    return None if reference is None else _selected(reference, view=view)


def _selected(reference: InventoryReference, *, view: InventoryView) -> "str | None":
    try:
        return view.selected_name(reference=reference)
    except InventoryError:  # BareReferenceRefused included
        return None


def referenced_by_name(
    *, references: "Iterable[InventoryReference]", view: InventoryView
) -> "dict[str, set[str]]":
    """Return each inventory name and the keys *references* select in it, under *view*'s policy.

    *view* is the project's own (:meth:`~otto.inventory.InventoryView.for_project`),
    so a bare reference counts against ``default`` only where the project
    allows it. A reference the view refuses (D1) or cannot place counts
    against no inventory: it is a lab problem, never a reference to
    ``default``. No lookup is made.
    """
    keys: dict[str, set[str]] = {}
    for reference in references:
        name = _selected(reference, view=view)
        if name is not None:
            keys.setdefault(name, set()).add(reference.key)
    return keys


def orphan_warning(inventory: Inventory, *, referenced: set[str]) -> "str | None":
    """Return the warning for records no lab file here references (spec §11), or ``None``.

    *referenced* is the keys this project's references select in *this*
    inventory (:func:`referenced_by_name`); a key referenced in another
    inventory of the same label does not count. The text names the
    inventory by its label only: the doctor attributes it to the
    inventory's name, once.
    """
    orphans = sorted(set(inventory.list_keys()) - referenced)
    if not orphans:
        return None
    shown = ", ".join(orphans[:_ORPHAN_LIST_CAP])
    extra = len(orphans) - _ORPHAN_LIST_CAP
    more = f" … and {extra} more" if extra > 0 else ""
    return (
        f"inventory '{inventory.label}': {len(orphans)} record(s) referenced by no lab file "
        f"here: {shown}{more} (expected during the bridge, where the inventory is wider than "
        "any project)"
    )


def orphan_creds_warning(inventory: Inventory) -> "str | None":
    """Return the warning for creds-store keys the inventory does not hold (spec 2026-09-06 §7.1).

    ``None`` without a store, and when the store cannot enumerate
    (``list_keys()`` is ``None`` — a vault): an empty list must never be read
    as "no orphans". An unreadable store (a missing file) also cannot
    enumerate — :func:`creds_mode_warnings` is what names that file, so this
    stays silent rather than raising. Reads the store through the
    ``CredsOverlay`` type check, like :func:`creds_mode_warnings`.
    """
    if not isinstance(inventory, CredsOverlay):
        return None
    from ..creds.errors import CredsError  # function-local: otto.inventory is on the bootstrap path

    try:
        listed = inventory.store.list_keys()
    except CredsError:
        return None
    if listed is None:
        return None
    orphans = sorted(set(listed) - set(inventory.list_keys()))
    if not orphans:
        return None
    shown = ", ".join(orphans[:_ORPHAN_LIST_CAP])
    extra = len(orphans) - _ORPHAN_LIST_CAP
    more = f" … and {extra} more" if extra > 0 else ""
    return (
        f"creds store '{inventory.store.label}': {len(orphans)} key(s) the inventory does not "
        f"hold: {shown}{more} (a renamed or retired inventory key leaves its creds behind)"
    )


def creds_mode_warnings(inventory: Inventory) -> list[str]:
    """Warnings for creds-store files readable by group/others, or missing (spec 2026-09-06 §7.1).

    Asks the store which files its freshness is derived from (``stat_paths``)
    and checks each; a store with no stat paths — a vault — yields nothing.
    Reads the store only through the ``CredsOverlay`` type check, and only
    when the overlay is OUTERMOST, which is how ``construct_inventory`` builds
    one; a future wrapper placed outside it would silently hide this warning.
    """
    if not isinstance(inventory, CredsOverlay):
        return []
    stat_paths = getattr(inventory.store, "stat_paths", None)
    paths = stat_paths() if callable(stat_paths) else None
    out: list[str] = []
    for path in paths or []:
        try:
            mode = stat.S_IMODE(Path(path).stat().st_mode)
        except FileNotFoundError:
            out.append(f"creds store file {path} does not exist")
            continue
        except OSError as e:
            out.append(f"creds store file {path} could not be checked: {e}")
            continue
        if mode & 0o077:
            out.append(
                f"creds store file {path} is mode {mode:04o}; it holds passwords — "
                "make it 0600 (chmod 600)"
            )
    return out
