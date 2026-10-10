"""Tool-agnostic host inventory beneath ``lab.json`` (spec 2026-08-28 host-inventory).

A host entry that says ``"inventory": "<key>"`` gets its machine facts —
address, interfaces, credentials, versions, location — from an inventory
backend; everything otto-specific stays in the lab file. The loader joins the
two with :func:`resolve_host_entry`; configuration selects the backend by
registered name (:func:`build_inventory`).

Every name is exported lazily (PEP 562): building the inventory for a lab
imports the configured backend's module, not every backend otto ships.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import RefreshResult as RefreshResult
    from .cache import SnapshotCache as SnapshotCache
    from .cache import snapshot_cache_of as snapshot_cache_of
    from .config import CompiledInventory as CompiledInventory
    from .config import InventoryDeclaration as InventoryDeclaration
    from .config import JsonInventoryConfig as JsonInventoryConfig
    from .config import NetBoxInventoryConfig as NetBoxInventoryConfig
    from .config import build_inventory as build_inventory
    from .config import build_inventory_from_declarations as build_inventory_from_declarations
    from .config import compile_inventory as compile_inventory
    from .config import construct_inventory as construct_inventory
    from .creds import CredsOverlay as CredsOverlay
    from .creds import merge_creds as merge_creds
    from .errors import InventoryConstructionError as InventoryConstructionError
    from .errors import InventoryError as InventoryError
    from .errors import InventoryKeyError as InventoryKeyError
    from .json_backend import JsonInventory as JsonInventory
    from .json_backend import parse_inventory_document as parse_inventory_document
    from .netbox import NetBoxInventory as NetBoxInventory
    from .protocol import Inventory as Inventory
    from .protocol import SupportsStatPaths as SupportsStatPaths
    from .protocol import check_supplies as check_supplies
    from .registry import InventoryEnv as InventoryEnv
    from .registry import InventoryMetadata as InventoryMetadata
    from .registry import register_inventory_backend as register_inventory_backend
    from .resolve import ResolvedEntry as ResolvedEntry
    from .resolve import resolve_host_entry as resolve_host_entry
    from .snapshot import RecordDifference as RecordDifference
    from .snapshot import diff_records as diff_records
    from .snapshot import document_to_records as document_to_records
    from .snapshot import records_to_document as records_to_document

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "RefreshResult": "otto.inventory.cache",
    "SnapshotCache": "otto.inventory.cache",
    "snapshot_cache_of": "otto.inventory.cache",
    "CompiledInventory": "otto.inventory.config",
    "InventoryDeclaration": "otto.inventory.config",
    "build_inventory": "otto.inventory.config",
    "build_inventory_from_declarations": "otto.inventory.config",
    "compile_inventory": "otto.inventory.config",
    "construct_inventory": "otto.inventory.config",
    "CredsOverlay": "otto.inventory.creds",
    "merge_creds": "otto.inventory.creds",
    "InventoryError": "otto.inventory.errors",
    "InventoryKeyError": "otto.inventory.errors",
    "JsonInventory": "otto.inventory.json_backend",
    "parse_inventory_document": "otto.inventory.json_backend",
    "NetBoxInventory": "otto.inventory.netbox",
    "Inventory": "otto.inventory.protocol",
    "SupportsStatPaths": "otto.inventory.protocol",
    "check_supplies": "otto.inventory.protocol",
    "register_inventory_backend": "otto.inventory.registry",
    "ResolvedEntry": "otto.inventory.resolve",
    "resolve_host_entry": "otto.inventory.resolve",
    "RecordDifference": "otto.inventory.snapshot",
    "diff_records": "otto.inventory.snapshot",
    "document_to_records": "otto.inventory.snapshot",
    "records_to_document": "otto.inventory.snapshot",
    "InventoryEnv": "otto.inventory.registry",
    "InventoryMetadata": "otto.inventory.registry",
    "InventoryConstructionError": "otto.inventory.errors",
    "JsonInventoryConfig": "otto.inventory.config",
    "NetBoxInventoryConfig": "otto.inventory.config",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.inventory's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CompiledInventory",
    "CredsOverlay",
    "Inventory",
    "InventoryConstructionError",
    "InventoryDeclaration",
    "InventoryEnv",
    "InventoryError",
    "InventoryKeyError",
    "InventoryMetadata",
    "JsonInventory",
    "JsonInventoryConfig",
    "NetBoxInventory",
    "NetBoxInventoryConfig",
    "RecordDifference",
    "RefreshResult",
    "ResolvedEntry",
    "SnapshotCache",
    "SupportsStatPaths",
    "build_inventory",
    "build_inventory_from_declarations",
    "check_supplies",
    "compile_inventory",
    "construct_inventory",
    "diff_records",
    "document_to_records",
    "merge_creds",
    "parse_inventory_document",
    "records_to_document",
    "register_inventory_backend",
    "resolve_host_entry",
    "snapshot_cache_of",
]
