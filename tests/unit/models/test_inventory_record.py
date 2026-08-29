"""InventoryRecord: the boundary spec for one inventory record (spec §4, §12)."""

import pytest
from pydantic import ValidationError

from otto.models.host import HostSpec, UnixHostSpec
from otto.models.inventory import (
    FILLABLE_INVENTORY_FIELDS,
    INVENTORY_KEY_FIELDS,
    InventoryRecord,
    coerce_digit_string,
)

UNIX_ONLY_RECORD_FIELDS = {"hw_version", "sw_version"}
"""``InventoryRecord`` fields with no counterpart on the abstract ``HostSpec``
base: ``hw_version``/``sw_version`` are declared only on ``UnixHostSpec`` —
an existing, deliberate restriction (lab-config.md marks them "Unix hosts
only") this task does not widen to embedded hosts. Named explicitly, not
folded into a wider union, so a *new* stray field is still caught."""


def test_every_record_field_except_extra_is_a_hostspec_field():
    """Spec §4: record field names are HostSpec field names, 1:1 — no mapping table in core.

    ``hw_version``/``sw_version`` are the sole, named exception (see
    ``UNIX_ONLY_RECORD_FIELDS``); every other field must be on the shared
    base so a stray field anywhere else still fails this guard.
    """
    record_fields = set(InventoryRecord.model_fields) - {"extra"} - UNIX_ONLY_RECORD_FIELDS
    assert record_fields <= set(HostSpec.model_fields)
    assert set(UnixHostSpec.model_fields) >= UNIX_ONLY_RECORD_FIELDS  # not vacuous
    # The carve-out must stay a carve-out. If the open R5 question is ever
    # answered by widening hw_version/sw_version onto the base, this goes red
    # and the exception retires loudly instead of sitting here dead.
    assert not (UNIX_ONLY_RECORD_FIELDS & set(HostSpec.model_fields))


def test_fillable_fields_are_derived_not_listed():
    expected = frozenset(InventoryRecord.model_fields) - {"element_id", "extra"}
    assert expected == FILLABLE_INVENTORY_FIELDS
    assert "ip" in FILLABLE_INVENTORY_FIELDS
    assert frozenset({"element_id"}) == INVENTORY_KEY_FIELDS
    assert not (FILLABLE_INVENTORY_FIELDS & INVENTORY_KEY_FIELDS)


def test_ip_is_required_and_extra_keys_are_forbidden():
    # Anchored, like test_hostspec_forbids_unknown_field in test_host_specs.py:
    # a bare substring couldn't discriminate "ip" from other field-name echoes.
    with pytest.raises(ValidationError, match=r"ip\s+Field required"):
        InventoryRecord.model_validate({})
    with pytest.raises(ValidationError, match=r"(?m)^bogus\n\s+Extra inputs are not permitted"):
        InventoryRecord.model_validate({"ip": "10.0.0.1", "bogus": 1})


def test_comment_keys_are_dropped():
    rec = InventoryRecord.model_validate({"ip": "10.0.0.1", "_note": "ignored"})
    assert rec.ip == "10.0.0.1"


@pytest.mark.parametrize(
    ("raw", "expected"), [(3, 3), ("3", 3), ("R3", "R3"), ("lab-a", "lab-a"), ("03", 3)]
)
def test_site_and_rack_coerce_digit_strings(raw, expected):
    rec = InventoryRecord.model_validate({"ip": "10.0.0.1", "site": raw, "rack": raw})
    assert rec.site == expected
    assert rec.rack == expected


def test_shelf_and_slot_reject_non_integers_and_negatives():
    with pytest.raises(ValidationError, match="shelf"):
        InventoryRecord.model_validate({"ip": "10.0.0.1", "shelf": "top"})
    with pytest.raises(ValidationError, match="slot"):
        InventoryRecord.model_validate({"ip": "10.0.0.1", "slot": -1})
    with pytest.raises(ValidationError, match="shelf"):
        InventoryRecord.model_validate({"ip": "10.0.0.1", "shelf": -1})


def test_coerce_digit_string_is_ascii_digits_only():
    assert coerce_digit_string("42") == 42
    # fullwidth digits: isdigit() is True, isascii() is False — must stay a string.
    assert coerce_digit_string("４２") == "４２"  # noqa: RUF001 — the fullwidth digits are the point
    assert coerce_digit_string("4a") == "4a"
    assert coerce_digit_string(7) == 7


def test_creds_and_interfaces_validate_through_the_host_specs():
    rec = InventoryRecord.model_validate(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "u", "password": "p"}],
            "interfaces": {"eth0": "192.168.1.5"},
        }
    )
    assert rec.creds[0].login == "u"
    assert rec.interfaces["eth0"].ip == "192.168.1.5"
    with pytest.raises(ValidationError, match="via"):
        InventoryRecord.model_validate({"ip": "10.0.0.1", "creds": [{"login": "u", "via": "x"}]})
