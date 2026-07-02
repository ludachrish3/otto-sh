"""Unit tests for `otto.docker.compose` orchestration.

These mock the parent host's `oneshot`/`put` so no real docker is invoked.
"""

from __future__ import annotations

import getpass
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.configmodule.lab import Lab
from otto.configmodule.repo import (
    Repo,
)
from otto.docker.compose import (
    _resolve_parent,
    _safe_username,
    compose_down,
    compose_ps,
    compose_up,
    composed,
    get_container_host,
    get_user_compose_project,
    register_declared_container_hosts,
)
from otto.host.docker_host import DockerContainerHost
from otto.host.unix_host import UnixHost
from otto.result import CommandResult, Result
from otto.utils import Status

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
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "Dockerfile").write_text("FROM alpine\n")
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    services_toml = "[" + ", ".join(f'"{s}"' for s in services) + "]"
    (sut / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\n'
        f'version = "1.0.0"\n'
        f"\n"
        f"[docker]\n"
        f"\n"
        f"[[docker.images]]\n"
        f'name = "api"\n'
        f'dockerfile = "${{sut_dir}}/docker/Dockerfile"\n'
        f'context = "${{sut_dir}}/docker"\n'
        f"\n"
        f"[[docker.composes]]\n"
        f'path = "${{sut_dir}}/docker/compose.yml"\n'
        f'default_host = "{default_host}"\n'
        f"services = {services_toml}\n"
    )
    return Repo(sut_dir=sut)


def _capable_host(host_id: str = "pepper_seed", ne: str = "pepper") -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element=ne,
        creds={"vagrant": "vagrant"},
        board="seed",
        docker_capable=True,
    )


def _wire_parent_mock(host: UnixHost) -> UnixHost:
    """Replace the host's network methods with AsyncMocks so we never connect."""
    host.oneshot = AsyncMock(return_value=_ok())  # type: ignore[method-assign]
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
            creds={"u": "p"},
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

    async def oneshot(cmd, *_, **__):
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

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

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

    async def oneshot(cmd, *_, **__):
        call_log.append(cmd)
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("")
        if "compose" in cmd and " up -d" in cmd:
            return _fail("Error response from daemon: pull access denied for repo1-api")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

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

    async def oneshot(cmd, *_, **__):
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

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" in hosts, "service must register once the container becomes visible"
    assert hosts["api"].container_id == "abc123"
    assert resolve_calls >= 2, "resolve must poll past the first empty result"


@pytest.mark.asyncio
async def test_compose_up_resolve_gives_up_after_bounded_polls(tmp_path, monkeypatch):
    """If the container never becomes visible, resolve gives up after a bounded
    number of polls (no infinite wait) and the service is skipped."""
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0.0, raising=False)
    monkeypatch.setattr("otto.docker.compose._CONTAINER_ID_RESOLVE_ATTEMPTS", 3, raising=False)
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    resolve_calls = 0

    async def oneshot(cmd, *_, **__):
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

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    hosts = await compose_up(repo, lab)
    assert "api" not in hosts
    assert resolve_calls == 3, "must poll exactly _CONTAINER_ID_RESOLVE_ATTEMPTS times then stop"


@pytest.mark.asyncio
async def test_compose_down_removes_registered_hosts(tmp_path):
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]
    parent.oneshot.return_value = _ok()  # type: ignore[union-attr]

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

    async def oneshot(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")  # always "up"
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]
    parent.oneshot_orig = parent.oneshot

    async with composed(repo, lab):
        pass

    cmds = [c.args[0] for c in parent.oneshot.call_args_list]  # type: ignore[union-attr]
    assert not any(("compose" in c and " down" in c) for c in cmds), (
        "composed(own=False) must skip teardown when stack was already running"
    )


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
    assert any(("compose" in c and " down" in c) for c in cmds), (
        "composed(own=True) must tear down even if stack was already up"
    )


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


def _make_bare_repo(tmp: Path, *, name: str = "bare1") -> Repo:
    """Build a Repo with NO [[docker.composes]] entries."""
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / ".otto" / "settings.toml").write_text(f'name = "{name}"\nversion = "1.0.0"\n')
    return Repo(sut_dir=sut)


