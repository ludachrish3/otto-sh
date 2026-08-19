"""Unit tests for `otto.docker.compose` orchestration.

These mock the parent host's `exec`/`put` so no real docker is invoked.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.config.repo import (
    Repo,
)
from otto.docker.compose import (
    _resolve_parent,
    _safe_username,
    _stack_already_up,
    compose_down,
    compose_ps,
    compose_up,
    composed,
    get_container_host,
    get_user_compose_project,
    register_declared_container_hosts,
)
from otto.host.docker_host import DockerContainerHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandResult, Result
from otto.utils import Status
from tests._fixtures.sutrepo import make_sut_repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(out: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command="", retcode=0)


def _fail(out: str = "") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


# The libnetwork race compose_up retries past: the network is Created, the
# container is Created, then attaching it at "Starting" fails because the
# just-created network isn't yet visible to the daemon's networking setup.
_TRANSIENT_NETWORK_RACE_OUTPUT = (
    " Network otto-repo1-x_default  Created\n"
    " Container otto-repo1-x-api-1  Starting\n"
    "Error response from daemon: failed to set up container networking: "
    "network otto-repo1-x_default not found\n"
)


def _make_repo(
    tmp: Path, *, name: str = "repo1", services: tuple = ("api",), default_host: str = "pepper_seed"
) -> Repo:
    services_toml = "[" + ", ".join(f'"{s}"' for s in services) + "]"
    sut = make_sut_repo(
        tmp / name,
        name=name,
        extra=(
            f"[docker]\n"
            f"\n"
            f"[[docker.images]]\n"
            f'name = "api"\n'
            f'dockerfile = "docker/Dockerfile"\n'
            f'context = "docker"\n'
            f"\n"
            f"[[docker.composes]]\n"
            f'path = "docker/compose.yml"\n'
            f'default_host = "{default_host}"\n'
            f"services = {services_toml}\n"
        ),
        files={
            "docker/Dockerfile": "FROM alpine\n",
            "docker/compose.yml": "services: {}\n",
        },
    )
    return Repo(sut_dir=sut)


def _capable_host(host_id: str = "pepper_seed", ne: str = "pepper") -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element=ne,
        creds=[Cred(login="vagrant", password="vagrant")],
        board="seed",
        docker_capable=True,
    )


def _wire_parent_mock(host: UnixHost) -> UnixHost:
    """Replace the host's network methods with AsyncMocks so we never connect."""
    host.exec = AsyncMock(return_value=_ok())  # type: ignore[method-assign]
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]
    host.get = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]
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
    parent = _resolve_parent(
        repo, lab, on="pepper_seed", composes=list(repo.docker_settings.composes)
    )
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
    other = _wire_parent_mock(
        UnixHost(
            ip="1.2.3.4",
            element="other",
            creds=[Cred(login="u", password="p")],
            board="seed",
            docker_capable=False,
        )
    )
    lab.hosts[other.id] = other
    with pytest.raises(ValueError, match="not docker_capable"):
        _resolve_parent(repo, lab, on=None, composes=list(repo.docker_settings.composes))


def test_resolve_parent_errors_when_no_host(tmp_path):
    repo = _make_repo(tmp_path, default_host="pepper_seed")
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

    # Sequence the parent's exec responses:
    # 1) staging mkdir/rm calls — return ok
    # 2) `docker ps -q --filter ...project=...` — empty (not up)
    # 3) `docker compose ... up -d` — ok
    # 4) `docker compose ... config --services` — list of services
    # 5) docker ps -q --filter project + service — container id
    call_log: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
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

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

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

    async def exec_side_effect(cmd, *_, **__):
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

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

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

    async def exec_side_effect(cmd, *_, **__):
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

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    await compose_up(repo, lab, build=False)
    assert not any(c.startswith("docker image inspect") for c in call_log), (
        "build=False must skip the build path entirely"
    )


