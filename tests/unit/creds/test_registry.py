"""The creds-store registry mirrors ``otto.inventory.registry`` (spec 2026-09-06 §5.3)."""

import pytest

from otto.creds import CredsConstructionError, CredsEnv, JsonCredsStore, register_creds_backend
from otto.creds.registry import CREDS_BACKENDS
from otto.models.base import OttoModel


class _Config(OttoModel, frozen=True):
    pass


def _fake(c):
    raise NotImplementedError


def _other(c):
    raise NotImplementedError


def test_json_is_pre_registered_and_builds_the_json_store(tmp_path):
    prepared = CREDS_BACKENDS.prepare("json", {"path": "c.json"}, CredsEnv(tmp_path, "o.toml"))
    store = CREDS_BACKENDS.build(prepared)
    assert isinstance(store, JsonCredsStore)
    assert store.path == (tmp_path / "c.json").resolve()


def test_register_and_lookup():
    register_creds_backend("mine-test", config=_Config, factory=_fake)
    entry = CREDS_BACKENDS.get("mine-test")
    assert (entry.config, entry.factory, entry.metadata) == (_Config, _fake, None)


def test_duplicate_registration_raises_naming_both_origins():
    register_creds_backend("dup-test", config=_Config, factory=_fake)
    with pytest.raises(ValueError, match="creds backend 'dup-test' is already registered") as exc:
        register_creds_backend("dup-test", config=_Config, factory=_other)
    assert str(exc.value).count(__name__) == 2
    assert "overwrite=True" in str(exc.value)
    register_creds_backend("dup-test", config=_Config, factory=_other, overwrite=True)
    assert CREDS_BACKENDS.get("dup-test").factory is _other


def test_unknown_name_lists_registered_and_points_at_the_registrar(tmp_path):
    with pytest.raises(
        CredsConstructionError, match="Unknown creds backend 'does-not-exist'"
    ) as exc:
        CREDS_BACKENDS.prepare("does-not-exist", {}, CredsEnv(tmp_path, "o.toml"))
    assert "Registered: json" in str(exc.value)
    assert "otto.creds.register_creds_backend()" in str(exc.value)
