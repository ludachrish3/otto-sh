"""Resolution through the named-inventory view, and inventory provenance (lab templating, #1176)."""

import json
import re
from pathlib import Path

import pytest

from otto.host.element import Element
from otto.host.inventory_ref import DEFAULT_INVENTORY, InventoryRef
from otto.inventory import (
    BareReferenceRefused,
    InventoryDeclaration,
    InventoryError,
    InventoryView,
    build_inventory_view_from_declarations,
    no_default_message,
    resolve_host_entry,
    resolve_view_entry,
)
from otto.inventory.view import RefusedDefault
from tests._fixtures.named_inventory import LookupCounting, register_lookup_counting

_DUT = Element(name="dut")
"""The element every entry here belongs to."""


def _refused(default: "LookupCounting | None") -> RefusedDefault:
    return RefusedDefault(
        origin="/p/.otto/settings.toml",
        declared=["physical"],
        inventories={} if default is None else {"default": default},
    )


def _view(*, refusing: bool = False, **inventories) -> InventoryView:
    root = Path("/p")
    view = InventoryView(
        inventories=inventories,
        bare_refused_by={root: "/p/.otto/settings.toml"} if refusing else {},
    )
    return view.for_project(root=root)


def _quoted_json(message: str) -> dict:
    """Return the one ``{...}`` object *message* quotes, parsed as JSON."""
    match = re.search(r"\{.*?\}", message)
    assert match is not None, message
    return json.loads(match.group(0))


def test_the_migration_trap_fails_before_any_lookup():
    """A project names only ``physical``; the user's ``default`` happens to hold the key.

    The bare reference must not quietly resolve through the user's inventory:
    it is refused before the supplies check (the entry states ``ip``, which
    ``default`` supplies) and before any lookup.
    """
    default = LookupCounting({"edge-17": {"ip": "10.9.9.9"}})
    entry = {"inventory": "edge-17", "ip": "10.0.0.1", "os_type": "unix"}
    for route in (
        lambda: resolve_host_entry(entry, inventory=_refused(default), element=_DUT),
        lambda: resolve_view_entry(entry, view=_view(refusing=True, default=default), element=_DUT),
    ):
        with pytest.raises(BareReferenceRefused) as info:
            route()
        assert _quoted_json(str(info.value)) == {"name": "<inventory>", "key": "edge-17"}
    assert default.lookups == 0


def test_the_resolver_order():
    counting = LookupCounting({"k": {"ip": "10.0.0.7"}})
    inline = {"ip": "1.2.3.4", "hop": "gw"}
    # 1. inline resolves, even in a refusing project
    out = resolve_host_entry(inline, inventory=_refused(counting), element=_DUT)
    assert out.host_data == inline
    assert out.ref == InventoryRef()
    # 2. bare with no inventory: today's text, unchanged
    with pytest.raises(InventoryError) as info:
        resolve_host_entry({"inventory": "k"}, inventory=None, element=_DUT)
    assert str(info.value) == no_default_message(key="k")
    assert str(info.value) == (
        "references inventory key 'k' but no inventory is configured; declare "
        "[inventory] in ~/.otto/settings.toml or in the project's .otto/settings.toml"
    )
    # 3. qualified default goes through RefusedDefault.default
    qualified_default = {"inventory": {"name": DEFAULT_INVENTORY, "key": "k"}}
    out = resolve_host_entry(qualified_default, inventory=_refused(counting), element=_DUT)
    assert out.host_data["ip"] == "10.0.0.7"
    assert counting.lookups == 1
    with pytest.raises(InventoryError) as info:
        resolve_host_entry(qualified_default, inventory=_refused(None), element=_DUT)
    assert str(info.value) == no_default_message(key="k")
    # 4. any other name needs a view; nothing is consulted
    physical = {"inventory": {"name": "physical", "key": "k"}}
    with pytest.raises(InventoryError, match="needs a named-inventory view"):
        resolve_host_entry(physical, inventory=counting, element=_DUT)
    assert counting.lookups == 1


def test_a_qualified_reference_consults_only_its_inventory():
    default = LookupCounting({"k": {"ip": "10.0.0.1"}})
    physical = LookupCounting({"k": {"ip": "10.0.0.2"}}, label="fake:physical")
    virtual = LookupCounting({"k": {"ip": "10.0.0.3"}}, label="fake:virtual")
    view = _view(default=default, physical=physical, virtual=virtual)
    out = resolve_view_entry(
        {"inventory": {"name": "physical", "key": "k"}}, view=view, element=_DUT
    )
    assert out.host_data["ip"] == "10.0.0.2"
    assert (default.lookups, physical.lookups, virtual.lookups) == (0, 1, 0)
    out = resolve_view_entry({"inventory": "k"}, view=view, element=_DUT)
    assert out.host_data["ip"] == "10.0.0.1"
    assert (default.lookups, physical.lookups, virtual.lookups) == (1, 1, 0)
    with pytest.raises(InventoryError, match=r"'lab' .*not configured"):
        resolve_view_entry({"inventory": {"name": "lab", "key": "k"}}, view=view, element=_DUT)


