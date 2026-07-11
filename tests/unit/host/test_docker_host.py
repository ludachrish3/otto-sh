"""Unit tests for DockerContainerHost.

These tests use mocked parents so they run without docker, ssh, or any
network. They verify command-shape correctness, two-step staging, and
the placeholder ``container_id == ""`` guard.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.login_proxy import Cred
from otto.result import CommandResult, Result
from otto.utils import Status
from tests.conftest import active_context


def _ok(cmd: str = "", out: str = "") -> CommandResult:
    return CommandResult(status=Status.Success, value=out, command=cmd, retcode=0)


def _fail(cmd: str = "", out: str = "boom") -> CommandResult:
    return CommandResult(status=Status.Failed, value=out, command=cmd, retcode=1)


def _sm(result) -> tuple[Status, str]:
    """Unwrap ``(status, msg)`` from a transfer aggregate :class:`~otto.result.Result`."""
    return result.status, result.msg


def _mock_parent(parent_id: str = "pepper_seed", *, term: str = "ssh"):
    parent = MagicMock()
    parent.id = parent_id
    parent.name = parent_id
    parent.term = term
    parent.resources = set()
    parent.exec = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))
    parent.get = AsyncMock(return_value=Result(Status.Success, value={}))
    return parent


def _make_container(parent=None, container_id: str = "abc123def456") -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent or _mock_parent(),
        container_id=container_id,
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )


def _build_fake_ssh_remote_host():
    """Construct a real UnixHost with an injected fake ConnectionManager.

    Real UnixHost is needed so `isinstance(parent, UnixHost)` passes in
    `_make_session`; the fake ConnectionManager keeps the test offline.
    """
    from otto.host.connections import ConnectionManager
    from otto.host.unix_host import UnixHost

    class FakeConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self._ssh_conn = MagicMock()  # not awaited in unit tests
            self._sftp_conn = None
            self._ftp_conn = None
            self._telnet_conn = None
            self._name = kwargs.get("name", "fake")
            self._term = kwargs.get("term", "ssh")
            self._hop = None

        async def ssh(self):
            return self._ssh_conn

    return UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element="fake_ne",
        term="ssh",
        _connection_factory=FakeConnections,
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
# exec — single command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_wraps_in_docker_exec():
    parent = _mock_parent()
    h = _make_container(parent)
    parent.exec.return_value = _ok(out="hello")

    result = await h.exec("echo hello")

    assert result.status == Status.Success
    assert result.command == "echo hello"  # caller-visible command, not the wrapper
    parent.exec.assert_awaited_once()
    sent = parent.exec.call_args.args[0]
    assert sent.startswith(f"docker exec -i {h.container_id} sh -c ")
    assert "'echo hello'" in sent or "echo hello" in sent  # quoted


@pytest.mark.asyncio
async def test_exec_quotes_dangerous_chars():
    """Single quotes / spaces / semicolons must be safely escaped via shlex."""
    parent = _mock_parent()
    h = _make_container(parent)
    await h.exec("echo 'hi' ; rm -rf /")
    sent = parent.exec.call_args.args[0]
    # The whole thing must be wrapped so the parent's shell doesn't see
    # ; as a command separator.
    assert "rm -rf /" in sent
    # And the inner ; is not directly exposed at the parent level.
    parent_cmd_after_sh_c = sent.split("sh -c ", 1)[1]
    assert parent_cmd_after_sh_c.startswith("'")  # shlex.quote uses single quotes


# ---------------------------------------------------------------------------
# run — persistent-shell dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rejects_non_ssh_remote_parent():
    """run() requires an SSH-based UnixHost parent (telnet → NotImplementedError)."""
    parent = _mock_parent(term="telnet")
    h = _make_container(parent)
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.run("pwd")


@pytest.mark.asyncio
async def test_run_rejects_localhost_parent():
    """run() requires a UnixHost parent — LocalHost is rejected."""
    from otto.host.local_host import LocalHost

    h = DockerContainerHost(
        parent=LocalHost(),
        container_id="abc123",
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.run("pwd")


@pytest.mark.asyncio
async def test_run_with_ssh_parent_uses_docker_session():
    """run() against an SSH-based UnixHost parent opens a _DockerSshSession."""
    from otto.host.session import _DockerSshSession

    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent)

    # Patch the factory to return a controllable mock; verify the right session
    # type would be requested without actually opening a docker exec channel.
    real_factory = h._session_mgr._session_factory
    sentinel_session = real_factory()
    assert isinstance(sentinel_session, _DockerSshSession)


@pytest.mark.asyncio
async def test_session_factory_resolves_container_id_lazily():
    """The cid_getter closure reads the host's current container_id at session-open
    time — not the value at construction time. This means a placeholder host
    constructed with `container_id=""` works correctly once `_ensure_running`
    populates the id (e.g., via parent.exec lookup)."""
    parent = _build_fake_ssh_remote_host()
    h = DockerContainerHost(
        parent=parent,
        container_id="",  # placeholder
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )
    # Simulate _ensure_running populating the id post-hoc.
    h.container_id = "resolved_cid_xyz"
    session = h._session_mgr._session_factory()
    assert session._cid_getter() == "resolved_cid_xyz"


# ---------------------------------------------------------------------------
# Placeholder (container_id == "") — auto-up behavior
# ---------------------------------------------------------------------------


def _mock_repos(repo_name: str | None = "repo1"):
    """Return a fake repos list optionally including *repo_name*."""
    if repo_name:
        repo = MagicMock()
        repo.name = repo_name
        return [repo]
    return []


@pytest.mark.asyncio
async def test_placeholder_auto_ups_stack(monkeypatch):
    """Accessing a declared-but-down container auto-starts its stack."""
    parent = _mock_parent()  # docker ps returns empty out -> not running
    h = _make_container(parent, container_id="")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    result = await h.exec("echo hi")

    compose_up.assert_awaited_once()
    assert compose_up.call_args.kwargs["build"] is False
    assert compose_up.call_args.kwargs["project_name"] == "otto-repo1-vagrant"
    # Auto-up composes on the container's OWN parent host, not a global
    # default_host: a `carrot_seed.repo1.api` container must auto-start on
    # carrot, not on whatever host happens to be the compose default. (Latent
    # bug surfaced by the multi-host docker pool — see docker_host.py::_auto_up.)
    assert compose_up.call_args.kwargs["on"] == parent.id
    assert h.container_id == "freshcid"
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_placeholder_no_repo_raises(monkeypatch):
    """No configured repo to auto-start -> clear 'not running' error."""
    h = _make_container(container_id="")
    monkeypatch.setattr("otto.config.get_repos", lambda: _mock_repos(repo_name=None))
    monkeypatch.setattr("otto.config.get_lab", MagicMock())
    with pytest.raises(RuntimeError, match="not running"):
        await h.exec("echo hi")


@pytest.mark.asyncio
async def test_placeholder_auto_up_failure_raises(monkeypatch):
    """A compose_up failure surfaces as a 'not running' RuntimeError."""
    h = _make_container(container_id="")
    compose_up = AsyncMock(side_effect=RuntimeError("compose boom"))
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())
    with pytest.raises(RuntimeError, match="not running"):
        await h.exec("echo hi")


@pytest.mark.asyncio
async def test_concurrent_access_triggers_single_auto_up(monkeypatch):
    """Two concurrent calls against a down container auto-up exactly once."""
    import asyncio

    parent = _mock_parent()
    h = _make_container(parent, container_id="")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    await asyncio.gather(h.exec("echo a"), h.exec("echo b"))

    compose_up.assert_awaited_once()
    assert h.container_id == "freshcid"


@pytest.mark.asyncio
async def test_put_placeholder_auto_ups(tmp_path, monkeypatch):
    """File transfer against a down container also auto-starts the stack."""
    parent = _mock_parent()
    h = _make_container(parent, container_id="")
    f = tmp_path / "x"
    f.write_text("x")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    status, _ = _sm(await h.put([f], Path("/tmp")))

    compose_up.assert_awaited_once()
    assert status == Status.Success


# ---------------------------------------------------------------------------
# Sessions / send / expect — gated on SSH-based UnixHost parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_session_rejects_non_remote_parent():
    h = _make_container()  # MagicMock parent
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.open_session("foo")


@pytest.mark.asyncio
async def test_send_rejects_non_remote_parent():
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.send("hi")


@pytest.mark.asyncio
async def test_expect_rejects_non_remote_parent():
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
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

    status, _ = _sm(await h.put([f], Path("/srv/in")))

    assert status == Status.Success
    parent.put.assert_awaited_once()
    # Verify the calls to exec in order: mkdir, docker cp, rm -rf
    cmds = [c.args[0] for c in parent.exec.call_args_list]
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
    def exec_side_effect(cmd, *_, **__):
        if "docker cp" in cmd:
            return _fail(cmd, out="cp failed")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/srv/in")))
    assert status == Status.Error
    assert "cp failed" in msg
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any("rm -rf" in c for c in cmds), "cleanup must run on failure"


@pytest.mark.asyncio
async def test_get_two_step_via_parent():
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = _sm(await h.get(Path("/etc/os-release"), Path("./out")))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any("docker cp" in c and h.container_id in c for c in cmds), cmds
    parent.get.assert_awaited_once()
    args, _ = parent.get.call_args
    assert args[1] == Path("./out")


# ---------------------------------------------------------------------------
# login() preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_requires_remote_ssh_parent():
    # Parent is a MagicMock, NOT a UnixHost — the isinstance check should reject it.
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based parent"):
        await h._login()


@pytest.mark.asyncio
async def test_login_as_user_raises_not_implemented():
    """Task 9: DockerContainerHost has no login-proxy chain of its own —
    passing --as-user must raise loudly rather than being silently ignored
    or (worse) silently forwarded to the container's default shell."""
    h = _make_container()
    with pytest.raises(NotImplementedError, match="--as-user"):
        await h._login(as_user="mysql")


