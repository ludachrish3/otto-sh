"""What TAB offers on the docker verbs: declared names, then what the daemon last said."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from otto.cli import docker as docker_cli
from otto.config.completion_cache import ObservedDockerState

NAMES = {
    "hosts": ["test3", "alt2", "test3.integration.api", "alt2.integration.db", "local"],
    "docker_hosts": ["test3", "alt2"],
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
            "otto.config.completion_cache.read_docker_observed_hosts",
            return_value=sorted(OBSERVED),
        ),
        patch(
            "otto.config.completion_cache.read_docker_observed",
            side_effect=lambda _r, h: OBSERVED.get(h, ObservedDockerState([], [], [], [])),
        ),
        patch("otto.bootstrap.discover", return_value=SimpleNamespace(repos=["repo"])),
    ):
        yield


def test_container_completer_without_on_offers_host_ids_then_names_then_ids(observed):
    assert docker_cli._container_completer(_ctx(on=None), "") == [
        "alt2.integration.db",
        "test3.integration.api",
        "unix-i-db-1",
        "unix-i-api-1",
        "77ee",
        "3f9a",
    ]


def test_container_completer_with_on_offers_that_hosts_observed_names_only(observed):
    assert docker_cli._container_completer(_ctx(on="test3"), "") == ["unix-i-api-1", "3f9a"]
    assert docker_cli._container_completer(_ctx(on="nowhere"), "") == []


def test_container_completer_prefix_filters_every_tier(observed):
    assert docker_cli._container_completer(_ctx(on=None), "unix-i-a") == ["unix-i-api-1"]
    assert docker_cli._container_completer(_ctx(on=None), "test3") == ["test3.integration.api"]


def test_container_host_ids_are_the_dotted_ids_under_a_docker_host():
    assert docker_cli._container_candidates(
        ["test3", "test3.x.y", "weird.name", "local"], ["test3"], {}, None
    ) == ["test3.x.y"]


def test_container_candidates_sort_hosts_and_keep_the_first_of_a_duplicate():
    # The dict arrives UNSORTED and `web` is observed on both hosts (and is also a
    # docker-capable host id, which is undotted so never a candidate): one offer, at
    # its first position. The shim mirrors this order.
    observed = {
        "zeta": ObservedDockerState([], [], ["web", "z-only"], ["id-z"]),
        "alpha": ObservedDockerState([], [], ["a-only", "web"], ["id-a", "id-z"]),
    }
    assert docker_cli._container_candidates(
        ["zeta.s.web", "web"], ["zeta", "web"], observed, None
    ) == ["zeta.s.web", "a-only", "web", "z-only", "id-a", "id-z"]
    assert docker_cli._container_candidates([], [], observed, "") == []


def test_tag_completer_offers_the_on_hosts_references_and_nothing_without_on(observed):
    assert docker_cli._tag_completer(_ctx(on="test3"), "") == ["api:latest"]
    assert docker_cli._tag_completer(_ctx(on="alt2"), "") == []
    assert docker_cli._tag_completer(_ctx(on=None), "") == []


def test_the_observed_completers_never_bootstrap_inside_a_tab(observed):
    # `get_repos()` is phase-2 bootstrap: it imports the user's init code. A TAB
    # reaches the cache file through discovery (settings + lab data) alone.
    with patch(
        "otto.config.bootstrapped.get_repos",
        side_effect=AssertionError("bootstrap inside a TAB"),
    ) as get_repos:
        assert docker_cli._container_completer(_ctx(on="test3"), "") == ["unix-i-api-1", "3f9a"]
        assert docker_cli._container_completer(_ctx(on=None), "unix-i-d") == ["unix-i-db-1"]
        assert docker_cli._tag_completer(_ctx(on="test3"), "") == ["api:latest"]
    get_repos.assert_not_called()


def test_container_completer_with_on_reads_that_host_alone(observed):
    with patch("otto.config.completion_cache.read_docker_observed_hosts") as hosts:
        assert docker_cli._container_completer(_ctx(on="alt2"), "") == ["unix-i-db-1", "77ee"]
    hosts.assert_not_called()


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
        patch("otto.config.completion_cache.read_docker_observed_hosts", side_effect=boom),
        patch("otto.config.completion_cache.read_docker_observed", side_effect=boom),
    ):
        assert docker_cli._image_completer(_ctx(), "") == []
        assert docker_cli._service_completer(_ctx(use_case="integration"), "") == []
        assert docker_cli._repo_completer(_ctx(), "") == []
        assert docker_cli._container_completer(_ctx(on=None), "") == []
        assert docker_cli._container_completer(_ctx(on="test3"), "") == []
        assert docker_cli._tag_completer(_ctx(on="test3"), "") == []


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
