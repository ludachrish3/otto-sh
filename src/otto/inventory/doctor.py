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
    """Every ``inventory`` key any flattened host entry names, across all files."""
    keys: set[str] = set()
    for elements in element_lists:
        for element in elements:
            for host_data in element.flatten():
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


def creds_mode_warning(inventory: Inventory) -> "str | None":
    """Return the warning for a ``creds_file`` readable by group/others (spec §9.4), or ``None``.

    Reads ``creds_path`` only through the ``CredsOverlay`` type check — the
    ``isinstance`` stays even though ``CredsOverlay.__init__`` always sets
    the attribute, because a third-party backend that happens to expose an
    unrelated, possibly non-``Path`` ``creds_path`` of its own must not
    traceback the doctor. This also means the warning only fires when
    ``CredsOverlay`` is the OUTERMOST wrapper — which is how
    :func:`~otto.inventory.config.construct_inventory` builds one today; a
    future wrapper placed outside it would silently hide this warning.
    """
    if not isinstance(inventory, CredsOverlay):
        return None
    path: Path = inventory.creds_path
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return f"creds_file {path} does not exist"
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"creds_file {path} is mode {mode:04o}; it holds passwords — make it 0600 (chmod 600)"
        )
    return None
