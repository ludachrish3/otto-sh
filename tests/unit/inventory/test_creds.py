"""CredsOverlay: the store→record merge, outermost on the inventory (spec 2026-09-06 §5.4)."""

import json

import pytest

from otto.creds import CredsError, JsonCredsStore
from otto.inventory import CredsOverlay, Inventory, InventoryError

from .test_resolve import FakeInventory


def _store(tmp_path, data):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(data))
    return JsonCredsStore(p)


class _Raising:
    label = "boom:store"

    def lookup(self, key):
        raise CredsError("boom:store: service unreachable")

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_the_overlay_satisfies_the_protocol(tmp_path):
    assert isinstance(CredsOverlay(FakeInventory({}), store=_store(tmp_path, {})), Inventory)


def test_overlay_supplies_creds_from_the_store(tmp_path):
    store = _store(tmp_path, {"k": [{"login": "root", "password": "x"}]})
    inv = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"]), store=store)
    assert "creds" in inv.supplies
    assert inv.label == "fake:mem"
    assert inv.store is store
    rec = inv.lookup("k")
    assert [(c.login, c.password) for c in rec.creds] == [("root", "x")]
    assert "creds" in rec.model_fields_set  # the join reads STATED fields; this one is
    assert inv.list_keys() == ["k"]


def test_record_creds_layer_over_store_creds_by_login(tmp_path):
    """Spec §3 statement 3 at the first merge: the record is the higher layer, and leads."""
    store = _store(
        tmp_path, {"k": [{"login": "vagrant", "password": "v"}, {"login": "test", "password": "t"}]}
    )
    inner = FakeInventory(
        {
            "k": {
                "ip": "10.0.0.1",
                "creds": [
                    {"login": "test", "password": "override"},
                    {"login": "svc", "proxy": "su", "via": "vagrant"},
                ],
            }
        }
    )
    rec = CredsOverlay(inner, store=store).lookup("k")
    assert [(c.login, c.password, c.proxy) for c in rec.creds] == [
        ("test", "override", None),
        ("svc", None, "su"),
        ("vagrant", "v", None),
    ]


def test_a_key_absent_from_the_store_keeps_the_records_own_creds(tmp_path):
    store = _store(tmp_path, {})
    inner = FakeInventory({"k": {"ip": "10.0.0.1", "creds": [{"login": "r", "password": "p"}]}})
    assert [c.login for c in CredsOverlay(inner, store=store).lookup("k").creds] == ["r"]
    bare = FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    assert CredsOverlay(bare, store=store).lookup("k").creds == []


def test_construction_does_no_io_and_a_broken_store_names_itself_at_first_use(tmp_path):
    inner = FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    inv = CredsOverlay(inner, store=JsonCredsStore(tmp_path / "absent.json"))
    with pytest.raises(InventoryError, match=r"absent\.json: "):
        inv.lookup("k")


def test_a_store_error_is_re_raised_as_an_inventory_error_with_its_text(tmp_path):
    inv = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"]), store=_Raising())
    with pytest.raises(InventoryError, match="boom:store: service unreachable"):
        inv.lookup("k")


class _ConstructedEntry:
    """A ``CredSpec.model_construct``-style entry: a bad field type, no validation run.

    A real ``CredSpec.model_construct(login="root", password=123)`` reaches
    the same broken dict, but its OWN ``model_dump`` warns on the type
    mismatch mid-serialization — a warning this repo's suite promotes to an
    error (``filterwarnings = ["error"]``), which would fail the test before
    it ever reached the overlay's compose step. A plain stand-in with a
    hand-written ``model_dump`` reaches the identical merged dict without
    going through pydantic's serializer at all.
    """

    def model_dump(self, **kwargs):
        return {"login": "root", "password": 123}


class _ConstructedEntries:
    """A store that hands out entries ``merge_creds`` cannot validate (reviewer's probe)."""

    label = "bad:store"

    def lookup(self, key):
        return [_ConstructedEntry()]

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_a_merged_entry_that_fails_validation_names_the_login(tmp_path):
    """spec 2026-09-06 creds-store §6.1: the compose error names the key AND the login.

    Field-level failures (``password: 123``) have no ``CredSpec``-authored
    message to carry the login incidentally — only the entry-level try/except
    around the revalidation can name it.
    """
    inner = FakeInventory({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    with pytest.raises(InventoryError) as excinfo:
        CredsOverlay(inner, store=_ConstructedEntries()).lookup("k")
    message = str(excinfo.value)
    assert "'k'" in message
    assert "'root'" in message


def test_a_duplicate_login_in_the_record_layer_names_the_key(tmp_path):
    inner = FakeInventory({"k": {"ip": "10.0.0.1", "creds": [{"login": "u"}, {"login": "u"}]}})
    with pytest.raises(
        InventoryError,
        match=r"duplicate cred login 'u' in the inventory record layer \(inventory key 'k'\)",
    ):
        CredsOverlay(inner, store=_store(tmp_path, {})).lookup("k")


def test_fingerprint_combines_inner_and_store(tmp_path):
    store = _store(tmp_path, {})
    inv = CredsOverlay(FakeInventory({}), store=store)
    before = inv.fingerprint()
    assert before is not None
    assert before.startswith("fake|creds:")
    (tmp_path / "creds.json").write_text(json.dumps({"k": []}))
    assert inv.fingerprint() != before
    assert CredsOverlay(_NoFingerprint(), store=store).fingerprint() is None
    assert CredsOverlay(FakeInventory({}), store=_Raising()).fingerprint() is None  # opaque store


def test_fingerprint_survives_a_missing_store_file(tmp_path):
    inv = CredsOverlay(FakeInventory({}), store=JsonCredsStore(tmp_path / "absent.json"))
    assert inv.fingerprint() == f"fake|creds:{tmp_path / 'absent.json'}|missing"


class _NoFingerprint(FakeInventory):
    def __init__(self):
        super().__init__({})

    def fingerprint(self):
        return None
