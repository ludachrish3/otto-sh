"""A ``[creds]`` table is prepared by its store's config model and built by its registry."""

from pathlib import Path

import pytest

from otto.creds import CredsConstructionError, CredsEnv
from otto.creds.config import compile_creds_table, construct_creds_store
from otto.creds.registry import _check_creds_result, register_creds_backend
from otto.models.base import OttoModel


def _compile(table: dict, anchor: Path, *, origin: str = "o.toml"):
    return compile_creds_table(table, anchor_dir=anchor, origin=origin)


def test_two_repos_naming_one_creds_file_from_their_own_roots_are_the_same(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _compile({"backend": "json", "path": "creds.json"}, tmp_path / "a")
    b = _compile({"backend": "json", "path": str(tmp_path / "a" / "creds.json")}, tmp_path / "b")
    assert a.same_as(b)
    assert a.prepared.env == CredsEnv(tmp_path / "a", "o.toml")


def test_an_unknown_json_key_is_a_parse_error_naming_backend_and_origin(tmp_path):
    with pytest.raises(CredsConstructionError, match=r"'json'.*configured in o\.toml.*parse"):
        _compile({"backend": "json", "path": "c.json", "pth": "y"}, tmp_path)


def test_a_factory_s_error_names_origin_and_backend(tmp_path):
    class Cfg(OttoModel, frozen=True):
        pass

    def boom(c):
        raise TypeError("unexpected keyword argument 'urll'")

    register_creds_backend("boom", config=Cfg, factory=boom)
    with pytest.raises(
        CredsConstructionError,
        match=r"'boom' \(registered by .*; configured in o\.toml\): construction failed.*urll",
    ):
        construct_creds_store(_compile({"backend": "boom"}, tmp_path))


def test_the_built_in_store_passes_the_result_check(tmp_path):
    _check_creds_result(
        "json", construct_creds_store(_compile({"backend": "json", "path": "c.json"}, tmp_path))
    )


class _Guarded:
    @property
    def label(self):
        raise AssertionError("the result check ran the label getter")

    def lookup(self, key):
        return []

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


def test_a_property_backed_store_passes_without_running_its_getter():
    _check_creds_result("guarded", _Guarded())


_MEMBERS = ["lookup", "list_keys", "fingerprint", "label"]


def _lacking(member: str) -> object:
    attrs = {
        "lookup": lambda self, key: [],
        "list_keys": lambda self: [],
        "fingerprint": lambda self: None,
        "label": "x",
    }
    del attrs[member]
    return type("Partial", (), attrs)()


@pytest.mark.parametrize("member", _MEMBERS)
def test_a_store_missing_any_protocol_member_is_a_result_error(tmp_path, member):
    class Cfg(OttoModel, frozen=True):
        pass

    register_creds_backend("partial", config=Cfg, factory=lambda c: _lacking(member))
    with pytest.raises(CredsConstructionError, match=rf"result failed.*\b{member}\b"):
        construct_creds_store(_compile({"backend": "partial"}, tmp_path))
