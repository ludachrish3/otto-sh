"""Conformance cases for the inventory backends and creds stores (configured backend seams)."""

import functools
from dataclasses import dataclass

import pytest

from otto.creds import CredsEnv
from otto.creds.errors import CredsConstructionError, CredsError
from otto.creds.registry import CREDS_BACKENDS, register_creds_backend
from otto.inventory import InventoryEnv
from otto.inventory.errors import InventoryConstructionError, InventoryError
from otto.inventory.registry import (
    INVENTORY_BACKENDS,
    InventoryMetadata,
    register_inventory_backend,
)
from otto.registry import configured_backend
from tests.unit.registry import conformance

COVERS = [
    "otto.inventory.registry:INVENTORY_BACKENDS",
    "otto.creds.registry:CREDS_BACKENDS",
]


class _Inventory:
    """The least a built inventory must be."""

    label = "case"
    supplies = frozenset({"ip"})

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


class _Store:
    """The least a built creds store must be."""

    label = "case"

    def lookup(self, key):
        return []

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


@dataclass
class _Spy:
    parses: int = 0
    builds: int = 0


def _spy_entry(built: type, metadata: object):
    def make():
        spy = _Spy()

        @dataclass
        class SpyConfig:
            @classmethod
            def model_validate(cls, raw, context=None):
                spy.parses += 1
                if "token" in raw:
                    raise ValueError(f"bad value {raw['token']}")
                return cls()

            def model_dump(self, mode="python"):
                return {}

        def factory(c):
            spy.builds += 1
            return built()

        return configured_backend(config=SpyConfig, factory=factory, metadata=metadata), spy

    return make


def test_inventory_backend_case(tmp_path):
    conformance.assert_backend_registry(
        INVENTORY_BACKENDS,
        env=InventoryEnv(tmp_path, "o.toml"),
        make_spy_entry=_spy_entry(_Inventory, InventoryMetadata()),
        good_raw={"path": "x.json"},
        bad_raw={"path": "x.json", "token": "s3cr3t-value"},
    )


def test_creds_backend_case(tmp_path):
    conformance.assert_backend_registry(
        CREDS_BACKENDS,
        env=CredsEnv(tmp_path, "o.toml"),
        make_spy_entry=_spy_entry(_Store, None),
        good_raw={"path": "x.json"},
        bad_raw={"path": "x.json", "token": "s3cr3t-value"},
    )


@dataclass(frozen=True)
class _CaseConfig:
    @classmethod
    def model_validate(cls, raw, context=None):
        return cls()

    def model_dump(self, mode="python"):
        return {}


def _inventory_factory(c):
    return _Inventory()


def _store_factory(c):
    return _Store()


def test_register_inventory_backend_matches_the_raw_path():
    conformance.assert_wrapper_matches_raw(
        INVENTORY_BACKENDS,
        via_wrapper=functools.partial(
            register_inventory_backend,
            "case-inventory",
            config=_CaseConfig,
            factory=_inventory_factory,
            snapshot_cache=False,
        ),
        record_for=lambda: configured_backend(
            config=_CaseConfig,
            factory=_inventory_factory,
            metadata=InventoryMetadata(snapshot_cache=False),
        ),
    )


def test_register_creds_backend_matches_the_raw_path():
    conformance.assert_wrapper_matches_raw(
        CREDS_BACKENDS,
        via_wrapper=functools.partial(
            register_creds_backend, "case-store", config=_CaseConfig, factory=_store_factory
        ),
        record_for=lambda: configured_backend(
            config=_CaseConfig, factory=_store_factory, metadata=None
        ),
    )


def test_the_construction_errors_are_value_errors_under_each_seam_s_base():
    assert issubclass(InventoryConstructionError, InventoryError)
    assert issubclass(InventoryConstructionError, ValueError)
    assert issubclass(CredsConstructionError, CredsError)
    assert issubclass(CredsConstructionError, ValueError)


@pytest.mark.parametrize(
    ("table", "env", "raw", "error"),
    [
        (
            INVENTORY_BACKENDS,
            InventoryEnv,
            {"path": "x.json", "token": "hunter2-secret"},
            InventoryConstructionError,
        ),
        (
            CREDS_BACKENDS,
            CredsEnv,
            {"path": "x.json", "token": "hunter2-secret"},
            CredsConstructionError,
        ),
    ],
)
def test_a_json_parse_failure_never_echoes_the_rejected_value(tmp_path, table, env, raw, error):
    with pytest.raises(error, match="parse failed") as err:
        table.prepare("json", raw, env(tmp_path, "o.toml"), source="o.toml")
    assert "hunter2-secret" not in str(err.value)


@pytest.mark.parametrize(
    ("table", "env", "error"),
    [
        (INVENTORY_BACKENDS, InventoryEnv, InventoryConstructionError),
        (CREDS_BACKENDS, CredsEnv, CredsConstructionError),
    ],
    ids=["inventory", "creds"],
)
def test_a_json_path_that_cannot_anchor_never_echoes_it(tmp_path, table, env, error):
    """The built-in model's own validator names the rule, never the value it refused."""
    raw = {"path": "~nosuchuser_xyz/hunter2-secret"}
    with pytest.raises(error, match="parse failed: path: ") as err:
        table.prepare("json", raw, env(tmp_path, "o.toml"), source="o.toml")
    assert "hunter2-secret" not in str(err.value)
    assert "nosuchuser_xyz" not in str(err.value)
