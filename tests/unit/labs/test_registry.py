"""Unit tests for the host-source (LabRepository) backend registry."""

import pytest

from otto.host.os_profile import ProfileContext
from otto.labs import (
    JsonFileLabRepository,
    LabRepositoryError,
    LabSourceConstructionError,
    LabSourceEnv,
    register_lab_repository,
)
from otto.labs.registry import LAB_REPOSITORIES


def _env(tmp_path):
    return LabSourceEnv(tmp_path, "r/src", "settings.toml", ProfileContext.empty())


def test_json_builtin_builds_a_json_repository_over_anchored_paths(tmp_path):
    prepared = LAB_REPOSITORIES.prepare("json", {"paths": ["lab_data"]}, _env(tmp_path))
    built = LAB_REPOSITORIES.build(prepared)
    assert isinstance(built, JsonFileLabRepository)
    assert built.search_paths == [tmp_path / "lab_data"]


def test_register_and_build(tmp_path):
    class MyRepo:
        def load_lab(self, name, preferences=None, inventory=None):
            raise NotImplementedError

        def list_labs(self):
            return []

    class MyConfig:
        @classmethod
        def model_validate(cls, raw, context=None):
            return cls()

        def model_dump(self, mode="python"):
            return {}

    register_lab_repository("mine-test", config=MyConfig, factory=lambda c: MyRepo())
    try:
        prepared = LAB_REPOSITORIES.prepare("mine-test", {}, _env(tmp_path))
        assert isinstance(LAB_REPOSITORIES.build(prepared), MyRepo)
    finally:
        LAB_REPOSITORIES.unregister("mine-test")


def test_unknown_name_lists_registered(tmp_path):
    with pytest.raises(LabSourceConstructionError, match="Unknown lab repository backend") as err:
        LAB_REPOSITORIES.prepare("does-not-exist", {}, _env(tmp_path))
    assert isinstance(err.value, LabRepositoryError)
    assert "json" in str(err.value)
