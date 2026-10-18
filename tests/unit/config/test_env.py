import os

import pytest

from otto.config.env import (
    LAB_ENV_VAR,
    LOG_DAYS_ENV_VAR,
    OTTOPATH_ENV_VAR,
    XDIR_ENV_VAR,
    load_otto_env,
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Clear the environment variables Otto cares about before every test"""

    monkeypatch.delenv(OTTOPATH_ENV_VAR, None)


def test_env_not_set() -> None:

    env = load_otto_env()

    assert env.ottopath == []


def test_env_lab_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""  # noqa: E501 — intentional descriptive docstring

    assert LAB_ENV_VAR == "OTTO_LAB"


def test_env_sutdirs_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""  # noqa: E501 — intentional descriptive docstring

    assert OTTOPATH_ENV_VAR == "OTTOPATH"


def test_env_log_days_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""  # noqa: E501 — intentional descriptive docstring

    assert LOG_DAYS_ENV_VAR == "OTTO_LOG_DAYS"


def test_env_sutdirs_set_to_one_path_that_exists(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{tmpdir}")
    env = load_otto_env()

    assert env.ottopath == [tmpdir]


def test_env_sutdirs_set_to_one_path_that_does_not_exist(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{tmpdir}_typo")

    with pytest.raises(FileNotFoundError):
        load_otto_env()


def test_env_sutdirs_set_to_multiple_paths_that_exist(monkeypatch, tmpdir_factory) -> None:

    sut_dir1 = tmpdir_factory.mktemp("dir1")
    sut_dir2 = tmpdir_factory.mktemp("dir2")

    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{sut_dir1}{os.pathsep}{sut_dir2}")
    env = load_otto_env()

    assert env.ottopath == [sut_dir1, sut_dir2]


def test_env_sutdirs_set_to_multiple_paths_one_does_not_exist(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{tmpdir}{os.pathsep}{tmpdir}_typo")

    with pytest.raises(FileNotFoundError):
        load_otto_env()


def test_env_xdir_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""  # noqa: E501 — intentional descriptive docstring

    assert XDIR_ENV_VAR == "OTTO_XDIR"


def test_the_loader_reads_sut_dirs_through_split_path_list(monkeypatch, tmp_path) -> None:
    """A separator-space list names a path with a leading space, which does not exist."""
    monkeypatch.chdir(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{one}{os.pathsep}{two}")
    assert load_otto_env().ottopath == [one, two]

    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{one}{os.pathsep} {two}")
    with pytest.raises(FileNotFoundError) as raised:
        load_otto_env()
    assert str(raised.value) == f"Path {tmp_path}/ {two} does not exist"


def test_the_loader_reads_a_comma_list_as_one_path(monkeypatch, tmp_path) -> None:
    """A comma does not separate: two real repos joined by one name a missing path."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setenv(OTTOPATH_ENV_VAR, f"{one},{two}")
    with pytest.raises(FileNotFoundError) as raised:
        load_otto_env()
    assert str(raised.value) == f"Path {one},{two} does not exist"
