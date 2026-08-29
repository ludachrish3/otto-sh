"""creds_file: one home for credentials, both backends (spec §9.4)."""

import json

import pytest

from otto.inventory import CredsOverlay, Inventory, InventoryError, load_creds_file

from .test_resolve import FakeInventory


def _creds_file(tmp_path, data):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(data))
    return p


def test_the_overlay_satisfies_the_protocol(tmp_path):
    """A wrapper is only a drop-in if it is still an Inventory."""
    inv = CredsOverlay(FakeInventory({}), path=tmp_path / "creds.json")
    assert isinstance(inv, Inventory)


def test_overlay_supplies_creds_from_the_file(tmp_path):
    path = _creds_file(tmp_path, {"k": [{"login": "root", "password": "x"}]})
    inv = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"]), path=path)
    assert "creds" in inv.supplies
    assert inv.label == "fake:mem"
    assert inv.creds_path == path
    rec = inv.lookup("k")
    assert [c.login for c in rec.creds] == ["root"]
    assert inv.list_keys() == ["k"]


def test_construction_does_no_io_and_a_broken_file_names_itself_at_first_use(tmp_path):
    inner = FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    inv = CredsOverlay(inner, path=tmp_path / "absent.json")
    with pytest.raises(InventoryError, match=r"creds_file .*absent.json"):
        inv.lookup("k")


def test_a_record_carrying_creds_beside_a_creds_file_is_an_error(tmp_path):
    path = _creds_file(tmp_path, {})
    inner = FakeInventory({"k": {"ip": "10.0.0.1", "creds": [{"login": "r", "password": "p"}]}})
    inv = CredsOverlay(inner, path=path)
    with pytest.raises(InventoryError, match="inventory key 'k': 'creds' come from creds_file"):
        inv.lookup("k")


def test_a_key_absent_from_the_creds_file_gets_no_creds(tmp_path):
    path = _creds_file(tmp_path, {})
    inv = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"]), path=path)
    assert inv.lookup("k").creds == []


def test_a_record_level_error_names_the_file_the_key_and_the_index(tmp_path):
    """``via`` without ``proxy`` fails CredSpec's model validator, whose loc is empty."""
    path = _creds_file(tmp_path, {"k": [{"login": "r", "via": "x"}]})
    with pytest.raises(InventoryError, match=r"creds.json.*key 'k'.*\[0\].*via"):
        load_creds_file(path)


def test_a_field_level_error_names_the_offending_field(tmp_path):
    """The other half of ``_compact``: a per-field error puts the FIELD left of the colon.

    Every other creds test raises through CredSpec's model validator, whose
    ``loc`` is ``()`` — so without this case the whole ``'.'.join(loc)`` half
    of the renderer is unpinned and could join to anything.
    """
    path = _creds_file(tmp_path, {"k": [{"login": 3}]})
    with pytest.raises(InventoryError, match=r"login: Input should be a valid string"):
        load_creds_file(path)


def test_the_creds_file_must_be_a_json_object(tmp_path):
    path = _creds_file(tmp_path, [])
    with pytest.raises(InventoryError, match="must be a JSON object"):
        load_creds_file(path)


def test_a_comment_key_is_skipped(tmp_path):
    path = _creds_file(tmp_path, {"_comment": "not a key", "k": []})
    assert load_creds_file(path) == {"k": []}


def test_a_non_list_creds_value_names_its_key(tmp_path):
    path = _creds_file(tmp_path, {"k": {"login": "r"}})
    with pytest.raises(InventoryError, match="key 'k': expected a list of creds"):
        load_creds_file(path)


def test_fingerprint_combines_inner_and_file(tmp_path):
    path = _creds_file(tmp_path, {})
    inv = CredsOverlay(FakeInventory({}), path=path)
    before = inv.fingerprint()
    path.write_text(json.dumps({"k": []}))
    assert inv.fingerprint() != before
    assert CredsOverlay(_NoFingerprint(), path=path).fingerprint() is None


def test_fingerprint_survives_a_missing_creds_file(tmp_path):
    inv = CredsOverlay(FakeInventory({}), path=tmp_path / "absent.json")
    assert inv.fingerprint() == "fake|creds:missing"


class _NoFingerprint(FakeInventory):
    def __init__(self):
        super().__init__({})

    def fingerprint(self):
        return None
