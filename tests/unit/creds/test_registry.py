"""The creds-store registry mirrors ``otto.inventory.registry`` (spec 2026-09-06 §5.3)."""

import pytest

from otto.creds import JsonCredsStore, get_creds_backend_class, register_creds_backend
from otto.creds.registry import CREDS_BACKENDS


class _Fake:
    label = "fake"


class _Other:
    label = "other"


def test_json_is_pre_registered_through_the_public_path():
    assert get_creds_backend_class("json") is JsonCredsStore


def test_register_and_lookup():
    register_creds_backend("mine-test", _Fake)
    try:
        assert get_creds_backend_class("mine-test") is _Fake
    finally:
        CREDS_BACKENDS.unregister("mine-test")


def test_duplicate_registration_raises_naming_both_origins():
    register_creds_backend("dup-test", _Fake)
    try:
        with pytest.raises(
            ValueError, match="creds backend 'dup-test' is already registered"
        ) as exc:
            register_creds_backend("dup-test", _Other)
        assert str(exc.value).count(__name__) == 2
        assert "overwrite=True" in str(exc.value)
        register_creds_backend("dup-test", _Other, overwrite=True)
        assert get_creds_backend_class("dup-test") is _Other
    finally:
        CREDS_BACKENDS.unregister("dup-test")


def test_unknown_name_lists_registered_and_points_at_the_registrar():
    with pytest.raises(ValueError, match="Unknown creds backend 'does-not-exist'") as exc:
        get_creds_backend_class("does-not-exist")
    assert "Registered: json" in str(exc.value)
    assert "otto.creds.registry.register_creds_backend()" in str(exc.value)
