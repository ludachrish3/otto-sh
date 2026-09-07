"""Which inventories can the shim check by stat alone."""

from pathlib import Path

from otto.creds import JsonCredsStore
from otto.inventory.creds import CredsOverlay
from otto.inventory.json_backend import JsonInventory
from otto.inventory.protocol import SupportsStatPaths


def test_json_inventory_reports_its_file(tmp_path: Path):
    inv = JsonInventory(tmp_path / "inv.json")
    assert isinstance(inv, SupportsStatPaths)
    assert inv.stat_paths() == [tmp_path / "inv.json"]


def test_creds_overlay_adds_its_creds_file(tmp_path: Path):
    inner = JsonInventory(tmp_path / "inv.json")
    overlay = CredsOverlay(inner, store=JsonCredsStore(tmp_path / "creds.json"))
    assert overlay.stat_paths() == [tmp_path / "inv.json", tmp_path / "creds.json"]


def test_creds_overlay_over_an_opaque_inner_is_opaque(tmp_path: Path):
    class Opaque:
        def lookup(self, key):
            raise KeyError(key)

        def fingerprint(self):
            return "hash"

    assert (
        CredsOverlay(Opaque(), store=JsonCredsStore(tmp_path / "creds.json")).stat_paths() is None
    )


def test_creds_overlay_over_an_opaque_store_is_opaque(tmp_path: Path):
    class OpaqueStore:
        label = "vault:x"

        def lookup(self, key):
            return []

        def list_keys(self):
            return None

        def fingerprint(self):
            return None

    inner = JsonInventory(tmp_path / "inv.json")
    assert CredsOverlay(inner, store=OpaqueStore()).stat_paths() is None