@pytest.mark.asyncio
async def test_compose_up_idempotent_when_already_running(tmp_path):
    """If the stack is already up, compose_up reuses it (no second `up -d`)."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    call_log: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # stack IS up
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts
    up_cmds = [c for c in call_log if "compose" in c and "up -d" in c]
    assert up_cmds == [], "must NOT issue a second `up -d` when already running"


@pytest.mark.asyncio
async def test_compose_up_second_call_reregisters_without_raising(tmp_path):
    """A second compose_up for the same service (e.g. after a container
    restart bumped its container id) must re-register cleanly, not raise.

    The registered host id (``<parent_id>.<project>.<service>``) is stable
    across calls, so a second ``compose_up`` targets the SAME id every time —
    exercising the explicit ``lab.hosts.pop(host.id, None); lab.add_host(host)``
    pattern in ``compose_up`` (Lab.add_host on its own rejects a duplicate id
    outright; see test_placeholder_id_collision_with_different_host_is_rejected).
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    container_ids = iter(["abc111", "def222"])

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # stack always reports "up"
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok(f"{next(container_ids)}\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    first = await compose_up(repo, lab, build=False)
    second = await compose_up(repo, lab, build=False)  # must NOT raise

    assert first["api"].id == second["api"].id
    assert first["api"].container_id == "abc111"
    assert second["api"].container_id == "def222"  # replaced in place
    assert lab.hosts[second["api"].id] is second["api"]  # exactly one entry, the latest


@pytest.mark.asyncio
async def test_compose_up_retries_once_on_transient_network_race(tmp_path, monkeypatch):
    """A transient libnetwork "network ... not found" on the first `up -d` is
    retried once; the convergent re-run then starts the already-created
    container and succeeds."""
    monkeypatch.setattr("otto.docker.compose._NETWORK_RACE_RETRY_BACKOFF_S", 0.0, raising=False)
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    up_attempts = 0
    call_log: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
        nonlocal up_attempts
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")  # stack not up
        if "compose" in cmd and " up -d" in cmd:
            up_attempts += 1
            if up_attempts == 1:
                return _fail(_TRANSIENT_NETWORK_RACE_OUTPUT)
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("abc123\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts
    up_cmds = [c for c in call_log if "compose" in c and "up -d" in c]
    assert len(up_cmds) == 2, "transient network race must trigger exactly one retry"


@pytest.mark.asyncio
async def test_compose_up_does_not_retry_real_compose_failure(tmp_path, monkeypatch):
    """A genuine compose failure (not the network race) is NOT retried — it
    propagates as RuntimeError after a single attempt, so the retry can't
    mask real errors (bad compose file, pull denied, port clash)."""
    monkeypatch.setattr("otto.docker.compose._NETWORK_RACE_RETRY_BACKOFF_S", 0.0, raising=False)
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    call_log: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _fail("Error response from daemon: pull access denied for repo1-api")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="docker compose up failed"):
        await compose_up(repo, lab)
    up_cmds = [c for c in call_log if "compose" in c and "up -d" in c]
    assert len(up_cmds) == 1, "a non-transient failure must NOT be retried"


@pytest.mark.asyncio
async def test_compose_up_polls_for_container_id_after_start(tmp_path, monkeypatch):
    """A just-Started container can briefly not appear in `docker ps` on a busy
    daemon; the container-id lookup must poll past that empty first result so
    the service is registered instead of silently skipped (0 containers)."""
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0.0, raising=False)
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    resolve_calls = 0

    async def exec_side_effect(cmd, *_, **__):
        nonlocal resolve_calls
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")  # stack not up yet
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            resolve_calls += 1
            if resolve_calls == 1:
                return _ok("")  # container not yet visible
            return _ok("abc123\n")  # now it appears
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts, "service must register once the container becomes visible"
    assert hosts["api"].container_id == "abc123"
    assert resolve_calls >= 2, "resolve must poll past the first empty result"


