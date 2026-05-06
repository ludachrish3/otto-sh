"""Unit tests for `otto.docker.compose` orchestration.

These mock the parent host's `oneshot`/`put` so no real docker is invoked.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.configmodule.lab import Lab
from otto.configmodule.repo import (
    DockerCompose,
    DockerImage,
    DockerSettings,
    Repo,
)
from otto.docker.compose import (
    _resolve_parent,
    compose_down,
    compose_up,
    composed,
    get_user_compose_project,
    register_declared_container_hosts,
)
from otto.host.dockerHost import DockerContainerHost
from otto.host.remoteHost import RemoteHost
from otto.utils import CommandStatus, Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(out: str = "") -> CommandStatus:
    return CommandStatus(command="", output=out, status=Status.Success, retcode=0)


def _make_repo(tmp: Path, *, name: str = "repo1", services: tuple = ("api",), default_host: str = "pepper_seed") -> Repo:
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "Dockerfile").write_text("FROM alpine\n")
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    services_toml = "[" + ", ".join(f'"{s}"' for s in services) + "]"
    (sut / ".otto" / "settings.toml").write_text(
        f"name = \"{name}\"\n"
        f"version = \"1.0.0\"\n"
        f"\n"
        f"[docker]\n"
        f"\n"
        f"[[docker.images]]\n"
        f"name = \"api\"\n"
        f"dockerfile = \"${{sutDir}}/docker/Dockerfile\"\n"
        f"context = \"${{sutDir}}/docker\"\n"
        f"\n"
        f"[[docker.composes]]\n"
        f"path = \"${{sutDir}}/docker/compose.yml\"\n"
        f"default_host = \"{default_host}\"\n"
        f"services = {services_toml}\n"
    )
    return Repo(sutDir=sut)


def _capable_host(host_id: str = "pepper_seed", ne: str = "pepper") -> RemoteHost:
    return RemoteHost(
        ip="10.10.200.13",
        ne=ne,
        creds={"vagrant": "vagrant"},
        board="seed",
        docker_capable=True,
    )


def _wire_parent_mock(host: RemoteHost) -> RemoteHost:
    """Replace the host's network methods with AsyncMocks so we never connect."""
    host.oneshot = AsyncMock(return_value=_ok())  # type: ignore[method-assign]
    host.put = AsyncMock(return_value=(Status.Success, ""))  # type: ignore[method-assign]
    host.get = AsyncMock(return_value=(Status.Success, ""))  # type: ignore[method-assign]
    return host


def _make_lab() -> Lab:
    lab = Lab(name="test")
    parent = _wire_parent_mock(_capable_host())
    lab.hosts[parent.id] = parent
    return lab


# ---------------------------------------------------------------------------
# get_user_compose_project
# ---------------------------------------------------------------------------

def test_compose_project_uses_user_when_no_suffix(monkeypatch):
    monkeypatch.delenv("OTTO_COMPOSE_SUFFIX", raising=False)
    name = get_user_compose_project("Repo1")
    assert name == f"otto-repo1-{getpass.getuser().lower()}"


def test_compose_project_honors_env_override(monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "ci-7")
    assert get_user_compose_project("repo1") == "otto-repo1-ci-7"


# ---------------------------------------------------------------------------
# _resolve_parent
# ---------------------------------------------------------------------------

