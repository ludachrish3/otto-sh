"""The session policy and the tree-transfer arm live on the base, once.

``open_session``, ``send`` and ``expect``'s ``_expect_one`` each make the
same decision — dry run announces and declines, a live run prepares the
device and then touches the session manager — and that decision is
``BaseHost``'s. The families contribute only ``_prepare_live_session``
(nothing, except the container host's ``_ensure_running``). The same holds
for ``get``/``put``'s ``recursive=True`` arm: one ``_get_tree`` / ``_put_tree``
on the base. These tests drive the families through the shared code so a
copy reappearing in one of them is the thing that fails.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.remote_host import RemoteHost
from otto.host.session import DeclinedSession
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests.conftest import active_context
from tests.unit.host.test_docker_host import _make_container, _mock_parent

FAMILIES = (LocalHost, RemoteHost, UnixHost, EmbeddedHost, DockerContainerHost)
POLICY = ("open_session", "send", "_expect_one", "_get_tree", "_put_tree", "_as_sources")


def _unix() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element("box"),
        creds=[Cred(login="user", password="pass")],
        log=LogMode.QUIET,
    )


def _local() -> LocalHost:
    return LocalHost()


def _container() -> DockerContainerHost:
    return _make_container(_mock_parent())


BUILDERS = {"local": _local, "unix": _unix, "container": _container}


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("name", POLICY)
def test_the_policy_is_the_base_s(family, name):
    """No family carries its own copy: the attribute resolves to BaseHost's."""
    assert getattr(family, name) is getattr(BaseHost, name), f"{family.__name__} restates {name}"


@pytest.mark.parametrize("family", FAMILIES)
def test_only_the_container_host_prepares_anything(family):
    hook = family._prepare_live_session
    if family is DockerContainerHost:
        assert hook is not BaseHost._prepare_live_session
    else:
        assert hook is BaseHost._prepare_live_session


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
        assert isinstance(await host.open_session("aux"), DeclinedSession)
        await host.send("echo hi\n")
    prepared.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BUILDERS))
async def test_a_live_arm_prepares_then_sends_through_the_manager(kind, monkeypatch):
    """Preparation precedes the manager on every arm, for every family."""
    host = BUILDERS[kind]()
    order: list[str] = []

    async def prepare() -> None:
        order.append("prepare")

    manager = MagicMock()
    manager.send = AsyncMock(side_effect=lambda *a, **k: order.append("send"))
    manager.expect = AsyncMock(side_effect=lambda *a, **k: order.append("expect") or "match")
    manager.open_session = AsyncMock(side_effect=lambda *a, **k: order.append("open") or "handle")
    monkeypatch.setattr(host, "_prepare_live_session", prepare)
    monkeypatch.setattr(host, "_live_session_mgr", lambda: manager)

    await host.send("x\n", log=LogMode.QUIET)
    assert await host.expect("p", timeout=1.0) == "match"
    assert await host.open_session("aux") == "handle"
    assert order == ["prepare", "send", "prepare", "expect", "prepare", "open"]
    manager.send.assert_awaited_once_with("x\n", log=LogMode.QUIET)


@pytest.mark.asyncio
async def test_the_container_host_s_preparation_is_ensure_running(monkeypatch):
    host = _container()
    ensured = AsyncMock()
    monkeypatch.setattr(host, "_ensure_running", ensured)
    await host._prepare_live_session()
    ensured.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BUILDERS))
@pytest.mark.parametrize("verb", ["get", "put"])
async def test_the_recursive_arm_is_the_base_s_tree_walk(kind, verb, monkeypatch, tmp_path):
    """``recursive=True`` on every family reaches ``_get_tree`` / ``_put_tree``
    with the sources as one list, before any dry-run or live arm."""
    host = BUILDERS[kind]()
    walked = AsyncMock(return_value="walked")
    monkeypatch.setattr(host, f"_{verb}_tree", walked)
    src = tmp_path / "dir"
    src.mkdir()
    result = await getattr(host, verb)(src, Path("/dest"), recursive=True)
    assert result == "walked"
    (call,) = walked.await_args_list
    assert call.args[0] == [src], "a lone path reaches the walk as a list of one"
