"""What `otto run cleanup` removes from a docker parent, and what `is_clean` asks of it.

Every staging tree a project owns on the parent is surveyed once
(`staging_to_remove`): its per-project compose stack's (where its builds stage
too) and each of its use-cases'. A tree that exists and that no stack needs is
removed (`remove_project_staging`). A compose project that still has
containers under its label keeps its staging, because that staging scopes the
stack's next `down`. Cleanup never takes a stack down.
"""

import logging
import os
import subprocess
from unittest.mock import AsyncMock

import pytest

from otto.config.scope import DockerImage, DockerSettings
from otto.docker.compose import (
    StagingKey,
    StagingSurvey,
    project_staging_keys,
    remove_project_staging,
    staging_keys_on,
    staging_to_remove,
)
from otto.docker.staging import present_roots_command, stack_cleanup_command
from otto.host.docker_host import parent_staging_root, staging_root
from otto.host.element import Element
from otto.host.errors import HostCommandError, HostUnreachableError, StagingRefusedError
from otto.host.unix_host import UnixHost
from otto.result import CommandNotRunError
from otto.utils import Status
from tests._fixtures.fake_project import fake_project
from tests.conftest import active_context

from .test_compose import _capable_host, _fail, _make_lab, _make_project, _ok, _wire_parent_mock

_STACK = "otto-project1-u"
_USE_CASE = "unix-project1-u"
_STAGING = staging_root("vagrant")
"""The parent's private staging root: the fixture parent logs in as ``vagrant``."""
_LISTING = present_roots_command(_STAGING, [_STACK, _USE_CASE])


def _root(key: str) -> str:
    return f"{_STAGING}/{key}"


def _probe(key: str) -> str:
    return f"docker ps -aq --filter label=com.docker.compose.project={key}"


def _rm(key: str) -> str:
    return f"rm -rf {_root(key)}"


def _remove(key: str) -> str:
    """A stack's removal: its own container check and the ``rm -rf``, in one shell command."""
    return (
        f'if c=$({_probe(key)}); then if [ -z "$c" ]; then {_rm(key)}; '
        "else echo otto-staging-kept; fi; else echo otto-staging-unreadable; fi"
    )


