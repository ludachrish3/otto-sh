"""Tool-agnostic host inventory beneath ``lab.json`` (spec 2026-08-28 host-inventory).

A host entry that says ``"inventory": "<key>"`` gets its machine facts —
address, interfaces, credentials, versions, location — from an inventory
backend; everything otto-specific stays in the lab file. The loader joins the
two with :func:`resolve_view_entry` (through the named inventories) or
:func:`resolve_host_entry` (through ``default`` alone); configuration selects the backend by
registered name (:func:`build_inventory`).

Every name is exported lazily (PEP 562): building the inventory for a lab
imports the configured backend's module, not every backend otto ships.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..utils import parse_max_stale as parse_max_stale
    from .cache import RefreshResult as RefreshResult
    from .cache import SnapshotCache as SnapshotCache
    from .cache import snapshot_cache_of as snapshot_cache_of
    from .config import CompiledInventory as CompiledInventory
    from .config import InventoryDeclaration as InventoryDeclaration
    from .config import JsonInventoryConfig as JsonInventoryConfig
    from .config import NetBoxInventoryConfig as NetBoxInventoryConfig
    from .config import build_inventory as build_inventory
    from .config import build_inventory_from_declarations as build_inventory_from_declarations
    from .config import build_inventory_view as build_inventory_view
    from .config import (
        build_inventory_view_from_declarations as build_inventory_view_from_declarations,
    )
    from .config import compile_inventory as compile_inventory
    from .config import construct_inventory as construct_inventory
    from .config import deferred_inventory_view as deferred_inventory_view
    from .config import inventory_declaration as inventory_declaration
    from .creds import CredsOverlay as CredsOverlay
    from .creds import below_creds_overlay as below_creds_overlay
    from .creds import merge_creds as merge_creds
    from .discovery import Discovery as Discovery
    from .discovery import SupportsDiscovery as SupportsDiscovery
    from .discovery import discover_inventory as discover_inventory
    from .errors import BareReferenceRefused as BareReferenceRefused
    from .errors import InventoryConstructionError as InventoryConstructionError
    from .errors import InventoryDataError as InventoryDataError
    from .errors import InventoryError as InventoryError
    from .errors import InventoryKeyError as InventoryKeyError
    from .errors import InventoryUnreachableError as InventoryUnreachableError
    from .json_backend import JsonInventory as JsonInventory
    from .json_backend import parse_discovery_document as parse_discovery_document
    from .json_backend import parse_inventory_document as parse_inventory_document
    from .protocol import Inventory as Inventory
    from .protocol import SupportsStatPaths as SupportsStatPaths
    from .protocol import check_supplies as check_supplies
    from .registry import InventoryEnv as InventoryEnv
    from .registry import InventoryMetadata as InventoryMetadata
    from .registry import register_inventory_backend as register_inventory_backend
    from .resolve import ResolvedEntry as ResolvedEntry
    from .resolve import resolve_host_entry as resolve_host_entry
    from .resolve import resolve_view_entry as resolve_view_entry
    from .snapshot import RecordDifference as RecordDifference
    from .snapshot import diff_instances as diff_instances
    from .snapshot import diff_records as diff_records
    from .snapshot import discovery_to_document as discovery_to_document
    from .snapshot import document_to_discovery as document_to_discovery
    from .snapshot import document_to_records as document_to_records
    from .snapshot import records_to_document as records_to_document
    from .view import InventoryView as InventoryView
    from .view import RefusedDefault as RefusedDefault
    from .view import bare_refusal_message as bare_refusal_message
    from .view import no_default_message as no_default_message
    from .view import refuses_bare as refuses_bare

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
    "below_creds_overlay": "otto.inventory.creds",
    "merge_creds": "otto.inventory.creds",
    "Discovery": "otto.inventory.discovery",
    "SupportsDiscovery": "otto.inventory.discovery",
    "discover_inventory": "otto.inventory.discovery",
    "InventoryError": "otto.inventory.errors",
    "InventoryKeyError": "otto.inventory.errors",
    "JsonInventory": "otto.inventory.json_backend",
    "parse_discovery_document": "otto.inventory.json_backend",
    "parse_inventory_document": "otto.inventory.json_backend",
    "Inventory": "otto.inventory.protocol",
    "SupportsStatPaths": "otto.inventory.protocol",
    "check_supplies": "otto.inventory.protocol",
    "register_inventory_backend": "otto.inventory.registry",
    "ResolvedEntry": "otto.inventory.resolve",
    "resolve_host_entry": "otto.inventory.resolve",
    "resolve_view_entry": "otto.inventory.resolve",
    "RecordDifference": "otto.inventory.snapshot",
    "diff_instances": "otto.inventory.snapshot",
    "diff_records": "otto.inventory.snapshot",
    "discovery_to_document": "otto.inventory.snapshot",
    "document_to_discovery": "otto.inventory.snapshot",
    "document_to_records": "otto.inventory.snapshot",
    "records_to_document": "otto.inventory.snapshot",
    "InventoryEnv": "otto.inventory.registry",
    "InventoryMetadata": "otto.inventory.registry",
    "InventoryConstructionError": "otto.inventory.errors",
    "JsonInventoryConfig": "otto.inventory.config",
    "NetBoxInventoryConfig": "otto.inventory.config",
    "InventoryDataError": "otto.inventory.errors",
    "InventoryUnreachableError": "otto.inventory.errors",
    "parse_max_stale": "otto.utils",
    "BareReferenceRefused": "otto.inventory.errors",
    "InventoryView": "otto.inventory.view",
    "RefusedDefault": "otto.inventory.view",
    "bare_refusal_message": "otto.inventory.view",
    "no_default_message": "otto.inventory.view",
    "refuses_bare": "otto.inventory.view",
    "build_inventory_view": "otto.inventory.config",
    "build_inventory_view_from_declarations": "otto.inventory.config",
    "deferred_inventory_view": "otto.inventory.config",
    "inventory_declaration": "otto.inventory.config",
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
    "BareReferenceRefused",
    "CompiledInventory",
    "CredsOverlay",
    "Discovery",
    "Inventory",
    "InventoryConstructionError",
    "InventoryDataError",
    "InventoryDeclaration",
    "InventoryEnv",
    "InventoryError",
    "InventoryKeyError",
    "InventoryMetadata",
    "InventoryUnreachableError",
    "InventoryView",
    "JsonInventory",
    "JsonInventoryConfig",
    "NetBoxInventoryConfig",
    "RecordDifference",
    "RefreshResult",
    "RefusedDefault",
    "ResolvedEntry",
    "SnapshotCache",
    "SupportsDiscovery",
    "SupportsStatPaths",
    "bare_refusal_message",
    "below_creds_overlay",
    "build_inventory",
    "build_inventory_from_declarations",
    "build_inventory_view",
    "build_inventory_view_from_declarations",
    "check_supplies",
    "compile_inventory",
    "construct_inventory",
    "deferred_inventory_view",
    "diff_instances",
    "diff_records",
    "discover_inventory",
    "discovery_to_document",
    "document_to_discovery",
    "document_to_records",
    "inventory_declaration",
    "merge_creds",
    "no_default_message",
    "parse_discovery_document",
    "parse_inventory_document",
    "parse_max_stale",
    "records_to_document",
    "refuses_bare",
    "register_inventory_backend",
    "resolve_host_entry",
    "resolve_view_entry",
    "snapshot_cache_of",
]
