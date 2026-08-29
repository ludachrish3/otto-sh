"""assert_inventory_conforms catches the contract violations it exists for."""

import json

import pytest

from otto.inventory import InventoryKeyError, JsonInventory, check_supplies
from otto.models.inventory import InventoryRecord
from otto.testing import assert_inventory_conforms


class _Good:
    label = "good:mem"
    supplies = check_supplies(["ip"])

    def lookup(self, key):
        if key != "k":
            raise InventoryKeyError(key, self.label)
        return InventoryRecord(ip="10.0.0.1")

    def list_keys(self):
        return ["k"]

    def fingerprint(self):
        return None


def test_good_backend_passes():
    assert_inventory_conforms(_Good(), expected_keys=["k"])


def test_record_outside_supplies_fails():
    class Leaky(_Good):
        def lookup(self, key):
            if key != "k":
                raise InventoryKeyError(key, self.label)
            return InventoryRecord(ip="10.0.0.1", site="x")  # site is not supplied

    with pytest.raises(AssertionError, match=r"outside supplies.*site"):
        assert_inventory_conforms(Leaky())


def test_unknown_key_must_raise_inventory_key_error():
    class Lenient(_Good):
        def lookup(self, key):
            return InventoryRecord(ip="10.0.0.1")

    with pytest.raises(AssertionError, match="InventoryKeyError"):
        assert_inventory_conforms(Lenient())


def test_lookup_must_be_idempotent():
    class Drifting(_Good):
        n = 0

        def lookup(self, key):
            if key != "k":
                raise InventoryKeyError(key, self.label)
            self.n += 1
            return InventoryRecord(ip=f"10.0.0.{self.n}")

    # Anchored on the guard's own message, not a bare "idempotent": the
    # ExpectCollector appends the caller's locals to every failure report,
    # and `repr(Drifting())` is
    # "...test_lookup_must_be_idempotent.<locals>.Drifting object at 0x...",
    # which itself contains the substring "idempotent" — a loose match="idempotent"
    # is satisfied by that locals dump even with the idempotence check deleted.
    with pytest.raises(
        AssertionError, match=r"must be idempotent \(equal record on a second call\)"
    ):
        assert_inventory_conforms(Drifting())


def test_expected_key_missing_fails():
    # Anchored on "did not resolve", not the bare "expected key 'zz'": since
    # the R11 membership check landed, that looser substring is ALSO emitted
    # by "expected key 'zz' to appear in list_keys()" — a different guard,
    # over in test_expected_key_resolves_but_is_missing_from_list_keys_fails
    # below. A loose match here would pass with either guard swallowed,
    # leaving the resolve guard with no test that can actually fail it.
    with pytest.raises(AssertionError, match=r"expected key 'zz' did not resolve"):
        assert_inventory_conforms(_Good(), expected_keys=["zz"])


def test_expected_key_resolves_but_is_missing_from_list_keys_fails():
    """R11: expected_keys must ALSO appear in list_keys(), not just resolve.

    ``Hidden`` answers ``lookup("zz")`` correctly but never lists it — the
    kind of backend that would silently defeat `--lab`-scoped completion or
    any other caller that enumerates keys instead of guessing them.
    """

    class Hidden(_Good):
        def lookup(self, key):
            if key not in ("k", "zz"):
                raise InventoryKeyError(key, self.label)
            return InventoryRecord(ip="10.0.0.9")

    with pytest.raises(AssertionError, match=r"expected key 'zz' to appear in list_keys\(\)"):
        assert_inventory_conforms(Hidden(), expected_keys=["zz"])


def test_list_keys_raising_is_reported_not_escaped(tmp_path):
    """A backend's list_keys() failing must surface as AssertionError, not escape raw.

    JsonInventory at a path that was never written raises InventoryError from
    list_keys() (the lazy parse fails); the helper must catch that instead of
    letting it propagate past its own AssertionError contract.
    """
    inv = JsonInventory(tmp_path / "absent.json")
    with pytest.raises(AssertionError, match=r"list_keys\(\) raised InventoryError"):
        assert_inventory_conforms(inv)


def test_positive_control_needs_a_backend_that_honours_inventory(tmp_path):
    """The repository=/lab= arm: a backend that drops ``inventory=`` on the floor fails.

    ``Ignores`` loads the lab correctly — but WITHOUT the inventory, so its
    referenced entry raises and the "loaded WITH the inventory" rule fires.
    """
    from otto.inventory import JsonInventory as _JsonInventory
    from otto.labs.json_repository import JsonFileLabRepository

    (tmp_path / "lab.json").write_text(
        json.dumps(
            {
                "labs": {"l": {}},
                "elements": [
                    {
                        "name": "dut",
                        "labs": ["l"],
                        "hosts": [
                            {
                                "inventory": "k",
                                "os_type": "unix",
                                "creds": [{"login": "u", "password": "p"}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    (tmp_path / "inventory.json").write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    inv = _JsonInventory(tmp_path / "inventory.json", supplies=["ip"])
    honours = JsonFileLabRepository(search_paths=[tmp_path])
    assert_inventory_conforms(inv, repository=honours, lab="l")

    class Ignores:  # a backend that drops inventory= on the floor
        def load_lab(self, name, preferences=None, inventory=None):
            return JsonFileLabRepository(search_paths=[tmp_path]).load_lab(name)

        def list_labs(self):
            return ["l"]

    with pytest.raises(AssertionError, match=r"failed to load WITH the inventory"):
        assert_inventory_conforms(inv, repository=Ignores(), lab="l")
