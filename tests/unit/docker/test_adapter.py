"""Adapter registration mirrors register_project_actions' attribution rules."""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from otto.docker import adapter as adapter_mod
from otto.docker.adapter import AdapterResult, adapter_for, register_compose_adapter


@pytest.fixture
def _as_repo():
    with patch.object(adapter_mod, "get_registering_repo", return_value="repo1"):
        yield
    with contextlib.suppress(ValueError):
        adapter_mod.COMPOSE_ADAPTERS.unregister("repo1:integration")


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
    """The collision names both the (repo, use_case) key and the purpose-written hint.

    Anchored on more than Registry's generic "already registered" phrase so a
    regression that drops the collision_hint (dead text otherwise, since
    nothing else reads it) would be caught here.
    """

    @register_compose_adapter("integration")
    def one(facts):
        return AdapterResult()

    with pytest.raises(
        ValueError,
        match=r"compose adapter 'repo1:integration' is already registered.*"
        r"One adapter per \(repo, use-case\); merge the logic into it\.",
    ):

        @register_compose_adapter("integration")
        def two(facts):
            return AdapterResult()


def test_outside_init_module_is_refused():
    with (
        patch.object(adapter_mod, "get_registering_repo", return_value=None),
        pytest.raises(
            ValueError,
            match=r"register_compose_adapter\(\) must be called from a repo init module",
        ),
    ):
        register_compose_adapter("integration")(lambda facts: AdapterResult())


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
