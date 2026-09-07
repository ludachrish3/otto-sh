"""Inventory findings for the ``otto init`` doctor (spec §11, §13).

Pure — takes an inventory and the keys the lab files reference, returns
strings — so ``otto.cli.init`` stays the one reader of a repo's files.
"""

import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..models.lab import ElementSpec
from .creds import CredsOverlay
from .protocol import Inventory

_ORPHAN_LIST_CAP = 10


def references_inventory(host_data: "dict[str, Any]") -> bool:
    """Whether *host_data* names a non-empty string ``inventory`` key (R7).

    Mirrors the first checks :func:`otto.inventory.resolve_host_entry` makes
    before it ever touches an actual inventory: a missing or ``None`` key
    means "references nothing" (an entry that never mentioned the
    inventory), and any other non-string — the empty string included — is a
    malformed reference, a problem of its own regardless of whether an
    inventory even resolves. Both cases return ``False`` here: a caller
    using this to decide "does resolving this entry need a working
    inventory" must not gate an unrelated finding (a bogus ``os_type``, a
    malformed key) behind the inventory resolving at all. The single
    definition is shared by :func:`referenced_keys` and
    ``otto.cli.init._validate_lab``'s broken-declaration skip so the two
    cannot drift apart.
    """
    key = host_data.get("inventory")
    return isinstance(key, str) and bool(key)


def referenced_keys(element_lists: "Iterable[list[ElementSpec]]") -> set[str]:
    """Every ``inventory`` key any host entry names, across all files."""
    keys: set[str] = set()
    for elements in element_lists:
        for element in elements:
            for host_data in element.hosts:
                if references_inventory(host_data):
                    keys.add(host_data["inventory"])
    return keys


def orphan_warning(inventory: Inventory, *, referenced: set[str]) -> "str | None":
    """Return the warning for records no lab file here references (spec §11), or ``None``."""
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
