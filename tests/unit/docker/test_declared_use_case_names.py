"""Which use-cases are declared: one function answers, and every caller asks it.

Fragments sharing a name, in one project or across several, are one use-case
(spec §3.1). The staging keys a project owns, the use-case grouping the
resolver selects from, the completion names, the services completion keys
and the default use-case all read the declared names; each is driven here
through :func:`otto.config.scope.declared_use_case_names` and must reach it.
"""

from unittest.mock import patch

import pytest

from otto.cli import docker as docker_cli
from otto.config import completion_cache, scope
from otto.config.project import Project
from otto.config.scope import DockerSettings, DockerUseCase, declared_use_case_names
from otto.docker import compose, resolve
from otto.docker.compose import project_staging_keys, use_case_project
from tests._fixtures.fake_project import fake_project


def _project(name: str, *use_cases: str) -> Project:
    fragments = tuple(DockerUseCase(name=uc, composes=("c",)) for uc in use_cases)
    return fake_project(name, docker_settings=DockerSettings(use_cases=fragments))


# A name repeated in one project and across projects, declared out of sorted order.
PROJECTS = [_project("a", "soak", "integration", "soak"), _project("b", "integration", "alpha")]
NAMES = ["soak", "integration", "alpha"]


@pytest.fixture
def asked(monkeypatch):
    """Every projects list the one function is asked about, wherever a caller binds it."""
    seen: list[list[Project]] = []

    def spy(projects):
        projects = list(projects)
        seen.append(projects)
        return declared_use_case_names(projects)

    # compose and resolve bind the name at import; the others import it from scope at call time.
    for module in (scope, compose, resolve):
        monkeypatch.setattr(module, "declared_use_case_names", spy, raising=False)
    return seen


def test_each_name_counts_once_in_declaration_order():
    assert declared_use_case_names(PROJECTS) == NAMES


def test_a_project_without_use_cases_declares_none():
    assert declared_use_case_names([fake_project("a", docker_settings=DockerSettings())]) == []


def test_the_resolver_groups_under_the_declared_names(asked):
    assert list(resolve.declared_use_cases(PROJECTS)) == NAMES
    assert asked == [PROJECTS]


def test_the_completion_names_are_the_declared_names_sorted(asked):
    assert completion_cache.collect_docker_use_case_names(PROJECTS) == sorted(NAMES)
    assert asked == [PROJECTS]


def test_the_services_completion_is_keyed_by_the_declared_names(asked):
    # No compose handle is declared, so every use-case completes no service.
    assert completion_cache.collect_docker_services_by_use_case(PROJECTS) == {
        n: [] for n in sorted(NAMES)
    }
    assert asked == [PROJECTS]


def test_a_project_stages_one_stack_per_declared_name(asked):
    keys = project_staging_keys(PROJECTS[0], "lab")
    assert [k.key for k in keys] == [use_case_project("lab", n) for n in ("soak", "integration")]
    assert all(k.stack for k in keys)
    assert asked == [[PROJECTS[0]]]


def test_the_default_use_case_is_the_one_declared_name(asked):
    one = [_project("a", "integration"), _project("b", "integration")]
    with patch("otto.declared.get_projects", return_value=one):
        assert docker_cli._default_use_case(None) == "integration"
    assert asked == [one]
