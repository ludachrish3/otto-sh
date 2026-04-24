from pathlib import Path

import pytest

from otto.configmodule.env import (
    LAB_ENV_VAR,
    LOG_DAYS_ENV_VAR,
    SUT_DIRS_ENV_VAR,
    XDIR_ENV_VAR,
    OttoEnv,
)


@pytest.fixture(autouse=True, scope='function')
def clearEnv(monkeypatch):
    """Clear the environment variables Otto cares about before every test"""

    monkeypatch.delenv(SUT_DIRS_ENV_VAR, None)


def test_env_not_set() -> None:

    env = OttoEnv()

    assert env.sutDirs == []


def test_env_lab_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""

    assert LAB_ENV_VAR == 'OTTO_LAB'


def test_env_sutdirs_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""

    assert SUT_DIRS_ENV_VAR == 'OTTO_SUT_DIRS'


def test_env_log_days_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""

    assert LOG_DAYS_ENV_VAR == 'OTTO_LOG_DAYS'


def test_env_sutdirs_set_to_one_path_that_exists(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(SUT_DIRS_ENV_VAR, f'{tmpdir}')
    env = OttoEnv()

    assert env.sutDirs == [tmpdir]


def test_env_sutdirs_set_to_one_path_that_does_not_exist(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(SUT_DIRS_ENV_VAR, f'{tmpdir}_typo')

    with pytest.raises(FileNotFoundError):
        OttoEnv()


def test_env_sutdirs_set_to_multiple_paths_that_exist(monkeypatch, tmpdir_factory) -> None:

    sutDir1 = tmpdir_factory.mktemp('dir1')
    sutDir2 = tmpdir_factory.mktemp('dir2')

    monkeypatch.setenv(SUT_DIRS_ENV_VAR, f'{sutDir1},{sutDir2}')
    env = OttoEnv()

    assert env.sutDirs == [sutDir1, sutDir2]


def test_env_sutdirs_set_to_multiple_paths_one_does_not_exist(monkeypatch, tmpdir) -> None:

    monkeypatch.setenv(SUT_DIRS_ENV_VAR, f'{tmpdir},{tmpdir}_typo')

    with pytest.raises(FileNotFoundError):
        OttoEnv()


def test_env_xdir_value() -> None:
    """Notify users if this test fails or if its logic changes because this is an external interface."""

    assert XDIR_ENV_VAR == 'OTTO_XDIR'
