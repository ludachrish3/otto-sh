"""Tool-agnostic host inventory beneath ``lab.json`` (spec 2026-08-28 host-inventory).

A host entry that says ``"inventory": "<key>"`` gets its machine facts —
address, interfaces, credentials, versions, location — from an inventory
backend; everything otto-specific stays in the lab file. The loader joins the
two with :func:`resolve_host_entry`; configuration selects the backend by
registered name (:func:`build_inventory`).
"""

from .cache import RefreshResult as RefreshResult
from .cache import SnapshotCache as SnapshotCache
from .cache import snapshot_cache_of as snapshot_cache_of
from .config import CompiledInventory as CompiledInventory
from .config import InventoryDeclaration as InventoryDeclaration
from .config import build_inventory as build_inventory
from .config import build_inventory_from_declarations as build_inventory_from_declarations
from .config import compile_inventory as compile_inventory
from .config import construct_inventory as construct_inventory
from .creds import CredsOverlay as CredsOverlay
from .creds import load_creds_file as load_creds_file
from .errors import InventoryError as InventoryError
from .errors import InventoryKeyError as InventoryKeyError
from .json_backend import JsonInventory as JsonInventory
from .json_backend import parse_inventory_document as parse_inventory_document
from .netbox import NetBoxInventory as NetBoxInventory
from .protocol import Inventory as Inventory
from .protocol import check_supplies as check_supplies
from .registry import get_inventory_backend_class as get_inventory_backend_class
from .registry import register_inventory_backend as register_inventory_backend
from .resolve import ResolvedEntry as ResolvedEntry
from .resolve import resolve_host_entry as resolve_host_entry
from .snapshot import RecordDifference as RecordDifference
from .snapshot import diff_records as diff_records
from .snapshot import document_to_records as document_to_records
from .snapshot import records_to_document as records_to_document
