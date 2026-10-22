"""otto.docker's image verbs: ``docker image ls|rm|prune`` on one parent or every one.

The command is docker's own, built from docker's own flags; otto resolves the
hosts, runs it on each, and keeps every answer whole. Nothing here dials: the
hosts are real ``UnixHost`` objects whose ``exec`` records what it was given.
"""

from unittest.mock import AsyncMock

import pytest

from otto.docker.image_verbs import (
    image_prune_command,
    image_rm_command,
    prune_images,
    remove_images,
)
from otto.docker.observe import DockerVerbError, list_images
from otto.result import CommandNotRunError, CommandResult
from otto.utils import Status
from tests.conftest import active_context

from .test_deploy import _host, _lab


def _plain(host_id: str, ip: str):
    host = _host(host_id, ip)
    host.docker_capable = False
    return host


def _record(host, *, fail: bool = False):
    """Make *host*'s exec record its commands and answer like a daemon would."""
    commands: list[str] = []

    async def _exec(cmd, *_a, **_kw):
        commands.append(cmd)
        if fail:
            return CommandResult(
                Status.Failed, value=f"Error: No such image on {host.id}\n", command=cmd, retcode=1
            )
        return CommandResult(
            Status.Success, value=f"Deleted on {host.id}\n", command=cmd, retcode=0
        )

    host.exec = AsyncMock(side_effect=_exec)
    host.commands = commands
    return host


@pytest.fixture
def lab():
    return _lab(
        _host("test3", "10.10.200.13"),
        _plain("test2", "10.10.200.12"),
        _host("alt2", "10.10.200.22"),
    )


@pytest.fixture
def two_hosts(lab):
    with active_context(lab):
        yield _record(lab.hosts["test3"]), _record(lab.hosts["alt2"])


# ---------------------------------------------------------------------------
# The commands: docker's own, flag for flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("images", "kwargs", "command"),
    [
        (["api"], {}, "docker image rm -- api"),
        (["api", "db:1.2"], {}, "docker image rm -- api db:1.2"),
        (["api"], {"force": True}, "docker image rm -f -- api"),
        (["api"], {"no_prune": True}, "docker image rm --no-prune -- api"),
        (["api"], {"force": True, "no_prune": True}, "docker image rm -f --no-prune -- api"),
        # A name goes into a shell on the parent: quoted, never interpreted.
        (["my image"], {}, "docker image rm -- 'my image'"),
        (["a'b"], {}, "docker image rm -- 'a'\"'\"'b'"),
        (["x;rm -rf /"], {}, "docker image rm -- 'x;rm -rf /'"),
        # An image named like a flag is still an image: docker's `--` ends the flags.
        (["-f"], {}, "docker image rm -- -f"),
        (["--no-prune", "api"], {"force": True}, "docker image rm -f -- --no-prune api"),
    ],
)
def test_image_rm_command_is_dockers_own(images, kwargs, command):
    assert image_rm_command(images, **kwargs) == command


@pytest.mark.parametrize(
    ("kwargs", "command"),
    [
        ({}, "docker image prune -f"),
        ({"all": True}, "docker image prune -a -f"),
        ({"filters": ["until=24h"]}, "docker image prune -f --filter until=24h"),
        (
            {"all": True, "filters": ["label=a=b", "until=1h"]},
            "docker image prune -a -f --filter label=a=b --filter until=1h",
        ),
        ({"filters": ["label=has space"]}, "docker image prune -f --filter 'label=has space'"),
        ({"filters": ["label!=x'y"]}, "docker image prune -f --filter 'label!=x'\"'\"'y'"),
    ],
)
def test_image_prune_command_is_dockers_own_and_always_forced(kwargs, command):
    assert image_prune_command(**kwargs) == command


# ---------------------------------------------------------------------------
# Which hosts: every docker-capable one, or the one --parent names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "command"),
    [
        (list_images, "docker image ls"),
        (lambda: list_images(all=True), "docker image ls -a"),
        (lambda: remove_images(["api"], force=True), "docker image rm -f -- api"),
        (lambda: prune_images(force=True, all=True), "docker image prune -a -f"),
    ],
    ids=["ls", "ls-a", "rm", "prune"],
)
async def test_no_parent_runs_on_every_docker_capable_host_in_lab_order(two_hosts, call, command):
    report = await call()
    assert [h.host_id for h in report.hosts] == ["test3", "alt2"]
    assert [h.command for h in report.hosts] == [command, command]
    # The device call itself, not only what the report says it was.
    assert all(host.commands == [command] for host in two_hosts)
    assert report.hosts[0].result.value == "Deleted on test3\n"
    assert report.ok


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda: list_images(parent="alt2"),
        lambda: remove_images(["api"], parent="alt2"),
        lambda: prune_images(force=True, parent="alt2"),
    ],
    ids=["ls", "rm", "prune"],
)
async def test_a_parent_narrows_to_that_host(two_hosts, call):
    t3, alt2 = two_hosts
    report = await call()
    assert [h.host_id for h in report.hosts] == ["alt2"]
    assert t3.commands == []
    assert len(alt2.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda: list_images(parent="test2"),
        lambda: remove_images(["api"], parent="test2"),
        lambda: prune_images(force=True, parent="test2"),
    ],
    ids=["ls", "rm", "prune"],
)
async def test_a_parent_that_is_not_docker_capable_refuses_by_field(two_hosts, call):
    with pytest.raises(DockerVerbError, match="'test2' is not a docker-capable") as exc:
        await call()
    assert exc.value.field == "parent"
    assert all(host.commands == [] for host in two_hosts)


