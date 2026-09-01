"""Unit tests for `otto.docker.compose` orchestration.

These mock the parent host's `exec`/`put` so no real docker is invoked.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.config.repo import (
    DockerCompose,
    DockerUseCase,
    Repo,
)
from otto.docker.compose import (
    _resolve_parent,
    _safe_username,
    _stack_already_up,
    compose_down,
    compose_down_project,
    compose_ps,
    compose_up,
    composed,
    get_container_host,
    get_user_compose_project,
    merge_declared_users,
    register_declared_container_hosts,
    register_stack_hosts,
    unregister_container_hosts,
    use_case_project,
)
from otto.host.docker_host import DockerContainerHost
from otto.host.lab_info import LabInfo
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandNotRunError, CommandResult, Result
from otto.utils import Status
from tests._fixtures.sutrepo import make_sut_repo
from tests.conftest import active_context

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
    tmp: Path,
    *,
    name: str = "repo1",
    services: tuple = ("api",),
    host: str = "test3",
    users: dict | None = None,
) -> Repo:
    """A repo whose lone use-case fragment pins its stack to *host* — the
    same "declared exact host" semantics ``default_host`` used to carry,
    now expressed as a committed placement pin (spec §14)."""
    services_toml = "[" + ", ".join(f'"{s}"' for s in services) + "]"
    users_toml = (
        "users = {" + ", ".join(f'{k} = "{v}"' for k, v in users.items()) + "}\n" if users else ""
    )
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
            f'name = "core"\n'
            f'path = "docker/compose.yml"\n'
            f"services = {services_toml}\n"
            f"{users_toml}"
            f"\n"
            f"[[docker.use_cases]]\n"
            f'name = "{name}"\n'
            f'composes = ["core"]\n'
            f'role = "docker"\n'
            f'placement = {{ docker = "{host}" }}\n'
        ),
        files={
            "docker/Dockerfile": "FROM alpine\n",
            "docker/compose.yml": "services: {}\n",
        },
    )
    return Repo(sut_dir=sut)


def _capable_host(host_id: str = "test3", ne: str = "test3") -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element=ne,
        creds=[Cred(login="vagrant", password="vagrant")],
        docker_capable=True,
    )


def _wire_parent_mock(host: UnixHost) -> UnixHost:
    """Replace the host's network methods with AsyncMocks so we never connect."""
    host.exec = AsyncMock(return_value=_ok())  # type: ignore[method-assign]
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]
    host.get = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]
    return host


def _container_host(parent: UnixHost, project: str, service: str) -> DockerContainerHost:
    """A registered container host with an AsyncMock ``close`` we can assert on."""
    host = DockerContainerHost(
        parent=parent,
        container_id="cid",
        project=project,
        service=service,
        compose_project=f"unix-{project}-u",
    )
    host.close = AsyncMock()  # type: ignore[method-assign]
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
# use_case_project (spec §9)
# ---------------------------------------------------------------------------


def test_use_case_project_slugs_illegal_lab_characters(monkeypatch):
    """A lab name is free-form; a compose project name is not."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    assert use_case_project("Unix Lab.2", "integration") == "unix-lab-2-integration-u"


def test_use_case_project_slugs_every_segment_by_the_same_rule(monkeypatch):
    """Docker does not care WHICH segment carried the illegal character."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "first.last")
    assert use_case_project("Unix Lab", "smoke test") == "unix-lab-smoke-test-first-last"


def test_use_case_project_result_starts_compose_legal(monkeypatch):
    """`-p` must match [a-z0-9][a-z0-9_-]* — a leading _ or - is rejected."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    project = use_case_project("_scratch", "integration")
    assert project == "scratch-integration-u"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project), project


def test_use_case_project_survives_an_unattributed_lab(monkeypatch):
    """An empty source_lab must still yield a usable project, not a leading dash."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    project = use_case_project("", "integration")
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project), project
    assert project == "integration-u"


def test_use_case_project_has_no_otto_prefix(monkeypatch):
    """Spec §9: the deployment belongs to the product, not to otto.

    Pinned as its own assertion rather than folded into the equality above:
    re-introducing an ``otto-`` prefix is the specific regression this naming
    decision exists to prevent, and it should fail by name.
    """
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    project = use_case_project("unix", "integration")
    assert not project.startswith("otto-")
    assert project == "unix-integration-u"


