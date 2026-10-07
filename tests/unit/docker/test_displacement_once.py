"""Every provider displacement is shown ONCE on the console, whichever verb ran.

The sentence used to be worded in four places. A real ``compose up`` printed it
twice (the library's log line, then the CLI's stack report echoing it), and a
``--dry-run`` printed it twice (the log line, then the plan the decline
carries). These tests drive the real CLI leaf over the real library and count
the sentence in what the code actually produced: the rich output the leaf
printed plus the log records the library emitted. A mock's call list would
count what the test arranged.

Stubbed: the lab and its host's ``exec``/``put`` transport, the repo tables,
``scope_for_repo`` (no ``[project]`` scope narrows anything), the container-id
backoff, and, in the live ``compose build`` test only, ``_build_plan`` (no
image is built). Everything else, selection through rendering, is the shipped
code.

The counted token is the loser's repo plus priority (``a (priority 10)``), so
the count does not move when the sentence is reworded and a second report
phrased another way is still counted; the wording itself is pinned on
:meth:`~otto.docker.resolve.Displacement.describe`, and a sentinel patched over
it proves every site renders through it.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from otto.cli import docker as docker_cli
from otto.config.lab import Lab
from otto.docker import resolve as resolve_mod
from otto.docker.reports import ImageBuild, RepoBuild
from otto.docker.resolve import Displacement
from otto.host.element import Element
from otto.host.unix_host import UnixHost
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner
from tests.conftest import active_context

from .test_cli import _uc, _uc_repo
from .test_deploy import _compose_file, _frag, _host, _install, _lab, _repo, _wire

_LOSER = "a (priority 10)"
_SENTINEL = "SENTINEL-FROM-DESCRIBE"


@pytest.fixture(autouse=True)
def _admit_all_scopes():
    with patch.object(resolve_mod, "scope_for_repo", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_container_id_backoff():
    with patch("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0):
        yield


def _console_text(result, caplog) -> str:
    """What a user would see: the leaf's printed output plus every INFO+ log message.

    The default console level is INFO, so every record at or above it reaches
    the console handler; filtering here approximates that cut (it ignores the
    handler's host filter).
    """
    logged = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    return " ".join((result.output + "\n" + logged).split())


def _two_providers(tmp_path, *, images=()):
    """``a`` (priority 10) is displaced by ``b`` (priority 5) via ``--provide edge=b``."""
    compose = _compose_file(tmp_path, "core")
    a = _repo("a", _frag(provides="edge", priority=10), composes=[compose], images=images)
    b = _repo("b", _frag(provides="edge", priority=5), composes=[compose], images=images)
    host = _wire(_host("test3", "10.10.200.13"))
    return a, b, _lab(host)


def _invoke(*argv: str):
    return DispatchRunner().invoke(docker_cli.docker_app, list(argv), spec_name="docker")


def test_describe_names_who_won_at_what_and_who_stands_down_without_ranking():
    d = Displacement(
        capability="edge", loser_repo="a", loser_priority=10, winner_repo="b", winner_priority=5
    )
    assert d.describe() == "edge goes to b (priority 5); a (priority 10) stands down"
    assert "higher" not in d.describe()
    assert "lower" not in d.describe()


def test_compose_up_shows_a_displacement_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    a, b, lab = _two_providers(tmp_path)
    with (
        caplog.at_level(logging.INFO, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        result = _invoke("compose", "up", "integration", "--parent", "test3", "--provide", "edge=b")

    assert result.exit_code == 0, result.output
    assert "container(s) registered" in result.output, "the real deploy ran to its stack report"
    text = _console_text(result, caplog)
    assert text.count(_LOSER) == 1, text


def test_compose_up_dry_run_shows_a_displacement_once(tmp_path, caplog):
    a, b, lab = _two_providers(tmp_path)
    with (
        caplog.at_level(logging.INFO, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
        active_context(dry_run=True),
    ):
        result = _invoke("compose", "up", "integration", "--parent", "test3", "--provide", "edge=b")

    assert result.exit_code == 0, result.output
    assert "Resolved plan:" in " ".join(result.output.split()), "the dry run declined with its plan"
    text = _console_text(result, caplog)
    assert text.count(_LOSER) == 1, text


def test_compose_build_shows_a_displacement_once(tmp_path, caplog):
    a, b, lab = _two_providers(tmp_path, images=("api",))
    built = AsyncMock(
        return_value=[
            RepoBuild(
                "b",
                "test3",
                "built",
                {
                    "api": ImageBuild(
                        "api", ["api:latest"], "abc123abc123", CommandResult(Status.Success)
                    )
                },
            )
        ]
    )
    with (
        caplog.at_level(logging.INFO),
        _install(lab, [a, b]),
        patch("otto.docker.build_verbs._build_plan", built),
    ):
        result = _invoke(
            "compose", "build", "integration", "--parent", "test3", "--provide", "edge=b"
        )

    assert result.exit_code == 0, result.output
    built.assert_awaited_once()
    assert _console_text(result, caplog).count(_LOSER) == 1


def test_compose_build_dry_run_shows_a_displacement_once(tmp_path, caplog):
    a, b, lab = _two_providers(tmp_path, images=("api",))
    with (
        caplog.at_level(logging.INFO),
        _install(lab, [a, b]),
        active_context(dry_run=True),
    ):
        result = _invoke(
            "compose", "build", "integration", "--parent", "test3", "--provide", "edge=b"
        )

    assert result.exit_code == 0, result.output
    text = _console_text(result, caplog)
    assert "Build plan:" in text, "the dry run declined with its plan"
    assert text.count(_LOSER) == 1, text


def test_use_cases_shows_a_displacement_once(caplog):
    winner = _uc_repo("b", _uc(provides="edge", priority=10))
    loser = _uc_repo("a", _uc(provides="edge", priority=5))
    lab = Lab(name="unix")
    lab.add_host(UnixHost(ip="10.0.0.1", creds=[], element=Element("test3"), docker_capable=True))
    with (
        caplog.at_level(logging.INFO),
        patch("otto.bootstrap.get_repos", return_value=[winner, loser]),
        patch("otto.config.fleet.get_lab", return_value=lab),
    ):
        result = _invoke("use-cases")

    assert result.exit_code == 0, result.output
    assert _console_text(result, caplog).count("a (priority 5)") == 1


@pytest.fixture
def sentinel():
    """Every site that words a displacement must go through ``describe()``."""
    with patch.object(Displacement, "describe", lambda self: _SENTINEL):
        yield


_UP = ("compose", "up", "integration", "--parent", "test3", "--provide", "edge=b")
_BUILD = ("compose", "build", "integration", "--parent", "test3", "--provide", "edge=b")


def test_the_live_up_log_line_renders_through_describe(tmp_path, caplog, monkeypatch, sentinel):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    a, b, lab = _two_providers(tmp_path)
    with (
        caplog.at_level(logging.INFO, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        result = _invoke(*_UP)

    assert result.exit_code == 0, result.output
    assert _SENTINEL in caplog.text


def test_the_up_dry_run_plan_renders_through_describe(tmp_path, sentinel):
    a, b, lab = _two_providers(tmp_path)
    with _install(lab, [a, b]), active_context(dry_run=True):
        result = _invoke(*_UP)

    assert result.exit_code == 0, result.output
    assert f"Displaced: {_SENTINEL}." in " ".join(result.output.split())


def test_the_live_build_report_renders_through_describe(tmp_path, caplog, sentinel):
    a, b, lab = _two_providers(tmp_path, images=("api",))
    built = AsyncMock(
        return_value=[
            RepoBuild(
                "b",
                "test3",
                "built",
                {
                    "api": ImageBuild(
                        "api", ["api:latest"], "abc123abc123", CommandResult(Status.Success)
                    )
                },
            )
        ]
    )
    with _install(lab, [a, b]), patch("otto.docker.build_verbs._build_plan", built):
        result = _invoke(*_BUILD)

    assert result.exit_code == 0, result.output
    assert _SENTINEL in result.output


def test_the_build_dry_run_plan_renders_through_describe(tmp_path, sentinel):
    a, b, lab = _two_providers(tmp_path, images=("api",))
    with _install(lab, [a, b]), active_context(dry_run=True):
        result = _invoke(*_BUILD)

    assert result.exit_code == 0, result.output
    assert f"Displaced: {_SENTINEL}." in " ".join(result.output.split())


def test_use_cases_renders_through_describe(sentinel):
    winner = _uc_repo("b", _uc(provides="edge", priority=10))
    loser = _uc_repo("a", _uc(provides="edge", priority=5))
    lab = Lab(name="unix")
    lab.add_host(UnixHost(ip="10.0.0.1", creds=[], element=Element("test3"), docker_capable=True))
    with (
        patch("otto.bootstrap.get_repos", return_value=[winner, loser]),
        patch("otto.config.fleet.get_lab", return_value=lab),
    ):
        result = _invoke("use-cases")

    assert result.exit_code == 0, result.output
    assert _SENTINEL in result.output


def _one_repo_two_losers(tmp_path):
    """``a`` holds two ``edge`` fragments (priority 1 and 10); ``b`` holds one at 5.

    ``--provide edge=b`` narrows the field to ``b``, so BOTH of a's fragments
    stand down: two displacements in one plan.
    """
    compose = _compose_file(tmp_path, "core")
    a = _repo(
        "a",
        _frag(provides="edge", priority=1),
        _frag(provides="edge", priority=10),
        composes=[compose],
        images=("api",),
    )
    b = _repo("b", _frag(provides="edge", priority=5), composes=[compose], images=("api",))
    return a, b, _lab(_wire(_host("test3", "10.10.200.13")))


@pytest.mark.parametrize("argv", [_UP, _BUILD], ids=["up", "build"])
def test_a_dry_run_plan_joins_two_displacements_as_sentences(tmp_path, argv):
    a, b, lab = _one_repo_two_losers(tmp_path)
    with _install(lab, [a, b]), active_context(dry_run=True):
        result = _invoke(*argv)

    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    first = "edge goes to b (priority 5); a (priority 10) stands down"
    second = "edge goes to b (priority 5); a (priority 1) stands down"
    assert text.count(first) == 1, text
    assert text.count(second) == 1, text
    assert f"Displaced: {first}. {second}." in text or f"Displaced: {second}. {first}." in text


def test_the_log_line_keeps_markup_looking_names_literal(caplog):
    """A `[x]` in a repo or capability name is data, not a rich style tag."""
    from rich.text import Text

    from otto.docker.deployment import _log_displacements
    from otto.docker.resolve import Selection

    d = Displacement(
        capability="edge[x]",
        loser_repo="a[bold]",
        loser_priority=10,
        winner_repo="b",
        winner_priority=5,
    )
    with caplog.at_level(logging.INFO, logger="otto.docker.deployment"):
        _log_displacements("integration", Selection("integration", [], displaced=[d]))

    (record,) = caplog.records
    rendered = Text.from_markup(record.getMessage()).plain
    assert rendered == f"[docker] use-case integration: {d.describe()}"
    assert "edge[x]" in rendered
    assert "a[bold]" in rendered
