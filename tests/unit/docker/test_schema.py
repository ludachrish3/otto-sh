"""Boundary + runtime tests for the [[docker.use_cases]] schema (spec §3.1)."""

from __future__ import annotations

import pytest

from otto.config.repo import Repo
from otto.models.settings import DockerUseCaseSpec
from tests._fixtures.sutrepo import make_sut_repo

_FILES = {"docker/Dockerfile": "FROM alpine\n", "docker/compose.yml": "services: {}\n"}

_UC_TOML = """
[[docker.composes]]
name = "core"
path = "docker/compose.yml"
services = ["api", "db"]

[[docker.use_cases]]
name = "integration"
composes = ["core"]
provides = "edge"
priority = 10
env = { LOG_LEVEL = "debug", PORT = 8080 }
pass_env = ["EDGE_TAG"]
"""


def test_use_case_round_trip(tmp_path):
    repo = Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=_UC_TOML, files=_FILES))
    (uc,) = repo.docker_settings.use_cases
    assert uc.name == "integration"
    assert uc.composes == ("core",)
    assert not hasattr(uc, "role"), "a use-case deploys on one parent: no role"
    assert not hasattr(uc, "placement"), "a use-case deploys on one parent: no pin"
    assert uc.provides == "edge"
    assert uc.priority == 10
    assert uc.env == {"LOG_LEVEL": "debug", "PORT": "8080"}  # scalars stringified
    assert uc.pass_env == ("EDGE_TAG",)


@pytest.mark.parametrize("line", ['role = "edge"', 'placement = { edge = "test3" }'])
def test_a_settings_file_with_role_or_placement_is_refused_at_load(tmp_path, line):
    """The old model's keys fail the repo load, never silently ignored."""
    toml = _UC_TOML.replace('provides = "edge"', f'{line}\nprovides = "edge"')
    assert line in toml
    with pytest.raises(Exception, match="is gone: a use-case deploys on one parent"):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))


def test_compose_name_defaults_to_path_stem(tmp_path):
    toml = '[[docker.composes]]\npath = "docker/compose.yml"\nservices = ["api"]\n'
    repo = Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))
    (c,) = repo.docker_settings.composes
    assert c.name == "compose"


def test_unknown_compose_handle_is_refused(tmp_path):
    toml = (
        '[[docker.composes]]\nname = "core"\npath = "docker/compose.yml"\nservices = ["api"]\n'
        '[[docker.use_cases]]\nname = "x"\ncomposes = ["nope"]\n'
    )
    with pytest.raises(Exception, match="unknown compose handle"):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))


def test_duplicate_compose_names_refused(tmp_path):
    toml = (
        '[[docker.composes]]\nname = "core"\npath = "docker/compose.yml"\nservices = ["a"]\n'
        '[[docker.composes]]\nname = "core"\npath = "docker/compose.yml"\nservices = ["b"]\n'
    )
    with pytest.raises(Exception, match="unique"):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))


def test_compose_users_round_trip(tmp_path):
    toml = (
        '[[docker.composes]]\npath = "docker/compose.yml"\n'
        'services = ["api", "db"]\n'
        'users = { db = "postgres", api = "1000:1000" }\n'
    )
    repo = Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))
    (c,) = repo.docker_settings.composes
    assert c.users == (("api", "1000:1000"), ("db", "postgres"))


def test_compose_users_unknown_service_refused(tmp_path):
    toml = (
        '[[docker.composes]]\npath = "docker/compose.yml"\nservices = ["api"]\n'
        'users = { db = "postgres" }\n'
    )
    with pytest.raises(Exception, match="users keys must name declared services"):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))


@pytest.mark.parametrize("bad", ["", "a b", " root", "\troot", "a\tb"])
def test_compose_users_bad_form_refused(tmp_path, bad):
    toml = (
        '[[docker.composes]]\npath = "docker/compose.yml"\nservices = ["api"]\n'
        f'users = {{ api = "{bad}" }}\n'
    )
    with pytest.raises(Exception, match="non-empty string with no whitespace"):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))


@pytest.mark.parametrize("form", ["root", "1000", "1000:1000", "postgres:staff"])
def test_compose_users_forms_pass_verbatim(tmp_path, form):
    toml = (
        '[[docker.composes]]\npath = "docker/compose.yml"\nservices = ["api"]\n'
        f'users = {{ api = "{form}" }}\n'
    )
    repo = Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))
    (c,) = repo.docker_settings.composes
    assert dict(c.users)["api"] == form


def test_priority_requires_provides():
    with pytest.raises(Exception, match="provides"):
        DockerUseCaseSpec(name="x", composes=["core"], priority=5)


def test_default_host_is_refused(tmp_path):
    """Hard cutover (spec §14): default_host is gone, not deprecated.

    [[docker.composes]] is a pure file inventory now — placement lives on
    [[docker.use_cases]] fragments. OttoModel forbids extras, so a straggler
    default_host fails loud with pydantic's own message; there is no shim
    and no warning period.
    """
    toml = (
        '[[docker.composes]]\npath = "docker/compose.yml"\n'
        'default_host = "test3"\nservices = ["api"]\n'
    )
    with pytest.raises(
        Exception,
        match=r"(?m)^docker\.composes\.0\.default_host\n\s+Extra inputs are not permitted",
    ):
        Repo(sut_dir=make_sut_repo(tmp_path / "r", name="r", extra=toml, files=_FILES))