def test_use_case_project_separates_labs(monkeypatch):
    """The lab segment is load-bearing: --remove-orphans reaps within a project."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    assert use_case_project("labA", "integration") != use_case_project("labB", "integration")


def test_use_case_project_honors_env_suffix(monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "ci-7")
    assert use_case_project("unix", "integration") == "unix-integration-ci-7"


def test_use_case_project_explicit_suffix_beats_env(monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "ci-7")
    assert use_case_project("unix", "integration", "mine") == "unix-integration-mine"


def test_use_case_project_falls_back_to_username(monkeypatch):
    monkeypatch.delenv("OTTO_COMPOSE_SUFFIX", raising=False)
    assert (
        use_case_project("unix", "integration") == f"unix-integration-{getpass.getuser().lower()}"
    )


# ---------------------------------------------------------------------------
# compose_down_project / unregister_container_hosts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_down_project_uses_the_project_label_and_no_f_flags():
    """Teardown by label: no -f, so deleting never re-runs an adapter (spec §8)."""
    lab = _make_lab()
    parent = lab.hosts["test3"]
    result = await compose_down_project(
        parent, "unix-integration-u", lab=lab, remove_ids_under=None
    )
    (cmd,) = [c.args[0] for c in parent.exec.call_args_list]
    assert cmd == "docker compose -p unix-integration-u down --remove-orphans --timeout 1"
    assert " -f " not in cmd
    assert result.is_ok


@pytest.mark.asyncio
async def test_compose_down_project_unregisters_only_under_the_prefix():
    lab = _make_lab()
    parent = lab.hosts["test3"]
    mine = _container_host(parent, "integration", "api")
    theirs = _container_host(parent, "other", "api")
    lab.hosts[mine.id] = mine
    lab.hosts[theirs.id] = theirs

    await compose_down_project(
        parent, "unix-integration-u", lab=lab, remove_ids_under="test3.integration."
    )

    assert mine.id not in lab.hosts
    assert theirs.id in lab.hosts
    mine.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_compose_down_project_none_prefix_unregisters_nothing():
    """The rollback path: it registered nothing, so it may pop nothing."""
    lab = _make_lab()
    parent = lab.hosts["test3"]
    mine = _container_host(parent, "integration", "api")
    lab.hosts[mine.id] = mine

    await compose_down_project(parent, "unix-integration-u", lab=lab, remove_ids_under=None)

    assert mine.id in lab.hosts
    mine.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_compose_down_project_declines_under_a_dry_run():
    """The arm exists because the SECOND half of this helper mutates lab.hosts.

    Called directly rather than through `deploy`/`teardown` — both of them
    decline above it today, which is exactly what left this refusal untested
    and free to rot. `compose_down_project` is not in `otto.docker.__all__`,
    so the package's adjudication class does not reach it either.
    """
    lab = _make_lab()
    parent = lab.hosts["test3"]
    mine = _container_host(parent, "integration", "api")
    lab.hosts[mine.id] = mine

    with (
        active_context(lab=lab, dry_run=True),
        pytest.raises(CommandNotRunError, match="compose_down_project"),
    ):
        await compose_down_project(
            parent, "unix-integration-u", lab=lab, remove_ids_under="test3.integration."
        )

    assert parent.exec.await_count == 0
    assert mine.id in lab.hosts, "a dry run must leave the lab alone"


@pytest.mark.asyncio
async def test_compose_down_project_returns_a_failed_down_rather_than_raising():
    lab = _make_lab()
    parent = lab.hosts["test3"]
    parent.exec = AsyncMock(return_value=_fail("no such project"))
    result = await compose_down_project(parent, "p", lab=lab, remove_ids_under=None)
    assert not result.is_ok


@pytest.mark.asyncio
async def test_unregister_container_hosts_narrows_to_named_services():
    """Partial teardown leaves the rest of the stack registered."""
    lab = _make_lab()
    parent = lab.hosts["test3"]
    api = _container_host(parent, "integration", "api")
    db = _container_host(parent, "integration", "db")
    lab.hosts[api.id] = api
    lab.hosts[db.id] = db

    removed = await unregister_container_hosts(lab, "test3.integration.", services=["api"])

    assert removed == [api.id]
    assert db.id in lab.hosts


@pytest.mark.asyncio
async def test_unregister_container_hosts_pops_a_host_that_fails_to_close():
    """A container that cannot be closed is still gone; advertising it would lie."""
    lab = _make_lab()
    parent = lab.hosts["test3"]
    api = _container_host(parent, "integration", "api")
    api.close = AsyncMock(side_effect=OSError("broken pipe"))
    lab.hosts[api.id] = api

    removed = await unregister_container_hosts(lab, "test3.integration.")

    assert removed == [api.id]
    assert api.id not in lab.hosts


# ---------------------------------------------------------------------------
# _resolve_parent
# ---------------------------------------------------------------------------


def test_resolve_parent_prefers_explicit_on(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = _resolve_parent(repo, lab, on="test3")
    assert parent.id == "test3"


def test_resolve_parent_falls_back_to_use_case_placement(tmp_path):
    """No --on: the repo's sole use-case fragment's placement pin wins."""
    repo = _make_repo(tmp_path, host="test3")
    lab = _make_lab()
    parent = _resolve_parent(repo, lab, on=None)
    assert parent.id == "test3"


def test_resolve_parent_rejects_non_capable(tmp_path):
    """Explicit --on still enforces docker_capable, regardless of how it got here."""
    repo = _make_repo(tmp_path)
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
        _resolve_parent(repo, lab, on=other.id)


def test_resolve_parent_falls_back_to_a_non_capable_pin_still_refuses(tmp_path):
    """The fallback path enforces docker_capable too (T14 review M3) — via a
    DIFFERENT message than the ``--on`` path, because a committed placement
    pin validates capability itself, inside ``_place_fragment``, before
    ``_resolve_parent``'s own tail check is ever reached. The scenario the
    old ``on=None`` test asserted (a *resolved* candidate turning out
    non-capable) is not gone with the fallback rewrite — it just now raises
    from the pin's own validation, with its own wording.
    """
    other = _wire_parent_mock(
        UnixHost(
            ip="1.2.3.4",
            element="other",
            creds=[Cred(login="u", password="p")],
            board="seed",
            docker_capable=False,
        )
    )
    repo = _make_repo(tmp_path, host=other.id)
    lab = Lab(name="test")
    lab.hosts[other.id] = other
    with pytest.raises(ValueError, match="must name a docker-capable unix host"):
        _resolve_parent(repo, lab, on=None)


def test_resolve_parent_errors_when_no_host(tmp_path):
    repo = _make_repo(tmp_path, host="test3")
    lab = _make_lab()
    # Use a wholly unknown host.
    with pytest.raises(ValueError, match="not in lab"):
        _resolve_parent(repo, lab, on="nobody")


def test_resolve_parent_refuses_use_cases_split_across_hosts(tmp_path):
    """A repo declaring TWO use-case fragments that each resolve cleanly but
    to DIFFERENT hosts is ambiguous for a per-repo verb — a bare
    ``compose_up``/``build`` cannot guess which one the caller means, so it
    must pass ``--on``. This is the replacement for the old "no
    default_host" fallback failure (spec §14): the per-repo primitives stay
    public, but a repo whose use-cases place onto several hosts needs
    disambiguation the per-repo surface has no way to ask for.
    """
    sut = make_sut_repo(
        tmp_path / "repo1",
        name="repo1",
        extra=(
            "[[docker.composes]]\n"
            'name = "core"\n'
            'path = "docker/compose.yml"\n'
            'services = ["api"]\n'
            "\n[[docker.use_cases]]\n"
            'name = "web"\n'
            'composes = ["core"]\n'
            'role = "docker"\n'
            'placement = { docker = "test1" }\n'
            "\n[[docker.use_cases]]\n"
            'name = "worker"\n'
            'composes = ["core"]\n'
            'role = "docker"\n'
            'placement = { docker = "test3" }\n'
        ),
        files={"docker/compose.yml": "services: {}\n"},
    )
    repo = Repo(sut_dir=sut)

    lab = Lab(name="test")
    for ne in ("test1", "test3"):
        lab.hosts[ne] = _wire_parent_mock(_capable_host(ne, ne=ne))

    with pytest.raises(ValueError, match="ambiguous for a per-repo verb"):
        _resolve_parent(repo, lab, on=None)

    # Load-bearing, not decorative: --on sidesteps the ambiguity entirely.
    assert _resolve_parent(repo, lab, on="test1").id == "test1"


# ---------------------------------------------------------------------------
# compose_up command construction & idempotence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_up_constructs_expected_command(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["test3"]

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
    assert cmd.rstrip().endswith("up -d --remove-orphans")


@pytest.mark.asyncio
async def test_compose_up_registers_containers_with_their_declared_users(tmp_path):
    """The legacy path merges `users` over the repo's own composes and hands
    the result to registration — a service with no declaration stays unset."""
    repo = _make_repo(tmp_path, services=("api", "db"), users={"db": "postgres"})
    lab = _make_lab()
    parent = lab.hosts["test3"]

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\ndb\n")
        if "service=api" in cmd:
            return _ok("cid-api\n")
        if "service=db" in cmd:
            return _ok("cid-db\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab, build=False)

    assert hosts["db"].user == "postgres"
    assert hosts["api"].user is None


@pytest.mark.asyncio
async def test_compose_up_builds_images_first_by_default(tmp_path):
    """compose_up's default is build=True so locally-built images exist before compose runs."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
