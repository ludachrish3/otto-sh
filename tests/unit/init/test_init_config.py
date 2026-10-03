"""``InitConfig`` owns ``otto init``'s input rules; its refusals name a field, never a flag."""

from pathlib import Path

import pytest

from otto.errors import FieldError
from otto.init import InitConfig, InitInputError
from tests._fixtures.sutrepo import make_sut_repo


def test_a_root_that_is_not_a_directory_is_refused_naming_root(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(InitInputError) as caught:
        InitConfig(missing, "widget", "0.1.0")
    assert caught.value.field == "root"
    assert str(missing) in str(caught.value)


@pytest.mark.parametrize("kmodcov_dir", ["/etc/kmodcov", "../escaped", ".", ""])
def test_a_kmodcov_dir_outside_or_onto_the_root_is_refused(
    tmp_path: Path, kmodcov_dir: str
) -> None:
    with pytest.raises(InitInputError) as caught:
        InitConfig(tmp_path, "widget", "0.1.0", kmodcov_dir=kmodcov_dir)
    assert caught.value.field == "kmodcov_dir"
    assert repr(kmodcov_dir) in str(caught.value)
    assert "--" not in str(caught.value)  # the library never spells a CLI flag


@pytest.mark.parametrize("kmodcov_dir", ["third_party/otto_kmodcov", "vendor/kmodcov/", "a/../b"])
def test_a_kmodcov_dir_strictly_inside_the_root_is_accepted(
    tmp_path: Path, kmodcov_dir: str
) -> None:
    cfg = InitConfig(tmp_path, "widget", "0.1.0", kmodcov_dir=kmodcov_dir)
    assert cfg.kmodcov_path == tmp_path / kmodcov_dir


def test_init_input_error_is_a_field_error() -> None:
    assert issubclass(InitInputError, FieldError)


def test_for_repo_defaults_the_name_to_the_settings_name(tmp_path: Path) -> None:
    make_sut_repo(tmp_path, name="acme", version="1.0.0")
    assert InitConfig.for_repo(tmp_path).name == "acme"


def test_for_repo_defaults_the_name_to_the_directory_without_settings(tmp_path: Path) -> None:
    repo = tmp_path / "widget"
    repo.mkdir()
    assert InitConfig.for_repo(repo).name == "widget"


def test_for_repo_prefers_an_explicit_name_and_resolves_the_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = InitConfig.for_repo(Path(), name="given", version="2.0.0")
    assert (cfg.root, cfg.name, cfg.version) == (tmp_path.resolve(), "given", "2.0.0")


def test_for_repo_still_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(InitInputError) as caught:
        InitConfig.for_repo(tmp_path / "nope")
    assert caught.value.field == "root"