def test_supplies_belong_to_the_selected_inventory():
    """``default`` supplies only ``ip``; ``physical`` supplies ``site`` too."""
    default = LookupCounting({"k": {"ip": "10.0.0.1", "site": "a"}}, supplies={"ip"})
    physical = LookupCounting(
        {"k": {"ip": "10.0.0.2", "site": "b"}}, supplies={"ip", "site"}, label="fake:physical"
    )
    view = _view(default=default, physical=physical)
    out = resolve_view_entry({"inventory": "k", "site": "mine"}, view=view, element=_DUT)
    assert out.host_data["site"] == "mine"
    with pytest.raises(InventoryError, match="'site' is inventory-owned"):
        resolve_view_entry(
            {"inventory": {"name": "physical", "key": "k"}, "site": "mine"},
            view=view,
            element=_DUT,
        )
    assert physical.lookups == 0


def test_provenance_carries_the_name_and_qualification():
    default = LookupCounting({"k": {"ip": "10.0.0.1", "extra": {"a": 1}}})
    physical = LookupCounting({"k": {"ip": "10.0.0.2"}}, label="fake:physical")
    view = _view(default=default, physical=physical)
    bare = resolve_view_entry({"inventory": "k"}, view=view, element=_DUT).ref
    assert bare == InventoryRef(key="k", backend="fake:mem", extra={"a": 1})
    assert (bare.name, bare.qualified) == (DEFAULT_INVENTORY, False)
    qualified_default = resolve_view_entry(
        {"inventory": {"name": "default", "key": "k"}}, view=view, element=_DUT
    ).ref
    assert (qualified_default.name, qualified_default.qualified) == (DEFAULT_INVENTORY, True)
    named = resolve_view_entry(
        {"inventory": {"name": "physical", "key": "k"}}, view=view, element=_DUT
    ).ref
    assert named == InventoryRef(key="k", backend="fake:physical", name="physical", qualified=True)
    # the legacy resolver stamps the same provenance for a bare reference
    assert resolve_host_entry({"inventory": "k"}, inventory=default, element=_DUT).ref == bare


def test_a_directly_built_ref_defaults_its_name():
    assert InventoryRef(key="k", backend="b").name == "default"
    assert InventoryRef().name == ""


def test_existing_ref_equality_holds():
    """``test_resolve.py`` compares refs it builds without ``name`` with resolved ones."""
    default = LookupCounting({"k": {"ip": "10.0.0.1", "extra": {"asset": "A-7"}}})
    out = resolve_host_entry({"inventory": "k"}, inventory=default, element=_DUT)
    assert out.ref == InventoryRef(key="k", backend="fake:mem", extra={"asset": "A-7"})


def test_a_bare_key_with_no_default_keeps_todays_text_on_the_view_route():
    with pytest.raises(InventoryError) as info:
        resolve_view_entry({"inventory": "k"}, view=InventoryView(inventories={}), element=_DUT)
    assert str(info.value) == no_default_message(key="k")


def test_an_empty_raw_table_never_admits_the_user_default(tmp_path, monkeypatch):
    from otto.config.user_settings import load_user_settings

    counting = register_lookup_counting(monkeypatch, records={"k": {"ip": "10.66.0.1"}})
    user = tmp_path / "user-settings.toml"
    user.write_text('[inventory]\nbackend = "fake:counting"\n')  # the user file
    physical = tmp_path / "physical.json"
    physical.write_text(json.dumps({"k": {"ip": "10.7.0.1"}}))
    root = tmp_path / "p"
    declaration = InventoryDeclaration(
        origin=str(root / ".otto" / "settings.toml"),
        anchor_dir=root,
        table={},
        inventories=[{"name": "physical", "backend": "json", "path": str(physical)}],
    )
    view = build_inventory_view_from_declarations(
        declarations=[declaration],
        user_settings=load_user_settings(path=user),
        user_settings_file=user,
    ).for_project(root=root)
    with pytest.raises(BareReferenceRefused):
        resolve_view_entry({"inventory": "k"}, view=view, element=_DUT)
    assert counting.lookups == 0
    out = resolve_view_entry(
        {"inventory": {"name": "physical", "key": "k"}}, view=view, element=_DUT
    )
    assert out.host_data["ip"] == "10.7.0.1"
