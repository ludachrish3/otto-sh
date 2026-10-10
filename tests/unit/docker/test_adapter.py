"""Adapter registration mirrors register_project_actions' attribution rules."""

import pytest

from otto.docker import adapter as adapter_mod
from otto.docker.adapter import AdapterResult, adapter_for, register_compose_adapter
from otto.registry import DuplicateRegistration, RegistrationRefused, registering_repo


@pytest.fixture
def _as_repo():
    with registering_repo("repo1"):
        yield


@pytest.mark.usefixtures("_as_repo")
def test_register_and_lookup():
    @register_compose_adapter("integration")
    def render(facts):
        return AdapterResult(env={"X": "1"})

    fn = adapter_for("repo1", "integration")
    assert fn is render
    assert adapter_for("repo1", "other") is None
    assert adapter_for("repo2", "integration") is None


@pytest.mark.usefixtures("_as_repo")
def test_duplicate_registration_is_loud():
    """A second adapter for one (repo, use case) names the key and the way to replace it."""

    @register_compose_adapter("integration")
    def one(facts):
        return AdapterResult()

    with pytest.raises(
        DuplicateRegistration,
        match=r"compose adapter 'repo1:integration' is already registered.*"
        r"Pass overwrite=True to replace it deliberately\.",
    ):

        @register_compose_adapter("integration")
        def two(facts):
            return AdapterResult()

    assert adapter_for("repo1", "integration") is one


@pytest.mark.usefixtures("_as_repo")
def test_overwrite_replaces_the_repos_adapter():
    @register_compose_adapter("integration")
    def one(facts):
        return AdapterResult()

    @register_compose_adapter("integration", overwrite=True)
    def two(facts):
        return AdapterResult()

    assert adapter_for("repo1", "integration") is two


def test_outside_init_module_is_refused():
    names, revision = adapter_mod.COMPOSE_ADAPTERS.names(), adapter_mod.COMPOSE_ADAPTERS.revision
    with pytest.raises(
        RegistrationRefused,
        match=r"^compose adapter 'integration' must be registered from a repo init module",
    ):
        register_compose_adapter("integration")(lambda facts: AdapterResult())
    assert adapter_mod.COMPOSE_ADAPTERS.names() == names
    assert adapter_mod.COMPOSE_ADAPTERS.revision == revision


@pytest.mark.usefixtures("_as_repo")
def test_use_case_containing_colon_is_refused():
    """A ':' in use_case would collide with the 'repo_name:use_case' key separator."""
    with pytest.raises(
        ValueError,
        match=r"register_compose_adapter\(\): use_case 'a:b' must not contain ':'",
    ):
        register_compose_adapter("a:b")(lambda facts: AdapterResult())


def test_register_hint_names_the_registration_function():
    """COMPOSE_ADAPTERS.get() on an unknown key surfaces register_hint.

    adapter_for() itself never reaches this path (it pre-checks membership
    and returns None on a miss, same idiom as project.actions.actions_for()
    over PROJECT_ACTIONS) -- this test exercises the registry's own lookup
    failure directly so the hint text is verified correct rather than dead
    and untested, even though no current public call site renders it.
    """
    with pytest.raises(ValueError, match=r"otto\.docker\.register_compose_adapter\(\)"):
        adapter_mod.COMPOSE_ADAPTERS.get("no-such-repo:no-such-use-case")
