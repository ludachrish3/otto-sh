"""Inventory doctor findings (spec §11, §13): pure functions over an inventory + referenced keys."""

import json
from pathlib import Path

from otto.creds.json_store import _JsonCredsStore
from otto.inventory import CredsOverlay, JsonInventory
from otto.inventory.doctor import (
    creds_mode_warnings,
    orphan_creds_warning,
    orphan_warning,
    referenced_by_name,
    referenced_references,
    selected_inventory,
)
from otto.models.lab import ElementSpec


def _inv(tmp_path, keys):
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps({k: {"ip": f"10.0.0.{i}"} for i, k in enumerate(keys, start=1)}))
    return JsonInventory(p, supplies=["ip"])


def test_only_a_well_formed_reference_selects_an_inventory(tmp_path):
    """R7: missing/``None`` means "references nothing"; a malformed key is a problem, not a ref."""
    view = _view(tmp_path, refusing=False)
    assert selected_inventory({"inventory": "k"}, view=view) == "default"
    for host_data in ({}, {"inventory": None}, {"inventory": ""}, {"inventory": 3}):
        assert selected_inventory(host_data, view=view) is None, host_data


def test_references_count_across_every_flattened_entry(tmp_path):
    elements = [
        ElementSpec(name="a", labs=["l"], hosts=[{"inventory": "k1"}, {"ip": "10.0.0.9"}]),
        ElementSpec(name="b", labs=["l"], hosts=[{"inventory": "k2"}]),
    ]
    refs = referenced_references(element_lists=[elements])
    assert referenced_by_name(references=refs, view=_view(tmp_path, refusing=False)) == {
        "default": {"k1", "k2"}
    }


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
    inv = CredsOverlay(_inv(tmp_path, ["k"]), store=_JsonCredsStore(creds))
    (w,) = creds_mode_warnings(inv)
    assert "0644" in w
    assert str(creds) in w
    assert "0600" in w
    creds.chmod(0o600)
    assert creds_mode_warnings(inv) == []
    assert creds_mode_warnings(_inv(tmp_path, ["k"])) == []  # no creds store configured


def test_creds_mode_warning_names_a_missing_creds_file(tmp_path):
    """A declared but absent creds store file warns by name — it does not silently vanish."""
    missing = tmp_path / "nope" / "creds.json"
    inv = CredsOverlay(_inv(tmp_path, ["k"]), store=_JsonCredsStore(missing))
    (w,) = creds_mode_warnings(inv)
    assert str(missing) in w
    assert "does not exist" in w


def test_creds_mode_warning_names_a_path_that_cannot_be_stat_ed(tmp_path, monkeypatch):
    """spec 2026-09-06 creds-store §7.1: an OSError other than "missing" still warns.

    A ``PermissionError`` on the store file's stat (an unreadable directory,
    say) used to fall into a silent ``except OSError: continue`` — the doctor
    would report nothing at all for a store it could not check.
    """
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    inv = CredsOverlay(_inv(tmp_path, ["k"]), store=_JsonCredsStore(creds))
    real_stat = Path.stat

    def _denied(self, *args, **kwargs):
        if self == creds:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _denied)
    (w,) = creds_mode_warnings(inv)
    assert str(creds) in w
    assert "Permission denied" in w


def test_creds_mode_warnings_is_silent_over_a_vault_store(tmp_path):
    """A store with no stat paths (a vault) is opaque; nothing here to check by mode (§7.1)."""

    class NoStatPaths:
        label = "vault:x"

        def lookup(self, key):
            return []

        def list_keys(self):
            return None

        def fingerprint(self):
            return None

    assert creds_mode_warnings(CredsOverlay(_inv(tmp_path, ["k"]), store=NoStatPaths())) == []

    class OpaqueStatPaths(NoStatPaths):
        def stat_paths(self):
            return None

    assert creds_mode_warnings(CredsOverlay(_inv(tmp_path, ["k"]), store=OpaqueStatPaths())) == []


