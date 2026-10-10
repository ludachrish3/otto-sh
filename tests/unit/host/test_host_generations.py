"""A close never touches a later connection.

For each concrete family: an old close that resumes after a reconnect
(following abandonment, or a same-loop rebuild_connections) leaves the new
managers and the owner untouched; on DockerContainerHost the run channel's
user binding too. A stale abandonment is a no-op.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from otto import invocation as inv
from otto.host import DockerContainerHost, LocalHost, ZephyrHost
from otto.host.element import Element
from otto.logger.mode import LogMode
from tests._fixtures.labdata import make_host
from tests._fixtures.registry import registered_ids

pytestmark = pytest.mark.asyncio


class _Closer:
    """A manager whose close can be held open, counting the closes that ran."""

    has_live_default_session = True

    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.closed = 0

    async def _close(self) -> None:
        self.entered.set()
        if self.block:
            await self.gate.wait()
        self.closed += 1

    close_all = _close
    close = _close


def _unix():
    return make_host("test1")


def _zephyr():
    return ZephyrHost(ip="192.0.2.1", element=Element("zephyr37_fat"), log=LogMode.QUIET)


def _local():
    return LocalHost()


def _docker():
    parent = MagicMock()
    parent.id = "parent"
    return DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )


FAMILIES = [_unix, _zephyr, _local, _docker]


def _install(host, *, block: bool) -> "dict[str, _Closer]":
    managers = {"_session_mgr": _Closer(block=block)}
    if hasattr(host, "_connections"):
        managers["_connections"] = _Closer()
    for attr, manager in managers.items():
        setattr(host, attr, manager)
    return managers


async def _old_close_held_open(host):
    old = _install(host, block=True)
    host._claim_loop()
    closing = asyncio.ensure_future(host.close())
    await asyncio.wait_for(old["_session_mgr"].entered.wait(), 5)
    return old, closing


async def _resume(old, closing) -> None:
    """Let the held-open close finish, bounded so a defect fails instead of hanging."""
    old["_session_mgr"].gate.set()
    await asyncio.wait_for(closing, 5)


def _assert_untouched(host, new, loop):
    for attr, manager in new.items():
        assert getattr(host, attr) is manager, attr
        assert manager.closed == 0, attr
    assert host._owner_loop is loop


@pytest.mark.parametrize("family", FAMILIES)
async def test_an_old_close_after_a_same_loop_rebuild_leaves_the_new_managers(family):
    # G2
    host, loop = family(), asyncio.get_running_loop()
    old, closing = await _old_close_held_open(host)
    try:
        host.rebuild_connections()
        new = _install(host, block=False)
    finally:
        await _resume(old, closing)
    _assert_untouched(host, new, loop)
    if "_connections" in old:
        assert old["_connections"].closed == 1  # the old close closed what it set out to


@pytest.mark.parametrize("family", FAMILIES)
async def test_an_old_close_after_abandonment_and_a_reconnect_leaves_the_new_managers(family):
    # G3
    host, loop = family(), asyncio.get_running_loop()
    old, closing = await _old_close_held_open(host)
    try:
        host._abandon_registration(loop, host._generation)
        new = _install(host, block=False)
        host._claim_loop()  # the next use reconnects on this loop
    finally:
        await _resume(old, closing)
    _assert_untouched(host, new, loop)


async def test_an_old_docker_close_leaves_the_new_channels_user_binding():
    # G4
    host = _docker()
    old, closing = await _old_close_held_open(host)
    try:
        host.rebuild_connections()
        _install(host, block=False)
        host._record_run_channel_open("alice")
    finally:
        await _resume(old, closing)
    assert host._bound_run_user == "alice"
    with pytest.raises(RuntimeError, match="bound to user 'alice'"):
        await host.run("id", user="bob")


@pytest.mark.parametrize("family", FAMILIES)
async def test_the_generation_moves_on_rebuild_claim_and_abandonment(family):
    # G1
    host, loop = family(), asyncio.get_running_loop()
    start = host._generation
    host._claim_loop()
    claimed = host._generation
    host.rebuild_connections()
    rebuilt = host._generation
    host._abandon_registration(loop, rebuilt)
    assert start < claimed < rebuilt < host._generation
    assert host._owner_loop is None


@pytest.mark.parametrize("family", FAMILIES)
async def test_a_stale_abandonment_is_a_no_op(family):
    # G5
    host, loop = family(), asyncio.get_running_loop()
    host._claim_loop()
    stale = host._generation
    host.rebuild_connections()
    current = _install(host, block=False)
    assert host._abandon_registration(loop, stale) is False
    _assert_untouched(host, current, loop)
    other = asyncio.new_event_loop()
    try:
        # another loop's abandonment
        assert host._abandon_registration(other, host._generation) is False
        _assert_untouched(host, current, loop)
    finally:
        other.close()


@pytest.mark.parametrize("family", FAMILIES)
async def test_a_context_less_connect_registers_with_its_loop(family):
    host, loop = family(), asyncio.get_running_loop()
    host._claim_loop()
    assert registered_ids(loop) == [host.id]
    await inv.sweep_unheld(loop, label="t", deadline=5)


@pytest.mark.parametrize("family", FAMILIES)
async def test_a_rebuild_re_registers_at_the_new_generation_without_changing_the_owner(family):
    host, loop = family(), asyncio.get_running_loop()
    host._claim_loop()
    host.rebuild_connections()
    record = inv._REGISTRIES[loop].records[id(host)]
    assert record.generation == host._generation
    assert host._owner_loop is loop
    await inv.sweep_unheld(loop, label="t", deadline=5)


@pytest.mark.parametrize("family", FAMILIES)
async def test_abandonment_leaves_no_record_behind(family):  # G6
    host, loop = family(), asyncio.get_running_loop()
    host._claim_loop()
    assert host._abandon_registration(loop, host._generation) is True  # it dropped something
    assert registered_ids(loop) == []


async def test_a_docker_channel_depends_on_its_parent():
    host = _docker()
    record = host._registration()
    assert record.depends_on == [id(host.parent)]
