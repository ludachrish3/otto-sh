"""--parent: given, defaulted, or refused at exit 2 with the hosts named."""

from unittest.mock import AsyncMock, patch

import pytest

from otto.cli import docker as docker_cli
from tests._fixtures.dispatch import DispatchRunner
from tests.conftest import active_context
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
    with active_context(_lab(alt2=0, test3=0)):
        yield


@pytest.fixture
def lab_one_parent():
    with active_context(_lab(test3=0)):
        yield


def _invoke(*argv):
    return DispatchRunner().invoke(docker_cli.docker_app, list(argv), spec_name="docker")


def _flat(output: str) -> str:
    """The error panel's text on one line: CI renders Rich at 80 columns and wraps it."""
    return " ".join(output.replace("│", " ").split())


def _unspaced(text: str) -> str:
    """*text* with the panel's borders and every whitespace character removed.

    For a message that carries a path: at 80 columns Rich breaks a path longer
    than the panel mid-word (``…/pro`` then ``j/.otto``), which :func:`_flat`
    would rejoin with a space. CI's ``tmp_path`` is long enough to be broken.
    """
    return "".join(text.replace("│", " ").split())


def _empty_build_report():
    from otto.docker.reports import BuildReport

    return BuildReport(projects=[])


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
    assert build.await_args.kwargs["parent"] is None


def test_a_given_parent_reaches_the_library_verbatim(lab_one_parent):
    with (
        patch(
            "otto.docker.build_verbs.build_on", AsyncMock(return_value=_empty_build_report())
        ) as build,
        patch.object(docker_cli, "_record_observed", AsyncMock()),
    ):
        result = _invoke("build", "--parent", "test3")
    assert result.exit_code == 0, result.output
    assert build.await_args.kwargs["parent"] == "test3"


def test_the_old_flag_is_gone(lab_one_parent):
    result = _invoke("ps", "--on", "test3")
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output


def test_parent_flag_out_of_scope_refused(tmp_path):
    """``--parent`` naming a lab host outside the scope exits 2 with the scope's own text."""
    from tests._fixtures.fleet import install_scoped_double

    lab = _lab(alt2=0, test3=0)
    install_scoped_double(tmp_path, lab, targets=["test3"])
    files = tmp_path / "proj" / ".otto" / "settings.toml"
    result = DispatchRunner().invoke(
        docker_cli.docker_app,
        ["ps", "--parent", "alt2"],
        spec_name="docker",
        env={"COLUMNS": "400"},
    )
    assert result.exit_code == 2, result.output
    assert _unspaced(
        "host 'alt2' is in lab 'east' but outside this run's scope: no declared project "
        "targets it. To reach it, add its id to [project] host_patterns (or its lab to "
        f"lab_patterns) in {files}."
    ) in _unspaced(result.output)
    assert "--parent" in result.output