@pytest.mark.asyncio
async def test_a_failing_host_keeps_its_own_error_and_the_others_still_run(two_hosts):
    t3, alt2 = two_hosts
    _record(t3, fail=True)
    report = await remove_images(["api"])
    assert [h.result.is_ok for h in report.hosts] == [False, True]
    assert report.hosts[0].result.value == "Error: No such image on test3\n"
    assert report.hosts[1].result.value == "Deleted on alt2\n"
    assert not report.ok
    assert alt2.commands == ["docker image rm -- api"]


@pytest.mark.asyncio
async def test_an_unreachable_host_is_reported_unreached_and_the_others_still_run(two_hosts):
    t3, alt2 = two_hosts
    t3.exec = AsyncMock(side_effect=ConnectionError())
    report = await prune_images(force=True)
    assert [h.reached for h in report.hosts] == [False, True]
    assert report.hosts[0].result.value == "otto: host 'test3' unreachable: ConnectionError()"
    assert not report.ok
    assert alt2.commands == ["docker image prune -f"]


@pytest.mark.asyncio
async def test_a_daemons_refusal_is_still_a_reached_host(two_hosts):
    t3, _alt2 = two_hosts
    _record(t3, fail=True)
    report = await remove_images(["api"])
    assert [h.reached for h in report.hosts] == [True, True]


# ---------------------------------------------------------------------------
# Each refusal runs nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_without_force_refuses_saying_f_is_needed_and_runs_nothing(two_hosts):
    with pytest.raises(DockerVerbError, match=r"-f") as exc:
        await prune_images()
    assert exc.value.field == "force"
    assert "never prompts" in str(exc.value)
    assert all(host.commands == [] for host in two_hosts)


@pytest.mark.asyncio
async def test_prune_without_force_refuses_before_resolving_any_host():
    # A lab with no docker-capable host would refuse by --parent; -f is asked first,
    # so the user fixes the one thing otto will always refuse.
    with (
        active_context(_lab(_plain("test2", "10.10.200.12"))),
        pytest.raises(DockerVerbError) as exc,
    ):
        await prune_images()
    assert exc.value.field == "force"


@pytest.mark.asyncio
async def test_rm_with_no_image_refuses_by_field_and_runs_nothing(two_hosts):
    with pytest.raises(DockerVerbError) as exc:
        await remove_images([])
    assert exc.value.field == "images"
    assert all(host.commands == [] for host in two_hosts)


# ---------------------------------------------------------------------------
# Dry run: every host's exact command, nothing run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "command", "untouched"),
    [
        (lambda: list_images(all=True), "docker image ls -a", "No daemon was asked."),
        (
            lambda: remove_images(["my image"], force=True),
            "docker image rm -f -- 'my image'",
            "No image was removed.",
        ),
        (
            lambda: prune_images(force=True, filters=["label=x"]),
            "docker image prune -f --filter label=x",
            "No image was removed.",
        ),
    ],
    ids=["ls", "rm", "prune"],
)
async def test_a_dry_run_names_each_hosts_exact_command_and_runs_nothing(
    lab, call, command, untouched
):
    t3, alt2 = _record(lab.hosts["test3"]), _record(lab.hosts["alt2"])
    with active_context(lab, dry_run=True), pytest.raises(CommandNotRunError) as exc:
        await call()
    lines = str(exc.value).splitlines()
    # One host per line, the exact command alone on it, then what did not happen.
    assert lines[-3:] == [f"on test3: {command}", f"on alt2: {command}", untouched]
    assert t3.commands == []
    assert alt2.commands == []
    # Positive control: the same call, not a dry run, runs exactly that command.
    with active_context(lab):
        report = await call()
    assert t3.commands == [command]
    assert alt2.commands == [command]
    assert report.ok


@pytest.mark.asyncio
async def test_a_dry_run_prune_without_force_still_refuses(lab):
    _record(lab.hosts["test3"])
    with active_context(lab, dry_run=True), pytest.raises(DockerVerbError):
        await prune_images()


def test_the_image_verbs_are_lazy_exports():
    import otto.docker as docker_mod
    from otto.docker import image_verbs, observe

    assert docker_mod.remove_images is image_verbs.remove_images
    assert docker_mod.prune_images is image_verbs.prune_images
    assert docker_mod.list_images is observe.list_images
