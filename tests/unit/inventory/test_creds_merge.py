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
        match=r"duplicate cred entry 'u' in the lab file layer \(inventory key 'k'\)",
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
        match=r"duplicate cred entry 'u' in the creds store layer \(inventory key 'k'\)",
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


def test_same_login_different_scope_are_two_entries_that_never_merge():
    lower = [{"login": "admin", "password": "unix"}]
    higher = [{"login": "admin", "password": "ftp-pw", "protocols": ["ftp"]}]
    assert merge_creds(lower, higher) == [
        {"login": "admin", "password": "ftp-pw", "protocols": ["ftp"]},
        {"login": "admin", "password": "unix"},
    ]


def test_a_matching_login_and_scope_composes_field_by_field_higher_wins():
    lower = [{"login": "admin", "password": "old", "protocols": ["ftp"]}]
    higher = [{"login": "admin", "protocols": ["ftp"]}]
    assert merge_creds(lower, higher) == [
        {"login": "admin", "password": "old", "protocols": ["ftp"]}
    ]
    higher = [{"login": "admin", "password": "new", "protocols": ["ftp"]}]
    assert merge_creds(lower, higher) == [
        {"login": "admin", "password": "new", "protocols": ["ftp"]}
    ]


def test_scope_order_does_not_change_identity():
    lower = [{"login": "root", "password": "p", "protocols": ["ssh", "telnet"]}]
    higher = [{"login": "root", "protocols": ["telnet", "ssh"]}]
    assert len(merge_creds(lower, higher)) == 1


def test_a_login_and_scope_repeated_within_one_layer_names_the_identity():
    with pytest.raises(
        InventoryError,
        match=r"duplicate cred entry 'u \[ftp\]' in the lab file layer \(inventory key 'k'\)",
    ) as exc:
        merge_creds(
            [],
            [
                {"login": "u", "protocols": ["ftp"]},
                {"login": "u", "protocols": ["ftp"], "password": "x"},
            ],
            key="k",
            higher_name="lab file",
        )
    assert "x" not in str(exc.value)


def test_a_non_list_protocols_names_the_field_not_the_value():
    with pytest.raises(
        InventoryError, match=r"'protocols' must be a list of strings.*creds store layer"
    ) as exc:
        merge_creds([{"login": "u", "protocols": "ftp"}], [], lower_name="creds store")
    assert "ftp" not in str(exc.value)
    with pytest.raises(
        InventoryError, match=r"'protocols' must be a list of strings.*creds store layer"
    ):
        merge_creds([{"login": "u", "protocols": ""}], [], lower_name="creds store")


def test_a_protocols_list_with_a_non_string_names_the_field_not_the_value():
    with pytest.raises(
        InventoryError, match=r"'protocols' must be a list of strings.*creds store layer"
    ):
        merge_creds([{"login": "u", "protocols": [1]}], [], lower_name="creds store")
