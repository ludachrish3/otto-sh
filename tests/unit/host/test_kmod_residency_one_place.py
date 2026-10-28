"""The kmod product and the kmod dev tool answer "is it resident" and "unload" the same way.

Each kind carried its own ``is_installed`` and ``uninstall``; now both ask
:func:`~otto.host.kmod_tool_kind.module_resident` and
:func:`~otto.host.kmod_tool_kind.unload_module`. This drives both kinds
through one host and asserts one answer, so the two cannot drift apart
again — a module listed by a failing ``lsmod`` is not resident for either.
"""

import pytest

from otto.host.kmod_tool_kind import module_resident, unload_module
from otto.result import Result
from otto.utils import Status
from tests.unit.host.test_kmod_kind import _build as build_product
from tests.unit.host.test_kmod_tool_kind import _build as build_tool
from tests.unit.host.test_kmod_tool_kind import _Host

KINDS = {
    "product": lambda host: build_product(host, artifact="build/demo/tracer.ko"),
    "tool": lambda host: build_tool(host, artifact="build/tracer.ko"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_both_kinds_read_residency_and_unload_through_the_one_pair(kind):
    host = _Host(loaded=["ext4", "tracer"])
    entry = KINDS[kind](host)
    assert entry.module_name == "tracer"
    assert await entry.is_installed(host) is await module_resident(host, "tracer") is True
    host.lsmod.return_value = Result(Status.Error, value=["tracer"], msg="boom")
    assert await entry.is_installed(host) is await module_resident(host, "tracer") is False
    assert (await entry.uninstall(host)).is_ok
    host.unload.assert_awaited_once_with("tracer")


@pytest.mark.asyncio
async def test_the_pair_itself():
    host = _Host(loaded=["tracer"])
    assert await module_resident(host, "tracer") is True
    assert await module_resident(host, "other") is False
    assert (await unload_module(host, "tracer")).is_ok
    host.unload.assert_awaited_once_with("tracer")
