"""resolve_host_entry: the join between a lab entry and its inventory record (spec §2, §5, §6)."""

import pytest

from otto.host.inventory_ref import InventoryRef
from otto.inventory import (
    Inventory,
    InventoryError,
    InventoryKeyError,
    ResolvedEntry,
    check_supplies,
    resolve_host_entry,
)
from otto.models.inventory import FILLABLE_INVENTORY_FIELDS, InventoryRecord


class FakeInventory:
    """The smallest Inventory: a dict of records and a declared supplies set."""

    def __init__(self, records: dict[str, dict], supplies=None, label="fake:mem"):
        self._records = {k: InventoryRecord.model_validate(v) for k, v in records.items()}
        self.supplies = check_supplies(supplies)
        self.label = label

    def lookup(self, key):
        try:
            return self._records[key]
        except KeyError:
            raise InventoryKeyError(key, self.label) from None

    def list_keys(self):
        return sorted(self._records)

    def fingerprint(self):
        return "fake"


_REC = {
    "ip": "10.0.0.7",
    "interfaces": {"eth0": "192.168.1.7"},
    "is_virtual": True,
    "site": "lab-a",
    "rack": 3,
    "creds": [{"login": "u", "password": "p"}],
    "extra": {"asset": "A-7"},
}


def test_fake_satisfies_the_protocol():
    assert isinstance(FakeInventory({}), Inventory)


def test_inline_entry_passes_through_untouched():
    entry = {"ip": "1.2.3.4", "element": "dut"}
    out = resolve_host_entry(entry, FakeInventory({}))
    assert out == ResolvedEntry(host_data={"ip": "1.2.3.4", "element": "dut"}, ref=InventoryRef())
    assert out.host_data is not entry  # a new dict, never the caller's


def test_a_null_reference_is_inline_too():
    """R7: ``"inventory": null`` references NOTHING — the key is dropped, not an error.

    A host entry that never mentioned the inventory validates as
    ``{"inventory": None, ...}`` under schema-legal round-tripping, and
    ``otto.host.factory.reject_unresolved_reference`` reads it the same way.
    """
    entry = {"inventory": None, "ip": "1.2.3.4", "element": "dut"}
    out = resolve_host_entry(entry, FakeInventory({}))
    assert out == ResolvedEntry(host_data={"ip": "1.2.3.4", "element": "dut"}, ref=InventoryRef())
    assert out.host_data is not entry


def test_referenced_entry_is_filled_from_the_record_and_keeps_otto_fields():
    inv = FakeInventory({"k": _REC})
    entry = {"inventory": "k", "element": "dut", "os_type": "unix", "hop": "gw"}
    out = resolve_host_entry(entry, inv)
    assert "inventory" not in out.host_data
    assert out.host_data["ip"] == "10.0.0.7"
    assert out.host_data["interfaces"] == {"eth0": {"ip": "192.168.1.7"}}
    assert out.host_data["is_virtual"] is True
    assert out.host_data["site"] == "lab-a"
    assert out.host_data["rack"] == 3
    assert out.host_data["creds"] == [{"login": "u", "password": "p"}]
    assert out.host_data["hop"] == "gw"
    assert out.host_data["os_type"] == "unix"
    assert out.ref == InventoryRef(key="k", backend="fake:mem", extra={"asset": "A-7"})


def test_a_none_in_the_record_is_not_stated():
    inv = FakeInventory({"k": {"ip": "10.0.0.7"}})  # os_version None → entry default applies
    out = resolve_host_entry({"inventory": "k", "element": "dut"}, inv)
    assert "os_version" not in out.host_data
    assert "creds" not in out.host_data  # never stated, so the host's own default applies


def test_a_field_stated_at_its_default_value_is_still_stated():
    """A field the record SET counts as stated, even when it equals the default.

    ``exclude_defaults`` compares VALUES, so it drops an explicit
    ``is_virtual: False`` and an explicit ``creds: []`` — the two cases where a
    deployment says "this one really is bare metal / really has no creds" and
    means to override a host default. ``exclude_unset`` asks the question the
    rule actually poses.
    """
    inv = FakeInventory({"k": {"ip": "10.0.0.7", "is_virtual": False, "creds": []}})
    out = resolve_host_entry({"inventory": "k", "element": "dut"}, inv)
    assert out.host_data["is_virtual"] is False
    assert out.host_data["creds"] == []
    # ... and the contrast: the same two fields, left unstated, stay absent.
    silent = FakeInventory({"k": {"ip": "10.0.0.7"}})
    bare = resolve_host_entry({"inventory": "k", "element": "dut"}, silent)
    assert "is_virtual" not in bare.host_data
    assert "creds" not in bare.host_data


