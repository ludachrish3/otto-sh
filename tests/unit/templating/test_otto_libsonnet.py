"""``otto.libsonnet``, the scaffolding otto ships to template authors.

Every test renders an entry file under ``libsonnet/`` through the one
renderer, with the packaged library as the reserved import, and gives it the
``otto`` argument :func:`~otto.inventory.snapshot.template_inputs` builds from
small fixture inventories, so the library is tested against the real shape
templates receive.
"""

import json
import re
import string
from pathlib import Path
from typing import Any

import pytest

from otto.inventory import Inventory, InventoryView
from otto.inventory.snapshot import template_inputs
from otto.labs.json_repository import parse_lab_sections
from otto.models.lab import ElementSpec, LabEntrySpec
from otto.templating.errors import RenderError
from otto.templating.imports import reserved_library
from otto.templating.render import RenderRequest, render
from tests._fixtures.named_inventory import FakeDiscoveryInventory, FakeInventory

from ._render_helpers import policy

ENTRIES = Path(__file__).parent / "libsonnet"
"""The entry files these tests render."""

INSTANCE_FIELDS = {"id", "records", "roles", "missing", "error"}
"""The one instance shape both providers return."""


def _rec(site: str, rack: "str | None", *groups: str, **labels: "str | int") -> dict[str, Any]:
    """Return a record at *site* and *rack* (``None`` states none), in *groups*, with *labels*."""
    record: dict[str, Any] = {"ip": "10.0.0.1", "site": site}
    if groups:
        record["groups"] = list(groups)
    if rack is not None:
        record["rack"] = rack
    if labels:
        record["labels"] = labels
    return record


def _render(entry: str, inventories: "dict[str, Inventory]", **tla: object) -> Any:
    """Render ``libsonnet/<entry>`` against *inventories*, with *tla* beside ``otto``."""
    names = list(inventories)
    inputs = template_inputs(view=InventoryView(inventories=inventories), names=names, project="p")
    return _evaluate(entry, {"otto": inputs, **tla})


def _evaluate(entry: str, tla: "dict[str, object]") -> Any:
    """Render ``libsonnet/<entry>`` with *tla* and the packaged library, and parse it."""
    request = RenderRequest(
        entry=ENTRIES / entry, tla=tla, policy=policy(reserved=reserved_library())
    )
    return json.loads(render(request).text)


def _router_fleet(**virtual: dict[str, Any]) -> "dict[str, Inventory]":
    """A ``physical`` inventory of DUTs and a ``virtual`` one holding *virtual*."""
    return {
        "physical": FakeInventory(
            {"edge-17": _rec("east", "R7", "dut"), "edge-18": _rec("west", "R8", "dut")}
        ),
        "virtual": FakeInventory(virtual),
    }


def test_east_dut_only_and_west_tgen_only_are_two_unavailable_labs():
    inventories = {
        "physical": FakeInventory({"edge-17": _rec("east", "R7", "dut")}),
        "virtual": FakeInventory({"ixia-9": _rec("west", "R7", "tgen")}),
    }
    document = _render("router_lab.jsonnet", inventories)
    assert document["labs"] == {
        "router-lab.east-R7": {"unavailable": "tgen: expected 1, found 0 (none)"},
        "router-lab.west-R7": {"unavailable": "dut: expected 1, found 0 (none)"},
    }
    assert document["elements"] == []
    assert document["_otto"] == {"provenance": {}}


def test_grouping_spans_inventories():
    # The traffic generator states its rack as a label, not a fact: the scope
    # reads facts first, then labels.
    inventories = _router_fleet(
        **{
            "ixia-9": {
                "ip": "10.0.0.9",
                "site": "east",
                "groups": ["tgen"],
                "labels": {"rack": "R7"},
            }
        }
    )
    document = _render("router_lab.jsonnet", inventories)
    assert document["labs"] == {
        "router-lab.east-R7": {},
        "router-lab.west-R8": {"unavailable": "tgen: expected 1, found 0 (none)"},
    }
    lab = re.escape("router-lab.east-R7")
    assert document["elements"] == [
        {
            "name": "east-R7-dut0",
            "labs": [lab],
            "hosts": [
                {
                    "os_type": "unix",
                    "valid_terms": ["ssh"],
                    "inventory": {"name": "physical", "key": "edge-17"},
                }
            ],
        },
        {
            "name": "east-R7-tgen0",
            "labs": [lab],
            "hosts": [{"os_type": "unix", "inventory": {"name": "virtual", "key": "ixia-9"}}],
        },
    ]


