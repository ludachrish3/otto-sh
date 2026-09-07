"""The json creds store (spec 2026-09-06 creds-store §5.2)."""

import json

import pytest

from otto.creds import CredsError, CredsStore, JsonCredsStore, parse_creds_document
from otto.inventory.protocol import SupportsStatPaths


def _store(tmp_path, data):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(data))
    return JsonCredsStore(p)


def test_the_json_store_satisfies_the_protocol(tmp_path):
    store = _store(tmp_path, {})
    assert isinstance(store, CredsStore)
    assert isinstance(store, SupportsStatPaths)
    assert store.label == f"json:{tmp_path / 'creds.json'}"


def test_lookup_returns_entries_and_an_unknown_key_is_empty(tmp_path):
    store = _store(
        tmp_path,
        {"k": [{"login": "root", "password": "x"}, {"login": "svc", "proxy": "su", "via": "root"}]},
    )
    creds = store.lookup("k")
    assert [(c.login, c.password, c.proxy, c.via) for c in creds] == [
        ("root", "x", None, None),
        ("svc", None, "su", "root"),
    ]
    assert store.lookup("absent") == []
    assert store.list_keys() == ["k"]


def test_list_keys_is_sorted_not_insertion_order(tmp_path):
    store = _store(tmp_path, {"zed": [], "alpha": []})
    assert store.list_keys() == ["alpha", "zed"]


def test_lookup_hands_out_a_fresh_list_each_time(tmp_path):
    store = _store(tmp_path, {"k": [{"login": "root"}]})
    store.lookup("k").append("junk")
    assert len(store.lookup("k")) == 1


def test_the_file_is_parsed_once(tmp_path):
    """A second ``lookup`` must not re-read the file: any re-read here would raise."""
    store = _store(tmp_path, {"k": [{"login": "root"}]})
    first = store.lookup("k")
    (tmp_path / "creds.json").unlink()
    assert store.lookup("k") == first


def test_construction_does_no_io_and_a_missing_file_names_itself_at_first_use(tmp_path):
    store = JsonCredsStore(tmp_path / "absent.json")  # no raise: nothing read yet
    with pytest.raises(CredsError, match=r"absent\.json: "):
        store.lookup("k")


def test_an_entry_level_error_names_the_file_the_key_and_the_index():
    """``via`` without ``proxy`` fails CredSpec's model validator, whose loc is empty."""
    with pytest.raises(CredsError, match=r"creds\.json: key 'k': creds\[0\]: .*require 'proxy'"):
        parse_creds_document({"k": [{"login": "r", "via": "x"}]}, source="creds.json")


def test_a_field_level_error_names_the_offending_field():
    with pytest.raises(CredsError, match=r"login: Input should be a valid string"):
        parse_creds_document({"k": [{"login": 3}]}, source="creds.json")


def test_the_document_must_be_a_json_object():
    with pytest.raises(CredsError, match="must be a JSON object"):
        parse_creds_document([], source="creds.json")


def test_schema_and_comment_keys_are_skipped():
    out = parse_creds_document({"$schema": "x", "_comment": "not a key", "k": []}, source="s")
    assert out == {"k": []}


def test_a_non_list_value_names_its_key():
    with pytest.raises(CredsError, match="key 'k': expected a list of creds"):
        parse_creds_document({"k": {"login": "r"}}, source="s")


def test_a_duplicate_login_within_one_key_is_refused():
    with pytest.raises(CredsError, match="key 'k': duplicate cred login 'root'"):
        parse_creds_document(
            {"k": [{"login": "root"}, {"login": "root", "password": "x"}]}, source="s"
        )


def test_fingerprint_tracks_the_file_and_survives_its_absence(tmp_path):
    store = _store(tmp_path, {})
    before = store.fingerprint()
    assert before is not None
    assert before.startswith(str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text(json.dumps({"k": []}))  # size changes
    assert store.fingerprint() != before
    gone = JsonCredsStore(tmp_path / "gone.json")
    assert gone.fingerprint() == f"{tmp_path / 'gone.json'}|missing"
    assert store.stat_paths() == [tmp_path / "creds.json"]
