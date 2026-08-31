"""Deploy pipeline (spec §8, §11): one merged compose stack per resolved host.

The parent host's ``exec``/``put`` are mocked, so no docker is invoked and
nothing leaves the machine; everything above them — selection, placement,
facts, the adapter call, staging, the merged command, registration, rollback
— is the real code path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from otto.config.lab import Lab
from otto.config.repo import DockerCompose
from otto.docker import deployment as deploy_mod
from otto.docker import resolve as resolve_mod
from otto.docker.adapter import AdapterResult
from otto.docker.deployment import UseCaseStack, deploy, deployed, teardown
from otto.docker.resolve import UseCaseResolutionError
from otto.host.errors import HostCommandError
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandNotRunError, CommandResult, Result
from otto.utils import Status

from .test_resolve_select import _frag  # reuse the fragment table builder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPOSE_YAML = "services:\n  api:\n    image: alpine\n"


def _ok(out: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command="", retcode=0)


def _fail(out: str = "boom") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


def _compose_file(tmp_path: Path, handle: str, *, services=("api",), text=None) -> DockerCompose:
    path = tmp_path / f"{handle}.yml"
    path.write_text(text if text is not None else _COMPOSE_YAML)
    return DockerCompose(path=path, name=handle, services=tuple(services))


def _repo(name, *fragments, composes=(), images=()):
    """A repo table with docker settings, in the shape resolve/deploy read."""
    return SimpleNamespace(
        name=name,
        docker_settings=SimpleNamespace(
            use_cases=tuple(fragments),
            composes=tuple(composes),
            images=tuple(images),
        ),
    )


def _host(host_id: str, ip: str, *, roles=()) -> UnixHost:
    host = UnixHost(
        ip=ip,
        element=host_id,
        creds=[Cred(login="vagrant", password="vagrant")],
        docker_capable=True,
    )
    host.roles = list(roles)
    return host


def _wire(host: UnixHost, *, already_up: bool = False, cid: str = "cid1") -> UnixHost:
    """Mock the host's transport, recording commands and staged file content.

    ``host.commands`` is every ``exec`` string in order; ``host.staged`` is a
    list of ``(basename, remote dir, content)`` triples from ``put``.
    """
    commands: list[str] = []
    staged: list[tuple[str, str, str]] = []

    async def _exec(cmd, *_a, **_kw):
        commands.append(cmd)
        if "com.docker.compose.service=" in cmd:
            return _ok(cid)
        if "docker ps -q --filter label=com.docker.compose.project=" in cmd:
            return _ok("running-cid" if already_up else "")
        return _ok()

    async def _put(paths, dest):
        staged.extend((Path(p).name, str(dest), Path(p).read_text()) for p in paths)
        return Result(Status.Success, value={})

    host.exec = AsyncMock(side_effect=_exec)  # type: ignore[method-assign]
    host.put = AsyncMock(side_effect=_put)  # type: ignore[method-assign]
    host.commands = commands  # type: ignore[attr-defined]
    host.staged = staged  # type: ignore[attr-defined]
    return host


def _lab(*hosts: UnixHost, name: str = "unix") -> Lab:
    lab = Lab(name=name)
    for host in hosts:
        lab.add_host(host)
    return lab


def _up_command(host: UnixHost) -> str:
    """The single ``docker compose ... up -d`` command this host was given."""
    ups = [c for c in host.commands if " up -d" in c]  # type: ignore[attr-defined]
    assert len(ups) == 1, f"expected exactly one up command, got {ups}"
    return ups[0]


def _staged_env_text(host: UnixHost) -> str:
    (text,) = [c for name, _dir, c in host.staged if name == "otto.env"]  # type: ignore[attr-defined]
    return text


@contextmanager
def _install(lab, repos, ordered=None):
    """Patch deploy.py's three config seams for the duration of the block."""
    with (
        patch.object(deploy_mod, "get_lab", return_value=lab),
        patch.object(deploy_mod, "get_repos", return_value=list(repos)),
        patch.object(deploy_mod, "get_ordered_repos", return_value=list(ordered or repos)),
    ):
        yield


