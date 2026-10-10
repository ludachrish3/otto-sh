"""Unit tests for the inventory backend registry.

Mirrors ``tests/unit/reservations/test_registry.py``. The built-ins, ``json``
and ``netbox``, are registered by reference with their config models.
"""

import pytest

from otto.inventory import InventoryConstructionError, InventoryEnv, register_inventory_backend
from otto.inventory.registry import INVENTORY_BACKENDS, InventoryMetadata
from otto.models.base import OttoModel


class _Config(OttoModel, frozen=True):
    pass


def _fake(c):
    raise NotImplementedError


def _other(c):
    raise NotImplementedError


def test_the_built_ins_state_their_snapshot_cache_metadata():
    assert INVENTORY_BACKENDS.peek("json").metadata == InventoryMetadata(snapshot_cache=False)
    assert INVENTORY_BACKENDS.peek("netbox").metadata == InventoryMetadata(snapshot_cache=True)


def test_register_and_lookup():
    register_inventory_backend("mine-test", config=_Config, factory=_fake)
    entry = INVENTORY_BACKENDS.get("mine-test")
    assert (entry.config, entry.factory) == (_Config, _fake)
    assert entry.metadata == InventoryMetadata(snapshot_cache=True)


def test_duplicate_registration_raises_naming_both_origins():
    register_inventory_backend("dup-test", config=_Config, factory=_fake)
    with pytest.raises(
        ValueError, match="inventory backend 'dup-test' is already registered"
    ) as exc:
        register_inventory_backend("dup-test", config=_Config, factory=_other)
    # Both origins are named — this module registered it twice.
    assert str(exc.value).count(__name__) == 2
    assert "overwrite=True" in str(exc.value)
    assert INVENTORY_BACKENDS.get("dup-test").factory is _fake  # the first one still stands
    register_inventory_backend("dup-test", config=_Config, factory=_other, overwrite=True)
    assert INVENTORY_BACKENDS.get("dup-test").factory is _other


def test_unknown_name_lists_registered_and_points_at_the_registrar(tmp_path):
    with pytest.raises(InventoryConstructionError, match="lookup failed") as exc:
        INVENTORY_BACKENDS.prepare("does-not-exist", {}, InventoryEnv(tmp_path, "o.toml"))
    message = str(exc.value)
    assert "Unknown inventory backend 'does-not-exist'" in message
    assert "Registered:" in message
    assert "otto.inventory.register_inventory_backend()" in message
