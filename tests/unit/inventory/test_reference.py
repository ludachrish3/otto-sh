"""One normalizer for every ``inventory`` reference form, bare or qualified (spec §5, References).

The normalizer, the emitted ``lab.json`` schema, the host spec, the runtime
join and the doctor all decide "is this a reference, and to what" — these
tests drive the same inputs through each, so a form one of them accepts and
another refuses fails here.
"""

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from otto.host.inventory_ref import (
    DEFAULT_INVENTORY,
    INVENTORY_REFERENCE_SCHEMA,
    InventoryReference,
    inventory_reference_schema,
    normalize_reference,
)
from otto.host.specs import UnixHostSpec
from otto.inventory import InventoryError, resolve_host_entry
from otto.inventory.doctor import referenced_references
from otto.models.inventory import FILLABLE_INVENTORY_FIELDS
from otto.models.jsonschema import build_schemas
from otto.models.lab import ElementSpec
from tests._fixtures.labdata import lab_json_v2

from .test_resolve import _DUT, FakeInventory

CASES = [
    ("edge-17", True),
    ({"name": "physical", "key": "edge-17"}, True),
    ("", False),
    (None, None),
    ({"name": "physical"}, False),
    ({"name": "p", "kye": "k"}, False),
    ({"name": "", "key": "k"}, False),
    ({"name": "p", "key": 5}, False),
    (7, False),
]
"""``(value, ok)``: ``True`` is a reference, ``False`` is refused, ``None`` references nothing."""

_INLINE = {"ip": "10.0.0.1"}
"""Every field an inline Unix entry must state that a record could otherwise fill."""


@pytest.fixture(scope="module")
def lab_validator() -> Draft202012Validator:
    return Draft202012Validator(build_schemas()["lab"])


def test_the_inline_fields_are_every_fillable_required_one():
    """The premise of ``inline_complete``: ``_INLINE`` is the whole inline-required set."""
    required = set(UnixHostSpec.model_json_schema()["required"])
    assert required & FILLABLE_INVENTORY_FIELDS == set(_INLINE)


@pytest.mark.parametrize(("value", "ok"), CASES)
@pytest.mark.parametrize("inline_complete", [True, False])
def test_the_normalizer_and_the_emitted_lab_schema_agree(lab_validator, value, ok, inline_complete):
    """The schema an editor checks ``lab.json`` against says what the loader says.

    ``None`` references nothing, so the entry is inline: valid exactly when it
    states every inline-required field.
    """
    host = {"inventory": value, "element": "test1", "docker_capable": True}
    if inline_complete:
        host.update(_INLINE)
    errors = list(lab_validator.iter_errors(lab_json_v2([host])))
    if ok is None:
        assert normalize_reference(value=value) is None
        assert (errors == []) is inline_complete, [e.message for e in errors]
    elif ok:
        assert normalize_reference(value=value) is not None
        assert errors == [], [e.message for e in errors]
    else:
        with pytest.raises(ValueError, match=r"^'inventory' must name a key"):
            normalize_reference(value=value)
        assert errors, f"the schema accepted {value!r}, which the normalizer refuses"


@pytest.mark.parametrize(("value", "ok"), CASES)
def test_the_host_spec_and_the_doctor_agree_with_the_normalizer(value, ok):
    """``HostSpec.inventory`` and the doctor's reference reading make the normalizer's call."""
    assert bool(referenced_references(element_lists=[[_el({"inventory": value})]])) is bool(ok)
    if ok is False:
        with pytest.raises(ValidationError, match="'inventory' must name a key"):
            UnixHostSpec.model_validate({**_INLINE, "inventory": value})
    else:
        spec = UnixHostSpec.model_validate({**_INLINE, "inventory": value})
        assert spec.inventory == value  # the stated form, never the normalized one


def test_bare_is_default_and_unqualified():
    assert DEFAULT_INVENTORY == "default"
    assert normalize_reference(value="edge-17") == InventoryReference(
        name="default", key="edge-17", qualified=False
    )
    assert normalize_reference(value={"name": "default", "key": "edge-17"}) == (
        InventoryReference(name="default", key="edge-17", qualified=True)
    )
    assert normalize_reference(value={"name": "physical", "key": "edge-17"}) == (
        InventoryReference(name="physical", key="edge-17", qualified=True)
    )


def test_the_refusal_ends_with_the_value():
    with pytest.raises(ValueError, match=r", got 7$"):
        normalize_reference(value=7)


def test_a_misspelled_qualified_ref_is_refused_naming_kye():
    with pytest.raises(ValueError, match="'kye'") as exc:
        normalize_reference(value={"name": "p", "kye": "k"})
    assert str(exc.value).startswith("'inventory' must name a key (a non-empty string)")
    assert str(exc.value).endswith(", got {'name': 'p', 'kye': 'k'}")


def test_the_schema_constant_is_the_non_null_form():
    assert inventory_reference_schema(nullable=False) == INVENTORY_REFERENCE_SCHEMA
    nullable = inventory_reference_schema(nullable=True)
    assert {"type": "null"} in nullable["anyOf"]
    assert {"type": "null"} not in INVENTORY_REFERENCE_SCHEMA["anyOf"]
    # a fresh document each call: a caller decorating one never edits another's
    assert inventory_reference_schema(nullable=False) is not INVENTORY_REFERENCE_SCHEMA


def _el(*hosts: dict) -> ElementSpec:
    return ElementSpec(name="a", labs=["l"], hosts=list(hosts))


def test_referenced_references_keep_qualification():
    refs = referenced_references(
        element_lists=[[_el({"inventory": "k1"}, {"inventory": {"name": "default", "key": "k1"}})]]
    )
    assert refs == [
        InventoryReference(name="default", key="k1", qualified=False),
        InventoryReference(name="default", key="k1", qualified=True),
    ]


def test_referenced_references_dedupe_sort_and_skip_the_rest():
    refs = referenced_references(
        element_lists=[
            [_el({"inventory": "k2"}, {"inventory": {"name": "physical", "key": "a"}})],
            [_el({"inventory": "k2"}, {"inventory": None}, {"inventory": ""}, {"ip": "10.0.0.9"})],
        ]
    )
    assert refs == [
        InventoryReference(name="default", key="k2", qualified=False),
        InventoryReference(name="physical", key="a", qualified=True),
    ]


def test_the_join_resolves_a_qualified_default_reference():
    out = resolve_host_entry(
        {"inventory": {"name": "default", "key": "k"}},
        inventory=FakeInventory({"k": {"ip": "10.0.0.7"}}, supplies=["ip"]),
        element=_DUT,
    )
    assert out.host_data == {"ip": "10.0.0.7"}
    assert out.ref.key == "k"


def test_the_join_refuses_a_named_inventory_it_has_no_view_of():
    with pytest.raises(InventoryError, match=r"'physical'.*needs a named-inventory view"):
        resolve_host_entry(
            {"inventory": {"name": "physical", "key": "k"}},
            inventory=FakeInventory({"k": {"ip": "10.0.0.7"}}, supplies=["ip"]),
            element=_DUT,
        )


def test_the_join_wraps_the_normalizer_s_refusal():
    with pytest.raises(InventoryError, match="'kye'"):
        resolve_host_entry(
            {"inventory": {"name": "p", "kye": "k"}}, inventory=FakeInventory({}), element=_DUT
        )
