"""The factory takes the element as one argument (spec 2026-09-05 §2.6, §5)."""

import pytest
from pydantic import ValidationError

from otto.host.element import Element
from otto.host.factory import create_host_from_dict, host_identity, validate_host_dict
from otto.inventory import resolve_host_entry
from otto.models.host import UnixHostSpec

ENTRY = {"ip": "10.0.0.1", "creds": [{"login": "r", "password": "p"}], "board": "cpu", "slot": 2}


def test_element_is_required_by_keyword():
    with pytest.raises(TypeError, match="element"):
        create_host_from_dict(dict(ENTRY))  # type: ignore[call-arg]


def test_built_host_shares_the_passed_instance():
    element = Element("Chassis", id=9, metadata={"row": 3}, resources=frozenset({"chassis-9"}))
    host = create_host_from_dict(dict(ENTRY), element=element)
    assert host.element is element
    assert host.id == "chassis_cpu2"
    assert host.name == "Chassis cpu 2"


def test_host_identity_matches_the_built_id():
    element = Element("Chassis")
    assert (
        host_identity(dict(ENTRY), element).id
        == create_host_from_dict(dict(ENTRY), element=element).id
    )


@pytest.mark.parametrize("key", ["element", "element_id"])
def test_host_spec_refuses_element_keys(key):
    with pytest.raises(ValidationError, match=key) as exc:
        UnixHostSpec.model_validate({**ENTRY, key: "x" if key == "element" else 1})
    # The type matters: a missing REQUIRED field's message quotes the whole
    # input dict, so ``match=`` alone passes while the field still exists.
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [("extra_forbidden", (key,))]


def test_validate_host_dict_needs_no_element():
    validate_host_dict(dict(ENTRY))


def test_flatten_is_gone():
    from otto.models.lab import ElementSpec

    assert not hasattr(ElementSpec, "flatten")


def test_resolve_host_entry_cross_checks_the_element_id():
    from tests.unit.inventory.test_resolve import FakeInventory  # the suite's existing double

    inv = FakeInventory({"k": {"ip": "10.0.0.9", "element_id": 2}}, supplies={"ip", "element_id"})
    ok = resolve_host_entry({"inventory": "k"}, inv, Element("dut", id=2))
    assert ok.host_data["ip"] == "10.0.0.9"
    with pytest.raises(Exception, match="element_id disagrees"):
        resolve_host_entry({"inventory": "k"}, inv, Element("dut", id=1))
