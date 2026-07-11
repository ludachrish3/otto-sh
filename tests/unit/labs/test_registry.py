"""Unit tests for the host-source (LabRepository) backend registry."""

import pytest

from otto.labs import (
    JsonFileLabRepository,
    LabRepositoryError,
    register_lab_repository,
)
from otto.labs.registry import (
    LAB_REPOSITORIES,
    get_lab_repository_class,
)


def test_json_builtin_registered():
    assert get_lab_repository_class("json") is JsonFileLabRepository


def test_register_and_lookup():
    class MyRepo:
        def load_lab(self, name, preferences=None):
            raise NotImplementedError

        def list_labs(self):
            return []

    register_lab_repository("mine-test", MyRepo)
    try:
        assert get_lab_repository_class("mine-test") is MyRepo
    finally:
        LAB_REPOSITORIES.unregister("mine-test")


def test_unknown_name_lists_registered():
    with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
        get_lab_repository_class("does-not-exist")