@pytest.mark.asyncio
async def test_compose_up_resolve_gives_up_after_bounded_polls(tmp_path, monkeypatch):
    """Bounded polling (no infinite wait) — and a stack where NOTHING resolves fails.

    The bounded-poll count is the subject; the raise is the point. A stack
    that came up but registered no host used to return {}, which
    `otto docker up` printed as "0 container(s) registered" in green, exit 0.
    """
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0.0, raising=False)
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_ATTEMPTS", 3, raising=False)
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    resolve_calls = 0

    async def exec_side_effect(cmd, *_, **__):
        nonlocal resolve_calls
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            resolve_calls += 1
            return _ok("")  # never visible
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="1 service\\(s\\) resolved to a running"):
        await compose_up(repo, lab)
    assert resolve_calls == 3, "must poll exactly _CONTAINER_ID_RESOLVE_ATTEMPTS times then stop"


@pytest.mark.asyncio
async def test_compose_up_still_skips_one_unresolvable_service_among_several(tmp_path, monkeypatch):
    """One service failing to resolve is a warning, not a failure.

    The counterpart to the test above, and the line between them: registering
    SOME hosts is a usable stack, registering NONE is not. Without this, the
    "no hosts" guard could be satisfied by making any unresolved service fatal,
    which would turn a one-shot sidecar into a broken `otto docker up`.
    """
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0.0, raising=False)
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_ATTEMPTS", 2, raising=False)
    repo = _make_repo(tmp_path, services=("api", "sidecar"))
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _ok()
        if "config" in cmd and "--services" in cmd:
            return _ok("api\nsidecar\n")
        if "service=api" in cmd:
            return _ok("cid-api\n")
        if "service=sidecar" in cmd:
            return _ok("")  # never visible
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert set(hosts) == {"api"}