class _Unenumerable:
    label = "vault:x"

    def lookup(self, key):
        return []

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_orphan_creds_are_named_bounded_and_labelled(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({f"k{i}": [] for i in range(12)}))
    inv = CredsOverlay(_inv(tmp_path, ["k0"]), store=_JsonCredsStore(creds))
    w = orphan_creds_warning(inv)
    assert w is not None
    assert w.startswith(f"creds store '{inv.store.label}': 11 key(s) the inventory does not hold:")
    assert "k1, k10, k11, k2, k3, k4, k5, k6, k7, k8" in w
    assert "… and 1 more" in w
    assert "k8 … and 1 more (a renamed" in w
    assert "leaves its creds behind" in w


def test_orphan_creds_is_silent_without_orphans_a_store_or_an_enumerable_store(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": []}))
    assert (
        orphan_creds_warning(
            CredsOverlay(_inv(tmp_path, ["k", "other"]), store=_JsonCredsStore(creds))
        )
        is None
    )
    assert orphan_creds_warning(_inv(tmp_path, ["k"])) is None
    assert orphan_creds_warning(CredsOverlay(_inv(tmp_path, ["k"]), store=_Unenumerable())) is None


def test_orphan_creds_is_silent_over_an_unreadable_store(tmp_path):
    """A store that cannot even enumerate (missing file, bad JSON) never raises here.

    :func:`otto.inventory.doctor.creds_mode_warnings` is what names the
    file — this finding just has nothing to report.
    """
    missing = tmp_path / "absent.json"
    assert (
        orphan_creds_warning(CredsOverlay(_inv(tmp_path, ["k"]), store=_JsonCredsStore(missing)))
        is None
    )

    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json")
    assert (
        orphan_creds_warning(CredsOverlay(_inv(tmp_path, ["k"]), store=_JsonCredsStore(malformed)))
        is None
    )


def _view(tmp_path, *, refusing: bool):
    from otto.inventory import InventoryView

    root = tmp_path / "project"
    physical = tmp_path / "physical"
    physical.mkdir()
    return InventoryView(
        inventories={"default": _inv(tmp_path, ["a", "b"]), "physical": _inv(physical, ["a"])},
        bare_refused_by={root: "settings.toml"} if refusing else {},
    ).for_project(root=root)


_ELEMENTS = [
    ElementSpec(
        name="e",
        labs=["l"],
        hosts=[
            {"inventory": "a"},
            {"inventory": {"name": "physical", "key": "a"}},
            {"inventory": {"name": "default", "key": "b"}},
            {"inventory": {"name": "nowhere", "key": "z"}},
            {"inventory": ""},
            {"ip": "10.0.0.9"},
        ],
    )
]


def test_references_count_per_selected_name(tmp_path):
    refs = referenced_references(element_lists=[_ELEMENTS])
    assert referenced_by_name(references=refs, view=_view(tmp_path, refusing=False)) == {
        "default": {"a", "b"},
        "physical": {"a"},
    }


def test_a_refused_bare_reference_counts_against_no_inventory(tmp_path):
    """D1: the bare ``a`` is a lab problem, never a reference to ``default``."""
    refs = referenced_references(element_lists=[_ELEMENTS])
    assert referenced_by_name(references=refs, view=_view(tmp_path, refusing=True)) == {
        "default": {"b"},
        "physical": {"a"},
    }


def test_selected_inventory_names_what_a_host_entry_selects(tmp_path):
    view = _view(tmp_path, refusing=True)
    hosts = _ELEMENTS[0].hosts
    assert [selected_inventory(host, view=view) for host in hosts] == [
        None,  # refused
        "physical",
        "default",
        None,  # not configured
        None,  # malformed
        None,  # references nothing
    ]


def test_orphans_of_a_named_inventory_count_only_its_own_keys(tmp_path):
    refs = referenced_references(element_lists=[_ELEMENTS])
    view = _view(tmp_path, refusing=False)
    by_name = referenced_by_name(references=refs, view=view)
    physical = view.select_name(name="physical")
    assert orphan_warning(physical, referenced=by_name["physical"]) is None
    default = view.select_name(name="default")
    assert orphan_warning(default, referenced=by_name.get("physical", set())) is not None
