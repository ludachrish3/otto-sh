"""Discovery: records plus instances, the capability and the v2 document (lab templating §6.2).

An inventory may declare which of its keys form one instance (a rig), and
which role each plays. ``instances`` of ``None`` means the backend has no such
notion; ``{}`` means it has one and declares no instance.
"""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from otto.creds.json_store import _JsonCredsStore
from otto.inventory import CredsOverlay, InventoryError, JsonInventory
from otto.inventory.discovery import Discovery, SupportsDiscovery, discover_inventory
from otto.inventory.json_backend import parse_discovery_document, parse_inventory_document
from otto.models.inventory import FILLABLE_INVENTORY_FIELDS, InventoryInstance
from otto.models.jsonschema import build_schemas

from .test_resolve import FakeInventory

FILL = FILLABLE_INVENTORY_FIELDS
V2 = {
    "$format": "otto.inventory.v2",
    "records": {"edge-17": {"ip": "10.0.0.1"}},
    "instances": {"rig-7": {"roles": {"dut": ["edge-17"], "tgen": ["ixia-9"]}}, "empty": {}},
}


def _parse(data: object) -> Discovery:
    return parse_discovery_document(data=data, source="f", supplies=FILL)


def test_roles_round_trip_and_a_swap_is_a_different_document():
    a = parse_discovery_document(data=V2, source="f", supplies=FILL)
    swapped = copy.deepcopy(V2)
    swapped["instances"]["rig-7"]["roles"] = {"dut": ["ixia-9"], "tgen": ["edge-17"]}
    assert a != parse_discovery_document(data=swapped, source="f", supplies=FILL)
    assert a.instances is not None
    assert a.instances["empty"] == InventoryInstance()
    assert a.instances["rig-7"].all_members() == ["edge-17", "ixia-9"]
    assert a.instances["rig-7"].roles == {"dut": ["edge-17"], "tgen": ["ixia-9"]}
    assert list(a.records) == ["edge-17"]
    # The records half is what the legacy parser returns.
    assert parse_inventory_document(data=V2, source="f", supplies=FILL) == a.records


def test_lists_are_canonical():
    one = InventoryInstance(members=["b", "a"], roles={"dut": ["z", "y"]})
    assert one.members == ["a", "b"]
    assert one.roles == {"dut": ["y", "z"]}
    assert one == InventoryInstance(members=["a", "b"], roles={"dut": ["y", "z"]})
    assert one.all_members() == ["a", "b", "y", "z"]


def test_flat_documents_keep_their_meaning():
    flat = {
        "$format": {"ip": "10.0.0.1"},
        "instances": {"ip": "10.0.0.2"},
        "records": {"ip": "10.0.0.3"},
        "$schema": "x",
        "_comment": "y",
    }
    d = _parse(flat)
    assert d.instances is None
    assert sorted(d.records) == ["$format", "instances", "records"]
    assert d.records["instances"].ip == "10.0.0.2"


def test_unsupported_empty_and_memberless_are_three_results():
    assert _parse({"k": {"ip": "10.0.0.1"}}).instances is None
    v2 = {"$format": "otto.inventory.v2"}
    assert _parse({**v2, "instances": {}}).instances == {}
    assert _parse({**v2, "instances": {"x": {}}}).instances == {"x": InventoryInstance()}


def test_a_v2_document_keeps_comment_space():
    d = _parse({**V2, "$schema": "s", "_note": 1, "instances": {"_c": 1, "x": {"_why": "y"}}})
    assert d.instances == {"x": InventoryInstance()}


@pytest.mark.parametrize(
    ("instance", "where"),
    [
        ({"members": ["a", "a"]}, "members"),
        ({"roles": {"dut": ["a", "b", "a"]}}, "role 'dut'"),
        ({"members": ["a"], "roles": {"dut": ["a"]}}, "'members'"),
    ],
)
def test_a_duplicate_member_is_refused(instance, where):
    with pytest.raises(InventoryError) as e:
        _parse({"$format": "otto.inventory.v2", "instances": {"rig-7": instance}})
    msg = str(e.value)
    assert "'rig-7'" in msg
    assert "'a'" in msg
    assert where in msg


def test_one_key_may_play_two_roles():
    rig = {"roles": {"tgen": ["b", "a"], "dut": ["a"]}}
    d = _parse({"$format": "otto.inventory.v2", "instances": {"rig-7": rig}})
    assert d.instances is not None
    one = d.instances["rig-7"]
    assert one.roles == {"dut": ["a"], "tgen": ["a", "b"]}
    assert one.all_members() == ["a", "b"]
    assert InventoryInstance.model_validate(one.model_dump()) == one


