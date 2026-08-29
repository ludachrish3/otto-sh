"""Unit tests for the inventory backend registry.

Mirrors ``tests/unit/reservations/test_registry.py``. There is no built-in to
assert on yet: ``json`` arrives with the JSON backend and ``netbox`` later, so
the "unknown name" case carries the anti-vacuity weight for now.
"""

import pytest

from otto.inventory import get_inventory_backend_class, register_inventory_backend
from otto.inventory.registry import INVENTORY_BACKENDS


class _Fake:
    label = "fake"


class _Other:
    label = "other"


def test_register_and_lookup():
    register_inventory_backend("mine-test", _Fake)
    try:
        assert get_inventory_backend_class("mine-test") is _Fake
    finally:
        INVENTORY_BACKENDS.unregister("mine-test")


def test_duplicate_registration_raises_naming_both_origins():
    register_inventory_backend("dup-test", _Fake)
    try:
        with pytest.raises(
            ValueError, match="inventory backend 'dup-test' is already registered"
        ) as exc:
            register_inventory_backend("dup-test", _Other)
        # Both origins are named — this module registered it twice.
        assert str(exc.value).count(__name__) == 2
        assert "overwrite=True" in str(exc.value)
        assert get_inventory_backend_class("dup-test") is _Fake  # the first one still stands
        register_inventory_backend("dup-test", _Other, overwrite=True)
        assert get_inventory_backend_class("dup-test") is _Other
    finally:
        INVENTORY_BACKENDS.unregister("dup-test")


def test_unknown_name_lists_registered_and_points_at_the_registrar():
    with pytest.raises(ValueError, match="Unknown inventory backend") as exc:
        get_inventory_backend_class("does-not-exist")
    message = str(exc.value)
    assert "Unknown inventory backend 'does-not-exist'" in message
    assert "Registered:" in message
    assert "otto.inventory.registry.register_inventory_backend()" in message
