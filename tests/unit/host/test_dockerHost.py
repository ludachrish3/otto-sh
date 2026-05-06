"""Unit tests for DockerContainerHost.

These tests use mocked parents so they run without docker, ssh, or any
network. They verify command-shape correctness, two-step staging, and
the placeholder ``container_id == ""`` guard.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.dockerHost import DockerContainerHost
from otto.utils import CommandStatus, Status


def _ok(cmd: str = "", out: str = "") -> CommandStatus:
    return CommandStatus(command=cmd, output=out, status=Status.Success, retcode=0)


def _fail(cmd: str = "", out: str = "boom") -> CommandStatus:
    return CommandStatus(command=cmd, output=out, status=Status.Failed, retcode=1)


def _mock_parent(parent_id: str = "pepper_seed", *, term: str = "ssh"):
    parent = MagicMock()
    parent.id = parent_id
    parent.name = parent_id
    parent.term = term
    parent.resources = set()
    parent.oneshot = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=(Status.Success, ""))
    parent.get = AsyncMock(return_value=(Status.Success, ""))
    return parent


def _make_container(parent=None, container_id: str = "abc123def456") -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent or _mock_parent(),
        container_id=container_id,
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )


# ---------------------------------------------------------------------------
# Construction & identity
# ---------------------------------------------------------------------------

def test_id_format():
    h = _make_container(_mock_parent("pepper_seed"))
    assert h.id == "pepper_seed.repo1.api"


def test_id_lowercased():
    h = DockerContainerHost(
        parent=_mock_parent("Pepper_SEED"),
        container_id="abc",
        project="Repo1",
        service="API",
        compose_project="proj",
    )
    assert h.id == "pepper_seed.repo1.api"


def test_is_virtual_default():
    assert _make_container().is_virtual is True


# ---------------------------------------------------------------------------
# oneshot — single command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oneshot_wraps_in_docker_exec():
    parent = _mock_parent()
    h = _make_container(parent)
    parent.oneshot.return_value = _ok(out="hello")

    result = await h.oneshot("echo hello")

    assert result.status == Status.Success
    assert result.command == "echo hello"  # caller-visible command, not the wrapper
    parent.oneshot.assert_awaited_once()
    sent = parent.oneshot.call_args.args[0]
    assert sent.startswith(f"docker exec -i {h.container_id} sh -c ")
    assert "'echo hello'" in sent or "echo hello" in sent  # quoted


@pytest.mark.asyncio
async def test_oneshot_quotes_dangerous_chars():
    """Single quotes / spaces / semicolons must be safely escaped via shlex."""
    parent = _mock_parent()
    h = _make_container(parent)
    await h.oneshot("echo 'hi' ; rm -rf /")
    sent = parent.oneshot.call_args.args[0]
    # The whole thing must be wrapped so the parent's shell doesn't see
    # ; as a command separator.
    assert "rm -rf /" in sent
    # And the inner ; is not directly exposed at the parent level.
    parent_cmd_after_sh_c = sent.split("sh -c ", 1)[1]
    assert parent_cmd_after_sh_c.startswith("'")  # shlex.quote uses single quotes


# ---------------------------------------------------------------------------
# run — list of commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_list_dispatches_each_via_docker_exec():
    parent = _mock_parent()
    h = _make_container(parent)
    parent.oneshot.return_value = _ok()

    result = await h.run(["pwd", "uname -a"])

    assert result.status == Status.Success
    assert len(result.statuses) == 2
    # Two parent.oneshot calls, both wrapped in docker exec.
    assert parent.oneshot.await_count == 2
    for c in parent.oneshot.call_args_list:
        assert c.args[0].startswith(f"docker exec -i {h.container_id} sh -c ")


@pytest.mark.asyncio
async def test_run_with_expects_raises():
    h = _make_container()
    with pytest.raises(NotImplementedError):
        await h._run_one("read x", expects=[("password:", "secret")])


# ---------------------------------------------------------------------------
# Placeholder (container_id == "") guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oneshot_placeholder_raises():
    h = _make_container(container_id="")
    with pytest.raises(RuntimeError, match="not running"):
        await h.oneshot("echo hi")


@pytest.mark.asyncio
async def test_put_placeholder_raises(tmp_path):
    h = _make_container(container_id="")
    f = tmp_path / "x"
    f.write_text("x")
    with pytest.raises(RuntimeError, match="not running"):
        await h.put([f], Path("/tmp"))


@pytest.mark.asyncio
async def test_get_placeholder_raises():
    h = _make_container(container_id="")
    with pytest.raises(RuntimeError, match="not running"):
        await h.get(Path("/etc/os-release"), Path("./"))


# ---------------------------------------------------------------------------
# Sessions / send / expect — explicit "not implemented" surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_session_raises():
    h = _make_container()
    with pytest.raises(NotImplementedError):
        await h.open_session("foo")


@pytest.mark.asyncio
async def test_send_raises():
    h = _make_container()
    with pytest.raises(NotImplementedError):
        await h.send("hi")


@pytest.mark.asyncio
async def test_expect_raises():
    h = _make_container()
    with pytest.raises(NotImplementedError):
        await h.expect("prompt> ")


# ---------------------------------------------------------------------------
# put / get — two-step staging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_stages_then_docker_cps_then_cleans_up(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x" * 16)

    status, _ = await h.put([f], Path("/srv/in"))

    assert status == Status.Success
    parent.put.assert_awaited_once()
    # Verify the calls to oneshot in order: mkdir, docker cp, rm -rf
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    assert any("mkdir -p" in c for c in cmds), cmds
    assert any("docker cp" in c and h.container_id in c and "/srv/in" in c for c in cmds), cmds
    assert any("rm -rf" in c for c in cmds), cmds


@pytest.mark.asyncio
async def test_put_failure_still_cleans_up(tmp_path):
    parent = _mock_parent()
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x")
    h = _make_container(parent)

    # Make docker cp fail; the surrounding mkdir & rm -rf should still both run.
    def oneshot_side_effect(cmd, *_, **__):
        if "docker cp" in cmd:
            return _fail(cmd, out="cp failed")
        return _ok()
    parent.oneshot.side_effect = oneshot_side_effect

    status, msg = await h.put([f], Path("/srv/in"))
    assert status == Status.Error
    assert "cp failed" in msg
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    assert any("rm -rf" in c for c in cmds), "cleanup must run on failure"


@pytest.mark.asyncio
async def test_get_two_step_via_parent():
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = await h.get(Path("/etc/os-release"), Path("./out"))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.oneshot.call_args_list]
    assert any("docker cp" in c and h.container_id in c for c in cmds), cmds
    parent.get.assert_awaited_once()
    args, _ = parent.get.call_args
    assert args[1] == Path("./out")


# ---------------------------------------------------------------------------
# interact() preconditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_interact_requires_remote_ssh_parent():
    # Parent is a MagicMock, NOT a RemoteHost — the isinstance check should reject it.
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based parent"):
        await h._interact()
