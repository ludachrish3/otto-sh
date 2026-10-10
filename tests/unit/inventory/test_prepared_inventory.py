"""An ``[inventory]`` table is prepared by its backend's config model and built by its registry."""

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from otto.inventory import InventoryConstructionError, InventoryEnv
from otto.inventory.config import compile_inventory, construct_inventory
from otto.inventory.registry import (
    INVENTORY_BACKENDS,
    _check_inventory_result,
    register_inventory_backend,
)
from otto.models.base import OttoModel
from otto.models.settings import InventoryConfigSpec


def _compile(table: dict, tmp_path: Path, *, origin: str = "o.toml"):
    return compile_inventory(
        InventoryConfigSpec.model_validate(table), anchor_dir=tmp_path, origin=origin
    )


class _Remote:
    """An inventory that reports no freshness, as a remote backend does."""

    label = "remote"
    supplies = frozenset({"ip"})

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


def test_two_repos_naming_one_json_file_from_their_own_roots_are_the_same(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _compile({"backend": "json", "path": "inv.json"}, tmp_path / "a")  # relative
    b = _compile({"backend": "json", "path": str(tmp_path / "a" / "inv.json")}, tmp_path / "b")
    assert a.same_as(b)  # anchoring, not resolution: both are <tmp>/a/inv.json


def test_cache_ttl_still_separates_otherwise_equal_tables(tmp_path):
    a = _compile({"backend": "json", "path": "x.json", "cache_ttl": "0"}, tmp_path)
    b = _compile({"backend": "json", "path": "x.json", "cache_ttl": "7d"}, tmp_path)
    assert not a.same_as(b)


def test_an_unknown_json_key_is_a_parse_error_naming_backend_and_origin(tmp_path):
    with pytest.raises(InventoryConstructionError, match=r"'json'.*configured in o\.toml.*parse"):
        _compile({"backend": "json", "path": "x.json", "pth": "y"}, tmp_path)


def test_snapshot_caching_reads_metadata_not_the_name(tmp_path):
    class Cfg(OttoModel, frozen=True):
        pass

    register_inventory_backend(
        "json", config=Cfg, factory=lambda c: _Remote(), snapshot_cache=True, overwrite=True
    )
    inv = construct_inventory(_compile({"backend": "json", "cache_ttl": "7d"}, tmp_path))
    assert type(inv).__name__ == "SnapshotCache"


def test_a_factory_s_type_error_names_origin_and_backend(tmp_path):
    class Cfg(OttoModel, frozen=True):
        pass

    def boom(c):
        raise TypeError("unexpected keyword argument 'urll'")

    register_inventory_backend("boom", config=Cfg, factory=boom)
    with pytest.raises(
        InventoryConstructionError,
        match=r"'boom' \(registered by .*; configured in o\.toml\): construction failed.*urll",
    ):
        construct_inventory(_compile({"backend": "boom"}, tmp_path))


def _slug(compiled) -> str:
    from otto.inventory.cache import snapshot_slug_material

    prepared = compiled.prepared
    return snapshot_slug_material(
        prepared.backend, "netbox:https://nb", prepared.normalized, cache_ttl=compiled.cache_ttl
    )


def test_the_snapshot_slug_and_same_as_share_their_inputs(tmp_path):
    zero = _compile({"backend": "netbox", "url": "https://nb", "cache_ttl": "0"}, tmp_path)
    week = _compile({"backend": "netbox", "url": "https://nb", "cache_ttl": "7d"}, tmp_path)
    assert not zero.same_as(week)
    assert _slug(zero) != _slug(week)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rel = _compile({"backend": "json", "path": "inv.json"}, tmp_path / "a")
    absolute = _compile(
        {"backend": "json", "path": str(tmp_path / "a" / "inv.json")}, tmp_path / "b"
    )
    assert rel.same_as(absolute)
    assert _slug(rel) == _slug(absolute)


def test_snapshot_identity_ignores_provenance(tmp_path, monkeypatch):
    """Equal normalized configs from different anchors AND origins open one snapshot.

    Through ``construct_inventory``, so the slug otto really keys the cache by
    is the one compared: a path-taking, snapshot-cached backend declared once
    relative to repo a and once absolute from repo b.
    """
    from otto.inventory.config import JsonInventoryConfig

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    register_inventory_backend(
        "pathy", config=JsonInventoryConfig, factory=lambda c: _Remote(), snapshot_cache=True
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _compile(
        {"backend": "pathy", "path": "inv.json", "cache_ttl": "7d"},
        tmp_path / "a",
        origin=str(tmp_path / "a.toml"),
    )
    b = _compile(
        {"backend": "pathy", "path": str(tmp_path / "a" / "inv.json"), "cache_ttl": "7d"},
        tmp_path / "b",
        origin=str(tmp_path / "b.toml"),
    )
    assert a.same_as(b)
    assert construct_inventory(a).snapshot_path == construct_inventory(b).snapshot_path


def test_one_netbox_declared_from_two_repos_shares_one_snapshot(tmp_path, monkeypatch):
    """The snapshot otto opens is keyed without the declaring repo or its settings file."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = {"backend": "netbox", "url": "https://nb", "cache_ttl": "7d"}
    a = construct_inventory(_compile(table, tmp_path / "a", origin=str(tmp_path / "a.toml")))
    b = construct_inventory(_compile(table, tmp_path / "b", origin=str(tmp_path / "b.toml")))
    assert type(a).__name__ == "SnapshotCache"
    assert a.snapshot_path == b.snapshot_path


_SLUG_SCRIPT = """
import sys
from pathlib import Path
from otto.inventory.config import compile_inventory
from otto.inventory.cache import snapshot_slug_material
from otto.models.settings import InventoryConfigSpec

compiled = compile_inventory(
    InventoryConfigSpec.model_validate(
        {"backend": "netbox", "url": "https://nb/", "filter": {"site": "a"}, "cache_ttl": "7d"}
    ),
    anchor_dir=Path(sys.argv[1]),
    origin="o.toml",
)
p = compiled.prepared
label = "netbox:https://nb"
print(snapshot_slug_material(p.backend, label, p.normalized, cache_ttl=compiled.cache_ttl))
"""


def test_snapshot_identity_is_deterministic_across_processes(tmp_path):
    def slug() -> str:
        done = subprocess.run(
            [sys.executable, "-c", _SLUG_SCRIPT, str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    first, second = slug(), slug()
    assert first
    assert first == second


def test_the_slug_keeps_a_ttl_of_whole_seconds_and_drops_the_adopted_url(tmp_path):
    compiled = _compile({"backend": "netbox", "url": "https://nb/", "cache_ttl": "2h"}, tmp_path)
    slug = _slug(compiled)
    assert slug.endswith(f"|ttl={int(timedelta(hours=2).total_seconds())}")
    assert "https://nb/" not in slug


# -- the result check -------------------------------------------------------


def test_every_built_in_passes_the_result_check(tmp_path):
    from otto.creds.config import compile_creds_table, construct_creds_store
    from otto.creds.registry import _check_creds_result

    for table in (
        {"backend": "json", "path": "inv.json"},
        {"backend": "netbox", "url": "https://nb", "cache_ttl": "0"},
    ):
        compiled = _compile(table, tmp_path)
        _check_inventory_result(table["backend"], INVENTORY_BACKENDS.build(compiled.prepared))
    store = construct_creds_store(
        compile_creds_table(
            {"backend": "json", "path": "creds.json"}, anchor_dir=tmp_path, origin="o.toml"
        )
    )
    _check_creds_result("json", store)


class _Guarded:
    """An inventory whose label and supplies are properties that must never be read."""

    @property
    def label(self):
        raise AssertionError("the result check ran the label getter")

    @property
    def supplies(self):
        raise AssertionError("the result check ran the supplies getter")

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


def test_a_property_backed_backend_passes_without_running_its_getters():
    _check_inventory_result("guarded", _Guarded())


_MEMBERS = ["lookup", "list_keys", "fingerprint", "label", "supplies"]


def _lacking(member: str) -> object:
    attrs = {
        "lookup": lambda self, key: None,
        "list_keys": lambda self: [],
        "fingerprint": lambda self: None,
        "label": "x",
        "supplies": frozenset({"ip"}),
    }
    del attrs[member]
    return type("Partial", (), attrs)()


@pytest.mark.parametrize("member", _MEMBERS)
def test_an_inventory_result_missing_any_protocol_member_is_a_result_error(tmp_path, member):
    class Cfg(OttoModel, frozen=True):
        pass

    register_inventory_backend(
        "partial", config=Cfg, factory=lambda c: _lacking(member), snapshot_cache=False
    )
    compiled = _compile({"backend": "partial"}, tmp_path)
    with pytest.raises(InventoryConstructionError, match=rf"result failed.*\b{member}\b"):
        construct_inventory(compiled)


def test_the_env_carries_the_anchor_and_the_origin(tmp_path):
    compiled = _compile({"backend": "json", "path": "inv.json"}, tmp_path)
    assert compiled.prepared.env == InventoryEnv(tmp_path, "o.toml")
    assert compiled.prepared.normalized["path"] == str(tmp_path / "inv.json")


class _ByClassmethod:
    """An inventory whose protocol methods are a classmethod and a staticmethod."""

    label = "cls"
    supplies = frozenset({"ip"})

    @classmethod
    def lookup(cls, key):
        raise NotImplementedError

    @staticmethod
    def list_keys():
        return []

    def fingerprint(self):
        return None


def test_classmethod_and_staticmethod_protocol_members_pass_the_result_check():
    _check_inventory_result("by-class", _ByClassmethod())


def test_a_direct_registration_without_inventory_metadata_is_refused():
    from otto.registry import configured_backend

    class Cfg(OttoModel, frozen=True):
        pass

    with pytest.raises(TypeError, match="not an InventoryMetadata"):
        INVENTORY_BACKENDS.register(
            "no-metadata",
            configured_backend(config=Cfg, factory=lambda c: _Remote(), metadata=None),
        )
    assert "no-metadata" not in INVENTORY_BACKENDS