@pytest.fixture(autouse=True)
def _admit_all_scopes():
    """No `[project]` scope narrows anything — the idiom from test_resolve_place.py."""
    with patch.object(resolve_mod, "scope_for_repo", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_container_id_backoff():
    """Zero the resolve backoff so the not-visible path does not sleep 1.5s."""
    with patch("otto.docker.compose._CONTAINER_ID_RESOLVE_BACKOFF_S", 0):
        yield


@pytest.fixture
def single(tmp_path):
    """One repo, one roleless fragment, one docker-capable host, wired up."""
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(env={"EDGE_ADDR": "${otto:parent.addr}"}), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    return SimpleNamespace(repo=repo, host=host, lab=_lab(host), compose=compose)


# ---------------------------------------------------------------------------
# The merged command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_single_repo_merged_command(single, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    with _install(single.lab, [single.repo]):
        stack = await deploy("integration", on="test3")

    cmd = _up_command(single.host)
    assert "docker compose -p unix-integration-u " in cmd
    assert not cmd.split("docker compose -p ")[1].startswith("otto-")
    assert "--env-file " in cmd
    assert cmd.endswith("up -d --remove-orphans")
    assert "EDGE_ADDR=10.10.200.13" in cmd
    assert cmd.index("EDGE_ADDR=") < cmd.index("docker compose")
    assert list(stack.hosts) == ["api"]
    assert stack.projects == {"test3": "unix-integration-u"}
    assert list(stack.by_host) == ["test3"]
    assert stack.env["EDGE_ADDR"] == "10.10.200.13"
    assert stack.use_case == "integration"
    assert isinstance(stack, UseCaseStack)


@pytest.mark.asyncio
async def test_container_hosts_are_registered_under_the_use_case(single, monkeypatch):
    """Spec §9: host id is ``<parent>.<usecase>.<service>``, not the repo name."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    with _install(single.lab, [single.repo]):
        stack = await deploy("integration", on="test3")

    assert stack.hosts["api"].id == "test3.integration.api"
    assert "test3.integration.api" in single.lab.hosts


@pytest.mark.asyncio
async def test_project_name_overrides_the_per_host_derivation(single, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    with _install(single.lab, [single.repo]):
        stack = await deploy("integration", on="test3", project_name="pinned")

    assert stack.projects == {"test3": "pinned"}
    assert "docker compose -p pinned " in _up_command(single.host)


@pytest.mark.asyncio
async def test_f_order_follows_dependency_order(tmp_path):
    """Dependents later, so their keys win the YAML merge (spec §8 step 4)."""
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    # Declaration order is [b, a]; dependency order is [a, b]. Only the second
    # may decide the -f order, so a wrong sort cannot pass by accident.
    with _install(lab, [b, a], ordered=[a, b]):
        await deploy("integration", on="test3")

    cmd = _up_command(host)
    assert cmd.index("acore.yml") < cmd.index("bcore.yml")


@pytest.mark.asyncio
async def test_every_staged_compose_file_gets_its_own_f_flag(tmp_path):
    """The -f list IS the merge; a file staged but not flagged never merges."""
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [a, b]):
        await deploy("integration", on="test3")

    cmd = _up_command(host)
    staged = {name: remote for name, remote, _c in host.staged}  # type: ignore[attr-defined]
    assert cmd.count(" -f ") == 2
    for name in ("acore.yml", "bcore.yml"):
        assert f"-f {staged[name]}/{name} " in cmd
    assert f"--env-file {staged['otto.env']}/otto.env " in cmd


# ---------------------------------------------------------------------------
# services narrowing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_services_narrowing_appends_only_the_named_services(tmp_path):
    compose = _compose_file(tmp_path, "core", services=("api", "db"))
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [repo]):
        stack = await deploy("integration", on="test3", services=["api"])

    assert _up_command(host).endswith("up -d --remove-orphans api")
    assert list(stack.hosts) == ["api"]


@pytest.mark.asyncio
async def test_unknown_service_is_refused(tmp_path):
    compose = _compose_file(tmp_path, "core", services=("api",))
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        pytest.raises(UseCaseResolutionError, match="no participating fragment declares"),
    ):
        await deploy("integration", on="test3", services=["nope"])

    assert host.exec.await_count == 0


@pytest.mark.asyncio
async def test_a_host_left_with_no_named_service_is_skipped(tmp_path):
    """Narrowing to one host's service must not deploy an empty stack elsewhere."""
    a = _repo(
        "a",
        _frag(role="edge", composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore", services=("api",))],
    )
    b = _repo(
        "b",
        _frag(role="db", composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("db",))],
    )
    edge = _wire(_host("test3", "10.10.200.13", roles=["edge"]))
    dbh = _wire(_host("test1", "10.10.200.11", roles=["db"]))
    lab = _lab(edge, dbh)
    with _install(lab, [a, b]):
        stack = await deploy("integration", services=["api"])

    assert list(stack.projects) == ["test3"]
    assert not [c for c in dbh.commands if " up -d" in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_skipped_host_log_names_the_use_case(tmp_path, caplog):
    """The skip log must name WHICH use-case left the host with nothing to
    do — `_acting_hosts` is shared by `deploy`, `teardown` and `deployed`,
    so a bare host id with no use-case name is ambiguous the moment more
    than one use-case is ever in play."""
    a = _repo(
        "a",
        _frag(role="edge", composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore", services=("api",))],
    )
    b = _repo(
        "b",
        _frag(role="db", composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("db",))],
    )
    edge = _wire(_host("test3", "10.10.200.13", roles=["edge"]))
    dbh = _wire(_host("test1", "10.10.200.11", roles=["db"]))
    lab = _lab(edge, dbh)
    with (
        caplog.at_level(logging.INFO, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        await deploy("integration", services=["api"])

    assert "declares none of the requested services" in caplog.text
    assert "use-case 'integration'" in caplog.text


# ---------------------------------------------------------------------------
# env layering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_env_beats_static_and_caller_beats_adapter(tmp_path):
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(env={"K": "static", "S": "static"}), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    def _adapter(_facts):
        return AdapterResult(env={"K": "adapter", "A": "adapter"})

    with _install(lab, [repo]), patch.object(deploy_mod, "adapter_for", return_value=_adapter):
        stack = await deploy("integration", on="test3", env={"K": "caller"})

    text = _staged_env_text(host)
    assert "K=caller" in text
    assert "A=adapter" in text
    assert "S=static" in text
    assert stack.env["K"] == "caller"


@pytest.mark.asyncio
async def test_env_files_layer_between_adapter_and_caller_env(tmp_path):
    env_file = tmp_path / "extra.env"
    env_file.write_text("# a comment\n\nK=from_file\nF=from_file\n")
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(env={"K": "static"}), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    def _adapter(_facts):
        return AdapterResult(env={"K": "adapter"})

    with _install(lab, [repo]), patch.object(deploy_mod, "adapter_for", return_value=_adapter):
        stack = await deploy("integration", on="test3", env_files=[env_file])

    assert stack.env["K"] == "from_file"
    assert stack.env["F"] == "from_file"


@pytest.mark.asyncio
async def test_pass_env_miss_is_warned_not_fatal(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("OTTO_T11_ABSENT", raising=False)
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(pass_env=("OTTO_T11_ABSENT",)), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        caplog.at_level(logging.WARNING, logger="otto.docker.deploy"),
        _install(lab, [repo]),
    ):
        await deploy("integration", on="test3")

    assert "OTTO_T11_ABSENT" in caplog.text


@pytest.mark.asyncio
async def test_newline_in_an_env_value_is_refused(tmp_path):
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        pytest.raises(UseCaseResolutionError, match="cannot be written to an env file"),
    ):
        await deploy("integration", on="test3", env={"K": "line1\nline2"})


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_facts_carry_only_its_own_repos_handles(tmp_path):
    """Spec §7: a repo's adapter sees ITS files, never a peer repo's."""
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    seen: dict[str, list[str]] = {}

    def _adapter_for(repo_name, _use_case):
        def _adapter(facts):
            seen[repo_name] = sorted(facts["files"])
            return AdapterResult()

        return _adapter

    with _install(lab, [a, b]), patch.object(deploy_mod, "adapter_for", side_effect=_adapter_for):
        await deploy("integration", on="test3")

    assert seen == {"a": ["acore"], "b": ["bcore"]}


@pytest.mark.asyncio
async def test_adapter_file_override_is_what_gets_staged(tmp_path):
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    rendered = "services:\n  api:\n    image: rendered\n"

    def _adapter(_facts):
        return AdapterResult(files={"core": rendered})

    with _install(lab, [repo]), patch.object(deploy_mod, "adapter_for", return_value=_adapter):
        await deploy("integration", on="test3")

    assert ("core.yml", rendered) in [
        (name, text)
        for name, _d, text in host.staged  # type: ignore[attr-defined]
    ]


@pytest.mark.asyncio
async def test_adapter_extra_files_reach_staging(tmp_path):
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    def _adapter(_facts):
        return AdapterResult(extra_files={"gen.env": "G=1\n"})

    with _install(lab, [repo]), patch.object(deploy_mod, "adapter_for", return_value=_adapter):
        await deploy("integration", on="test3")

    assert "gen.env" in [name for name, _d, _c in host.staged]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# `on` validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_that_names_no_host_is_refused(single):
    """T7 review I3: `on` is trusted for placement, so it must be checked here."""
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(UseCaseResolutionError, match="matches no host in lab"),
    ):
        await deploy("integration", on="ghost")

    assert single.host.exec.await_count == 0


@pytest.mark.asyncio
async def test_teardown_on_that_names_no_host_is_refused(single):
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(UseCaseResolutionError, match="matches no host in lab"),
    ):
        await teardown("integration", on="ghost")


@pytest.mark.asyncio
async def test_on_is_canonicalized_through_the_labs_handle_resolver(tmp_path):
    """`on` accepts every typed handle the rest of otto does, canonical id or not.

    Asserted through the resolver rather than by passing an id that happens
    to be canonical already: `on="test3"` would pass a straight
    `lab.hosts[on]` lookup too, so it proves nothing about handles.
    """
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    seen: list[str] = []
    real_resolver = lab.resolve_handle

    def _spy(handle):
        seen.append(handle)
        return real_resolver("test3") if handle == "edge-box" else real_resolver(handle)

    lab.resolve_handle = _spy  # type: ignore[method-assign]
    with _install(lab, [repo]):
        stack = await deploy("integration", on="edge-box")

    assert seen == ["edge-box"]
    assert list(stack.projects) == ["test3"]


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_on_registration_failure(single):
    """No container id resolves anywhere -> registration raises -> we tear down."""
    _wire(single.host, cid="")
    with _install(single.lab, [single.repo]), pytest.raises(HostCommandError):
        await deploy("integration", on="test3")

    downs = [c for c in single.host.commands if " down " in c]  # type: ignore[attr-defined]
    assert len(downs) == 1
    assert "--remove-orphans" in downs[0]


@pytest.mark.asyncio
async def test_shared_stack_not_rolled_back(single):
    """The stack was already up: it is someone else's, so failure must not down it."""
    _wire(single.host, already_up=True, cid="")
    with _install(single.lab, [single.repo]), pytest.raises(HostCommandError):
        await deploy("integration", on="test3")

    assert not [c for c in single.host.commands if " down " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rollback_covers_the_first_host_when_the_second_fails(tmp_path):
    """Multi-host generalization: what THIS call brought up, everywhere."""
    a = _repo(
        "a",
        _frag(role="edge", composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore")],
    )
    b = _repo(
        "b",
        _frag(role="db", composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("db",))],
    )
    first = _wire(_host("test3", "10.10.200.13", roles=["edge"]))
    second = _wire(_host("test1", "10.10.200.11", roles=["db"]), cid="")
    lab = _lab(first, second)
    with _install(lab, [a, b]), pytest.raises(HostCommandError):
        await deploy("integration")

    assert [c for c in first.commands if " down " in c]  # type: ignore[attr-defined]
    assert [c for c in second.commands if " down " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_failed_up_command_raises_and_rolls_back(single):
    async def _exec(cmd, *_a, **_kw):
        single.host.commands.append(cmd)
        if " up -d" in cmd:
            return _fail("port already allocated")
        if "com.docker.compose.service=" in cmd:
            return _ok("cid1")
        if "docker ps -q --filter label=com.docker.compose.project=" in cmd:
            return _ok("")
        return _ok()

    single.host.exec = AsyncMock(side_effect=_exec)
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(HostCommandError, match="port already allocated"),
    ):
        await deploy("integration", on="test3")

    assert [c for c in single.host.commands if " down " in c]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_runs_once_per_repo_in_dependency_order(tmp_path):
    a = _repo(
        "a",
        _frag(composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore")],
        images=("img-a",),
    )
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
        images=("img-b",),
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    built: list[str] = []

    async def _build(repo, _parent, **_kw):
        built.append(repo.name)
        return {}

    with (
        _install(lab, [b, a], ordered=[a, b]),
        patch.object(deploy_mod, "build_images", AsyncMock(side_effect=_build)),
    ):
        await deploy("integration", on="test3")

    assert built == ["a", "b"]


@pytest.mark.asyncio
async def test_build_false_skips_the_build(single):
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "build_images", AsyncMock(return_value={})) as builder,
    ):
        await deploy("integration", on="test3", build=False)

    builder.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_build_stops_before_up(tmp_path):
    repo = _repo(
        "a",
        _frag(),
        composes=[_compose_file(tmp_path, "core")],
        images=("img",),
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        patch.object(
            deploy_mod,
            "build_images",
            AsyncMock(return_value={"img": _fail("no such base image")}),
        ),
        pytest.raises(HostCommandError, match="no such base image"),
    ):
        await deploy("integration", on="test3")

    assert not [c for c in host.commands if " up -d" in c]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_declines_naming_use_case(single):
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await deploy("integration", on="test3")

    message = str(excinfo.value)
    assert "integration" in message
    assert "test3" in message
    assert "core" in message
    assert single.host.exec.await_count == 0


@pytest.mark.asyncio
async def test_dry_run_still_refuses_an_unresolvable_use_case(single):
    """The pure refusals fire identically either way — they settle from config."""
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(UseCaseResolutionError),
    ):
        await deploy("nope", on="test3")