async def test_compose_up_always_reissues_up_when_already_running(tmp_path):
    """`up -d --remove-orphans` is convergent (spec §8): compose_up issues it
    even when the already-up probe says the stack is up. Idempotency is now
    compose's job (a convergent re-run), not a skip on otto's side — the
    already-up probe answers ownership for rollback ONLY (see
    test_compose_up_second_call_reregisters_without_raising and
    _rollback_partial_up's ``brought_up_here`` gate)."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["test3"]
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
    assert len(up_cmds) == 1, "must issue `up -d` even when already running (convergent)"
    assert up_cmds[0].rstrip().endswith("up -d --remove-orphans")


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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
# register_stack_hosts — declared users (spec: users = {...})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_stack_hosts_threads_declared_user():
    """A declared service gets its user; an undeclared one stays None.

    Both halves matter: the second is what keeps `users` from becoming a
    blanket default applied to every container in the stack.
    """
    lab = _make_lab()
    parent = lab.hosts["test3"]

    async def exec_side_effect(cmd, *_, **__):
        if "service=api" in cmd:
            return _ok("cid-api\n")
        if "service=db" in cmd:
            return _ok("cid-db\n")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    hosts = await register_stack_hosts(
        lab,
        parent,
        compose_project="l-u-c",
        id_project="uc",
        services=["api", "db"],
        users={"db": "postgres"},
    )

    assert hosts["db"].user == "postgres"
    assert hosts["api"].user is None
    # ...and the lab's copy is the same object, so `otto host <id>` sees it too.
    assert lab.hosts["test3.uc.db"].user == "postgres"


@pytest.mark.asyncio
async def test_register_stack_hosts_without_users_leaves_every_container_unset():
    """The default path (no `users=`) must not invent an identity."""
    lab = _make_lab()
    parent = lab.hosts["test3"]
    parent.exec.side_effect = lambda cmd, *_, **__: _ok("cid\n")  # type: ignore[union-attr]

    hosts = await register_stack_hosts(
        lab, parent, compose_project="l-u-c", id_project="uc", services=["api"]
    )

    assert hosts["api"].user is None


# ---------------------------------------------------------------------------
# merge_declared_users
# ---------------------------------------------------------------------------


def test_merge_declared_users_conflict_refused():
    a = DockerCompose(path=Path("a.yml"), services=("db",), users=(("db", "postgres"),))
    b = DockerCompose(path=Path("b.yml"), services=("db",), users=(("db", "root"),))
    with pytest.raises(ValueError, match="conflicting declared users for service 'db'"):
        merge_declared_users([a, b])


def test_merge_declared_users_agreeing_duplicate_ok():
    """The same compose seen twice — or two files naming the same identity —
    is agreement, not a conflict: there is nothing for otto to invent."""
    a = DockerCompose(path=Path("a.yml"), services=("db",), users=(("db", "postgres"),))
    assert merge_declared_users([a, a]) == {"db": "postgres"}


def test_merge_declared_users_unions_across_composes():
    a = DockerCompose(path=Path("a.yml"), services=("db",), users=(("db", "postgres"),))
    b = DockerCompose(path=Path("b.yml"), services=("web",), users=(("web", "1000:1000"),))
    assert merge_declared_users([a, b]) == {"db": "postgres", "web": "1000:1000"}


def test_merge_declared_users_of_nothing_is_empty():
    assert merge_declared_users([]) == {}


# ---------------------------------------------------------------------------
# register_declared_container_hosts
# ---------------------------------------------------------------------------


def test_register_declared_creates_placeholders(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()

    n = register_declared_container_hosts(lab, [repo])
    assert n == 1
    placeholder = lab.hosts.get("test3.repo1.api")
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
    parent = lab.hosts["test3"]

    placeholder = DockerContainerHost(
        parent=parent,
        container_id="",
        project=repo.name,
        service="api",
        compose_project=get_user_compose_project(repo.name),
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


def test_register_declared_legacy_placeholder_carries_declared_user():
    """The legacy composes walk threads `users` onto its placeholders too, so
    an `otto host <id>` against a not-yet-up container already knows which
    identity it will act as."""
    lab = _make_lab()
    repo = _uc_repo("a", composes=[_core_compose(users=(("api", "postgres"),))])

    assert register_declared_container_hosts(lab, [repo]) == 1

    placeholder = lab.hosts["test3.a.api"]
    assert isinstance(placeholder, DockerContainerHost)
    assert placeholder.user == "postgres"


def _two_composes(users_a: tuple = (), users_b: tuple = ()) -> list[DockerCompose]:
    """Two compose entries that BOTH declare service `db` — the shape a
    cross-compose users disagreement (or a split declaration) needs."""
    return [
        DockerCompose(path=Path("docker/a.yml"), name="acore", services=("db",), users=users_a),
        DockerCompose(path=Path("docker/b.yml"), name="bcore", services=("db",), users=users_b),
    ]


def test_register_declared_legacy_placeholder_reads_the_merged_map_not_one_compose():
    """The user may be declared by the SECOND compose to name the service.

    Reading each entry's own `users` inside the loop would make the first
    compose win (the duplicate-id skip discards the later placeholder), so
    this container would come back unset while `compose_up` — which merges —
    gave it `postgres`. The two must not disagree.
    """
    lab = _make_lab()
    repo = _uc_repo("a", composes=_two_composes(users_b=(("db", "postgres"),)))

    assert register_declared_container_hosts(lab, [repo]) == 1
    assert lab.hosts["test3.a.db"].user == "postgres"


def test_register_declared_legacy_conflicting_users_refuse():
    """Same gate as `compose_up` on the same repo: a cross-compose
    disagreement refuses rather than silently first-winning."""
    lab = _make_lab()
    repo = _uc_repo(
        "a", composes=_two_composes(users_a=(("db", "postgres"),), users_b=(("db", "root"),))
    )

    with pytest.raises(ValueError, match="conflicting declared users for service 'db'"):
        register_declared_container_hosts(lab, [repo])


def test_register_declared_legacy_placeholder_without_users_is_unset():
    lab = _make_lab()
    repo = _uc_repo("a", composes=[_core_compose()])

    assert register_declared_container_hosts(lab, [repo]) == 1
    assert lab.hosts["test3.a.api"].user is None


def _merged_lab_with_stamped_parent() -> Lab:
    """A COMPOSITE lab ("a+b") whose docker-capable parent came from component "a".

    The hostile condition, injected rather than inherited: the lab a container
    is registered into no longer names any single component, so a container
    stamped from the lab (``Lab.add_host``'s backstop) says "a+b" — a name no
    ``lab_patterns`` entry fullmatches.

    The parent's ``lab_info`` carries a non-empty ``metadata`` table on purpose:
    it is the only way a container that ALIASES the parent's record can be told
    apart from one that copies it.
    """
    lab_a = Lab(name="a")
    parent = _wire_parent_mock(_capable_host())
    parent.source_lab = "a"
    parent.lab_info = LabInfo(name="a", metadata={"k": 1})
    lab_a.add_host(parent)

    merged = lab_a + Lab(name="b")
    assert merged.name == "a+b"
    return merged


def test_declared_container_inherits_its_parents_lab_not_the_composite(tmp_path):
    """A placeholder container is attributed to its parent's lab, not the merge's name."""
    repo = _make_repo(tmp_path)
    lab = _merged_lab_with_stamped_parent()
    parent = lab.hosts["test3"]

    assert register_declared_container_hosts(lab, [repo]) == 1

    (container,) = [h for h in lab.hosts.values() if isinstance(h, DockerContainerHost)]
    assert container.source_lab == "a"
    assert container.lab_info.name == "a"
    # ...and it must COPY that record, never alias it. ``LabInfo`` is frozen but
    # the dict behind ``metadata`` is not, so a shared table would make one
    # container's write visible on its parent and on every sibling container.
    container.lab_info.metadata["k"] = 99
    assert parent.lab_info.metadata == {"k": 1}


# ---------------------------------------------------------------------------
# register_declared_container_hosts — use-case branch (spec §9)
# ---------------------------------------------------------------------------


def _uc_repo(name: str, *fragments: DockerUseCase, composes: tuple = ()) -> SimpleNamespace:
    """A use-case-declaring repo table, in the shape register_declared_container_hosts reads."""
    return SimpleNamespace(
        name=name,
        docker_settings=SimpleNamespace(use_cases=tuple(fragments), composes=tuple(composes)),
    )


def _uc_frag(name: str = "integration", **kw: object) -> DockerUseCase:
    defaults: dict = {
        "composes": ("core",),
        "role": None,
        "placement": {},
        "provides": None,
        "priority": 0,
        "env": {},
        "pass_env": (),
    }
    defaults.update(kw)
    return DockerUseCase(name=name, **defaults)  # type: ignore[arg-type]


def _core_compose(services: tuple = ("api",), users: tuple = ()) -> DockerCompose:
    return DockerCompose(path=Path("docker/core.yml"), name="core", services=services, users=users)


def test_register_declared_use_case_repo_synthesizes_usecase_ids():
    """Spec §9: a use-case repo's placeholders carry <parent>.<usecase>.<service>,
    never <parent>.<repo>.<service> — that id is what routes DockerContainerHost's
    auto-up through deployment.deploy instead of the legacy compose_up."""
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        n = register_declared_container_hosts(lab, [repo])

    assert n == 1
    placeholder = lab.hosts.get("test3.integration.api")
    assert isinstance(placeholder, DockerContainerHost)
    assert placeholder.container_id == ""
    assert placeholder.project == "integration"
    assert "test3.a.api" not in lab.hosts  # never the legacy repo-scoped id
    parent = lab.hosts["test3"]
    assert placeholder.compose_project == use_case_project(parent.source_lab, "integration")


def test_register_declared_use_case_placeholder_carries_declared_user():
    """The use-case walk derives users from the SAME handle resolution that
    produced its services, so a fragment's placeholder carries the identity
    its deployed container will carry."""
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose(users=(("api", "postgres"),))])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        assert register_declared_container_hosts(lab, [repo]) == 1

    assert lab.hosts["test3.integration.api"].user == "postgres"


def test_register_declared_use_case_conflicting_users_refuse():
    """The users merge sits OUTSIDE this walk's `except UseCaseResolutionError`
    soft-skip on purpose: an unresolvable handle is normal here, a fragment
    whose composes name two different identities for one service is a settings
    mistake with no right answer. Moving the merge inside the try would hand
    out placeholders that disagree with the container `deploy` later registers.
    """
    lab = _make_lab()
    repo = _uc_repo(
        "a",
        _uc_frag(composes=("acore", "bcore")),
        composes=_two_composes(users_a=(("db", "postgres"),), users_b=(("db", "root"),)),
    )

    with (
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
        pytest.raises(ValueError, match="conflicting declared users for service 'db'"),
    ):
        register_declared_container_hosts(lab, [repo])


def test_register_declared_use_case_placeholder_without_users_is_unset():
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        assert register_declared_container_hosts(lab, [repo]) == 1

    assert lab.hosts["test3.integration.api"].user is None


def test_register_declared_use_case_fragment_with_no_declared_services_is_skipped():
    """A `[[docker.composes]]` entry may legitimately declare zero services
    (no `min_length` on that field) — a fragment resolving to none
    contributes no placeholder rather than an empty inner loop being
    mistaken for a bug."""
    lab = _make_lab()
    empty_compose = DockerCompose(path=Path("docker/core.yml"), name="core", services=())
    repo = _uc_repo("a", _uc_frag(), composes=[empty_compose])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        n = register_declared_container_hosts(lab, [repo])

    assert n == 0
    assert not any(hid.startswith("test3.integration.") for hid in lab.hosts)


def test_register_declared_use_case_repo_skips_legacy_composes_walk():
    """A repo declaring use_cases takes ONLY the use-case branch (spec §9);
    legacy composes-only repos are unaffected (see
    test_register_declared_creates_placeholders)."""
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        n = register_declared_container_hosts(lab, [repo])

    assert n == 1  # exactly the use-case placeholder, no legacy duplicate
    assert "test3.a.api" not in lab.hosts


def test_register_declared_use_case_fragment_whose_placement_raises_is_skipped():
    """Placeholders are best-effort (spec §9): a fragment whose placement
    raises contributes no placeholder, but a sibling fragment of the same
    repo still registers — only UseCaseResolutionError is swallowed."""
    lab = _make_lab()
    repo = _uc_repo(
        "a",
        _uc_frag(name="integration"),
        _uc_frag(name="ghost-uc", role="ghost"),  # no host carries role "ghost"
        composes=[_core_compose()],
    )

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        n = register_declared_container_hosts(lab, [repo])

    assert n == 1
    assert "test3.integration.api" in lab.hosts
    assert not any(hid.startswith("test3.ghost-uc.") for hid in lab.hosts)


def test_register_declared_use_case_skips_existing():
    """Second call is a no-op, matching the legacy walk's own guard — and
    it is THIS branch's guard doing it: `Lab.add_host` raises on a
    duplicate id, so deleting the `if placeholder.id in lab.hosts: continue`
    guard turns this red (verified by mutation)."""
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        n1 = register_declared_container_hosts(lab, [repo])
        n2 = register_declared_container_hosts(lab, [repo])

    assert (n1, n2) == (1, 0)
    assert [h for h in lab.hosts if h.startswith("test3.")] == ["test3.integration.api"]


def test_register_declared_use_case_non_resolution_error_propagates():
    """Only UseCaseResolutionError is swallowed (spec §9 best-effort): a
    broader except would eat a genuine bug silently, forever, on every
    otto invocation — this walk runs in cli/invoke.py's preamble."""
    lab = _make_lab()
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])

    with (
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
        patch("otto.docker.resolve.resolve_placement", side_effect=TypeError("boom")),
        pytest.raises(TypeError, match="boom"),
    ):
        register_declared_container_hosts(lab, [repo])