def test_record_missing_a_scope_value_fails_its_instance_only():
    # The lab name is built with `+`, which spells the null scope value 'null'.
    inventories = _router_fleet(
        **{"ixia-9": _rec("east", "R7", "tgen"), "ixia-10": _rec("west", None, "tgen")}
    )
    document = _render("router_lab.jsonnet", inventories)
    assert document["labs"] == {
        "router-lab.east-R7": {},
        "router-lab.west-R8": {"unavailable": "tgen: expected 1, found 0 (none)"},
        # The record without a rack forms its own instance, with the rack null.
        "router-lab.west-null": {"unavailable": "virtual/ixia-10 has no rack"},
    }
    assert [e["name"] for e in document["elements"]] == ["east-R7-dut0", "east-R7-tgen0"]


@pytest.mark.parametrize(
    ("s", "other"), [("a.b", "axb"), ("a+b", "aab"), ("x[1]", "x1"), ("p(q)", "pq")]
)
def test_escape_name_matches_only_itself(s: str, other: str):
    [rendered] = _evaluate("escape_name.jsonnet", {"names": [s]})
    assert rendered == re.escape(s)
    assert re.fullmatch(rendered, s)
    assert not re.fullmatch(rendered, other)


def test_escape_name_equals_re_escape_on_every_character():
    """Every ASCII character but NUL, and a few beyond ASCII, one at a time and joined."""
    chars = [chr(c) for c in range(1, 128)] + [*"éß€中😀", chr(0xA0), chr(0x2028)]
    names = [*chars, "".join(chars), string.printable]
    rendered = _evaluate("escape_name.jsonnet", {"names": names})
    assert rendered == [re.escape(n) for n in names]
    for name, pattern in zip(names, rendered, strict=True):
        assert re.fullmatch(pattern, name)


@pytest.mark.parametrize("name", ["\x00", "lab\x00x", "\x00lab", "lab\x00"])
def test_escape_name_refuses_a_nul(name: str):
    """A NUL, alone or inside a name, is refused, never escaped: no name holds one."""
    message = "escapeName: a name holds a NUL character, and names never contain one"
    with pytest.raises(RenderError, match=re.escape(message)):
        _evaluate("escape_name.jsonnet", {"names": ["ok", name]})


def test_pairing_ignores_record_order():
    # Two DUTs per lab, so the numbering shows which record each element got.
    inventories = {
        "physical": FakeInventory(
            {
                "edge-2": _rec("east", "R7", "dut"),
                "edge-1": _rec("east", "R7", "dut"),
                "edge-3": _rec("west", "R8", "dut"),
            }
        ),
        "virtual": FakeInventory(
            {
                "edge-0": _rec("east", "R7", "dut"),
                "ixia-9": _rec("east", "R7", "tgen"),
                "ixia-8": _rec("west", "R8", "tgen"),
            }
        ),
    }
    forward = _render("router_lab.jsonnet", inventories, duts=3)
    backward = _render("router_lab.jsonnet", inventories, duts=3, flip=True)
    assert forward == backward
    assert {
        e["name"]: (e["hosts"][0]["inventory"]["name"], e["hosts"][0]["inventory"]["key"])
        for e in forward["elements"]
        if "dut" in e["name"]
    } == {
        # Sorted by (inventory, key): physical's records before virtual's.
        "east-R7-dut0": ("physical", "edge-1"),
        "east-R7-dut1": ("physical", "edge-2"),
        "east-R7-dut2": ("virtual", "edge-0"),
    }
    assert forward["labs"]["router-lab.west-R8"] == {
        "unavailable": "dut: expected 3, found 1 (physical/edge-3)"
    }


def test_custom_lab_and_element_names_are_used():
    inventories = {
        "physical": FakeInventory(
            {
                "edge-17": _rec("east", "R7", "dut"),
                "edge-30": _rec("west", "R1", "dut"),
                "edge-31": _rec("west", "R2", "dut"),
            }
        )
    }
    document = _render("custom_names.jsonnet", inventories)
    # Only a runnable lab gets the author's lab entry.
    assert document["labs"] == {
        "bench(east)": {"metadata": {"site": "east"}},
        "bench(west)": {
            "unavailable": "dut: expected 1, found 2 (physical/edge-30, physical/edge-31)"
        },
    }
    [element] = document["elements"]
    assert element["name"] == "edge-17@east"
    # The lab name holds regex metacharacters; its membership matches only it.
    assert element["labs"] == [re.escape("bench(east)")]
    assert re.fullmatch(element["labs"][0], "bench(east)")
    assert not re.fullmatch(element["labs"][0], "bencheast")