@pytest.mark.asyncio
async def test_teardown_dry_run_declines(single):
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError, match="integration"),
    ):
        await teardown("integration", on="test3")

    assert single.host.exec.await_count == 0


@pytest.mark.asyncio
async def test_deployed_dry_run_declines(single):
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError, match="integration"),
    ):
        async with deployed("integration", on="test3"):
            pass


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_teardown_downs_by_project_and_unregisters(single, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    with _install(single.lab, [single.repo]):
        await deploy("integration", on="test3")
        assert "test3.integration.api" in single.lab.hosts
        single.host.commands.clear()
        await teardown("integration", on="test3")

    (down,) = [c for c in single.host.commands if " down " in c]
    assert down == "docker compose -p unix-integration-u down --remove-orphans --timeout 1"
    assert " -f " not in down
    assert "test3.integration.api" not in single.lab.hosts


@pytest.mark.asyncio
async def test_partial_teardown_stops_and_removes_only_named_services(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    compose = _compose_file(tmp_path, "core", services=("api", "db"))
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [repo]):
        await deploy("integration", on="test3")
        host.commands.clear()
        await teardown("integration", on="test3", services=["api"], stop_timeout=5)

    assert "docker compose -p unix-integration-u stop -t 5 api" in host.commands
    assert "docker compose -p unix-integration-u rm -f api" in host.commands
    assert not [c for c in host.commands if " down " in c]
    assert "test3.integration.api" not in lab.hosts
    assert "test3.integration.db" in lab.hosts


@pytest.mark.asyncio
async def test_teardown_unknown_service_is_refused(single):
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(UseCaseResolutionError, match="no participating fragment declares"),
    ):
        await teardown("integration", on="test3", services=["nope"])