# ---------------------------------------------------------------------------
# compose_ps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_ps_parses_json_lines(tmp_path):
    """Valid JSON lines are parsed; blank lines and non-JSON lines are skipped."""
    host = _capable_host()
    _wire_parent_mock(host)
    host.oneshot.return_value = _ok('{"ID":"a"}\n\n{"ID":"b"}\nnot-json\n')  # type: ignore[union-attr]
    result = await compose_ps(host)
    assert result == [{"ID": "a"}, {"ID": "b"}]


@pytest.mark.asyncio
async def test_compose_ps_non_ok_returns_empty():
    """A non-ok parent response returns an empty list without raising."""
    host = _capable_host()
    _wire_parent_mock(host)
    host.oneshot.return_value = _fail("boom")  # type: ignore[union-attr]
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

    with patch("otto.configmodule.get_lab", return_value=fake_lab):
        result = get_container_host(container.id)
    assert result is container


def test_get_container_host_missing_raises(tmp_path):
    """Raises KeyError when the host_id is not in the lab."""
    fake_lab = Lab(name="test")
    with patch("otto.configmodule.get_lab", return_value=fake_lab), pytest.raises(KeyError):
        get_container_host("does_not_exist")


def test_get_container_host_wrong_type_raises(tmp_path):
    """Raises KeyError when the host exists but is not a DockerContainerHost."""
    parent = _wire_parent_mock(_capable_host())
    fake_lab = Lab(name="test")
    fake_lab.hosts[parent.id] = parent  # a UnixHost, not a DockerContainerHost
    with patch("otto.configmodule.get_lab", return_value=fake_lab), pytest.raises(KeyError):
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

    # build_images returns dict[str, tuple[Status, str]]; a non-ok status trips the branch
    fake_results = {"api": (Status.Failed, "push access denied")}

    with (
        patch("otto.docker.build.build_images", new=AsyncMock(return_value=fake_results)),
        pytest.raises(RuntimeError, match="build for image"),
    ):
        await compose_up(repo, lab, build=True)


# ---------------------------------------------------------------------------
# _resolve_parent — error branches
# ---------------------------------------------------------------------------


def test_resolve_parent_no_candidate_raises(tmp_path):
    """Raises ValueError when no on= and no default_host is set in composes."""
    # Build a compose list with NO default_host
    sut = tmp_path / "repo1"
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    (sut / ".otto" / "settings.toml").write_text(
        'name = "repo1"\nversion = "1.0.0"\n\n'
        "[docker]\n\n"
        "[[docker.composes]]\n"
        'path = "docker/compose.yml"\n'
        'services = ["api"]\n'
        "# no default_host\n"
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
    """Returns Status.Skipped immediately when the repo has no composes."""
    repo = _make_bare_repo(tmp_path)
    lab = _make_lab()
    result = await compose_down(repo, lab)
    assert result is Status.Skipped


@pytest.mark.asyncio
async def test_compose_down_failure_logs_error(tmp_path, caplog):
    """Logs an ERROR containing 'compose down failed' when the down command fails."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    async def oneshot(cmd, *_, **__):
        # stage_compose_files uses mkdir/rm calls that should succeed
        if "compose" in cmd and " down" in cmd:
            return _fail("down boom")
        return _ok()

    parent.oneshot.side_effect = oneshot  # type: ignore[union-attr]

    with caplog.at_level(logging.ERROR):
        result = await compose_down(repo, lab)

    assert any("compose down failed" in r.message for r in caplog.records)
    # The function returns the failed Status — verify it didn't raise and the
    # failure path is confirmed by the returned value
    assert result is Status.Failed


@pytest.mark.asyncio
async def test_compose_down_swallows_host_close_error(tmp_path):
    """Does NOT propagate when a registered container host's close() raises."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    # Wire down command to succeed so we reach the host-close loop
    parent.oneshot.return_value = _ok()  # type: ignore[union-attr]

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
    # down command succeeded, so the returned status is Success
    assert result is Status.Success


# ---------------------------------------------------------------------------
# _safe_username
# ---------------------------------------------------------------------------


def test_safe_username_keyerror_returns_anon():
    """Falls back to 'anon' when getpass.getuser() raises KeyError."""
    with patch("otto.docker.compose.getpass.getuser", side_effect=KeyError("no user")):
        assert _safe_username() == "anon"