def test_register_declared_use_case_inherits_its_parents_lab_not_the_composite():
    """Same rule as the legacy walk's own test (see
    test_declared_container_inherits_its_parents_lab_not_the_composite): a
    use-case placeholder is attributed to its PARENT's lab, not the merge's
    name, and COPIES the parent's LabInfo rather than aliasing it."""
    repo = _uc_repo("a", _uc_frag(), composes=[_core_compose()])
    lab = _merged_lab_with_stamped_parent()
    parent = lab.hosts["test3"]

    with patch("otto.docker.resolve.scope_for_repo", return_value=None):
        assert register_declared_container_hosts(lab, [repo]) == 1

    (container,) = [h for h in lab.hosts.values() if isinstance(h, DockerContainerHost)]
    assert container.source_lab == "a"
    assert container.lab_info.name == "a"
    container.lab_info.metadata["k"] = 99
    assert parent.lab_info.metadata == {"k": 1}


def test_register_declared_use_case_respects_repo_project_scope(tmp_path):
    """The interaction production actually depends on and every other new
    test here patches away: a repo's `[project]` scope narrows `in_scope`
    inside `_place_fragment`, via the REAL `scope_for_repo` (not patched)."""
    extra = (
        "[project]\n"
        'lab_patterns = [".*"]\n'
        'host_patterns = ["test3"]\n'
        "\n"
        "[[docker.composes]]\n"
        'name = "core"\n'
        'path = "docker/compose.yml"\n'
        'services = ["api"]\n'
        "\n"
        "[[docker.use_cases]]\n"
        'name = "integration"\n'
        'composes = ["core"]\n'
    )
    sut = make_sut_repo(
        tmp_path / "a", name="a", extra=extra, files={"docker/compose.yml": "services: {}\n"}
    )
    repo = Repo(sut_dir=sut)

    lab = _make_lab()  # "test3", docker-capable
    other = _wire_parent_mock(_capable_host("test1", ne="test1"))
    lab.hosts[other.id] = other

    with patch("otto.config.get_repos", return_value=[repo]):
        n = register_declared_container_hosts(lab, [repo])

    assert n == 1
    assert "test3.integration.api" in lab.hosts
    assert not any(hid.startswith("test1.integration.") for hid in lab.hosts)