# --- deployed() -----------------------------------------------------------


@pytest.mark.asyncio
async def test_deployed_own_false_shares(single):
    """Already up on entry -> someone else's stack -> exiting must not down it."""
    _wire(single.host, already_up=True)
    with _install(single.lab, [single.repo]):
        async with deployed("integration", on="test3") as stack:
            assert list(stack.hosts) == ["api"]

    assert not [c for c in single.host.commands if " down " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_deployed_own_true_tears_down_through_compensate(single):
    _wire(single.host, already_up=True)
    seen: list[str] = []

    async def _compensate(coro, *, what="", **_kw):
        seen.append(what)
        return await coro

    with _install(single.lab, [single.repo]), patch("otto.lifecycle.compensate", _compensate):
        async with deployed("integration", on="test3", own=True):
            pass

    assert len(seen) == 1
    assert "integration" in seen[0]
    assert [c for c in single.host.commands if " down " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_deployed_not_up_beforehand_tears_down_on_exit(single):
    with _install(single.lab, [single.repo]):
        async with deployed("integration", on="test3"):
            pass

    assert [c for c in single.host.commands if " down " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_deployed_refuses_an_unknown_probe_answer(single):
    """Same contract as composed(): unknown must not be read as "nobody had it"."""

    async def _exec(cmd, *_a, **_kw):
        single.host.commands.append(cmd)
        if "docker ps -q --filter label=com.docker.compose.project=" in cmd:
            return _fail("permission denied")
        return _ok()

    single.host.exec = AsyncMock(side_effect=_exec)
    with _install(single.lab, [single.repo]), pytest.raises(HostCommandError, match="own=True"):
        async with deployed("integration", on="test3"):
            pass


# ---------------------------------------------------------------------------
# parent validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_docker_capable_parent_is_refused(single):
    single.host.docker_capable = False
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(
            UseCaseResolutionError, match="so a use-case stack cannot be deployed onto it"
        ),
    ):
        await deploy("integration", on="test3")


@pytest.mark.asyncio
async def test_a_fully_narrowed_away_host_is_never_docker_capable_checked(single):
    """_parent_for (see its docstring) is only consulted for a host
    _acting_hosts decided this call actually touches. `on=` collapses every
    fragment onto one host, so narrowing `services=` down to nothing for it
    (an explicit empty list, as opposed to naming an unknown service — which
    _validated_services refuses earlier) makes that host act on ZERO
    services. `deploy` must not then refuse it merely for not being
    docker-capable — it was never going to be touched either way."""
    single.host.docker_capable = False
    with _install(single.lab, [single.repo]):
        stack = await deploy("integration", on="test3", services=[])

    assert stack.hosts == {}
    assert stack.projects == {}
    assert single.host.exec.await_count == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_an_unknown_compose_handle_is_refused(tmp_path):
    repo = _repo("a", _frag(composes=("ghost",)), composes=[_compose_file(tmp_path, "core")])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        pytest.raises(UseCaseResolutionError, match="which the repo does not define"),
    ):
        await deploy("integration", on="test3")


@pytest.mark.asyncio
async def test_a_service_declared_twice_is_warned_about_not_refused(tmp_path, caplog):
    """§4's competition replaces a provider; the YAML merge must not do it silently."""
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo("b", _frag(composes=("bcore",)), composes=[_compose_file(tmp_path, "bcore")])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        caplog.at_level(logging.WARNING, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        stack = await deploy("integration", on="test3")

    assert "is declared by both" in caplog.text
    assert "a[acore]" in caplog.text
    assert "b[bcore]" in caplog.text
    assert list(stack.hosts) == ["api"]  # deduped: one service, one container host


@pytest.mark.asyncio
async def test_colliding_adapter_extra_files_are_warned_about(tmp_path, caplog):
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    def _adapter_for(repo_name, _use_case):
        return lambda _facts: AdapterResult(extra_files={"gen.env": f"OWNER={repo_name}\n"})

    with (
        caplog.at_level(logging.WARNING, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
        patch.object(deploy_mod, "adapter_for", side_effect=_adapter_for),
    ):
        await deploy("integration", on="test3")

    assert "two adapters generated different content" in caplog.text
    assert "gen.env" in caplog.text


@pytest.mark.asyncio
async def test_displacements_are_logged_without_claiming_the_loser_ranked_lower(tmp_path, caplog):
    """--provide narrows to a repo first, so the WINNER can rank lower (T6 M-3).

    Two fragments of one repo at priorities 1 and 10, plus a peer repo at 5,
    with --provide naming the peer: the winner is repo b at priority 5 and
    BOTH of a's fragments are displaced, one of them from priority 10. A log
    line saying "lower priority lost" would be false here, so the assertion
    is that both numbers are rendered and neither is editorialized.
    """
    compose = _compose_file(tmp_path, "core")
    a = _repo(
        "a",
        _frag(provides="edge", priority=1),
        _frag(provides="edge", priority=10),
        composes=[compose],
    )
    b = _repo("b", _frag(provides="edge", priority=5), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        caplog.at_level(logging.INFO, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        stack = await deploy("integration", on="test3", provide={"edge": "b"})

    assert [sf.repo.name for sf in stack.selection.fragments] == ["b"]
    assert "goes to b (priority 5)" in caplog.text
    assert "a (priority 10) stands down" in caplog.text
    assert "a (priority 1) stands down" in caplog.text
    assert "lower" not in caplog.text


@pytest.mark.asyncio
async def test_full_teardown_never_reads_a_compose_file(tmp_path, monkeypatch):
    """Teardown by label survives a compose file that has since been deleted."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [repo]):
        await deploy("integration", on="test3")
        compose.path.unlink()
        host.commands.clear()
        await teardown("integration", on="test3")

    assert [c for c in host.commands if " down " in c]
    assert "test3.integration.api" not in lab.hosts


# --- Fix round 1: the arms the coverage report showed as dead ---------------


@pytest.mark.asyncio
async def test_a_malformed_env_file_line_is_refused(tmp_path):
    """A line with no `=` means "inherit from the shell" to docker; pass_env owns that."""
    env_file = tmp_path / "bad.env"
    env_file.write_text("# fine\nJUST_A_NAME\n")
    repo = _repo("a", _frag(), composes=[_compose_file(tmp_path, "core")])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        pytest.raises(UseCaseResolutionError, match="is not a K=V assignment"),
    ):
        await deploy("integration", on="test3", env_files=[env_file])


@pytest.mark.asyncio
async def test_a_rollback_that_itself_fails_is_reported_and_never_masks(single, caplog):
    """The propagating error is the one the user needs; the residue still gets named."""
    _wire(single.host, cid="")  # registration will fail -> rollback runs
    with (
        caplog.at_level(logging.ERROR, logger="otto.docker.deployment"),
        _install(single.lab, [single.repo]),
        patch.object(
            deploy_mod,
            "compose_down_project",
            AsyncMock(side_effect=OSError("parent went away")),
        ),
        pytest.raises(HostCommandError) as excinfo,
    ):
        await deploy("integration", on="test3")

    # The ORIGINAL error propagates, not the rollback's.
    assert "resolved to a running container" in str(excinfo.value)
    assert "could not be rolled back" in caplog.text
    assert "parent went away" in caplog.text


@pytest.mark.asyncio
async def test_partial_teardown_skips_a_host_with_none_of_the_named_services(tmp_path):
    a = _repo(
        "a",
        _frag(role="edge", composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore", services=("api",))],
    )
    b = _repo(
        "b",
        _frag(role="db", composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("db",))],
    )
    edge = _wire(_host("test3", "10.10.200.13", roles=["edge"]))
    dbh = _wire(_host("test1", "10.10.200.11", roles=["db"]))
    lab = _lab(edge, dbh)
    with _install(lab, [a, b]):
        await teardown("integration", services=["api"])

    assert [c for c in edge.commands if " stop -t " in c]  # type: ignore[attr-defined]
    assert dbh.commands == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_partial_teardown_reports_a_failed_stop_and_still_removes(tmp_path, caplog):
    compose = _compose_file(tmp_path, "core", services=("api",))
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)

    async def _exec(cmd, *_a, **_kw):
        host.commands.append(cmd)
        if " stop -t " in cmd:
            return _fail("no such service")
        return _ok()

    host.exec = AsyncMock(side_effect=_exec)
    with (
        caplog.at_level(logging.ERROR, logger="otto.docker.deployment"),
        _install(lab, [repo]),
    ):
        await teardown("integration", on="test3", services=["api"])

    assert "no such service" in caplog.text
    assert "`stop` failed" in caplog.text
    # The sweep continues: rm -f still ran rather than aborting on the stop.
    assert [c for c in host.commands if " rm -f " in c]


@pytest.mark.asyncio
async def test_partial_teardown_unregisters_a_mixed_case_service(tmp_path, monkeypatch):
    """I2: DockerContainerHost.id is lower-cased; the narrowed id set must be too.

    Without the fix the container is stopped and `rm -f`'d and then STAYS in
    ``lab.hosts`` — otto advertising a host for a container it just removed.
    """
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    compose = _compose_file(
        tmp_path, "core", services=("MyApi",), text="services:\n  MyApi:\n    image: x\n"
    )
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [repo]):
        stack = await deploy("integration", on="test3")
        assert stack.hosts["MyApi"].id == "test3.integration.myapi"
        assert "test3.integration.myapi" in lab.hosts
        await teardown("integration", on="test3", services=["MyApi"])

    assert "test3.integration.myapi" not in lab.hosts


@pytest.mark.asyncio
async def test_one_repos_two_fragments_sharing_a_handle_stage_it_once(tmp_path):
    """The `_units` dedupe: one -f per handle, however many fragments name it."""
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(), _frag(role=None), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with _install(lab, [repo]):
        await deploy("integration", on="test3")

    assert _up_command(host).count(" -f ") == 1


@pytest.mark.asyncio
async def test_a_fragment_naming_one_handle_twice_is_not_a_collision(tmp_path, caplog):
    """Same fragment, same handle: nothing collides, so nothing may be warned about."""
    compose = _compose_file(tmp_path, "core")
    repo = _repo("a", _frag(composes=("core", "core")), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        caplog.at_level(logging.WARNING, logger="otto.docker.deployment"),
        _install(lab, [repo]),
    ):
        stack = await deploy("integration", on="test3")

    assert "is declared by both" not in caplog.text
    assert list(stack.hosts) == ["api"]


@pytest.mark.asyncio
async def test_a_successful_build_does_not_stop_the_deployment(tmp_path):
    """The ok-result loop-back in _build_for: a green build proceeds to `up`."""
    repo = _repo("a", _frag(), composes=[_compose_file(tmp_path, "core")], images=("img",))
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [repo]),
        patch.object(
            deploy_mod, "build_images", AsyncMock(return_value={"img": _ok("repo/img:abc")})
        ),
    ):
        stack = await deploy("integration", on="test3")

    assert list(stack.hosts) == ["api"]
    assert [c for c in host.commands if " up -d" in c]  # type: ignore[attr-defined]


# --- Fix round 1: ruled-in minors ------------------------------------------


@pytest.mark.asyncio
async def test_builds_skip_a_repo_whose_services_were_all_narrowed_away(tmp_path):
    """M5: `services=["api"]` is not a request to build the excluded repos' images."""
    a = _repo(
        "a",
        _frag(composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore", services=("api",))],
        images=("img-a",),
    )
    b = _repo(
        "b",
        _frag(composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("web",))],
        images=("img-b",),
    )
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    built: list[str] = []

    async def _build(repo, _parent, **_kw):
        built.append(repo.name)
        return {}

    with (
        _install(lab, [a, b]),
        patch.object(deploy_mod, "build_images", AsyncMock(side_effect=_build)),
    ):
        await deploy("integration", on="test3", services=["api"])

    assert built == ["a"]
    # b's compose file still joins the -f merge; only its IMAGE is skipped.
    assert _up_command(host).count(" -f ") == 2


@pytest.mark.asyncio
async def test_a_collision_warns_once_whether_or_not_services_is_passed(tmp_path, caplog):
    """M6: log volume must not vary with an unrelated flag."""
    a = _repo("a", _frag(composes=("acore",)), composes=[_compose_file(tmp_path, "acore")])
    b = _repo("b", _frag(composes=("bcore",)), composes=[_compose_file(tmp_path, "bcore")])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        caplog.at_level(logging.WARNING, logger="otto.docker.deployment"),
        _install(lab, [a, b]),
    ):
        await deploy("integration", on="test3", services=["api"])

    assert caplog.text.count("is declared by both") == 1


@pytest.mark.asyncio
async def test_an_env_key_with_a_leading_dash_is_refused(single):
    """M10: the same mapping is spliced into `env K=V ...`, where `-i` wipes it."""
    with (
        _install(single.lab, [single.repo]),
        pytest.raises(UseCaseResolutionError, match="WIPES the environment"),
    ):
        await deploy("integration", on="test3", env={"-i": "x"})


@pytest.mark.asyncio
async def test_deployed_does_not_probe_a_host_it_will_skip(tmp_path):
    """M12: an unrelated stack on a skipped host must not suppress our teardown."""
    a = _repo(
        "a",
        _frag(role="edge", composes=("acore",)),
        composes=[_compose_file(tmp_path, "acore", services=("api",))],
    )
    b = _repo(
        "b",
        _frag(role="db", composes=("bcore",)),
        composes=[_compose_file(tmp_path, "bcore", services=("db",))],
    )
    edge = _wire(_host("test3", "10.10.200.13", roles=["edge"]))
    # An unrelated stack IS up on the host this call will skip.
    dbh = _wire(_host("test1", "10.10.200.11", roles=["db"]), already_up=True)
    lab = _lab(edge, dbh)
    with _install(lab, [a, b]):
        async with deployed("integration", services=["api"]):
            pass

    assert dbh.commands == []  # type: ignore[attr-defined]
    # ...and the teardown we owed for the host we DID deploy to still happened.
    # It is the PARTIAL shape, because `services=` rode along to teardown too.
    assert [c for c in edge.commands if " stop -t " in c]  # type: ignore[attr-defined]
    assert [c for c in edge.commands if " rm -f " in c]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dry_run_plan_shows_displacements_as_they_are(tmp_path):
    """M2 + T6 M-3: the winner may rank LOWER, so the plan may not editorialize."""
    compose = _compose_file(tmp_path, "core")
    a = _repo("a", _frag(provides="edge", priority=10), composes=[compose])
    b = _repo("b", _frag(provides="edge", priority=5), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    lab = _lab(host)
    with (
        _install(lab, [a, b]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await deploy("integration", on="test3", provide={"edge": "b"})

    message = str(excinfo.value)
    assert "Displaced: edge -> b (priority 5), a (priority 10) stands down" in message
    assert "lower" not in message


@pytest.mark.asyncio
async def test_dry_run_plan_shows_the_exact_compose_command(single, monkeypatch):
    """§12 promises "the exact compose command" — so print it, gap clause deleted.

    The decline moved BELOW the adapter call and the file render (§7 makes
    both plain-data: an adapter's only sanctioned effect is writing its own
    ``scratch_dir``) and stays ABOVE build/stage/up, the first device touches.
    That is the whole reason the command can be exact: the ``-f`` set and the
    env mapping are the ones this deployment would really have used.
    """
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "u")
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await deploy("integration", on="test3")

    message = str(excinfo.value)
    assert "the exact compose command cannot be shown" not in message, (
        "the §12 gap clause survived the move: " + message
    )
    assert "EDGE_ADDR=10.10.200.13 docker compose -p unix-integration-u " in message
    assert "-f /tmp/otto-docker/unix-integration-u/compose/0/core.yml" in message
    assert "--env-file /tmp/otto-docker/unix-integration-u/compose/otto.env" in message
    assert message.rstrip().endswith("up -d --remove-orphans")
    assert "test3" in message


@pytest.mark.asyncio
async def test_dry_run_declines_below_the_adapter_so_its_content_is_in_the_command(single):
    """The adapter's env reaches the previewed command — proof it ran first.

    Without the move this assertion is unsatisfiable: the old arm fired
    before ``_plan_host``, so no adapter had contributed anything to show.
    """

    def _adapter(_facts):
        return AdapterResult(env={"FROM_ADAPTER": "yes"})

    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "adapter_for", return_value=_adapter),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await deploy("integration", on="test3")

    assert "FROM_ADAPTER=yes" in str(excinfo.value)


@pytest.mark.asyncio
async def test_dry_run_command_is_narrowed_by_services(tmp_path):
    """A ``services=`` narrowing shows in the previewed command, as it would run."""
    compose = _compose_file(
        tmp_path,
        "core",
        services=("api", "db"),
        text="services:\n  api:\n    image: alpine\n  db:\n    image: alpine\n",
    )
    repo = _repo("a", _frag(), composes=[compose])
    host = _wire(_host("test3", "10.10.200.13"))
    with (
        _install(_lab(host), [repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await deploy("integration", on="test3", services=["db"])

    message = str(excinfo.value)
    assert message.rstrip().endswith("up -d --remove-orphans db")


def test_a_preview_with_no_acting_host_says_so_rather_than_showing_nothing():
    """M6: the documented-unreachable arm, exercised rather than ledgered.

    `_acting_hosts` cannot return empty from a resolvable selection (a winner
    always participates, and `_validated_services` already refused a narrowing
    nothing declares), so the arm is driven directly. SUPPRESS THE PAYLOAD,
    NEVER THE ANNOUNCEMENT: an empty command list must still say why, not
    render as a plan that trails off.
    """
    note = deploy_mod._command_preview([])
    assert "no compose command to show" in note
    assert "docker compose" not in note


@pytest.mark.asyncio
async def test_dry_run_deploy_touches_no_device_before_it_declines(single):
    """MUTATION PROBE for the decline's POSITION: the transport saw nothing.

    The decline moved down past two phases; the guarantee it must keep is not
    "it still raises" but "nothing was contacted first". Asserted on the
    recorded transport (both spies), with a positive control on the same
    objects so a zero cannot come from a call that never reached the parent.
    """
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError),
    ):
        await deploy("integration", on="test3")

    assert single.host.exec.await_count == 0, (
        f"a dry-run deploy ran command(s) on the parent before declining: {single.host.commands}"  # type: ignore[attr-defined]
    )
    assert single.host.put.await_count == 0, "a dry-run deploy staged files on the parent"

    # POSITIVE CONTROL, same objects, dry run off: the deploy runs to
    # completion and drives the parent, so the zeros above are the decline
    # talking and not a call that fell over somewhere harmless.
    with _install(single.lab, [single.repo]):
        await deploy("integration", on="test3")
    assert single.host.exec.await_count > 0
    assert single.host.put.await_count > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["teardown", "deployed"])
async def test_teardown_and_deployed_dry_runs_carry_the_same_plan(single, verb):
    """M4: `--dry-run docker down` must tell you what `up` does."""

    async def _call_teardown():
        await teardown("integration", on="test3")

    async def _call_deployed():
        async with deployed("integration", on="test3"):
            pass

    call = _call_teardown if verb == "teardown" else _call_deployed
    with (
        _install(single.lab, [single.repo]),
        patch.object(deploy_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await call()

    message = str(excinfo.value)
    assert "Resolved plan: test3 <- a[core]" in message
    # No compose command here, and no gap clause either: neither verb renders
    # one. `teardown` runs `-p <proj> down` with NO `-f` at all, and
    # `deployed` declines above `deploy`. The clause the §12 ruling deleted
    # was the one that described the ABSENCE as a shortfall.
    assert "the exact compose command cannot be shown" not in message
    assert "docker compose -p" not in message
