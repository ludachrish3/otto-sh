"""Staging failures carry WHICH failure they were.

Every command here runs on the parent through ``exec``, so the same non-ok
status can mean "the parent said no" or "the parent never answered" — and the
second is not a docker problem at all. ``put`` is the one exception: it
returns a plain :class:`~otto.result.Result` with no ``timed_out`` to ask
about, so its failures are only ever the first kind.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.config.repo import DockerCompose
from otto.docker.staging import stage_compose_files
from otto.host.errors import HostCommandError, HostUnreachableError
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandResult, Result
from otto.utils import Status


def _parent() -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element="test3",
        creds=[Cred(login="vagrant", password="vagrant")],
        docker_capable=True,
    )


def _fail(out: str = "denied") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


def _timed_out() -> CommandResult:
    return CommandResult(Status.Failed, value="", command="", retcode=-1, timed_out=True)


def _compose(tmp_path: Path) -> list[DockerCompose]:
    path = tmp_path / "compose.yml"
    path.write_text("services: {}\n")
    return [DockerCompose(path=path, services=["api"])]


@pytest.mark.asyncio
async def test_a_refused_prepare_is_a_command_failure(tmp_path):
    parent = _parent()
    parent.exec = AsyncMock(return_value=_fail("mkdir: permission denied"))  # type: ignore[method-assign]
    with pytest.raises(HostCommandError, match="failed to prepare the compose staging dir"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))


@pytest.mark.asyncio
async def test_a_timed_out_prepare_is_an_unreachable_host(tmp_path):
    """Same message, different type: nothing was learned about the parent."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=_timed_out())  # type: ignore[method-assign]
    with pytest.raises(HostUnreachableError, match="failed to prepare the compose staging dir"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))


@pytest.mark.asyncio
async def test_a_failed_put_is_a_command_failure(tmp_path):
    """`put` returns a bare Result — there is no timed_out here to split on."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Error, msg="no space left"))  # type: ignore[method-assign]
    with pytest.raises(HostCommandError, match="failed to stage compose file"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))