@pytest.mark.asyncio
async def test_compose_down_removes_registered_hosts(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    parent.exec.return_value = _ok()  # type: ignore[union-attr]

    # Pre-populate with a fake registered container host.
    fake = DockerContainerHost(
        parent=parent,
        container_id="cid",
        project="repo1",
        service="api",
        compose_project="otto-repo1-x",
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

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # always "up"
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]
    parent.exec_orig = parent.exec

    async with composed(repo, lab):
        pass

    cmds = [c.args[0] for c in parent.exec.call_args_list]  # type: ignore[union-attr]
    assert not any(("compose" in c and " down" in c) for c in cmds), (
        "composed(own=False) must skip teardown when stack was already running"
    )


@pytest.mark.asyncio
async def test_composed_tears_down_when_own_true(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    async with composed(repo, lab, own=True):
        pass

    cmds = [c.args[0] for c in parent.exec.call_args_list]  # type: ignore[union-attr]
    assert any(("compose" in c and " down" in c) for c in cmds), (
        "composed(own=True) must tear down even if stack was already up"
    )


@pytest.mark.asyncio
async def test_composed_teardown_survives_cancellation(tmp_path):
    """A cancel landing during composed()'s finally must not half-tear the
    stack: compose_down still completes, then the cancel re-raises
    (spec: shielded compensating actions)."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    down_started = asyncio.Event()
    release_down = asyncio.Event()
    down_commands: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        if "compose" in cmd and " down" in cmd:
            down_started.set()
            await release_down.wait()  # hold the down so a cancel CAN land mid-teardown
            down_commands.append(cmd)
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    inside = asyncio.Event()
    hold_body = asyncio.Event()

    async def flow() -> None:
        async with composed(repo, lab, own=True):
            inside.set()
            await hold_body.wait()

    task = asyncio.ensure_future(flow())
    await inside.wait()
    task.cancel()  # cancel the body -> finally's compose_down starts
    await down_started.wait()
    task.cancel()  # second cancel lands MID-teardown: compensate must hold it
    await asyncio.sleep(0)
    release_down.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert down_commands, "compose down was torn mid-flight instead of completing"


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


def test_placeholder_id_collision_with_different_host_is_rejected(tmp_path):
    """A genuinely colliding placeholder — a DIFFERENT host already
    registered under the exact id a new placeholder would take — is
    rejected by Lab.add_host, not silently overwritten.

    register_declared_container_hosts's own `if placeholder.id in
    lab.hosts: continue` guard never reaches add_host for this case (it
    treats any existing entry, ours or otherwise, as "already handled"),
    so this exercises add_host directly — the safety net that compose_up's
    own explicit pop-then-add (see test_compose_up_second_call_reregisters_
    without_raising) deliberately routes around only for ITS OWN
    re-registrations.
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    placeholder = DockerContainerHost(
        parent=parent,
        container_id="",
        project=repo.name,
        service="api",
        compose_project=get_user_compose_project(repo.name),
        resources=set(parent.resources),
    )

    # A pre-existing, unrelated host occupying the exact id the placeholder
    # would take.
    existing = MagicMock()
    existing.id = placeholder.id
    lab.hosts[placeholder.id] = existing

    with pytest.raises(KeyError, match=re.escape(placeholder.id)):
        lab.add_host(placeholder)

    # The unrelated host must survive untouched — no silent overwrite.
    assert lab.hosts[placeholder.id] is existing


def _merged_lab_with_stamped_parent() -> Lab:
    """A COMPOSITE lab ("a+b") whose docker-capable parent came from component "a".

    The hostile condition, injected rather than inherited: the lab a container
    is registered into no longer names any single component, so a container
    stamped from the lab (``Lab.add_host``'s backstop) says "a+b" — a name no
    ``lab_patterns`` entry fullmatches.
    """
    lab_a = Lab(name="a")
    parent = _wire_parent_mock(_capable_host())
    parent.source_lab = "a"
    lab_a.add_host(parent)

    merged = lab_a + Lab(name="b")
    assert merged.name == "a+b"
    return merged


def test_declared_container_inherits_its_parents_lab_not_the_composite(tmp_path):
    """A placeholder container is attributed to its parent's lab, not the merge's name."""
    repo = _make_repo(tmp_path)
    lab = _merged_lab_with_stamped_parent()

    assert register_declared_container_hosts(lab, [repo]) == 1

    (container,) = [h for h in lab.hosts.values() if isinstance(h, DockerContainerHost)]
    assert container.source_lab == "a"


@pytest.mark.asyncio
async def test_compose_up_container_inherits_its_parents_lab_not_the_composite(tmp_path):
    """Same rule on the live registration path: parent's lab wins over the composite."""
    repo = _make_repo(tmp_path)
    lab = _merged_lab_with_stamped_parent()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # stack IS up
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("abc111\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab, build=False)

    assert hosts["api"].source_lab == "a"


def _make_bare_repo(tmp: Path, *, name: str = "bare1") -> Repo:
    """Build a Repo with NO [[docker.composes]] entries."""
    return Repo(sut_dir=make_sut_repo(tmp / name, name=name))


# ---------------------------------------------------------------------------
# compose_ps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_ps_parses_json_lines(tmp_path):
    """Valid JSON lines are parsed; blank lines and non-JSON lines are skipped."""
    host = _capable_host()
    _wire_parent_mock(host)
    host.exec.return_value = _ok('{"ID":"a"}\n\n{"ID":"b"}\nnot-json\n')  # type: ignore[union-attr]
    result = await compose_ps(host)
    assert result == [{"ID": "a"}, {"ID": "b"}]


@pytest.mark.asyncio
async def test_compose_ps_non_ok_returns_empty():
    """A non-ok parent response returns an empty list without raising."""
    host = _capable_host()
    _wire_parent_mock(host)
    host.exec.return_value = _fail("boom")  # type: ignore[union-attr]
    result = await compose_ps(host)
    assert result == []


# ---------------------------------------------------------------------------
# get_container_host
# ---------------------------------------------------------------------------


def test_get_container_host_success(tmp_path):
    """Returns the DockerContainerHost when found by id."""
    parent = _wire_parent_mock(_capable_host())
    container = DockerContainerHost(
        parent=parent,
        container_id="abc123",
        project="repo1",
        service="api",
        compose_project="otto-repo1-user",
    )
    fake_lab = Lab(name="test")
    fake_lab.hosts[container.id] = container  # type: ignore[assignment]

    with patch("otto.config.get_lab", return_value=fake_lab):
        result = get_container_host(container.id)
    assert result is container


def test_get_container_host_missing_raises(tmp_path):
    """Raises KeyError when the host_id is not in the lab."""
    fake_lab = Lab(name="test")
    with patch("otto.config.get_lab", return_value=fake_lab), pytest.raises(KeyError):
        get_container_host("does_not_exist")


def test_get_container_host_wrong_type_raises(tmp_path):
    """Raises KeyError when the host exists but is not a DockerContainerHost."""
    parent = _wire_parent_mock(_capable_host())
    fake_lab = Lab(name="test")
    fake_lab.hosts[parent.id] = parent  # a UnixHost, not a DockerContainerHost
    with patch("otto.config.get_lab", return_value=fake_lab), pytest.raises(KeyError):
        get_container_host(parent.id)


# ---------------------------------------------------------------------------
# compose_up — error branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_up_no_composes_raises(tmp_path):
    """Raises ValueError when the repo has no [[docker.composes]] entries."""
    repo = _make_bare_repo(tmp_path)
    lab = _make_lab()
    with pytest.raises(ValueError, match=r"no .*composes"):
        await compose_up(repo, lab)


@pytest.mark.asyncio
async def test_compose_up_build_failure_raises(tmp_path):
    """Raises RuntimeError when build_images returns a failed status for an image."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()

    # build_images returns dict[str, CommandResult]; a non-ok result trips the branch
    fake_results = {
        "api": CommandResult(
            Status.Failed, value="push access denied", command="docker build", retcode=1
        )
    }

    with (
        patch("otto.docker.build.build_images", new=AsyncMock(return_value=fake_results)),
        # Matches the PAYLOAD, not just the prefix: a value/msg inversion
        # would still produce "build for image 'api' failed before compose
        # up: " and pass a prefix-only match.
        pytest.raises(RuntimeError, match="push access denied"),
    ):
        await compose_up(repo, lab, build=True)


# ---------------------------------------------------------------------------
# _resolve_parent — error branches
# ---------------------------------------------------------------------------


def test_resolve_parent_no_candidate_raises(tmp_path):
    """Raises ValueError when no on= and no default_host is set in composes."""
    # Build a compose list with NO default_host
    sut = make_sut_repo(
        tmp_path / "repo1",
        name="repo1",
        extra=(
            "[docker]\n\n"
            "[[docker.composes]]\n"
            'path = "docker/compose.yml"\n'
            'services = ["api"]\n'
            "# no default_host\n"
        ),
        files={"docker/compose.yml": "services: {}\n"},
    )
    repo = Repo(sut_dir=sut)
    lab = _make_lab()
    composes = list(repo.docker_settings.composes)

    with pytest.raises(ValueError, match="No docker host"):
        _resolve_parent(repo, lab, on=None, composes=composes)


def test_resolve_parent_non_unixhost_raises(tmp_path):
    """Raises TypeError when the resolved host is not a UnixHost."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()

    # Install a non-UnixHost under the id "weird"
    weird = MagicMock()
    weird.id = "weird"
    lab.hosts["weird"] = weird

    composes = list(repo.docker_settings.composes)
    with pytest.raises(TypeError, match="must be a UnixHost"):
        _resolve_parent(repo, lab, on="weird", composes=composes)


# ---------------------------------------------------------------------------
# compose_down — error branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_down_no_composes_skipped(tmp_path):
    """Returns a Skipped CommandResult immediately when the repo has no composes."""
    repo = _make_bare_repo(tmp_path)
    lab = _make_lab()
    result = await compose_down(repo, lab)
    assert result.status is Status.Skipped
    assert result.retcode == -1  # never ran


@pytest.mark.asyncio
async def test_compose_down_failure_logs_error(tmp_path, caplog):
    """Logs an ERROR containing 'compose down failed' when the down command fails."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        # stage_compose_files uses mkdir/rm calls that should succeed
        if "compose" in cmd and " down" in cmd:
            return _fail("down boom")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with caplog.at_level(logging.ERROR):
        result = await compose_down(repo, lab)

    assert any("compose down failed" in r.message for r in caplog.records)
    # The function returns the failed CommandResult — verify it didn't raise and
    # the failure path is confirmed by the returned value
    assert result.status is Status.Failed


@pytest.mark.asyncio
async def test_compose_down_swallows_host_close_error(tmp_path):
    """Does NOT propagate when a registered container host's close() raises."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    # Wire down command to succeed so we reach the host-close loop
    parent.exec.return_value = _ok()  # type: ignore[union-attr]

    # Register a container host under this parent + repo whose close() raises
    noisy = DockerContainerHost(
        parent=parent,
        container_id="cid99",
        project=repo.name,
        service="api",
        compose_project="otto-repo1-x",
    )
    noisy.close = AsyncMock(side_effect=Exception("close exploded"))  # type: ignore[method-assign]
    lab.hosts[noisy.id] = noisy  # type: ignore[assignment]

    # Must NOT propagate the Exception from close()
    result = await compose_down(repo, lab)
    # Prove the close() branch was actually exercised (not just skipped)
    noisy.close.assert_called_once()
    # down command succeeded, so the returned result is Success
    assert result.status is Status.Success


# ---------------------------------------------------------------------------
# _safe_username
# ---------------------------------------------------------------------------


def test_safe_username_keyerror_returns_anon():
    """Falls back to 'anon' when getpass.getuser() raises KeyError."""
    with patch("otto.docker.compose.getpass.getuser", side_effect=KeyError("no user")):
        assert _safe_username() == "anon"


# ---------------------------------------------------------------------------
# Absorbed failures: every one either fails loud or says so out loud
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_up_fails_when_services_cannot_be_listed_and_none_declared(tmp_path):
    """The worst shape in the module: a failed stack reported as success.

    With no declared services, a failed `config --services` left `services`
    empty, the registration loop never ran, and compose_up returned {} —
    which `otto docker up` prints as "0 container(s) registered", exit 0.
    """
    repo = _make_repo(tmp_path, services=())
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "config" in cmd and "--services" in cmd:
            return _fail("cannot connect to the docker daemon")
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="declares none of its own"):
        await compose_up(repo, lab)


@pytest.mark.asyncio
async def test_compose_up_only_warns_when_the_cross_check_fails(tmp_path, caplog):
    """With services declared, the live listing is a cross-check, not the source.

    Losing it must cost a warning rather than the stack — the declared list is
    authoritative, so registration can proceed on it alone.
    """
    repo = _make_repo(tmp_path, services=("api",))
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "config" in cmd and "--services" in cmd:
            return _fail("daemon hiccup")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("cid-api\n")
        if "label=com.docker.compose.project=" in cmd:
            return _ok("")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with caplog.at_level(logging.WARNING, logger="otto.docker.compose"):
        hosts = await compose_up(repo, lab)

    assert set(hosts) == {"api"}
    assert any(
        "cross-check" in r.message
        and r.levelno == logging.WARNING
        and r.name == "otto.docker.compose"
        for r in caplog.records
    ), caplog.text


@pytest.mark.asyncio
async def test_stack_already_up_reports_unknown_rather_than_no(caplog):
    """An unanswerable `docker ps` is not the same as "nobody had it".

    Three states rather than a raise, because the two callers want opposite
    things: compose_up treats unknown as "not up" and runs the convergent
    `up -d` (a wrong guess costs nothing there), while composed() cannot
    guess at all — see the two tests below.
    """
    parent = _wire_parent_mock(_capable_host())
    parent.exec.return_value = _fail("cannot connect to the docker daemon")  # type: ignore[union-attr]

    with caplog.at_level(logging.WARNING, logger="otto.docker.compose"):
        assert await _stack_already_up(parent, "otto-repo1-vagrant") is None
    assert any(
        "could not tell whether" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    ), caplog.text


@pytest.mark.asyncio
async def test_composed_refuses_to_guess_whose_stack_it_is(tmp_path):
    """`composed(own=False)` cannot honour its contract on an unknown answer.

    It tears down only what it brought up; unknown read as "nobody had it"
    means the teardown yanks a stack an outer fixture is holding — precisely
    what the docstring promises not to do.
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _fail("cannot connect to the docker daemon")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="cannot tell whether"):
        async with composed(repo, lab):
            pass


@pytest.mark.asyncio
async def test_composed_does_not_even_probe_when_it_owns_the_stack(tmp_path):
    """`own=True` discards the answer, so it must not be paid for — or fail on.

    The gate is `if own or not was_up`, so with own=True the probe's value is
    dead. Failing the whole flow on a transient `docker ps` for a value nobody
    reads is the shape this sweep is supposed to remove, not add.
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    project_probes = 0

    async def exec_side_effect(cmd, *_, **__):
        nonlocal project_probes
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            project_probes += 1
            return _fail("cannot connect to the docker daemon")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "service=api" in cmd:
            return _ok("cid-api\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    async with composed(repo, lab, own=True) as hosts:
        assert set(hosts) == {"api"}
    # compose_up's own probe still runs (unknown -> convergent `up -d`), but
    # composed() adds none of its own.
    assert project_probes == 1, "composed(own=True) must not probe for a value it discards"


@pytest.mark.asyncio
async def test_compose_ps_warns_when_a_daemon_cannot_be_reached(caplog):
    """Still best-effort — `otto docker ps` tables the whole fleet — but LOUD.

    An empty list is otherwise indistinguishable from a host that simply has
    no containers running.
    """
    host = _capable_host()
    _wire_parent_mock(host)
    host.exec.return_value = _fail("cannot connect to the docker daemon")  # type: ignore[union-attr]

    with caplog.at_level(logging.WARNING, logger="otto.docker.compose"):
        result = await compose_ps(host)

    assert result == []
    # The host id is the entire point of this warning in a fleet-wide table.
    assert any(
        "could not list containers" in r.message
        and host.id in r.message
        and r.levelno == logging.WARNING
        and r.name == "otto.docker.compose"
        for r in caplog.records
    ), caplog.text


@pytest.mark.asyncio
async def test_stage_image_context_fails_when_the_dir_cannot_be_prepared(tmp_path):
    """`rm -rf && mkdir` is the invariant "no leftovers", and it was unchecked.

    `&&` means a failed rm skips the mkdir silently, and the later `tar -xf`
    OVERLAYS rather than replaces — so docker build would see a context still
    holding a file the user deleted locally and produce a wrong image under a
    context hash that says it is right.
    """
    from otto.config.repo import DockerImage
    from otto.docker.staging import stage_image_context

    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    image = DockerImage(name="api", dockerfile=df, context=tmp_path)
    parent = _wire_parent_mock(_capable_host())

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -rf "):
            return _fail("permission denied")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="prepare the build-context dir"):
        await stage_image_context(parent, "repo1", image)


@pytest.mark.asyncio
async def test_stage_compose_files_fails_when_the_dir_cannot_be_prepared(tmp_path):
    """Same unchecked wipe-and-recreate, compose side."""
    from otto.docker.staging import stage_compose_files

    compose_path = tmp_path / "compose.yml"
    compose_path.write_text("services: {}\n")
    compose = _make_repo(tmp_path / "r", services=("api",)).docker_settings.composes[0]
    parent = _wire_parent_mock(_capable_host())

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -rf "):
            return _fail("read-only filesystem")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="prepare the compose staging dir"):
        await stage_compose_files(parent, "otto-repo1", [compose])


@pytest.mark.asyncio
async def test_stage_compose_files_fails_when_a_numbered_subdir_cannot_be_made(tmp_path):
    """The per-file `mkdir -p` was discarded too; a failure here means the
    subsequent `put` lands somewhere unintended or not at all."""
    from otto.docker.staging import stage_compose_files

    compose = _make_repo(tmp_path / "r", services=("api",)).docker_settings.composes[0]
    parent = _wire_parent_mock(_capable_host())

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("mkdir -p ") and cmd.rstrip().endswith("/0"):
            return _fail("no space left on device")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="failed to create"):
        await stage_compose_files(parent, "otto-repo1", [compose])


@pytest.mark.asyncio
async def test_compose_up_rolls_back_a_stack_it_started_before_raising(tmp_path):
    """Failing loud must not be worse than the silent {} it replaced.

    Every raise in compose_up happens AFTER `up -d`, and no caller can clean
    up what it never received: `composed()` arms its try/finally only on a
    successful return. The old silent {} was accidentally safe there — it
    entered the try and tore the stack down — so raising without a rollback
    would strand a definitely-running stack.
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    downs = 0

    async def exec_side_effect(cmd, *_, **__):
        nonlocal downs
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")  # not already up -> WE bring it up
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "service=api" in cmd:
            return _ok("")  # never resolves -> the "no hosts" raise
        if "compose" in cmd and " down" in cmd:
            downs += 1
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="resolved to a running container"):
        await compose_up(repo, lab)
    assert downs == 1, "a stack compose_up started must not survive its own failure"


@pytest.mark.asyncio
async def test_compose_up_does_not_roll_back_someone_elses_stack(tmp_path):
    """The other half of the rollback rule: only tear down what WE started."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    downs = 0

    async def exec_side_effect(cmd, *_, **__):
        nonlocal downs
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("existing-cid\n")  # already up: someone else's
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "service=api" in cmd:
            return _ok("")
        if "compose" in cmd and " down" in cmd:
            downs += 1
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    with pytest.raises(RuntimeError, match="resolved to a running container"):
        await compose_up(repo, lab)
    assert downs == 0, "a stack that was already up belongs to whoever brought it up"


@pytest.mark.asyncio
async def test_compose_up_fails_when_the_stack_names_no_services_at_all(tmp_path):
    """`services: {}` with nothing declared: up, and nothing to register."""
    repo = _make_repo(tmp_path, services=())
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "config" in cmd and "--services" in cmd:
            return _ok("   \n")  # succeeds, lists nothing
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    # ValueError: nothing on the parent failed — the compose file declares no
    # services, the same class of refusal as _resolve_parent's own raises.
    with pytest.raises(ValueError, match="names no services"):
        await compose_up(repo, lab)


@pytest.mark.asyncio
async def test_compose_down_returns_a_failure_when_staging_cannot_be_prepared(tmp_path):
    """compose_down's contract is that a failed tear-down is RETURNED.

    Staging now raises, and letting that propagate would stop
    `otto docker down` mid-sweep with the remaining repos still up — and,
    inside `composed()`'s finally, replace the body's real exception with
    teardown noise.
    """
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -rf "):
            return _fail("read-only file system")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    result = await compose_down(repo, lab)
    assert not result.is_ok
    assert "read-only file system" in result.value
