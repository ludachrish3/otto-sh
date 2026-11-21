"""Unit tests for the host-source (LabRepository) backend registry."""

import logging

import pytest

from otto.host.os_profile import ProfileContext
from otto.labs import (
    LabRepositoryError,
    LabSourceConstructionError,
    LabSourceEnv,
    register_lab_repository,
)
from otto.labs.json_repository import _JsonFileLabRepository
from otto.labs.registry import LAB_REPOSITORIES
from otto.registry import DuplicateRegistration, configured_backend
from tests._fixtures.registrant import from_module


def _env(tmp_path):
    return LabSourceEnv(
        project_root=tmp_path,
        label="r/src",
        origin="settings.toml",
        profiles=ProfileContext.empty(),
    )


def test_json_builtin_builds_a_json_repository_over_anchored_paths(tmp_path):
    prepared = LAB_REPOSITORIES.prepare(
        name="json", raw={"paths": ["lab_data"]}, env=_env(tmp_path)
    )
    built = LAB_REPOSITORIES.build(prepared=prepared)
    assert isinstance(built, _JsonFileLabRepository)
    assert built.search_paths == [tmp_path / "lab_data"]


def test_register_and_build(tmp_path):
    class MyRepository:
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

    register_lab_repository("mine-test", config=MyConfig, factory=lambda c: MyRepository())
    try:
        prepared = LAB_REPOSITORIES.prepare(name="mine-test", raw={}, env=_env(tmp_path))
        assert isinstance(LAB_REPOSITORIES.build(prepared=prepared), MyRepository)
    finally:
        LAB_REPOSITORIES.unregister(name="mine-test")


def test_unknown_name_lists_registered(tmp_path):
    with pytest.raises(LabSourceConstructionError, match="Unknown lab repository backend") as err:
        LAB_REPOSITORIES.prepare(name="does-not-exist", raw={}, env=_env(tmp_path))
    assert isinstance(err.value, LabRepositoryError)
    assert "json" in str(err.value)


class _TeamConfig:
    @classmethod
    def model_validate(cls, raw, context=None):
        return cls()

    def model_dump(self, mode="python"):
        return {}


def test_a_team_lab_source_replaces_a_yielding_builtin_with_one_warning(caplog):
    builtin = configured_backend(config=_TeamConfig, factory=lambda c: None, metadata=None)
    from_module(
        "otto.labs.builtin_double",
        LAB_REPOSITORIES.register,
        name="yielding-test",
        entry=builtin,
        yielding=True,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(
                "acme.init",
                register_lab_repository,
                "yielding-test",
                config=_TeamConfig,
                factory=lambda c: None,
            )
        assert LAB_REPOSITORIES.origin(name="yielding-test") == "acme.init"
        assert [r.getMessage() for r in caplog.records if r.name == "otto.registry"] == [
            (
                "lab repository backend 'yielding-test': acme.init replaces otto's built-in "
                "(otto.labs.builtin_double); otto's 'yielding-test' is unavailable in this process"
            )
        ]
    finally:
        LAB_REPOSITORIES.unregister(name="yielding-test")


def test_backend_register_many_passes_yielding_through(caplog):
    builtin = configured_backend(config=_TeamConfig, factory=lambda c: None, metadata=None)
    names = ["yielding-a", "yielding-b"]
    from_module(
        "otto.labs.builtin_double",
        LAB_REPOSITORIES.register_many,
        [(name, builtin) for name in names],
        yielding=True,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            for name in names:
                from_module(
                    "acme.init",
                    register_lab_repository,
                    name,
                    config=_TeamConfig,
                    factory=lambda c: None,
                )
        assert [LAB_REPOSITORIES.origin(name=n) for n in names] == ["acme.init", "acme.init"]
        assert len([r for r in caplog.records if r.name == "otto.registry"]) == 2
    finally:
        for name in names:
            LAB_REPOSITORIES.unregister(name=name)


def test_the_json_builtin_does_not_yield():
    with pytest.raises(
        DuplicateRegistration, match=r"already registered by 'otto\.labs\.registry'"
    ):
        register_lab_repository("json", config=_TeamConfig, factory=lambda c: None)
