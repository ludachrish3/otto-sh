"""merge_creds: two cred layers composed by login (spec 2026-09-06 creds-store §3, §6.1)."""

import json

import pytest
from pydantic import SecretStr

from otto.creds import JsonCredsStore
from otto.host.element import Element
from otto.inventory import CredsOverlay, InventoryError, merge_creds, resolve_host_entry
from otto.logger.formatters import print_crash_traceback
from otto.models.base import CredSpec

from .test_resolve import FakeInventory

PLANTED = "PLANTED-SECRET-7f3a"

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


# The merge trap (#487): the overlay and the join compose creds through a dump.
# A JSON dump masks a SecretStr, so every merged password would become the mask;
# a revealing dump leaves the plain password in a merge frame's locals. Both
# dump in python mode, which keeps the SecretStr itself.


def _overlay(tmp_path, store_creds, record_creds):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"k": store_creds}))
    inner = FakeInventory({"k": {"ip": "10.0.0.1", "creds": record_creds}})
    return CredsOverlay(inner, store=JsonCredsStore(path))


def test_merge_keeps_passwords_that_differ_only_by_value(tmp_path):
    overlay = _overlay(
        tmp_path,
        [{"login": "root", "password": "A", "protocols": ["ssh"]}],
        [{"login": "root", "password": "B", "protocols": ["telnet"]}],
    )
    merged = overlay.lookup("k").creds
    assert {c.password.get_secret_value() for c in merged} == {"A", "B"}


def test_the_higher_layers_password_wins_with_its_real_value(tmp_path):
    overlay = _overlay(
        tmp_path,
        [{"login": "root", "password": PLANTED}],
        [{"login": "root", "password": "B"}],
    )
    [merged] = overlay.lookup("k").creds
    assert merged.password.get_secret_value() == "B"


def test_empty_password_survives_merge(tmp_path):
    merged = _overlay(tmp_path, [{"login": "root", "password": ""}], []).lookup("k").creds
    assert merged[0].password.get_secret_value() == ""


def test_the_join_keeps_the_record_password_under_lab_file_creds():
    inv = FakeInventory({"k": {"ip": "10.0.0.7", "creds": [{"login": "u", "password": PLANTED}]}})
    entry = {
        "inventory": "k",
        "os_type": "unix",
        "creds": [{"login": "root", "proxy": "su", "via": "u"}],
    }
    out = resolve_host_entry(entry, inv, Element("dut"))
    assert {"login": "u", "password": SecretStr(PLANTED)} in out.host_data["creds"]


def _crash_text(exc: BaseException, capsys) -> str:
    """Render *exc* exactly as otto renders a crash: Rich, every frame's locals shown."""
    print_crash_traceback(exc)
    return capsys.readouterr().err


def test_a_join_merge_error_leaves_no_plain_password_in_the_crash_locals(capsys):
    inv = FakeInventory({"k": {"ip": "10.0.0.7", "creds": [{"login": "u", "password": PLANTED}]}})
    entry = {"inventory": "k", "creds": [{"login": "root"}, {"login": "root"}]}
    with pytest.raises(InventoryError, match="duplicate cred entry 'root'") as exc:
        resolve_host_entry(entry, inv, Element("dut"))
    text = _crash_text(exc.value, capsys)
    assert "stated" in text  # the frame holding the dumped record is shown, locals and all
    assert PLANTED not in text


class _RepeatingStore:
    """A store answering one login twice, so the overlay's merge refuses its layer."""

    label = "fake:store"

    def lookup(self, key):
        return [CredSpec(login="u", password=PLANTED), CredSpec(login="u")]

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_an_overlay_merge_error_leaves_no_plain_password_in_the_crash_locals(capsys):
    overlay = CredsOverlay(FakeInventory({"k": {"ip": "10.0.0.1"}}), store=_RepeatingStore())
    with pytest.raises(InventoryError, match="duplicate cred entry 'u' in the creds store") as exc:
        overlay.lookup("k")
    text = _crash_text(exc.value, capsys)
    assert "lower" in text  # the merge frame's dumped layers are shown
    assert PLANTED not in text
