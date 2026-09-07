"""merge_creds: two cred layers composed by login (spec 2026-09-06 creds-store §3, §6.1)."""

import pytest

from otto.inventory import InventoryError, merge_creds

STORE = [{"login": "vagrant", "password": "vagrant"}, {"login": "test", "password": "Password1"}]


def test_higher_layer_logins_come_first_in_their_order_then_lower_only_logins():
    higher = [{"login": "root", "proxy": "sudo-root", "via": "vagrant"}]
    merged = merge_creds(STORE, higher)
    assert [c["login"] for c in merged] == ["root", "vagrant", "test"]
    assert merged[0] == {"login": "root", "proxy": "sudo-root", "via": "vagrant"}
    assert merged[1:] == STORE


def test_a_login_only_placeholder_fixes_the_order_and_takes_every_field_from_below():
    higher = [
        {"login": "vagrant"},
        {"login": "test"},
        {"login": "root", "proxy": "sudo-root", "via": "vagrant"},
    ]
    merged = merge_creds(STORE, higher)
    assert merged == [
        {"login": "vagrant", "password": "vagrant"},
        {"login": "test", "password": "Password1"},
        {"login": "root", "proxy": "sudo-root", "via": "vagrant"},
    ]


def test_the_higher_layer_overrides_a_stated_field_and_null_removes_nothing():
    lower = [{"login": "u", "password": "old", "proxy": "su", "via": "admin"}]
    higher = [{"login": "u", "password": "new", "proxy": None}]
    assert merge_creds(lower, higher) == [
        {"login": "u", "password": "new", "proxy": "su", "via": "admin"}
    ]


def test_an_empty_layer_on_either_side_is_the_other_layer():
    assert merge_creds([], STORE) == STORE
    assert merge_creds(STORE, []) == STORE
    assert merge_creds([], []) == []


def test_a_login_repeated_within_one_layer_names_the_layer_and_the_key():
    with pytest.raises(
        InventoryError,
        match=r"duplicate cred login 'u' in the lab file layer \(inventory key 'k'\)",
    ) as exc:
        merge_creds(
            [],
            [{"login": "u"}, {"login": "u", "password": "hunter2"}],
            key="k",
            higher_name="lab file",
        )
    assert "hunter2" not in str(exc.value)


def test_a_login_repeated_within_the_lower_layer_also_names_the_layer_and_the_key():
    with pytest.raises(
        InventoryError,
        match=r"duplicate cred login 'u' in the creds store layer \(inventory key 'k'\)",
    ):
        merge_creds([{"login": "u"}, {"login": "u"}], [], key="k", lower_name="creds store")


def test_an_entry_without_a_login_names_its_fields_never_its_values():
    with pytest.raises(
        InventoryError, match=r"without a 'login' string in the creds store layer.*\['password'\]"
    ) as exc:
        merge_creds([{"password": "hunter2"}], [], lower_name="creds store")
    assert "hunter2" not in str(exc.value)
    with pytest.raises(InventoryError, match=r"without a 'login' string"):
        merge_creds([], [{"login": ""}])
    with pytest.raises(InventoryError, match=r"without a 'login' string.*str"):
        merge_creds([], ["not-a-dict"])


def test_inputs_are_not_mutated():
    lower = [{"login": "u", "password": "p"}, {"login": "only-lower", "password": "q"}]
    higher = [{"login": "u", "proxy": "su", "via": None}]
    merged = merge_creds(lower, higher)
    assert lower == [{"login": "u", "password": "p"}, {"login": "only-lower", "password": "q"}]
    assert higher == [{"login": "u", "proxy": "su", "via": None}]
    assert all(m is not e for m in merged for e in lower + higher)
