"""--parent: given, defaulted, or refused at exit 2 with the hosts named."""

from unittest.mock import AsyncMock, patch

import pytest

from otto.cli import docker as docker_cli
from tests._fixtures.dispatch import DispatchRunner
from tests.unit.docker.test_deploy import _host
from tests.unit.docker.test_deploy import _lab as _lab_of


def _lab(**hosts):
    """hosts: id -> docker_priority, every one docker-capable. A lab named 'east'."""
    built = []
    for n, (hid, priority) in enumerate(hosts.items(), start=1):
        host = _host(hid, f"192.0.2.{n}")  # TEST-NET-1: no regression can reach a real host
        host.docker_priority = priority
        built.append(host)
    return _lab_of(*built, name="east")


@pytest.fixture
def lab_two_parents_tied():
    with patch("otto.config.fleet.get_lab", return_value=_lab(alt2=0, test3=0)):
        yield


@pytest.fixture
def lab_one_parent():
    with patch("otto.config.fleet.get_lab", return_value=_lab(test3=0)):
        yield


def _invoke(*argv):
    return DispatchRunner().invoke(docker_cli.docker_app, list(argv), spec_name="docker")


def _flat(output: str) -> str:
    """The error panel's text on one line: CI renders Rich at 80 columns and wraps it."""
    return " ".join(output.replace("│", " ").split())


def _empty_build_report():
    from otto.docker.reports import BuildReport

    return BuildReport(repos=[])


def test_an_unknown_parent_exits_2_and_names_the_capable_hosts(lab_two_parents_tied):
    result = _invoke("ps", "--parent", "nope")
    assert result.exit_code == 2, result.output
    flat = _flat(result.output)
    assert "'nope' is not a docker-capable unix host" in flat
    assert "docker-capable hosts here: ['alt2', 'test3']" in flat
    assert "--parent" in result.output


def test_an_omitted_parent_on_a_tied_lab_exits_2_and_names_the_tie(lab_two_parents_tied):
    result = _invoke("logs", "web-1")
    assert result.exit_code == 2, result.output
    assert "2 docker-capable hosts at priority 0 (alt2, test3)" in _flat(result.output)
    assert "--parent" in result.output


def test_an_omitted_parent_on_a_one_host_lab_builds_there(lab_one_parent):
    with (
        patch(
            "otto.docker.build_verbs.build_on", AsyncMock(return_value=_empty_build_report())
        ) as build,
        patch.object(docker_cli, "_record_observed", AsyncMock()),
    ):
        result = _invoke("build")
    assert result.exit_code == 0, result.output
    # The library resolves the default, never the CLI.
    assert build.await_args.args == (None,)


def test_a_given_parent_reaches_the_library_verbatim(lab_one_parent):
    with (
        patch(
            "otto.docker.build_verbs.build_on", AsyncMock(return_value=_empty_build_report())
        ) as build,
        patch.object(docker_cli, "_record_observed", AsyncMock()),
    ):
        result = _invoke("build", "--parent", "test3")
    assert result.exit_code == 0, result.output
    assert build.await_args.args == ("test3",)


def test_the_old_flag_is_gone(lab_one_parent):
    result = _invoke("ps", "--on", "test3")
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output
