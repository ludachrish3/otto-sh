"""assert_creds_store_conforms: the contract a store must pass (spec 2026-09-06 §5.3)."""

import json

import pytest

from otto.creds import JsonCredsStore
from otto.models.host import CredSpec
from otto.testing import assert_creds_store_conforms


def _json_store(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"k": [{"login": "u", "password": "p"}], "j": [{"login": "v"}]}))
    return JsonCredsStore(p)


class _Good:
    label = "good:mem"

    def __init__(self, data):
        self._data = data

    def lookup(self, key):
        return [CredSpec.model_validate(c) for c in self._data.get(key, [])]

    def list_keys(self):
        return sorted(self._data)

    def fingerprint(self):
        return None


def test_the_json_store_conforms(tmp_path):
    assert_creds_store_conforms(_json_store(tmp_path), known_key="k")


def test_a_store_that_cannot_enumerate_conforms():
    class Vault(_Good):
        def list_keys(self):
            return None

    assert_creds_store_conforms(Vault({"k": [{"login": "u"}]}), known_key="k")


def test_lookup_of_an_unknown_key_must_be_an_empty_list_not_none_and_not_raise():
    class ReturnsNone(_Good):
        def lookup(self, key):
            return None if key not in self._data else super().lookup(key)

    class Raises(_Good):
        def lookup(self, key):
            if key not in self._data:
                raise KeyError(key)
            return super().lookup(key)

    with pytest.raises(AssertionError, match=r"lookup\(unknown key\) must return \[\]"):
        assert_creds_store_conforms(ReturnsNone({"k": [{"login": "u"}]}), known_key="k")
    with pytest.raises(
        AssertionError, match=r"lookup\(unknown key\) must return \[\], raised KeyError"
    ):
        assert_creds_store_conforms(Raises({"k": [{"login": "u"}]}), known_key="k")


def test_a_listed_key_that_resolves_empty_and_a_duplicate_login_are_violations():
    class ListsGhost(_Good):
        def list_keys(self):
            return ["ghost", *super().list_keys()]

    with pytest.raises(
        AssertionError, match=r"list_keys\(\) names 'ghost' but lookup\('ghost'\) is empty"
    ):
        assert_creds_store_conforms(ListsGhost({"k": [{"login": "u"}]}), known_key="k")
    with pytest.raises(AssertionError, match=r"lookup\('k'\) repeats login 'u'"):
        assert_creds_store_conforms(
            _Good({"k": [{"login": "u"}, {"login": "u", "password": "x"}]}), known_key="k"
        )


def test_known_key_must_resolve_and_appear_in_list_keys():
    with pytest.raises(AssertionError, match=r"known_key 'missing' resolved to no entries"):
        assert_creds_store_conforms(_Good({"k": [{"login": "u"}]}), known_key="missing")

    class Hides(_Good):
        def lookup(self, key):
            if key == "hidden":
                return [CredSpec.model_validate({"login": "u"})]
            return super().lookup(key)

    with pytest.raises(AssertionError, match=r"known_key 'hidden' must appear in list_keys\(\)"):
        assert_creds_store_conforms(Hides({"k": [{"login": "u"}]}), known_key="hidden")


def test_unsorted_wrong_type_wrong_entries_and_non_idempotence_are_violations():
    class Unsorted(_Good):
        def list_keys(self):
            return ["k", "j"]

    with pytest.raises(AssertionError, match=r"list_keys\(\) must be sorted"):
        assert_creds_store_conforms(Unsorted({"j": [{"login": "v"}], "k": [{"login": "u"}]}))

    class TupleKeys(_Good):
        def list_keys(self):
            return ("k",)

    with pytest.raises(AssertionError, match=r"must return list\[str\] or None"):
        assert_creds_store_conforms(TupleKeys({"k": [{"login": "u"}]}))

    class RawDicts(_Good):
        def lookup(self, key):
            if key == "k":
                return self._data.get(key, [])
            return super().lookup(key)

    with pytest.raises(AssertionError, match=r"lookup\('k'\) must return list\[CredSpec\]"):
        assert_creds_store_conforms(RawDicts({"k": [{"login": "u"}]}))

    class Flaky(_Good):
        def __init__(self, data):
            super().__init__(data)
            self._calls = 0

        def lookup(self, key):
            if key == "k":
                self._calls += 1
                login = "u" if self._calls == 1 else "changed"
                return [CredSpec.model_validate({"login": login})]
            return super().lookup(key)

    with pytest.raises(AssertionError, match=r"lookup\('k'\) must be idempotent"):
        assert_creds_store_conforms(Flaky({"k": [{"login": "u"}]}))


def test_structural_violations_are_all_reported_at_once():
    class Broken:
        label = ""

        def lookup(self, key):
            return "nope"

        def fingerprint(self):
            return 3

    with pytest.raises(AssertionError) as exc:
        assert_creds_store_conforms(Broken())
    text = str(exc.value)
    assert "must satisfy the runtime_checkable CredsStore protocol" in text
    assert "label must be a non-empty str" in text
    assert "fingerprint() must be str or None" in text
