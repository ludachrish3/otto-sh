"""[dependencies] table validation on SettingsModel."""

import pytest
from pydantic import ValidationError

from otto.models.settings import SettingsModel
from tests._fixtures.sutrepo import make_sut_repo

BASE = {"name": "widget", "version": "1.0.0"}


def _model(**dependencies):
    return SettingsModel.model_validate({**BASE, "dependencies": dependencies})


def test_dependencies_default_empty():
    model = SettingsModel.model_validate(BASE)
    assert model.dependencies.required == []
    assert model.dependencies.optional == []


def test_valid_entries_accepted():
    model = _model(required=["vantage >= 2.1, < 3"], optional=["metrics"])
    assert model.dependencies.required == ["vantage >= 2.1, < 3"]


def test_malformed_entry_rejected():
    with pytest.raises(ValidationError, match="invalid clause"):
        _model(required=["vantage ~= 1.2"])


def test_self_contradictory_entry_rejected():
    with pytest.raises(ValidationError, match="can never be satisfied"):
        _model(required=["vantage >= 3, < 2"])


def test_self_dependency_rejected():
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        _model(required=["Widget >= 1"])  # normalized match against name


def test_same_name_in_both_lists_rejected():
    with pytest.raises(ValidationError, match="both required and optional"):
        _model(required=["My_Lib"], optional=["my-lib >= 1"])


def test_unknown_dependencies_key_rejected():
    with pytest.raises(
        ValidationError, match=r"(?m)^dependencies\.requird\n\s+Extra inputs are not permitted"
    ):
        _model(requird=["x"])  # typo'd key — extra='forbid'


def test_repo_parses_declared_dependencies(tmp_path):
    from otto.config.repo import Repo

    make_sut_repo(
        tmp_path,
        name="widget",
        extra='[dependencies]\nrequired = ["vantage >= 2.1"]\noptional = ["metrics"]\n',
    )
    repo = Repo(sut_dir=tmp_path)
    assert [(d.normalized, d.required) for d in repo.declared_dependencies] == [
        ("vantage", True),
        ("metrics", False),
    ]
    assert repo.dependencies == []  # resolution has not run
