"""~/.otto/settings.toml — the user-level settings file (spec §8)."""

import pytest

from otto.config.user_settings import load_user_settings, user_settings_path


def _user_file(dir_, body=""):
    """Write the USER-level settings file — the subject of every test here.

    Not a SUT repo: ``make_sut_repo`` writes ``<root>/.otto/settings.toml``
    inside a project, which is a different file that happens to share a
    basename. One writer, so the scaffold-policy exemption is stated once.
    """
    settings_file = dir_ / "settings.toml"
    settings_file.write_text(body)  # sutrepo-exempt: user-level ~/.otto file, not a SUT repo
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
    p = _user_file(
        tmp_path, '[inventory]\nbackend = "json"\npath = "i.json"\ncreds_file = "c.json"\n'
    )
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"
    assert model.inventory.creds_file == "c.json"
    assert model.inventory.model_extra == {"path": "i.json"}


def test_an_empty_file_parses_to_a_model_with_no_inventory(tmp_path):
    p = _user_file(tmp_path)
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is None


def test_a_repo_only_table_errors_naming_the_key_and_file(tmp_path):
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
    model = load_user_settings()
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"