def test_resolve_parent_prefers_explicit_on(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = _resolve_parent(repo, lab, on="pepper_seed", composes=list(repo.docker_settings.composes))
    assert parent.id == "pepper_seed"


def test_resolve_parent_falls_back_to_default_host(tmp_path):
    repo = _make_repo(tmp_path, default_host="pepper_seed")
    lab = _make_lab()
    parent = _resolve_parent(repo, lab, on=None, composes=list(repo.docker_settings.composes))
    assert parent.id == "pepper_seed"


def test_resolve_parent_rejects_non_capable(tmp_path):
    repo = _make_repo(tmp_path, default_host="other_seed")
    lab = _make_lab()
    # Add a host that is NOT docker_capable.
    other = _wire_parent_mock(RemoteHost(
        ip="1.2.3.4", ne="other", creds={"u": "p"}, board="seed", docker_capable=False,
    ))
    lab.hosts[other.id] = other
    with pytest.raises(ValueError, match="not docker_capable"):
        _resolve_parent(repo, lab, on=None, composes=list(repo.docker_settings.composes))


def test_resolve_parent_errors_when_no_host(tmp_path):
    repo = _make_repo(tmp_path, default_host=None or "pepper_seed")
    lab = _make_lab()
    # Use a wholly unknown host.
    with pytest.raises(ValueError, match="not in lab"):
        _resolve_parent(repo, lab, on="nobody", composes=list(repo.docker_settings.composes))


# ---------------------------------------------------------------------------
# compose_up command construction & idempotence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compose_up_constructs_expected_command(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    # Sequence the parent's oneshot responses:
    # 1) staging mkdir/rm calls — return ok
    # 2) `docker ps -q --filter ...project=...` — empty (not up)
    # 3) `docker compose ... up -d` — ok
    # 4) `docker compose ... config --services` — list of services
    # 5) docker ps -q --filter project + service — container id
    call_log: list[str] = []

    async def oneshot(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")  # stack not up
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("abc123def456\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts
    assert hosts["api"].container_id == "abc123def456"
    assert hosts["api"].id in lab.hosts
    # Verify a `docker compose -p ... -f ... up -d` was issued.
    up_cmds = [c for c in call_log if "compose" in c and "up -d" in c]
    assert len(up_cmds) == 1, call_log
    cmd = up_cmds[0]
    assert " -p otto-repo1-" in cmd
    assert " -f " in cmd
    assert cmd.rstrip().endswith("up -d")


@pytest.mark.asyncio
async def test_compose_up_builds_images_first_by_default(tmp_path):
    """compose_up's default is build=True so locally-built images exist before compose runs."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    call_log: list[str] = []

    async def oneshot(cmd, *_, **__):
        call_log.append(cmd)
        if cmd.startswith("docker image inspect"):
            return _ok()  # pretend the image is already built
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("abc123\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    await compose_up(repo, lab)
    # The build path must have been consulted (docker image inspect on the
    # full hash tag is the entry point of build_images).
    assert any(c.startswith("docker image inspect") for c in call_log), call_log


@pytest.mark.asyncio
async def test_compose_up_skips_build_when_build_false(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    call_log: list[str] = []

    async def oneshot(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("abc123\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    await compose_up(repo, lab, build=False)
    assert not any(c.startswith("docker image inspect") for c in call_log), \
        "build=False must skip the build path entirely"


@pytest.mark.asyncio
async def test_compose_up_idempotent_when_already_running(tmp_path):
    """If the stack is already up, compose_up reuses it (no second `up -d`)."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    call_log: list[str] = []

    async def oneshot(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # stack IS up
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts
    up_cmds = [c for c in call_log if "compose" in c and "up -d" in c]
    assert up_cmds == [], "must NOT issue a second `up -d` when already running"


@pytest.mark.asyncio
async def test_compose_down_removes_registered_hosts(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    parent.oneshot.return_value = _ok()  # type: ignore[union-attr]

    # Pre-populate with a fake registered container host.
    fake = DockerContainerHost(
        parent=parent, container_id="cid", project="repo1",
        service="api", compose_project="otto-repo1-x",
    )
    lab.hosts[fake.id] = fake  # type: ignore[assignment]
    assert fake.id in lab.hosts

    await compose_down(repo, lab)
    assert fake.id not in lab.hosts


@pytest.mark.asyncio
async def test_composed_does_not_teardown_when_already_running(tmp_path):
    """The default own=False contract — nested users don't yank the stack."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def oneshot(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # always "up"
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]
    seen_down = []
    parent.oneshot_orig = parent.oneshot

    async with composed(repo, lab):
        pass

    cmds = [c.args[0] for c in parent.oneshot.call_args_list]  # type: ignore[union-attr]
    assert not any(("compose" in c and " down" in c) for c in cmds), \
        "composed(own=False) must skip teardown when stack was already running"


@pytest.mark.asyncio
async def test_composed_tears_down_when_own_true(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def oneshot(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    async with composed(repo, lab, own=True):
        pass

    cmds = [c.args[0] for c in parent.oneshot.call_args_list]  # type: ignore[union-attr]
    assert any(("compose" in c and " down" in c) for c in cmds), \
        "composed(own=True) must tear down even if stack was already up"


# ---------------------------------------------------------------------------
# register_declared_container_hosts
# ---------------------------------------------------------------------------

def test_register_declared_creates_placeholders(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()

    n = register_declared_container_hosts(lab, [repo])
    assert n == 1
    placeholder = lab.hosts.get("pepper_seed.repo1.api")
    assert isinstance(placeholder, DockerContainerHost)
    assert placeholder.container_id == ""  # placeholder marker


def test_register_declared_skips_existing(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    register_declared_container_hosts(lab, [repo])  # first registration
    n2 = register_declared_container_hosts(lab, [repo])  # second
    assert n2 == 0  # nothing new


def test_register_declared_noop_without_capable_hosts(tmp_path):
    repo = _make_repo(tmp_path)
    lab = Lab(name="empty")  # no hosts at all
    n = register_declared_container_hosts(lab, [repo])
    assert n == 0