def test_several_hosts_per_role_are_numbered():
    inventories = {
        "physical": FakeInventory(
            {f"edge-{k}": _rec("east", "R7", "dut") for k in (3, 1, 2)}
            | {"ixia-9": _rec("east", "R7", "tgen")}
        ),
        "virtual": FakeInventory({}),
    }
    document = _render("router_lab.jsonnet", inventories, duts=3)
    named = {e["name"]: e["hosts"][0]["inventory"]["key"] for e in document["elements"]}
    assert named == {
        "east-R7-dut0": "edge-1",
        "east-R7-dut1": "edge-2",
        "east-R7-dut2": "edge-3",
        "east-R7-tgen0": "ixia-9",
    }


def test_both_instance_providers_normalize_to_one_shape():
    inventory = FakeDiscoveryInventory(
        {
            "edge-17": _rec("east", None, "dut"),
            "ixia-9": _rec("east", None, "tgen"),
            "spare-1": _rec("west", None),
        },
        instances={
            "rig-7": {"roles": {"dut": ["edge-17"], "tgen": ["ixia-9"]}},
            "rig-8": {"members": ["spare-1"]},
        },
    )
    shapes = _render("instance_shapes.jsonnet", {"lab": inventory})
    for instance in shapes["explicit"] + shapes["derived"]:
        assert set(instance) == INSTANCE_FIELDS
    edge, ixia, spare = (
        {**inventory_record, "inventory": "lab", "key": key}
        for key, inventory_record in (
            ("edge-17", _rec("east", None, "dut")),
            ("ixia-9", _rec("east", None, "tgen")),
            ("spare-1", _rec("west", None)),
        )
    )
    # Every record carries the instances it belongs to, from the adapter.
    edge["instances"] = [{"instance": "rig-7", "role": "dut"}]
    ixia["instances"] = [{"instance": "rig-7", "role": "tgen"}]
    spare["instances"] = [{"instance": "rig-8", "role": None}]
    assert shapes["explicit"] == [
        {
            "id": {"instance": "rig-7"},
            "records": [edge, ixia],
            "roles": {"dut": [edge], "tgen": [ixia]},
            "missing": [],
            "error": None,
        },
        {
            "id": {"instance": "rig-8"},
            "records": [spare],
            "roles": None,  # members without roles: a layout selects them by `match`
            "missing": [],
            "error": None,
        },
    ]
    assert shapes["derived"] == [
        {
            "id": {"site": "east"},
            "records": [edge, ixia],
            "roles": None,
            "missing": [],
            "error": None,
        },
        {"id": {"site": "west"}, "records": [spare], "roles": None, "missing": [], "error": None},
    ]


def test_explicit_instance_reports_an_instance_whose_hosts_are_gone():
    inventory = FakeDiscoveryInventory(
        {"edge-17": _rec("east", None, "dut"), "ixia-9": _rec("east", None, "tgen")},
        instances={
            "rig-7": {"roles": {"dut": ["edge-17"], "tgen": ["ixia-9"]}},
            "rig-9": {"roles": {"dut": ["edge-90"], "tgen": ["ixia-90"]}},
        },
    )
    [_, gone] = _render("instance_shapes.jsonnet", {"lab": inventory})["explicit"]
    assert gone == {
        "id": {"instance": "rig-9"},
        "records": [],
        "roles": {"dut": [], "tgen": []},
        "missing": [{"inventory": "lab", "key": "edge-90"}, {"inventory": "lab", "key": "ixia-90"}],
        "error": None,
    }
    document = _render("explicit_rig.jsonnet", {"lab": inventory})
    assert document["labs"] == {
        "rig.rig-7": {},
        "rig.rig-9": {"unavailable": "no record for lab/edge-90, lab/ixia-90"},
    }
    assert [e["name"] for e in document["elements"]] == ["rig-7-dut", "rig-7-tgen"]


def test_provenance_names_instance_role_and_host_label():
    inventories = _router_fleet(**{"ixia-9": _rec("east", "R7", "tgen")})
    document = _render("router_lab.jsonnet", inventories)
    assert document["_otto"] == {
        "provenance": {
            "east-R7-dut0": {
                "instance": {"site": "east", "rack": "R7"},
                "role": "dut",
                "host": "hosts.routerDut",
            },
            "east-R7-tgen0": {
                "instance": {"site": "east", "rack": "R7"},
                "role": "tgen",
                "host": "hosts.linuxTgen",
            },
        }
    }
    # The provenance is comment space to the one lab loader, and the rest is a
    # lab document it accepts.
    sections = parse_lab_sections(document, "router_lab.jsonnet")
    assert set(sections) == {"labs", "elements", "links"}
    for entry in sections["labs"].values():
        LabEntrySpec.model_validate(entry)
    for element in sections["elements"]:
        ElementSpec.model_validate(element)