def test_an_empty_role_name_is_refused():
    with pytest.raises(InventoryError, match=r"'rig-7'.*role name must be a non-empty"):
        _parse({"$format": "otto.inventory.v2", "instances": {"rig-7": {"roles": {"": ["a"]}}}})


def test_discovery_is_compared_by_value_and_unhashable():
    assert Discovery(records={}) == Discovery(records={})
    with pytest.raises(TypeError):
        hash(Discovery(records={}))


def test_an_empty_member_is_refused():
    with pytest.raises(InventoryError, match=r"'rig-7'.*members\.0.*at least 1"):
        _parse({"$format": "otto.inventory.v2", "instances": {"rig-7": {"members": [""]}}})


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"$format": "otto.inventory.v3"}, "'otto.inventory.v3'"),
        ({"$format": "otto.inventory.v2", "hosts": {}}, "'hosts'"),
        ({"$format": "otto.inventory.v2", "instances": []}, "instances"),
        ({"$format": "otto.inventory.v2", "instances": {"r": {"members": "a"}}}, "'r'"),
        ({"$format": "otto.inventory.v2", "instances": {"r": {"rolez": {}}}}, "rolez"),
        ({"$format": "otto.inventory.v2", "records": {"k": {"ipp": "x"}}}, "'k'"),
    ],
)
def test_an_unknown_format_or_section_is_refused(data, fragment):
    with pytest.raises(InventoryError) as e:
        _parse(data)
    assert str(e.value).startswith("f")
    assert fragment in str(e.value)


def test_a_plugin_method_named_discover_is_not_the_capability():
    class Plugin(FakeInventory):
        def discover(self):
            raise AssertionError("not the capability")

    inv = Plugin({"b": {"ip": "10.0.0.2"}, "a": {"ip": "10.0.0.1"}})
    assert not isinstance(inv, SupportsDiscovery)
    d = discover_inventory(inventory=inv)
    assert d.instances is None
    assert list(d.records) == ["a", "b"]
    assert d.records["a"].ip == "10.0.0.1"


def test_the_json_backend_has_the_capability(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(V2))
    inv = JsonInventory(path)
    assert isinstance(inv, SupportsDiscovery)
    assert discover_inventory(inventory=inv) == _parse(V2)
    assert inv.lookup("edge-17").ip == "10.0.0.1"
    assert inv.list_keys() == ["edge-17"]


def test_the_overlay_discovers_what_it_looks_up(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(V2))
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"edge-17": [{"login": "root", "password": "x"}]}))
    overlay = CredsOverlay(JsonInventory(path), store=_JsonCredsStore(creds))
    assert isinstance(overlay, SupportsDiscovery)
    d = discover_inventory(inventory=overlay)
    assert d.records == {"edge-17": overlay.lookup("edge-17")}
    assert [c.login for c in d.records["edge-17"].creds] == ["root"]
    assert d.instances == _parse(V2).instances
    # Over a backend without the capability, the overlay walks it.
    walked = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}), store=_JsonCredsStore(creds))
    assert discover_inventory(inventory=walked) == Discovery(records={"k": walked.lookup("k")})


def test_inventory_schema_accepts_both_formats():
    schema = build_schemas()["inventory"]
    Draft202012Validator.check_schema(schema)
    v = Draft202012Validator(schema)
    good = [
        {"$schema": "s", "_c": 1, "k": {"ip": "10.0.0.1"}},
        {"$format": {"ip": "10.0.0.1"}},
        {"instances": {"ip": "10.0.0.1"}},
        V2,
        {**V2, "$schema": "s", "_c": 1},
        {"$format": "otto.inventory.v2"},
    ]
    for doc in good:
        assert not list(v.iter_errors(doc)), doc
    bad = [
        {"k": {"ipp": "x"}},
        {"$format": "otto.inventory.v3"},
        {"$format": "otto.inventory.v2", "hosts": {}},
        {"$format": "otto.inventory.v2", "instances": {"r": {"members": "a"}}},
        {"$format": "otto.inventory.v2", "instances": {"r": {"members": [""]}}},
        {"$format": "otto.inventory.v2", "instances": {"r": {"members": ["a", "a"]}}},
        {"$format": "otto.inventory.v2", "instances": {"r": {"roles": {"d": ["a", "a"]}}}},
        {"$format": "otto.inventory.v2", "instances": {"r": {"roles": {"": ["a"]}}}},
        {"$format": "otto.inventory.v2", "records": {"k": {"ipp": "x"}}},
    ]
    for doc in bad:
        assert list(v.iter_errors(doc)), doc