# ---------------------------------------------------------------------------
# Dry-run behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_dry_run_skips_parent():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        result = await h.exec("echo hi")
    parent.exec.assert_not_awaited()
    assert result.status == Status.Skipped
    assert result.command == "echo hi"
    assert "[DRY RUN]" in result.value


@pytest.mark.asyncio
async def test_run_dry_run_skips_session():
    """_run_one returns dry-run sentinel without opening a session."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        result = await h.run("ls /")
    parent.exec.assert_not_awaited()
    assert result.only.status == Status.Skipped
    assert result.only.command == "ls /"


@pytest.mark.asyncio
async def test_send_dry_run_returns_without_session():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        await h.send("some text")
    # No session should be touched — parent is a MagicMock so open_session
    # would raise NotImplementedError if called.
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_expect_dry_run_returns_empty_string():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        result = await h.expect("prompt> ")
    assert result == ""
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_dry_run_skips_transfer(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "x.txt"
    f.write_text("hello")
    with active_context(dry_run=True):
        status, msg = _sm(await h.put([f], Path("/dest")))
    parent.exec.assert_not_awaited()
    parent.put.assert_not_awaited()
    assert status == Status.Skipped
    assert "[DRY RUN]" in msg
    assert "PUT" in msg


@pytest.mark.asyncio
async def test_get_dry_run_skips_transfer():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        status, msg = _sm(await h.get(Path("/etc/hosts"), Path("./out")))
    parent.exec.assert_not_awaited()
    parent.get.assert_not_awaited()
    assert status == Status.Skipped
    assert "[DRY RUN]" in msg
    assert "GET" in msg


# ---------------------------------------------------------------------------
# rebuild_connections
# ---------------------------------------------------------------------------


def test_rebuild_connections_swaps_session_mgr():
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent)
    old = h._session_mgr
    sentinel_mgr = MagicMock()
    with patch.object(h, "_build_session_mgr", return_value=sentinel_mgr):
        h.rebuild_connections()
    assert h._session_mgr is sentinel_mgr
    assert h._session_mgr is not old


# ---------------------------------------------------------------------------
# put / get error returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_mkdir_failure_returns_error(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    def exec_side_effect(cmd, *args, **kwargs):
        if "mkdir" in cmd:
            return _fail(cmd, out="Permission denied")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/dest")))
    assert status == Status.Error
    assert "failed to create staging dir" in msg


@pytest.mark.asyncio
async def test_put_parent_put_failure_passthrough(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    parent.put.return_value = Result(
        Status.Error,
        value={f: Result(Status.Error, msg="sftp connection lost")},
        msg="sftp connection lost",
    )

    result = await h.put([f], Path("/dest"))
    assert result.status == Status.Error
    assert "sftp connection lost" in result.msg
    # Failure path must key by the as-passed source paths.
    assert set(result.value.keys()) == {f}
    assert not result.value[f].is_ok


@pytest.mark.asyncio
async def test_put_partial_staging_failure_downgrades_staged_files(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    ok_file = tmp_path / "ok.bin"
    bad_file = tmp_path / "bad.bin"
    ok_file.write_bytes(b"data")
    bad_file.write_bytes(b"data")

    parent.put.return_value = Result(
        Status.Error,
        value={
            ok_file: Result(Status.Success, value=Path("/stage/ok.bin")),
            bad_file: Result(Status.Error, msg="bad.bin: sftp write failed"),
        },
        msg="bad.bin: sftp write failed",
    )

    result = await h.put([ok_file, bad_file], Path("/dest"))
    assert result.status == Status.Error
    # A file that only reached the parent staging dir must NOT read as
    # Success — docker cp never ran, so it never reached the container.
    assert result.value[ok_file].status == Status.Skipped
    assert not result.value[bad_file].is_ok


@pytest.mark.asyncio
async def test_put_docker_cp_failure_returns_error(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd:
            return _fail(cmd, out="no such container")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/dest")))
    assert status == Status.Error
    assert "docker cp failed" in msg


@pytest.mark.asyncio
async def test_get_mkdir_failure_returns_error():
    parent = _mock_parent()
    h = _make_container(parent=parent)

    def exec_side_effect(cmd, *args, **kwargs):
        if "mkdir" in cmd:
            return _fail(cmd, out="read-only filesystem")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.get(Path("/etc/os-release"), Path("./out")))
    assert status == Status.Error
    assert "failed to create staging dir" in msg


@pytest.mark.asyncio
async def test_get_docker_cp_failure_returns_error():
    parent = _mock_parent()
    h = _make_container(parent=parent)

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd:
            return _fail(cmd, out="container not found")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.get(Path("/etc/os-release"), Path("./out")))
    assert status == Status.Error
    assert "docker cp failed" in msg


@pytest.mark.asyncio
async def test_get_mid_batch_docker_cp_failure_keeps_every_source_key(tmp_path):
    """A mid-batch docker-cp failure must still key EVERY as-passed source
    path — including files already copied to parent staging before the
    failing file. Those earlier files never reach the caller (parent.get is
    never invoked and the staging dir is removed in `finally`), so they must
    be downgraded to Skipped rather than omitted; omitting them would make
    ``result.value[first_file]`` raise KeyError."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    first = Path("/remote/first.bin")
    second = Path("/remote/second.bin")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd and "second.bin" in cmd:
            return _fail(cmd, out="no such file or directory")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    result = await h.get([first, second], tmp_path)
    assert result.status == Status.Error
    # Every as-passed source path must be a key — no KeyError on lookup.
    assert set(result.value.keys()) == {first, second}
    assert result.value[first].status == Status.Skipped
    assert not result.value[second].is_ok


