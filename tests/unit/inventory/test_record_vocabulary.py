"""The discovery vocabulary on a record: ``groups`` and ``labels`` (lab templating spec §6.2).

Both are facts about the instance a record belongs to, read by discovery and
templates. Neither is a host field: the ``supplies`` partition does not govern
them, and the join never copies them onto a host.
"""

import pytest

from otto.host.element import Element
from otto.inventory import InventoryError, resolve_host_entry
from otto.inventory.json_backend import parse_inventory_document
from otto.models.inventory import (
    FILLABLE_INVENTORY_FIELDS,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryRecord,
)
from tests.unit.inventory.test_resolve import FakeInventory


def test_labels_refuse_bool_and_float_and_keep_leading_zeros():
    record = InventoryRecord.model_validate(
        {"ip": "10.0.0.1", "labels": {"pod": "007", "vlan": 12}}
    )
    assert record.labels == {"pod": "007", "vlan": 12}
    for bad in (True, 1.5, [1], {"a": 1}):
        with pytest.raises(InventoryError) as e:
            parse_inventory_document(
                data={"edge-17": {"ip": "10.0.0.1", "labels": {"pod": bad}}},
                source="f",
                supplies=frozenset({"ip"}),
            )
        assert "'edge-17'" in str(e.value)
        assert "labels.pod" in str(e.value)


def test_groups_are_strict_strings():
    record = InventoryRecord.model_validate({"ip": "10.0.0.1", "groups": ["edge", "pod-7"]})
    assert record.groups == ["edge", "pod-7"]
    with pytest.raises(InventoryError) as e:
        parse_inventory_document(
            data={"edge-17": {"ip": "10.0.0.1", "groups": ["edge", 7]}},
            source="f",
            supplies=frozenset({"ip"}),
        )
    assert "'edge-17'" in str(e.value)
    assert "groups.1" in str(e.value)


def test_discovery_fields_are_supplies_exempt():
    assert {"groups", "labels"} <= SUPPLIES_EXEMPT_FIELDS
    assert not ({"groups", "labels"} & FILLABLE_INVENTORY_FIELDS)
    # Outside the partition: a record may carry them whatever supplies says.
    records = parse_inventory_document(
        data={"k": {"ip": "10.0.0.1", "groups": ["edge"], "labels": {"pod": 7}}},
        source="f",
        supplies=frozenset({"ip"}),
    )
    assert records["k"].groups == ["edge"]


def test_groups_and_labels_never_reach_the_host():
    inventory = FakeInventory(
        {"k": {"ip": "10.0.0.7", "groups": ["edge"], "labels": {"pod": "007"}}}
    )
    out = resolve_host_entry({"inventory": "k"}, inventory=inventory, element=Element(name="dut"))
    assert out.host_data == {"ip": "10.0.0.7"}
    assert "groups" not in out.ref.extra
    assert "labels" not in out.ref.extra
