"""The raw-settings readers are tolerant: a repo otto has not scaffolded yet is not an error."""

from pathlib import Path

import pytest

from otto.init.settings_file import declared_init, existing_settings_name


def _write(root: Path, text: str) -> None:
    (root / ".otto").mkdir(exist_ok=True)
    settings = root / ".otto" / "settings.toml"
    settings.write_text(text)  # sutrepo-exempt: raw text the readers must tolerate


def test_declared_init_is_none_without_a_settings_file(tmp_path: Path) -> None:
    assert declared_init(tmp_path) is None


@pytest.mark.parametrize("text", ["not [valid toml", 'init = "foo"\n', "init = [1]\n"])
def test_declared_init_is_none_for_unreadable_or_malformed_settings(
    tmp_path: Path, text: str
) -> None:
    _write(tmp_path, text)
    assert declared_init(tmp_path) is None


@pytest.mark.parametrize("text", ['name = "x"\n', "init = []\n"])
def test_declared_init_is_empty_when_omitted_or_empty(tmp_path: Path, text: str) -> None:
    _write(tmp_path, text)
    assert declared_init(tmp_path) == []


def test_declared_init_lists_the_declared_modules(tmp_path: Path) -> None:
    _write(tmp_path, 'init = ["a", "b.c"]\n')
    assert declared_init(tmp_path) == ["a", "b.c"]


@pytest.mark.parametrize("text", ["not [valid toml", "name = 3\n", 'name = ""\n', "version = 1\n"])
def test_existing_settings_name_is_none_unless_a_nonempty_string(tmp_path: Path, text: str) -> None:
    _write(tmp_path, text)
    assert existing_settings_name(tmp_path) is None


def test_existing_settings_name_returns_the_name(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "acme"\n')
    assert existing_settings_name(tmp_path) == "acme"