@pytest.mark.parametrize("field", sorted(FILLABLE_INVENTORY_FIELDS))
def test_every_supplied_field_inline_beside_a_reference_is_an_error(field):
    inv = FakeInventory({"k": _REC})  # default supplies = every fillable field
    with pytest.raises(InventoryError, match=f"'{field}' is inventory-owned.*key 'k'"):
        resolve_host_entry({"inventory": "k", "element": "dut", field: "x"}, inv)


def test_a_null_inline_value_is_not_a_collision():
    """R7 again, one level down: ``"site": null`` beside a reference states nothing.

    A lab entry round-tripped through the schema carries every unset field as
    ``null``; a membership-only collision check would call each of them an
    inventory-owned field declared inline and refuse the whole entry.
    """
    inv = FakeInventory({"k": _REC})
    out = resolve_host_entry({"inventory": "k", "element": "dut", "site": None}, inv)
    assert out.host_data["site"] == "lab-a"


def test_an_unsupplied_field_inline_is_accepted_and_kept():
    inv = FakeInventory({"k": _REC}, supplies=["ip", "site"])
    out = resolve_host_entry({"inventory": "k", "element": "dut", "sw_version": "9.9"}, inv)
    assert out.host_data["sw_version"] == "9.9"
    assert out.host_data["ip"] == "10.0.0.7"
    assert out.host_data["site"] == "lab-a"
    assert "rack" not in out.host_data  # record has it, deployment does not supply it


def test_unknown_key_raises_inventory_key_error():
    with pytest.raises(
        InventoryKeyError, match="inventory key 'nope' not found in inventory 'fake:mem'"
    ):
        resolve_host_entry({"inventory": "nope", "element": "dut"}, FakeInventory({}))


def test_no_inventory_configured_names_both_settings_files():
    with pytest.raises(InventoryError, match=r"~/.otto/settings.toml.*\.otto/settings.toml"):
        resolve_host_entry({"inventory": "k", "element": "dut"}, None)


@pytest.mark.parametrize("bad", ["", 3, 0, []])
def test_inventory_key_must_be_a_nonempty_string(bad):
    """``None`` is inline (above); every OTHER non-string — ``""`` included — is loud.

    ``""`` and ``0`` are the discriminators: a guard spelled ``if not
    host_data.get("inventory")`` would swallow both as "inline".
    """
    with pytest.raises(InventoryError, match="'inventory' must name a key") as exc:
        resolve_host_entry({"inventory": bad, "element": "dut"}, FakeInventory({}))
    assert repr(bad) in str(exc.value)


def test_element_id_is_cross_checked_never_filled():
    inv = FakeInventory({"k": {"ip": "10.0.0.7", "element_id": 2}}, supplies=["ip", "element_id"])
    ok = resolve_host_entry({"inventory": "k", "element": "dut", "element_id": 2}, inv)
    assert ok.host_data["element_id"] == 2
    absent = resolve_host_entry({"inventory": "k", "element": "dut"}, inv)
    assert "element_id" not in absent.host_data  # a key is never copied
    with pytest.raises(
        InventoryError, match=r"element_id.*lab file says 1.*inventory key 'k' says 2"
    ):
        resolve_host_entry({"inventory": "k", "element": "dut", "element_id": 1}, inv)


def test_element_id_is_not_checked_when_the_inventory_does_not_supply_it():
    inv = FakeInventory({"k": {"ip": "10.0.0.7", "element_id": 2}}, supplies=["ip"])
    out = resolve_host_entry({"inventory": "k", "element": "dut", "element_id": 1}, inv)
    assert out.host_data["element_id"] == 1


def test_check_supplies_rules():
    assert check_supplies(None) == FILLABLE_INVENTORY_FIELDS
    assert check_supplies(["ip", "site"]) == frozenset({"ip", "site"})
    with pytest.raises(InventoryError, match="must include 'ip'"):
        check_supplies(["site"])
    with pytest.raises(InventoryError, match=r"not a record field.*'bogus'"):
        check_supplies(["ip", "bogus"])
    # a key IS allowed: listing it means "this deployment asserts element_id
    # and wants it cross-checked"
    assert check_supplies(["ip", "element_id"]) == frozenset({"ip", "element_id"})
