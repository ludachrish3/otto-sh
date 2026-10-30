"""~/.otto/settings.toml — the user-level settings file (spec §8)."""

import pytest

from otto.config.user_settings import (
    CACHE_ENABLED_KEY,
    CACHE_TABLE,
    CacheSetting,
    cache_enabled,
    clear_user_settings_memo,
    load_user_settings,
    user_settings_path,
)
from otto.models.settings import CacheSettingsSpec, UserSettingsModel


def _user_file(dir_, body=""):
    """Write the USER-level settings file — the subject of every test here.

    Not a project: ``make_project`` writes ``<root>/.otto/settings.toml``
    inside a project, which is a different file that happens to share a
    basename. One writer, so the scaffold-policy exemption is stated once.
    """
    settings_file = dir_ / "settings.toml"
    settings_file.write_text(body)  # scaffold-exempt: user-level ~/.otto file, not a project
    return settings_file


def test_path_follows_otto_home(tmp_path, monkeypatch):
    # tests/conftest.py strips OTTO_* at IMPORT time, so setting it here is honoured.
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert user_settings_path() == tmp_path / "settings.toml"


def test_path_is_pure_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "absent"))
    assert user_settings_path() == tmp_path / "absent" / "settings.toml"
    assert not (tmp_path / "absent").exists()


def test_absent_file_is_none(tmp_path):
    assert load_user_settings(tmp_path / "settings.toml") is None


def test_parses_inventory_table(tmp_path):
    p = _user_file(tmp_path, '[inventory]\nbackend = "json"\npath = "i.json"\n')
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"
    assert model.inventory.model_extra == {"path": "i.json"}


def test_an_empty_file_parses_to_a_model_with_no_inventory(tmp_path):
    p = _user_file(tmp_path)
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is None


def test_a_project_only_table_errors_naming_the_key_and_file(tmp_path):
    p = _user_file(tmp_path, '[reservations]\nbackend = "none"\n')
    with pytest.raises(
        ValueError,
        match=r"settings\.toml: [\s\S]*reservations\n\s+Extra inputs are not permitted",
    ):
        load_user_settings(p)


def test_a_broken_file_errors_naming_the_file(tmp_path):
    p = _user_file(tmp_path, "[inventory\n")
    with pytest.raises(
        ValueError, match=r"settings\.toml: Expected '\]' at the end of a table declaration"
    ):
        load_user_settings(p)


def test_default_path_is_used_when_none_is_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert load_user_settings() is None
    _user_file(tmp_path, '[inventory]\nbackend = "json"\npath = "i.json"\n')
    clear_user_settings_memo()  # the absent file was memoized
    model = load_user_settings()
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"


def test_parses_creds_table(tmp_path):
    p = _user_file(tmp_path, '[creds]\nbackend = "json"\npath = "c.json"\n')
    model = load_user_settings(p)
    assert model is not None
    assert model.creds is not None
    assert model.creds.backend == "json"
    assert model.creds.model_extra == {"path": "c.json"}
    assert model.inventory is None


def _own_file(body):
    """Write the default-path user settings file of this test's private OTTO_HOME."""
    p = user_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)  # scaffold-exempt: user-level ~/.otto file, not a project
    return p


def test_cache_table_names_the_model_field():
    assert CACHE_TABLE in UserSettingsModel.model_fields
    assert CACHE_ENABLED_KEY in CacheSettingsSpec.model_fields


def test_cache_on_by_default_without_a_settings_file():
    assert cache_enabled() == CacheSetting(on=True, source="default")


def test_cache_on_by_default_without_a_cache_table():
    _own_file('[inventory]\nbackend = "json"\npath = "i.json"\n')
    assert cache_enabled() == CacheSetting(on=True, source="default")


def test_cache_table_without_enabled_is_on():
    _own_file("[cache]\n")
    assert cache_enabled() == CacheSetting(on=True, source=f"settings.toml: {user_settings_path()}")


def test_cache_disabled_by_the_settings_file():
    _own_file("[cache]\nenabled = false\n")
    assert cache_enabled() == CacheSetting(
        on=False, source=f"settings.toml: {user_settings_path()}"
    )


def test_unparsable_settings_resolve_off_with_the_error():
    _own_file("[cache\n")
    s = cache_enabled()
    assert not s.on
    assert str(user_settings_path()) in s.source


def test_invalid_cache_value_resolves_off_with_the_error():
    _own_file('[cache]\nenabled = "maybe"\n')
    s = cache_enabled()
    assert not s.on
    assert "enabled" in s.source


def test_unknown_cache_key_resolves_off():
    _own_file("[cache]\nenable = false\n")
    s = cache_enabled()
    assert not s.on
    assert "enable" in s.source


def test_the_settings_file_is_read_once_per_process(monkeypatch):
    p = _own_file("[cache]\nenabled = false\n")
    reads = []
    real = type(p).read_text

    def spy(self, *a, **k):
        reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(type(p), "read_text", spy)
    cache_enabled()
    load_user_settings()
    cache_enabled()
    assert reads == [p]
    clear_user_settings_memo()
    cache_enabled()
    assert reads == [p, p]


def test_an_absent_file_is_memoized_and_an_error_is_not(monkeypatch):
    assert cache_enabled().on
    _own_file("[cache]\nenabled = false\n")
    assert cache_enabled().on  # the memoized None still answers
    clear_user_settings_memo()
    _own_file("[cache\n")
    assert not cache_enabled().on
    _own_file("[cache]\nenabled = true\n")  # a broken read left nothing behind
    assert cache_enabled().on


def test_an_explicit_path_bypasses_the_memo(tmp_path):
    assert load_user_settings() is None
    other = _user_file(tmp_path, "[cache]\nenabled = false\n")
    first = load_user_settings(other)
    assert first is not None
    assert first.cache is not None
    assert not first.cache.enabled
    other.write_text("[cache]\nenabled = true\n")  # scaffold-exempt: user-level file
    second = load_user_settings(other)
    assert second is not None
    assert second.cache is not None
    assert second.cache.enabled
