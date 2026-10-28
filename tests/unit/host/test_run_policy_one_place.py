"""The ``run`` policy lives on the base host, once; the families keep two hooks.

``_run_one`` — refuse a user the family cannot honour, decline under a dry
run, prepare the device, run the command inside the family's user binding —
was spelled in the local, remote and container families, each copy the twin
of the ``send`` policy the base already owns. Now the base owns this one
too, and a family contributes ``_refuse_run_user`` (local, remote) or
``_run_user_binding`` (container) and nothing else. These tests drive every
family through the shared method so a copy reappearing is what fails.
"""

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.host.remote_host import RemoteHost
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests.conftest import active_context
from tests.unit.host.test_session_policy_one_place import BUILDERS

FAMILIES = (LocalHost, RemoteHost, UnixHost, EmbeddedHost, DockerContainerHost)


@pytest.mark.parametrize("family", FAMILIES)
def test_the_run_policy_is_the_base_s(family):
    """No family restates ``_run_one``; the unix family's line lock delegates to it."""
    if family is UnixHost:
        assert "_run_one" in vars(family), "the console line lock wraps the base's run"
    else:
        assert family._run_one is BaseHost._run_one, f"{family.__name__} restates _run_one"


def test_only_the_container_host_binds_a_user_and_only_local_and_remote_refuse_one():
    assert DockerContainerHost._run_user_binding is not BaseHost._run_user_binding
    for family in (LocalHost, RemoteHost, UnixHost, EmbeddedHost):
        assert family._run_user_binding is BaseHost._run_user_binding
    assert LocalHost._refuse_run_user is not BaseHost._refuse_run_user
    assert RemoteHost._refuse_run_user is not BaseHost._refuse_run_user
    assert DockerContainerHost._refuse_run_user is BaseHost._refuse_run_user
    assert isinstance(BaseHost._run_user_binding(object(), "root"), type(nullcontext()))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BUILDERS))
async def test_a_dry_run_declines_before_preparing_or_touching_the_manager(kind, monkeypatch):
    host = BUILDERS[kind]()
    prepared = AsyncMock()
    monkeypatch.setattr(host, "_prepare_live_session", prepared)
    monkeypatch.setattr(
        host, "_live_session_mgr", MagicMock(side_effect=AssertionError("manager touched"))
    )
    with active_context(dry_run=True):
        result = await host._run_one("echo hi", timeout=5.0)
    assert result.command == "echo hi"
    prepared.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BUILDERS))
async def test_a_live_run_prepares_then_runs_inside_the_binding(kind, monkeypatch):
    """Preparation, then the binding's entry, then the manager, then the binding's exit."""
    host = BUILDERS[kind]()
    order: list[str] = []

    async def prepare() -> None:
        order.append("prepare")

    class Binding:
        def __enter__(self):
            order.append("bind")

        def __exit__(self, *exc):
            order.append("unbind")

    manager = MagicMock()
    manager.run_cmd = AsyncMock(side_effect=lambda *a, **k: order.append("run") or "ran")
    monkeypatch.setattr(host, "_prepare_live_session", prepare)
    monkeypatch.setattr(host, "_run_user_binding", lambda user: Binding())
    monkeypatch.setattr(host, "_live_session_mgr", lambda: manager)

    assert await host._run_one("x", timeout=3.0, log=LogMode.QUIET) == "ran"
    assert order == ["prepare", "bind", "run", "unbind"]
    manager.run_cmd.assert_awaited_once_with("x", expects=None, timeout=3.0, log=LogMode.QUIET)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["local", "unix"])
async def test_a_refusal_lands_above_the_dry_run_arm(kind, monkeypatch):
    """A family that cannot honour ``user`` refuses even under a dry run, before preparing."""
    host = BUILDERS[kind]()
    prepared = AsyncMock()
    monkeypatch.setattr(host, "_prepare_live_session", prepared)
    with (
        active_context(dry_run=True),
        pytest.raises(NotImplementedError, match="run\\(user=\\.\\.\\.\\)"),
    ):
        await host._run_one("id", timeout=5.0, user="root")
    prepared.assert_not_awaited()
