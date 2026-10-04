"""What TAB offers on the docker verbs: declared names, then what the daemon last said."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from otto.cli import docker as docker_cli
from otto.config.completion_cache import ObservedDockerState

NAMES = {
    "hosts": ["test3", "alt2", "local"],
    "hosts_by_lab": {"east": ["test3", "alt2"], "west": ["alt2"]},
    "docker_hosts": ["test3", "alt2"],
    "docker_default_parent_by_lab": {"east": "test3"},  # west: two at 0, the rule refuses
    "docker_images": ["api", "db"],
    "docker_services_by_use_case": {"integration": ["api", "db"], "smoke": ["api"]},
    "repos": ["one", "two"],
}


def _ctx(**params):
    return SimpleNamespace(params=params, parent=None, resilient_parsing=True)


@pytest.fixture(autouse=True)
def names():
    with patch("otto.config.bootstrapped.get_completion_names", return_value=NAMES):
        yield


def test_image_completer_offers_the_declared_images():
    assert docker_cli._image_completer(_ctx(), "") == ["api", "db"]
    assert docker_cli._image_completer(_ctx(), "d") == ["db"]


def test_service_completer_offers_the_use_case_on_the_line():
    assert docker_cli._service_completer(_ctx(use_case="integration"), "") == ["api", "db"]
    assert docker_cli._service_completer(_ctx(use_case="smoke"), "") == ["api"]


def test_service_completer_offers_nothing_until_a_use_case_is_on_the_line():
    assert docker_cli._service_completer(_ctx(use_case=None), "") == []
    assert docker_cli._service_completer(_ctx(use_case="unknown"), "") == []


def test_repo_completer_offers_the_repo_names():
    assert docker_cli._repo_completer(_ctx(), "t") == ["two"]


OBSERVED = {
    "test3": ObservedDockerState(["api:latest"], ["sha-a"], ["unix-i-api-1"], ["3f9a"]),
    "alt2": ObservedDockerState([], [], ["unix-i-db-1"], ["77ee"]),
}


@pytest.fixture
def observed():
    with (
        patch(
            "otto.config.completion_cache.read_docker_observed",
            side_effect=lambda _r, h: OBSERVED.get(h, ObservedDockerState([], [], [], [])),
        ),
        patch("otto.bootstrap.discover", return_value=SimpleNamespace(repos=["repo"])),
    ):
        yield


def test_container_completer_with_parent_offers_that_hosts_names_then_ids(observed):
    assert docker_cli._container_completer(_ctx(parent="test3"), "") == ["unix-i-api-1", "3f9a"]
    assert docker_cli._container_completer(_ctx(parent="nowhere"), "") == []


def test_container_completer_without_parent_offers_the_default_parents(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["east"])
    assert docker_cli._container_completer(_ctx(parent=None), "") == ["unix-i-api-1", "3f9a"]


def test_container_completer_without_parent_offers_nothing_when_the_rule_refuses(
    observed, monkeypatch
):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["west"])
    assert docker_cli._container_completer(_ctx(parent=None), "") == []


def test_container_completer_prefix_filters(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["east"])
    assert docker_cli._container_completer(_ctx(parent=None), "unix-i-a") == ["unix-i-api-1"]
    assert docker_cli._container_completer(_ctx(parent=None), "3f") == ["3f9a"]


def test_tag_completer_follows_the_same_rule(observed, monkeypatch):
    assert docker_cli._tag_completer(_ctx(parent="test3"), "") == ["api:latest"]
    assert docker_cli._tag_completer(_ctx(parent="alt2"), "") == []
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["east"])
    assert docker_cli._tag_completer(_ctx(parent=None), "") == ["api:latest"]
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["west"])
    assert docker_cli._tag_completer(_ctx(parent=None), "") == []


def test_no_lab_selected_uses_the_maps_only_entry(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: [])
    assert docker_cli._container_completer(_ctx(parent=None), "") == ["unix-i-api-1", "3f9a"]


def test_no_lab_selected_and_two_different_defaults_offers_nothing(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: [])
    two = {**NAMES, "docker_default_parent_by_lab": {"east": "test3", "west": "alt2"}}
    with patch("otto.config.bootstrapped.get_completion_names", return_value=two):
        assert docker_cli._container_completer(_ctx(parent=None), "") == []


def test_a_cold_cache_offers_nothing_without_a_parent(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: [])
    with patch("otto.config.bootstrapped.get_completion_names", return_value=None):
        assert docker_cli._container_completer(_ctx(parent=None), "") == []
    with patch("otto.config.bootstrapped.get_completion_names", return_value={}):
        assert docker_cli._tag_completer(_ctx(parent=None), "") == []


def test_the_observed_completers_never_bootstrap_inside_a_tab(observed, monkeypatch):
    # `get_repos()` is phase-2 bootstrap: it imports the user's init code. A TAB
    # reaches the cache file through discovery (settings + lab data) alone, with or
    # without a parent on the line.
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["east"])
    with patch(
        "otto.config.bootstrapped.get_repos",
        side_effect=AssertionError("bootstrap inside a TAB"),
    ) as get_repos:
        assert docker_cli._container_completer(_ctx(parent="test3"), "") == ["unix-i-api-1", "3f9a"]
        assert docker_cli._container_completer(_ctx(parent=None), "unix-i-a") == ["unix-i-api-1"]
        assert docker_cli._tag_completer(_ctx(parent="test3"), "") == ["api:latest"]
        assert docker_cli._tag_completer(_ctx(parent=None), "") == ["api:latest"]
    get_repos.assert_not_called()


def test_container_completer_reads_the_one_parents_state_alone(observed, monkeypatch):
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: ["east"])
    # A TAB reads the cache and discovery alone: it never loads a lab.
    with patch("otto.config.fleet.get_lab", side_effect=AssertionError("lab load in a TAB")) as lab:
        assert docker_cli._container_completer(_ctx(parent="alt2"), "") == ["unix-i-db-1", "77ee"]
        assert docker_cli._container_completer(_ctx(parent=None), "") == ["unix-i-api-1", "3f9a"]
    lab.assert_not_called()


def test_a_cold_cache_falls_back_to_the_collectors_for_declared_names():
    with (
        patch("otto.config.bootstrapped.get_completion_names", return_value=None),
        patch("otto.config.completion_cache.collect_docker_image_names", return_value=["cold"]),
        patch("otto.config.bootstrapped.get_repos", return_value=["repo"]),
    ):
        assert docker_cli._image_completer(_ctx(), "") == ["cold"]


def test_the_completers_never_raise():
    boom = RuntimeError("boom")
    with (
        patch("otto.config.bootstrapped.get_completion_names", side_effect=boom),
        patch("otto.config.bootstrapped.get_repos", side_effect=boom),
        patch("otto.bootstrap.discover", side_effect=boom),
        patch("otto.config.completion_cache.read_docker_observed", side_effect=boom),
    ):
        assert docker_cli._image_completer(_ctx(), "") == []
        assert docker_cli._service_completer(_ctx(use_case="integration"), "") == []
        assert docker_cli._repo_completer(_ctx(), "") == []
        assert docker_cli._container_completer(_ctx(parent=None), "") == []
        assert docker_cli._container_completer(_ctx(parent="test3"), "") == []
        assert docker_cli._tag_completer(_ctx(parent="test3"), "") == []


@pytest.mark.parametrize(
    ("leaf", "param", "completer"),
    [
        ("_build", "image", "_image_completer"),
        ("_build", "repo", "_repo_completer"),
        ("_build", "tag", "_tag_completer"),
        ("_compose_build", "image", "_image_completer"),
        ("_compose_up", "service", "_service_completer"),
        ("_compose_down", "service", "_service_completer"),
        ("_compose_logs", "service", "_service_completer"),
        ("_logs", "container", "_container_completer"),
    ],
)
def test_each_parameter_is_wired_to_its_completer(leaf, param, completer):
    import inspect

    # The raw annotation, not get_type_hints: on 3.10 get_type_hints wraps a `= None`
    # default's Annotated in an Optional that hides __metadata__.
    annotation = inspect.signature(getattr(docker_cli, leaf)).parameters[param].annotation
    (info,) = [m for m in annotation.__metadata__ if hasattr(m, "autocompletion")]
    assert info.autocompletion is getattr(docker_cli, completer)
