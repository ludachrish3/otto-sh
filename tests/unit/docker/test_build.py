"""Unit tests for `otto.docker.build`.

These mock the parent's `exec` so we never invoke real `docker build`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.config.repo import DockerImage, DockerSettings
from otto.docker._context_hash import context_hash
from otto.docker.build import (
    _build_one,
    image_full_tag,
    image_latest_tag,
)
from otto.result import CommandResult, Result
from otto.utils import Status


def _ok(out: str = "", command: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command=command, retcode=0)


def _fail(out: str = "boom", command: str = "") -> CommandResult:
    # `command` is carried so the fake matches what a real exec produces —
    # otherwise a test cannot tell "returned the result whole" from "rebuilt
    # a summary", which is exactly the property _build_one now promises.
    return CommandResult(Status.Failed, value=out, command=command, retcode=1)


def _mock_parent():
    parent = MagicMock()
    parent.exec = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))
    return parent


def _img(tmp: Path) -> DockerImage:
    df = tmp / "Dockerfile"
    df.write_text("FROM alpine\n")
    return DockerImage(name="api", dockerfile=df, context=tmp)


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


def test_default_registry_omits_prefix(tmp_path):
    img = _img(tmp_path)
    assert image_latest_tag("docker.io", "repo1", img) == "repo1-api:latest"
    assert (
        image_full_tag("docker.io", "repo1", img, "abcdef0123456789ff")
        == "repo1-api:abcdef0123456789"
    )


def test_empty_registry_omits_prefix(tmp_path):
    img = _img(tmp_path)
    assert image_latest_tag("", "repo1", img) == "repo1-api:latest"


def test_custom_registry_prefixes(tmp_path):
    img = _img(tmp_path)
    assert image_latest_tag("ghcr.io/me", "repo1", img) == "ghcr.io/me/repo1-api:latest"


# ---------------------------------------------------------------------------
# _build_one — skip vs build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_one_skipped_when_image_exists(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)

    # docker image inspect succeeds → skip path.
    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _ok()
        return _ok()

    parent.exec.side_effect = exec_side_effect

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    res = await _build_one(parent, "repo1", settings, img, rebuild=False)
    assert res.status is Status.Skipped
    assert res.value.startswith("repo1-api:")
    assert res.msg == "", "msg is a diagnostic slot; the tag is the payload"
    # Must NOT have called `docker build`.
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert not any(c.startswith("docker build ") for c in cmds), cmds
    # The re-tag's DIRECTION, spelled out: `docker tag <full> <latest>` with the
    # arguments swapped re-points the CONTEXT-HASH tag at the previous build,
    # which makes the build cache itself lie — and it is invisible to a test
    # that only asserts `:latest` still exists.
    full = image_full_tag("docker.io", "repo1", img, context_hash(img))
    latest = image_latest_tag("docker.io", "repo1", img)
    assert f"docker tag {full} {latest}" in cmds, cmds


@pytest.mark.asyncio
async def test_build_one_fails_when_the_cached_latest_retag_fails(tmp_path):
    """A cached image whose `:latest` could not be re-pointed is NOT a skip.

    `:latest` is the tag a user's compose.yml names. If `docker tag` fails
    (daemon error, image pruned between the inspect and the tag) and the
    result is discarded, otto reports "cached -> <tag>" while the stack comes
    up on a PREVIOUS build — a wrong-image run with nothing reporting a
    failure anywhere.
    """
    parent = _mock_parent()
    img = _img(tmp_path)
    tag_failure = _fail("no such image", command="docker tag full latest")

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _ok()  # cached: take the skip path
        if cmd.startswith("docker tag "):
            return tag_failure
        return _ok()

    parent.exec.side_effect = exec_side_effect

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    res = await _build_one(parent, "repo1", settings, img, rebuild=False)

    assert not res.is_ok, "compose_up gates on is_ok; a truthy skip would build on nothing"
    # Identity, not field-by-field equality: a rebuilt summary carrying
    # status/value/command/retcode passes every per-field assertion while
    # silently dropping `timed_out`, which is exactly how you tell a wedged
    # daemon from a rejected tag without string-matching the output.
    assert res is tag_failure
    # Still no build: the image really was cached.
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert not any(c.startswith("docker build ") for c in cmds), cmds


@pytest.mark.asyncio
async def test_build_one_runs_when_image_missing(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _fail("not found")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    res = await _build_one(parent, "repo1", settings, img, rebuild=False)
    status = res.status
    assert status is Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any(c.startswith("docker build ") for c in cmds), cmds
    # `tar -xf` (extracting the just-staged build context) must be unbounded
    # — its duration IS the extraction, which scales with the context size.
    tar_call = next(c for c in parent.exec.call_args_list if c.args[0].startswith("tar -xf "))
    assert tar_call.kwargs.get("timeout") == float("inf")


@pytest.mark.asyncio
async def test_rebuild_forces_build_even_when_image_exists(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)
    parent.exec.return_value = _ok()  # everything succeeds

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    res = await _build_one(parent, "repo1", settings, img, rebuild=True)
    status = res.status
    assert status is Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    # Critical: we must have built even though inspect would have succeeded.
    assert any(c.startswith("docker build ") for c in cmds), cmds


@pytest.mark.asyncio
async def test_build_failure_propagates(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)
    build_failure = _fail("syntax error in dockerfile", command="docker build ...")

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _fail()
        if cmd.startswith("docker build "):
            return build_failure
        return _ok()

    parent.exec.side_effect = exec_side_effect

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    res = await _build_one(parent, "repo1", settings, img, rebuild=False)
    assert res is build_failure, "the build's own result, not a summary of it"
    assert res.status is not Status.Success
    # The failing build's own result comes back whole: its captured output is
    # in value, and the command/retcode the old tuple discarded survive.
    assert "syntax error" in res.value
    # The failing result comes back WHOLE. `!= 0` would be vacuous: retcode
    # defaults to -1, so a rebuilt-and-summarized result would pass it.
    assert res.retcode == 1
    assert res.command.startswith("docker build ")
