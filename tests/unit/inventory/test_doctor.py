"""Inventory doctor findings (spec §11, §13): pure functions over an inventory + referenced keys."""

import json

from otto.inventory import CredsOverlay, JsonInventory
from otto.inventory.doctor import (
    creds_mode_warning,
    orphan_warning,
    referenced_keys,
    references_inventory,
)
from otto.models.lab import ElementSpec


def _inv(tmp_path, keys):
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps({k: {"ip": f"10.0.0.{i}"} for i, k in enumerate(keys, start=1)}))
    return JsonInventory(p, supplies=["ip"])


def test_references_inventory_true_only_for_a_nonempty_string_key():
    """R7: missing/``None`` means "references nothing"; a malformed key is a problem, not a ref."""
    assert references_inventory({"inventory": "k"}) is True
    assert references_inventory({}) is False
    assert references_inventory({"inventory": None}) is False
    assert references_inventory({"inventory": ""}) is False
    assert references_inventory({"inventory": 3}) is False


def test_referenced_keys_walks_every_flattened_entry():
    elements = [
        ElementSpec(name="a", labs=["l"], hosts=[{"inventory": "k1"}, {"ip": "10.0.0.9"}]),
        ElementSpec(name="b", labs=["l"], hosts=[{"inventory": "k2"}]),
    ]
    assert referenced_keys([elements]) == {"k1", "k2"}


def test_orphans_are_named_bounded_and_labelled(tmp_path):
    inv = _inv(tmp_path, [f"k{i}" for i in range(12)])
    w = orphan_warning(inv, referenced={"k0"})
    assert w is not None
    assert w.startswith(f"inventory '{inv.label}': 11 record(s) referenced by no lab file here:")
    assert "k1, k10, k11, k2, k3, k4, k5, k6, k7, k8" in w
    assert "… and 1 more" in w
    assert "expected during the bridge" in w
    assert orphan_warning(inv, referenced={f"k{i}" for i in range(12)}) is None


def test_creds_mode_warning_names_the_mode(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    creds.chmod(0o644)
    inv = CredsOverlay(_inv(tmp_path, ["k"]), path=creds)
    w = creds_mode_warning(inv)
    assert w is not None
    assert "0644" in w
    assert str(creds) in w
    assert "0600" in w
    creds.chmod(0o600)
    assert creds_mode_warning(inv) is None
    assert creds_mode_warning(_inv(tmp_path, ["k"])) is None  # no creds file configured


def test_creds_mode_warning_names_a_missing_creds_file(tmp_path):
    """A declared but absent ``creds_file`` warns by name — it does not silently vanish."""
    missing = tmp_path / "nope" / "creds.json"
    inv = CredsOverlay(_inv(tmp_path, ["k"]), path=missing)
    w = creds_mode_warning(inv)
    assert w is not None
    assert str(missing) in w
    assert "does not exist" in w
