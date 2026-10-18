"""Lab sources: the envelope at settings parse, preparation after init."""

import contextlib
import sys

import pytest

from otto.labs import LabRepositoryError
from otto.labs.errors import LabSourceConstructionError
from otto.labs.sources import build_lab_sources, prepared_lab_sources
from otto.registry import registering_project
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo


def _repo(tmp_path, body: str):
    """A parsed Repo whose settings declare *body* under [lab]."""
    from otto.config.project import Project

    make_sut_repo(tmp_path, name="r", extra=body)
    project = Project(root=tmp_path)
    project.parse_settings()
    return project


def test_settings_parse_validates_only_the_envelope(tmp_path):
    """A json source without paths parses; the shape error comes at preparation."""
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\n')
    with pytest.raises(LabSourceConstructionError, match="parse"):
        prepared_lab_sources(project)


def test_an_unknown_json_key_is_a_preparation_error_naming_the_key(tmp_path):
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["d"]\nbogus = 1\n')
    with pytest.raises(LabSourceConstructionError, match="bogus"):
        prepared_lab_sources(project)


def test_parse_imports_no_backend_config_model(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "otto.labs.json_repository", raising=False)
    _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    assert "otto.labs.json_repository" not in sys.modules


def test_an_unregistered_backend_is_unknown_not_unfiled(tmp_path):
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "nowhere"\n')
    (state,) = prepared_lab_sources(project)
    assert not state.is_known()
    with pytest.raises(LabSourceConstructionError, match="lookup") as err:
        build_lab_sources([project])
    assert isinstance(err.value, LabRepositoryError)


def test_a_json_source_is_file_backed_and_reads_its_anchored_files(tmp_path):
    write_lab_json(tmp_path / "lab_data" / "lab.json", [])
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    (state,) = prepared_lab_sources(project)
    assert state.is_known()
    assert state.is_file_backed()
    assert state.lab_files() == [tmp_path / "lab_data" / "lab.json"]


def test_preparation_is_cached_per_revision(tmp_path, monkeypatch):
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    from otto.labs import json_repository

    calls = []
    real = json_repository.JsonLabSourceConfig.model_validate
    monkeypatch.setattr(
        json_repository.JsonLabSourceConfig,
        "model_validate",
        classmethod(lambda cls, *a, **k: calls.append(1) or real(*a, **k)),
    )
    prepared_lab_sources(project)
    prepared_lab_sources(project)
    assert calls == [1]
    # a registry change (a replaced backend) must prepare again, against the new entry
    from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository

    entry = LAB_REPOSITORIES.peek("json")
    register_lab_repository("json", config=entry.config, factory=entry.factory, overwrite=True)
    (state,) = prepared_lab_sources(project)
    assert calls == [1, 1]
    assert state.prepared is not None
    assert state.prepared.generation == LAB_REPOSITORIES._generation("json")


def test_a_reparsed_repo_prepares_its_new_sources(tmp_path):
    """The memo is keyed by the declarations too: a settings edit is never served stale."""
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["one"]\n')
    (before,) = prepared_lab_sources(project)
    (tmp_path / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: a reparse
        'name = "r"\nversion = "1.0.0"\n[[lab.sources]]\nbackend = "json"\npaths = ["two"]\n'
    )
    project.parse_settings()
    (after,) = prepared_lab_sources(project)
    assert before.prepared is not None
    assert after.prepared is not None
    assert after.prepared.facts.file_inputs == (tmp_path / "two",)


def test_a_reparsed_repo_keeps_one_memo_entry(tmp_path):
    """A re-parse REPLACES the repo's prepared sources: the memo stays one entry per repo."""
    from otto.labs import sources

    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["one"]\n')
    held = len(sources._PREPARED)
    prepared_lab_sources(project)
    for paths in ('["two"]', '["three"]'):
        (tmp_path / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: a reparse
            f'name = "r"\nversion = "1.0.0"\n[[lab.sources]]\nbackend = "json"\npaths = {paths}\n'
        )
        project.parse_settings()
        prepared_lab_sources(project)
    assert len(sources._PREPARED) - held == 1


def test_the_json_config_model_validated_without_its_env_is_a_validation_error():
    """Outside otto's preparation there is no LabSourceEnv: a ValidationError, not a KeyError."""
    from pydantic import ValidationError

    from otto.labs.json_repository import JsonLabSourceConfig

    with pytest.raises(ValidationError, match="LabSourceEnv in the validation context"):
        JsonLabSourceConfig.model_validate({"paths": ["d"]})


def test_preparation_and_build_refuse_during_an_init_import(tmp_path):
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    with registering_project("r"):
        with pytest.raises(LabRepositoryError, match="init import"):
            prepared_lab_sources(project)
        with pytest.raises(LabRepositoryError, match="init import"):
            build_lab_sources([project])


def test_otto_session_build_lab_refuses_during_an_init_import(tmp_path):
    from otto.session import LabBuildError, build_lab

    write_lab_json(tmp_path / "lab_data" / "lab.json", [])
    project = _repo(tmp_path, '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    with registering_project("r"), pytest.raises(LabBuildError, match="init import"):
        build_lab([project], ["lab"])


def test_load_lab_fallback_builds_through_the_registry(tmp_path):
    """``load_lab(search_paths=)`` builds its json source through LAB_REPOSITORIES."""
    from otto.labs.json_repository import JsonFileLabRepository
    from otto.labs.loader import load_lab
    from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository

    write_lab_json(tmp_path / "lab_data" / "lab.json", [])
    built: list[object] = []
    entry = LAB_REPOSITORIES.peek("json")

    def spy(c):
        repository = JsonFileLabRepository(search_paths=list(c.config.paths))
        built.append(repository)
        return repository

    register_lab_repository("json", config=entry.config, factory=spy, overwrite=True)
    with contextlib.suppress(LabRepositoryError):  # no lab is declared; the build is the point
        load_lab("lab", search_paths=[tmp_path / "lab_data"])
    assert len(built) == 1


def test_load_lab_with_no_search_paths_fails_loud_with_guidance():
    from otto.labs.loader import load_lab

    with pytest.raises(LabRepositoryError):
        load_lab("lab")
