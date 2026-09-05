"""Which inventories can the shim check by stat alone."""

from pathlib import Path

from otto.inventory.creds import CredsOverlay
from otto.inventory.json_backend import JsonInventory
from otto.inventory.protocol import SupportsStatPaths


def test_json_inventory_reports_its_file(tmp_path: Path):
    inv = JsonInventory(tmp_path / "inv.json")
    assert isinstance(inv, SupportsStatPaths)
    assert inv.stat_paths() == [tmp_path / "inv.json"]


def test_creds_overlay_adds_its_creds_file(tmp_path: Path):
    inner = JsonInventory(tmp_path / "inv.json")
    overlay = CredsOverlay(inner, path=tmp_path / "creds.json")
    assert overlay.stat_paths() == [tmp_path / "inv.json", tmp_path / "creds.json"]


def test_creds_overlay_over_an_opaque_inner_is_opaque(tmp_path: Path):
    class Opaque:
        def lookup(self, key):
            raise KeyError(key)

        def fingerprint(self):
            return "hash"

    assert CredsOverlay(Opaque(), path=tmp_path / "creds.json").stat_paths() is None