# ---------------------------------------------------------------------------
# _login — non-ssh parent rejection + ssh happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_rejects_non_ssh_parent():
    """telnet parent raises NotImplementedError (parent is UnixHost but term != ssh)."""
    from otto.host.connections import ConnectionManager
    from otto.host.unix_host import UnixHost

    class FakeTelnetConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self._ssh_conn = None
            self._sftp_conn = None
            self._ftp_conn = None
            self._telnet_conn = None
            self._name = kwargs.get("name", "fake")
            self._term = "telnet"
            self._hop = None

    telnet_parent = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element="fake_ne",
        term="telnet",
        _connection_factory=FakeTelnetConnections,
    )
    h = _make_container(parent=telnet_parent)
    with pytest.raises(NotImplementedError):
        await h._login()


@pytest.mark.asyncio
async def test_login_ssh_runs_docker_exec():
    """SSH parent: _login calls run_ssh_login with docker exec -it command."""
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent, container_id="mycontainer123")

    with patch("otto.host.interact.run_ssh_login", new_callable=AsyncMock) as mock_login:
        await h._login()

    mock_login.assert_awaited_once()
    call_kwargs = mock_login.call_args.kwargs
    expected_cmd = f"docker exec -it {shlex.quote(h.container_id)} /bin/sh"
    assert "command" in call_kwargs
    assert expected_cmd in call_kwargs["command"]