@pytest.mark.parametrize(
    ("which", "message"),
    [
        ("lab", "layout emits the lab name 'bench' more than once"),
        ("element", "layout emits the element name 'dut' more than once"),
    ],
)
def test_duplicate_emitted_name_is_an_error(which: str, message: str):
    inventories = {
        "physical": FakeInventory(
            {"edge-17": _rec("east", None, "dut"), "edge-30": _rec("west", None, "dut")}
        )
    }
    with pytest.raises(RenderError, match=re.escape(message)):
        _render("duplicate_name.jsonnet", inventories, which=which)


def test_predicates_select_records():
    inventories = {
        "physical": FakeInventory(
            {
                "edge-17": _rec("east", "R7", "dut", pod="p1"),
                "edge-30": _rec("west", "R1", "dut"),
            }
        ),
        "virtual": FakeInventory({"ixia-9": _rec("east", "R7", "tgen", pod="p2")}),
    }
    assert _render("predicates.jsonnet", inventories) == {
        "inGroup": ["physical/edge-17", "physical/edge-30"],
        "label": ["physical/edge-17"],
        "fact": ["physical/edge-17", "virtual/ixia-9"],
        "inInventory": ["virtual/ixia-9"],
        "allOf": ["physical/edge-17"],
        "anyOf": ["physical/edge-17", "virtual/ixia-9"],
        "version": "1",
    }


@pytest.mark.parametrize("alone", [True, False], ids=["only-instance", "beside-a-good-one"])
def test_a_lab_name_that_is_a_null_scope_value_fails_naming_the_instance(alone: bool):
    """Never a silently dropped lab, nor an unnamed failure sorting a null."""
    records: dict[str, dict[str, Any]] = {"edge-30": {"ip": "10.0.0.1", "groups": ["dut"]}}
    if not alone:
        records["edge-17"] = _rec("east", None, "dut")
    message = (
        'layout: lab(i) returned null for instance {"site":null} (physical/edge-30 has no '
        "site); lab(i) must return a non-empty string, also for an instance whose id holds null"
    )
    with pytest.raises(RenderError, match=re.escape(message)):
        _render("name_checks.jsonnet", {"physical": FakeInventory(records)}, name="lab")


def test_an_element_name_that_is_not_a_string_fails_naming_its_record():
    inventories = {"physical": FakeInventory({"edge-17": _rec("east", None, "dut")})}
    message = (
        "layout: elementName(i, role, record, n) returned null for role dut, record "
        'physical/edge-17, instance {"site":"east"}; elementName(i, role, record, n) must '
        "return a non-empty string"
    )
    with pytest.raises(RenderError, match=re.escape(message)):
        _render("name_checks.jsonnet", inventories, name="element")


def test_element_escapes_its_labs_and_references_its_record():
    element = _evaluate(
        "element.jsonnet",
        {
            "name": "edge-17",
            "record": {"inventory": "physical", "key": "edge-17", "ip": "10.0.0.1"},
            "host": {"os_type": "unix", "inventory": "stale"},
            "labs": ["lab.x", "b(c)"],
        },
    )
    assert element == {
        "name": "edge-17",
        "labs": [re.escape("lab.x"), re.escape("b(c)")],
        # The record's qualified reference replaces any the host carried.
        "hosts": [{"os_type": "unix", "inventory": {"name": "physical", "key": "edge-17"}}],
    }


@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        ("nope", "instances: the template declares no inventory nope"),
        (
            "walked",
            (
                "instances: inventory walked declares no instances (its adapter has none); "
                "derive them with groupBy"
            ),
        ),
    ],
)
def test_instances_of_an_inventory_without_an_instance_map_is_an_error(
    inventory: str, message: str
):
    inventories = {"walked": FakeInventory({"edge-17": _rec("east", None, "dut")})}
    with pytest.raises(RenderError, match=re.escape(message)):
        _render("instances_of.jsonnet", inventories, inventory=inventory)


def _members_only() -> "dict[str, Inventory]":
    """A ``lab`` inventory whose one instance lists members and no roles."""
    return {
        "lab": FakeDiscoveryInventory(
            {"edge-17": _rec("east", None, "dut"), "ixia-9": _rec("east", None, "tgen")},
            instances={"rig-7": {"members": ["edge-17", "ixia-9"]}},
        )
    }


def test_a_role_without_match_fails_for_an_instance_without_roles():
    message = "layout: role dut has no match, and an instance without roles needs one"
    with pytest.raises(RenderError, match=re.escape(message)):
        _render("explicit_rig.jsonnet", _members_only())


def test_members_without_roles_are_selected_by_match():
    document = _render("explicit_rig.jsonnet", _members_only(), match=True)
    assert document["labs"] == {"rig.rig-7": {}}
    assert {e["name"]: e["hosts"][0]["inventory"]["key"] for e in document["elements"]} == {
        "rig-7-dut": "edge-17",
        "rig-7-tgen": "ixia-9",
    }
