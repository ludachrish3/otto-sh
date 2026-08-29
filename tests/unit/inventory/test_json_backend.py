"""The json inventory backend — the stage-1 bridge file (spec §9.1)."""

import json

import pytest

from otto.inventory import (
    InventoryError,
    InventoryKeyError,
    JsonInventory,
    get_inventory_backend_class,
    parse_inventory_document,
)
from otto.models.inventory import FILLABLE_INVENTORY_FIELDS
from otto.testing import assert_inventory_conforms

_DOC = {
    "$schema": "~/.otto/schemas/inventory.schema.json",
    "_note": "comment space",
    "b": {
        "ip": "10.0.0.2",
        "interfaces": {"eth0": "192.168.1.2"},
        "site": "3",
        "extra": {"tag": "x"},
    },
    "a": {"ip": "10.0.0.1", "element_id": 4, "_why": "per-record comment"},
}


def _file(tmp_path, doc=_DOC):
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps(doc))
    return p


def test_json_is_the_registered_builtin():
    assert get_inventory_backend_class("json") is JsonInventory


def test_construction_does_no_io(tmp_path):
    path = tmp_path / "absent.json"
    inv = JsonInventory(path)  # no error until first use
    # Pinned on the PREFIX, not just a substring: this file's errors all read
    # `<path>: …` — the same shape the per-key ones below use — so a reader
    # scanning for the file does not have to know which layer refused it. A
    # `match=` regex alone would also accept the old `inventory file <path>: …`
    # wording, since the old text contains this one as a substring.
    with pytest.raises(InventoryError) as excinfo:
        inv.list_keys()
    message = str(excinfo.value)
    assert message.startswith(f"{path}: ")
    assert "inventory file " not in message


def test_lookup_list_and_label(tmp_path):
    path = _file(tmp_path)
    inv = JsonInventory(path)
    assert inv.label == f"json:{path}"
    assert inv.supplies == FILLABLE_INVENTORY_FIELDS
    assert inv.list_keys() == ["a", "b"]
    rec = inv.lookup("b")
    assert rec.interfaces["eth0"].ip == "192.168.1.2"
    assert rec.site == 3
    assert rec.extra == {"tag": "x"}
    assert inv.lookup("a").element_id == 4
    with pytest.raises(InventoryKeyError, match="'nope' not found in inventory 'json:"):
        inv.lookup("nope")


def test_parsed_once_per_process(tmp_path):
    path = _file(tmp_path)
    inv = JsonInventory(path)
    inv.lookup("a")
    path.write_text(json.dumps({"a": {"ip": "9.9.9.9"}}))
    assert inv.lookup("a").ip == "10.0.0.1"  # held in memory; a new process sees the rewrite


def test_fingerprint_moves_on_rewrite(tmp_path):
    path = _file(tmp_path)
    inv = JsonInventory(path)
    before = inv.fingerprint()
    assert before is not None
    assert str(path) in before
    path.write_text(json.dumps(_DOC) + " ")  # size changes even if mtime granularity does not
    assert inv.fingerprint() != before


def test_bad_record_names_file_key_and_field(tmp_path):
    path = _file(tmp_path, {"k": {"ip": "10.0.0.1", "shelf": "top"}})
    with pytest.raises(InventoryError, match=r"inventory.json: key 'k'.*shelf"):
        JsonInventory(path).lookup("k")


def test_non_object_document_is_refused(tmp_path):
    path = _file(tmp_path, [1, 2])
    with pytest.raises(InventoryError, match="must be a JSON object"):
        JsonInventory(path).list_keys()


def test_non_object_record_value_is_refused_naming_the_key(tmp_path):
    path = _file(tmp_path, {"k": 5})
    with pytest.raises(InventoryError, match=r"key 'k': expected a record object"):
        JsonInventory(path).list_keys()


def test_fingerprint_of_a_missing_file_differs_in_shape_from_a_present_file(tmp_path):
    absent = tmp_path / "absent.json"
    missing_fp = JsonInventory(absent).fingerprint()
    assert missing_fp == f"{absent}|missing"
    present_fp = JsonInventory(_file(tmp_path)).fingerprint()
    assert present_fp is not None
    assert not present_fp.endswith("|missing")


def test_parse_inventory_document_guards_a_non_string_key():
    """Mirror ``creds.py``'s non-str-key guard on the comment-key skip.

    ``json.loads()`` output always has string keys, so this path is only
    reachable when ``parse_inventory_document`` is called directly — as the
    Task 9 snapshot cache will — with a document that skipped JSON. Without
    the ``isinstance(key, str)`` guard, ``key.startswith("_")`` raises
    ``AttributeError`` on a non-str key instead of falling through to normal
    record processing.
    """
    records = parse_inventory_document(
        {0: {"ip": "10.0.0.1"}}, source="snapshot", supplies=FILLABLE_INVENTORY_FIELDS
    )
    assert records[0].ip == "10.0.0.1"


def test_a_record_field_outside_supplies_is_refused_naming_key_and_field(tmp_path):
    path = _file(tmp_path, {"k": {"ip": "10.0.0.1", "os_version": "22.04"}})
    pattern = r"key 'k': 'os_version' is not in this inventory's supplies"
    with pytest.raises(InventoryError, match=pattern):
        JsonInventory(path, supplies=["ip", "site"]).lookup("k")


def test_keys_and_extra_are_allowed_whatever_supplies_says(tmp_path):
    path = _file(tmp_path, {"k": {"ip": "10.0.0.1", "element_id": 1, "extra": {"a": 1}}})
    assert JsonInventory(path, supplies=["ip"]).lookup("k").element_id == 1


def test_supplies_without_ip_is_refused_at_construction(tmp_path):
    with pytest.raises(InventoryError, match="must include 'ip'"):
        JsonInventory(tmp_path / "x.json", supplies=["site"])


def test_conforms(tmp_path):
    assert_inventory_conforms(JsonInventory(_file(tmp_path)), expected_keys=["a", "b"])
