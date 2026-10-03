"""The shell kind's plan is what its hooks do (spec §3, the recorder differential)."""

import asyncio
from pathlib import Path

import pytest

from otto.declared import DeclaredEntry
from otto.host import shell_kind  # noqa: F401 — import registers the kind
from otto.host.dev_tool import DEV_TOOL_KINDS, DevTool
from otto.host.product import LOGIN_HOME_PLACEHOLDER, PRODUCT_KINDS, Product, ProductPlan
from otto.result import Result
from otto.utils import Status


def _shell(host, **params):
    entry = DeclaredEntry(
        name="agent",
        kind="shell",
        seam="products",
        owner="r1",
        base_dir=Path("/sut"),
        match={},
        params={"artifact": "build/agent.tar.gz", **params},
    )
    return PRODUCT_KINDS.build([entry], host)[0]


async def _recorded(product, host) -> ProductPlan:
    """Run the REAL hooks against the recorder; hand back what they asked for, by phase."""
    recorded = ProductPlan([], [], [], [])
    assert (await product.stage(host)).is_ok
    recorded.stage = host.take()
    assert (await product.install(host)).is_ok
    recorded.install = host.take()
    assert (await product.uninstall(host)).is_ok
    recorded.uninstall = host.take()
    return recorded


@pytest.mark.asyncio
async def test_shell_plan_is_what_the_hooks_do(plan_recorder):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/stage")
    product = _shell(
        host, install="tar -xzf /opt/stage/agent.tar.gz -C /opt", uninstall="rm -rf /opt/agent"
    )
    plan = product.plan(host)
    recorded = await _recorded(product, host)
    assert recorded.stage == plan.stage == ["PUT /sut/build/agent.tar.gz -> /opt/stage"]
    assert recorded.install == plan.install == ["tar -xzf /opt/stage/agent.tar.gz -C /opt"]
    assert recorded.uninstall == plan.uninstall == ["rm -rf /opt/agent"]
    assert plan.unchecked == []


@pytest.mark.asyncio
async def test_shell_without_commands_plans_nothing_for_install_and_uninstall(plan_recorder):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/stage")
    product = _shell(host)
    plan = product.plan(host)
    recorded = await _recorded(product, host)
    assert recorded.stage == plan.stage == ["PUT /sut/build/agent.tar.gz -> /opt/stage"]
    assert recorded.install == plan.install == []
    assert recorded.uninstall == plan.uninstall == []
    assert plan.unchecked == []


def test_shell_plan_names_the_login_home_only_when_it_falls_through(plan_recorder):
    host = plan_recorder  # no default_dest_dir
    falls = _shell(host)
    pinned = _shell(host, stage_dir="/opt/pinned")
    assert falls.plan(host).stage == [f"PUT /sut/build/agent.tar.gz -> {LOGIN_HOME_PLACEHOLDER}"]
    assert falls.plan(host).unchecked == [
        "the login home — product 'agent' declares no stage_dir and h1 no default_dest_dir"
    ]
    assert pinned.plan(host).stage == ["PUT /sut/build/agent.tar.gz -> /opt/pinned"]
    assert pinned.plan(host).unchecked == []
    assert host.home_reads == 0


def test_a_host_with_no_login_home_is_a_refusal_not_a_placeholder():
    """An embedded target has no login home: the real staging raises, so the plan shows no PUT."""

    class _NoHome:
        id = "board1"

    product = _shell(_NoHome())
    plan = product.plan(_NoHome())
    assert plan.stage == plan.install == plan.uninstall == []
    assert plan.unchecked == [
        (
            "product 'agent' declares no stage_dir and board1 no default_dest_dir, and board1 "
            "has no login home to fall back on: the install is refused"
        )
    ]
    with pytest.raises(ValueError, match="no login home to fall back on"):
        asyncio.run(product.resolved_stage_dir(_NoHome()))


def test_a_dev_tool_entry_names_itself_a_dev_tool_in_the_login_home_gap(plan_recorder):
    host = plan_recorder
    entry = DeclaredEntry(
        name="helper",
        kind="shell",
        seam="dev_tools",
        owner="r1",
        base_dir=Path("/sut"),
        match={},
        params={"artifact": "build/helper.tgz"},
    )
    tool = DEV_TOOL_KINDS.build([entry], host)[0]
    assert tool.plan(host).unchecked == [
        "the login home — dev tool 'helper' declares no stage_dir and h1 no default_dest_dir"
    ]


@pytest.mark.asyncio
async def test_shell_plan_uses_a_login_home_the_host_already_knows(plan_recorder):
    host = plan_recorder
    host.cached_login_home = Path("/home/v")
    product = _shell(host)
    plan = product.plan(host)
    recorded = await _recorded(product, host)
    assert recorded.stage == plan.stage == ["PUT /sut/build/agent.tar.gz -> /home/v"]
    assert plan.unchecked == []
    assert host.home_reads == 0


def test_a_plan_is_pure_and_never_asks_the_host_for_its_home(plan_recorder):
    host = plan_recorder
    _shell(host).plan(host)
    assert host.lines == []
    assert host.home_reads == 0


class _CodeProduct(Product):
    name = "mine"

    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return False


def test_the_default_plan_names_the_class_and_guesses_nothing(plan_recorder):
    plan = _CodeProduct().plan(plan_recorder)
    path = f"{_CodeProduct.__module__}.{_CodeProduct.__qualname__}"
    assert plan.stage == plan.install == plan.uninstall == []
    assert plan.unchecked == [
        (
            f"`{path}`: stage, install and uninstall are Python code and "
            "are not previewed; implement plan() to describe them"
        )
    ]


class _CodeTool(DevTool):
    name = "probe"

    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return False


def test_the_default_tool_plan_names_the_class_and_guesses_nothing(plan_recorder):
    plan = _CodeTool().plan(plan_recorder)
    path = f"{_CodeTool.__module__}.{_CodeTool.__qualname__}"
    assert plan.stage == plan.install == plan.uninstall == []
    assert plan.unchecked == [
        (
            f"`{path}`: stage, install and uninstall are Python code and "
            "are not previewed; implement plan() to describe them"
        )
    ]