@pytest.fixture
def parent(monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    host = _wire_parent_mock(_capable_host())
    host.source_lab = "unix"
    _answer(host)
    return host


def _answer(parent, present=(_STACK, _USE_CASE), **by_command):
    """List *present* roots as existing; answer each command from *by_command*; else ok.

    An exception in *by_command* is raised instead of returned.
    """
    listing = _ok("".join(f"{_root(k)}\n" for k in present))

    async def exec_side_effect(cmd, *_, **__):
        outcome = by_command.get(cmd, listing if cmd == _LISTING else _ok())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    parent.exec = AsyncMock(side_effect=exec_side_effect)


def _sent(parent) -> list[str]:
    return [c.args[0] for c in parent.exec.call_args_list]


def _images_only(tmp_path, name="acme"):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    image = DockerImage(name="api", dockerfile=df, context=tmp_path)
    return fake_project(name, docker_settings=DockerSettings(images=(image,)))


# ── the keys ─────────────────────────────────────────────────────────────


def test_a_project_s_builds_stage_under_its_stack_s_key(monkeypatch, tmp_path):
    """One key per tree: the builds' key is the stack's, so it is not listed twice."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    assert project_staging_keys(_make_project(tmp_path), "unix") == [
        StagingKey(_STACK, stack=True),
        StagingKey(_USE_CASE, stack=True),
    ]


def test_an_images_only_project_owns_its_build_key_and_no_stack(monkeypatch, tmp_path):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    assert project_staging_keys(_images_only(tmp_path), "unix") == [
        StagingKey("otto-acme-u", stack=False)
    ]


# ── the survey ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_survey_names_every_present_tree_no_stack_needs(parent, tmp_path):
    survey = await staging_to_remove(parent, _make_project(tmp_path))

    assert [k.key for k in survey.remove] == [_STACK, _USE_CASE]
    assert survey.kept == []
    assert survey.unreadable == []
    assert _sent(parent) == [_LISTING, _probe(_STACK), _probe(_USE_CASE)]


@pytest.mark.asyncio
async def test_an_absent_tree_is_neither_probed_nor_named(parent, tmp_path):
    _answer(parent, present=(_USE_CASE,))

    survey = await staging_to_remove(parent, _make_project(tmp_path))

    assert [k.key for k in survey.remove] == [_USE_CASE]
    assert _probe(_STACK) not in _sent(parent)


@pytest.mark.asyncio
async def test_a_tree_without_a_stack_is_never_probed(parent, tmp_path):
    parent.exec = AsyncMock(return_value=_ok(f"{_root('otto-acme-u')}\n"))

    survey = await staging_to_remove(parent, _images_only(tmp_path))

    assert [k.key for k in survey.remove] == ["otto-acme-u"]
    assert not [c for c in _sent(parent) if c.startswith("docker ps")]


@pytest.mark.asyncio
async def test_the_survey_keeps_a_live_stack_and_marks_an_unlisted_one(parent, tmp_path):
    _answer(parent, **{_probe(_STACK): _ok("3f2a9c\n"), _probe(_USE_CASE): _fail("no daemon")})

    survey = await staging_to_remove(parent, _make_project(tmp_path))

    assert survey.remove == []
    assert survey.kept == [_STACK]
    assert survey.unreadable == [_USE_CASE]


@pytest.mark.asyncio
async def test_a_failed_listing_raises(parent, tmp_path):
    _answer(parent, **{_LISTING: _fail("sh: cannot fork")})

    with pytest.raises(HostCommandError, match="cannot fork"):
        await staging_to_remove(parent, _make_project(tmp_path))


@pytest.mark.asyncio
async def test_the_survey_refuses_a_dry_run(parent, tmp_path):
    with (
        active_context(lab=_make_lab(), dry_run=True),
        pytest.raises(CommandNotRunError),
    ):
        await staging_to_remove(parent, _make_project(tmp_path))

    parent.exec.assert_not_awaited()


# ── the removal ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_staging_tree_the_project_owns_is_removed(parent, tmp_path):
    result = await remove_project_staging(parent, _make_project(tmp_path))

    assert _sent(parent) == [
        _LISTING,
        _probe(_STACK),
        _probe(_USE_CASE),
        _remove(_STACK),
        _remove(_USE_CASE),
    ]
    assert result.status is Status.Success, result.msg


@pytest.mark.asyncio
async def test_a_stack_with_containers_keeps_its_staging_and_is_named(parent, tmp_path, caplog):
    _answer(parent, **{_probe(_STACK): _ok("3f2a9c\n")})

    with caplog.at_level(logging.WARNING):
        result = await remove_project_staging(parent, _make_project(tmp_path))

    assert _remove(_STACK) not in _sent(parent)
    assert _remove(_USE_CASE) in _sent(parent)
    assert result.status is Status.Skipped
    assert _STACK in result.msg
    kept = (
        f"test3: kept the staging of {_STACK}: the stack still has containers, "
        "and its next `down` reads that staging back"
    )
    assert [r.getMessage() for r in caplog.records if _STACK in r.getMessage()] == [kept]


@pytest.mark.asyncio
async def test_a_stack_nobody_could_list_keeps_its_staging_with_one_warning(
    parent, tmp_path, caplog
):
    """The probe's own warning is the one: the step adds none of its own."""
    _answer(parent, **{_probe(_USE_CASE): _fail("Cannot connect to the Docker daemon")})

    with caplog.at_level(logging.WARNING):
        result = await remove_project_staging(parent, _make_project(tmp_path))

    assert _remove(_USE_CASE) not in _sent(parent)
    assert _remove(_STACK) in _sent(parent)
    assert result.status is Status.Skipped
    assert f"{_USE_CASE} (containers could not be listed)" in result.msg
    assert len([r for r in caplog.records if _USE_CASE in r.getMessage()]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "removal", [_fail("rm: cannot remove: Permission denied"), HostUnreachableError("gone")]
)
async def test_a_failed_removal_is_reported_and_the_other_keys_still_go(parent, tmp_path, removal):
    _answer(parent, **{_remove(_STACK): removal})

    result = await remove_project_staging(parent, _make_project(tmp_path))

    assert _remove(_USE_CASE) in _sent(parent)
    assert result.status is Status.Failed
    assert _STACK in result.msg


@pytest.mark.asyncio
async def test_a_failed_survey_is_reported_and_removes_nothing(parent, tmp_path):
    _answer(parent, **{_LISTING: _fail("sh: cannot fork")})

    result = await remove_project_staging(parent, _make_project(tmp_path))

    assert not [c for c in _sent(parent) if "rm -rf" in c]
    assert result.status is Status.Failed
    assert "cannot fork" in result.msg


@pytest.mark.asyncio
async def test_a_project_with_no_docker_settings_touches_nothing(parent):
    """Red if a key is emitted for empty settings, such as an unconditional build key."""
    project = fake_project("acme")

    assert staging_keys_on(parent, project) == []
    result = await remove_project_staging(parent, project)

    parent.exec.assert_not_awaited()
    assert result.status is Status.Success


@pytest.mark.asyncio
async def test_a_host_that_is_not_docker_capable_is_left_alone(parent, tmp_path):
    """Red if the docker-parent check is dropped: this project owns keys on any parent."""
    parent.docker_capable = False
    project = _make_project(tmp_path)

    assert staging_keys_on(parent, project) == []
    result = await remove_project_staging(parent, project)

    parent.exec.assert_not_awaited()
    assert result.status is Status.Success


@pytest.fixture
def rootless_parent(monkeypatch):
    """A docker parent no cred names a login on, so it has no private staging root."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    host = _wire_parent_mock(
        UnixHost(ip="10.10.200.13", element=Element("test3"), creds=[], docker_capable=True)
    )
    host.source_lab = "unix"
    with pytest.raises(StagingRefusedError):
        parent_staging_root(host)
    return host


@pytest.mark.asyncio
async def test_a_parent_whose_staging_root_cannot_be_named_surveys_empty(rootless_parent, tmp_path):
    """otto can stage nothing there, so nothing is staged: no failure, no raise, no command.

    Red if the survey resolves the root unguarded: cleanup's step then fails
    and ``is_clean`` raises on a parent otto could never have staged on.
    """
    project = _make_project(tmp_path)

    assert staging_keys_on(rootless_parent, project) == []
    survey = await staging_to_remove(rootless_parent, project)
    result = await remove_project_staging(rootless_parent, project)

    assert survey == StagingSurvey([], [], [])
    assert result.status is Status.Success
    rootless_parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dry_run_removes_nothing(parent, tmp_path):
    with active_context(lab=_make_lab(), dry_run=True):
        result = await remove_project_staging(parent, _make_project(tmp_path))

    parent.exec.assert_not_awaited()
    assert result.status is Status.NotRun


# ── the removal asks again, in the same command ─────────────────────────


def test_a_stack_s_removal_carries_its_own_container_check():
    """The survey's answer can be stale by the time it removes: the ``rm -rf`` asks again.

    Red if the removal is a bare ``rm -rf``: a deployment of the same key that
    started containers since the survey would lose the staging its ``down`` reads.
    """
    assert stack_cleanup_command(_STAGING, _STACK) == _remove(_STACK)


_DOCKER = """#!/bin/sh
[ -n "$DOCKER_FAIL" ] && exit 1
[ -n "$DOCKER_IDS" ] && echo "$DOCKER_IDS"
exit 0
"""


@pytest.mark.parametrize(
    ("docker", "printed", "removed"),
    [
        ({}, "", True),
        ({"DOCKER_IDS": "3f2a9c"}, "otto-staging-kept", False),
        ({"DOCKER_FAIL": "1"}, "otto-staging-unreadable", False),
    ],
)
def test_the_removal_runs_only_when_the_stack_has_no_containers(tmp_path, docker, printed, removed):
    """The command itself, in a real shell against a stub docker, with the root under tmp_path."""
    staging = tmp_path / "staging"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text(_DOCKER)
    stub.chmod(0o755)
    root = staging / _STACK
    (root / "compose").mkdir(parents=True)
    text = stack_cleanup_command(staging, _STACK)
    done = subprocess.run(
        ["sh", "-c", text],
        env={**os.environ, "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}", **docker},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == printed
    assert root.exists() is not removed


@pytest.mark.asyncio
async def test_a_stack_whose_containers_appeared_after_the_survey_is_kept_not_failed(
    parent, tmp_path, caplog
):
    """The survey saw no containers; the removal's own check found some: a kept stack."""
    _answer(parent, **{_remove(_STACK): _ok("otto-staging-kept\n")})

    with caplog.at_level(logging.WARNING):
        result = await remove_project_staging(parent, _make_project(tmp_path))

    assert _remove(_USE_CASE) in _sent(parent)
    assert result.status is Status.Skipped, result.msg
    assert result.msg == f"test3: kept the docker staging of {_STACK}"
    assert any(f"kept the staging of {_STACK}" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_stack_the_removal_could_not_list_is_kept_as_unreadable(parent, tmp_path):
    _answer(parent, **{_remove(_USE_CASE): _ok("otto-staging-unreadable\n")})

    result = await remove_project_staging(parent, _make_project(tmp_path))

    assert result.status is Status.Skipped, result.msg
    assert result.msg == (
        f"test3: kept the docker staging of {_USE_CASE} (containers could not be listed)"
    )


@pytest.mark.asyncio
async def test_a_tree_without_a_stack_is_removed_by_a_bare_rm(parent, tmp_path):
    parent.exec = AsyncMock(return_value=_ok(f"{_root('otto-acme-u')}\n"))

    result = await remove_project_staging(parent, _images_only(tmp_path))

    assert _sent(parent)[-1] == _rm("otto-acme-u")
    assert result.status is Status.Success, result.msg
