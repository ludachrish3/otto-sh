"""Replacing a built-in inventory backend or creds store reaches the real product path.

``build_inventory`` is what bootstrap and the completion handover call; a
backend registered over a built-in name with ``overwrite=True`` must be what
it builds for a repo whose ``[inventory]``/``[creds]`` names it. The root
isolation fixture restores both tables.
"""

import pytest

from otto.creds.registry import CREDS_BACKENDS, register_creds_backend
from otto.inventory.config import build_inventory
from otto.inventory.creds import CredsOverlay
from otto.inventory.registry import INVENTORY_BACKENDS, register_inventory_backend
from otto.models.base import OttoModel
from tests._fixtures.fake_repo import fake_repo

REPLACES = [
    ("otto.inventory.registry:INVENTORY_BACKENDS", "json"),
    ("otto.inventory.registry:INVENTORY_BACKENDS", "netbox"),
    ("otto.creds.registry:CREDS_BACKENDS", "json"),
]
"""Every ``(table, built-in name)`` pair this module replaces."""


class _Config(OttoModel, extra="allow", frozen=True):
    """Accepts whatever keys a built-in's table carries, so the spy can stand in for it."""


class _SpyInventory:
    label = "spy"
    supplies = frozenset({"ip"})

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


class _SpyStore:
    label = "spy-store"

    def lookup(self, key):
        return []

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


def test_replaces_names_every_built_in_inventory_and_creds_backend():
    builtins = {
        ("otto.inventory.registry:INVENTORY_BACKENDS", name)
        for name in INVENTORY_BACKENDS.names()
        if INVENTORY_BACKENDS.origin(name).startswith("otto.")
    } | {
        ("otto.creds.registry:CREDS_BACKENDS", name)
        for name in CREDS_BACKENDS.names()
        if CREDS_BACKENDS.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


@pytest.mark.parametrize(
    ("name", "table"),
    [
        ("json", {"backend": "json", "path": "inv.json"}),
        ("netbox", {"backend": "netbox", "url": "https://nb", "cache_ttl": "0"}),
    ],
)
def test_a_replaced_inventory_backend_is_what_build_inventory_builds(tmp_path, name, table):
    spy = _SpyInventory()
    snapshot = INVENTORY_BACKENDS.peek(name).metadata.snapshot_cache
    register_inventory_backend(
        name, config=_Config, factory=lambda c: spy, snapshot_cache=snapshot, overwrite=True
    )
    repo = fake_repo("r", sut_dir=tmp_path, settings={"inventory": table})
    assert build_inventory([repo]) is spy


def test_a_replaced_creds_store_is_what_build_inventory_overlays(tmp_path):
    store = _SpyStore()
    register_creds_backend("json", config=_Config, factory=lambda c: store, overwrite=True)
    repo = fake_repo(
        "r",
        sut_dir=tmp_path,
        settings={
            "inventory": {"backend": "json", "path": "inv.json"},
            "creds": {"backend": "json", "path": "creds.json"},
        },
    )
    inventory = build_inventory([repo])
    assert isinstance(inventory, CredsOverlay)
    assert inventory.store is store