@pytest.mark.asyncio
async def test_compose_up_container_inherits_its_parents_lab_not_the_composite(tmp_path):
    """Same rule on the live registration path: parent's lab wins over the composite."""
    repo = _make_repo(tmp_path)
    lab = _merged_lab_with_stamped_parent()
    parent = lab.hosts["test3"]

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
    assert hosts["api"].lab_info.name == "a"
    # Same copy-not-alias rule on the live registration path (see the
    # placeholder test): the parent's ``metadata`` table must stay its own.
    hosts["api"].lab_info.metadata["k"] = 99
    assert parent.lab_info.metadata == {"k": 1}


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
    """Raises ValueError when no on= and no [[docker.use_cases]] is declared."""
    # Build a repo with a compose entry but NO use-case fragments.
    sut = make_sut_repo(
        tmp_path / "repo1",
        name="repo1",
        extra=(
            "[docker]\n\n"
            "[[docker.composes]]\n"
            'path = "docker/compose.yml"\n'
            'services = ["api"]\n'
            "# no [[docker.use_cases]] declared\n"
        ),
        files={"docker/compose.yml": "services: {}\n"},
    )
    repo = Repo(sut_dir=sut)
    lab = _make_lab()

    with pytest.raises(ValueError, match="No docker host"):
        _resolve_parent(repo, lab, on=None)


