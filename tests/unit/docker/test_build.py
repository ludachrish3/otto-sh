"""Unit tests for `otto.docker.build`.

These mock the parent's `oneshot` so we never invoke real `docker build`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.configmodule.repo import DockerImage, DockerSettings
from otto.docker.build import (
    _build_one,
    image_full_tag,
    image_latest_tag,
)
from otto.utils import CommandStatus, Status


def _ok(out: str = "") -> CommandStatus:
    return CommandStatus(command="", output=out, status=Status.Success, retcode=0)


def _fail(out: str = "boom") -> CommandStatus:
    return CommandStatus(command="", output=out, status=Status.Failed, retcode=1)


def _mock_parent():
    parent = MagicMock()
    parent.oneshot = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=(Status.Success, ""))
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
    async def oneshot(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _ok()
        return _ok()

    parent.oneshot.side_effect = oneshot

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    status, msg = await _build_one(parent, "repo1", settings, img, rebuild=False)
    assert status is Status.Skipped
    assert msg.startswith("repo1-api:")
    # Must NOT have called `docker build`.
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    assert not any(c.startswith("docker build ") for c in cmds), cmds


@pytest.mark.asyncio
async def test_build_one_runs_when_image_missing(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)

    async def oneshot(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _fail("not found")
        return _ok()

    parent.oneshot.side_effect = oneshot

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    status, msg = await _build_one(parent, "repo1", settings, img, rebuild=False)
    assert status is Status.Success
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    assert any(c.startswith("docker build ") for c in cmds), cmds


@pytest.mark.asyncio
async def test_rebuild_forces_build_even_when_image_exists(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)
    parent.oneshot.return_value = _ok()  # everything succeeds

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    status, _ = await _build_one(parent, "repo1", settings, img, rebuild=True)
    assert status is Status.Success
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    # Critical: we must have built even though inspect would have succeeded.
    assert any(c.startswith("docker build ") for c in cmds), cmds


@pytest.mark.asyncio
async def test_build_failure_propagates(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)

    async def oneshot(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _fail()
        if cmd.startswith("docker build "):
            return _fail("syntax error in dockerfile")
        return _ok()

    parent.oneshot.side_effect = oneshot

    settings = DockerSettings(registry_url="docker.io", images=(img,), composes=())
    status, msg = await _build_one(parent, "repo1", settings, img, rebuild=False)
    assert status is not Status.Success
    assert "syntax error" in msg