def test_resolve_parent_non_unixhost_raises(tmp_path):
    """Raises TypeError when the resolved host is not a UnixHost."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()

    # Install a non-UnixHost under the id "weird"
    weird = MagicMock()
    weird.id = "weird"
    lab.hosts["weird"] = weird

    with pytest.raises(TypeError, match="must be a UnixHost"):
        _resolve_parent(repo, lab, on="weird")


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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]
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
    parent = lab.hosts["test3"]

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
    parent = lab.hosts["test3"]

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -rf "):
            return _fail("read-only file system")
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    result = await compose_down(repo, lab)
    assert not result.is_ok
    assert "read-only file system" in result.value


@pytest.mark.asyncio
async def test_unregister_tolerates_a_concurrent_pop_of_a_snapshotted_id(caplog):
    """`await host.close()` yields, so a peer teardown can pop the next id first.

    The hostile condition is INJECTED (the first host's close pops the second
    out of the lab) rather than waited for: closing a `None` would turn a
    benign race into an AttributeError inside a best-effort sweep.

    The ``caplog`` assertion is load-bearing: without it, a mutation that
    deletes the ``if host is not None:`` guard still passes every assertion
    above it — the injected race already popped ``second`` out of
    ``lab.hosts``, so ``second.close.assert_not_awaited()`` holds regardless,
    and calling ``None.close()`` raises an ``AttributeError`` the surrounding
    ``except Exception`` below swallows into a warning rather than a crash.
    """
    lab = _make_lab()
    parent = lab.hosts["test3"]
    first = _container_host(parent, "integration", "api")
    second = _container_host(parent, "integration", "db")
    lab.hosts[first.id] = first
    lab.hosts[second.id] = second

    async def _close_and_race():
        lab.hosts.pop(second.id, None)  # a peer teardown got there first

    first.close = AsyncMock(side_effect=_close_and_race)

    with caplog.at_level(logging.WARNING, logger="otto.docker.compose"):
        removed = await unregister_container_hosts(lab, "test3.integration.")

    assert removed == [first.id, second.id]
    assert first.id not in lab.hosts
    assert second.id not in lab.hosts
    second.close.assert_not_awaited()
    assert "error closing container host" not in caplog.text
