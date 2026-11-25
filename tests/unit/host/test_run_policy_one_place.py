"""The ``run`` policy lives on the base host, once; the families keep two hooks.

``_run_one`` — decline under a dry run, prepare the device, run the command
inside the family's user binding — was spelled in the local, remote and
container families, each copy the twin of the ``send`` policy the base
already owns. Now the base owns this one too, and a family contributes
``_run_user_binding`` (container) and nothing else; a user the family cannot
honour is refused above it, from its declaration (``_refuse_option``). These
tests drive every family through the shared method so a copy reappearing is
what fails.
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
from otto.result import CommandResult
from otto.utils import Status
from tests.conftest import active_context
from tests.unit.host.test_session_policy_one_place import BUILDERS

FAMILIES = (LocalHost, RemoteHost, UnixHost, EmbeddedHost, DockerContainerHost)


@pytest.mark.parametrize("family", FAMILIES)
def test_the_run_policy_is_the_base_s(family):
    """No family restates ``_run_one``; the line turn is the base's ``run``'s, above it."""
    assert family._run_one is BaseHost._run_one, f"{family.__name__} restates _run_one"


def test_only_the_container_host_binds_a_user_and_no_family_restates_the_refusal():
    assert DockerContainerHost._run_user_binding is not BaseHost._run_user_binding
    for family in (LocalHost, RemoteHost, UnixHost, EmbeddedHost):
        assert family._run_user_binding is BaseHost._run_user_binding
    for family in FAMILIES:
        assert family._refuse_option is BaseHost._refuse_option, family.__name__
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

    # `run` passes the manager's primitive's result (`SessionManager.run_cmd`) through unchanged.
    ran = CommandResult(status=Status.Success, value="ran", command="x", retcode=0)
    manager = MagicMock()
    manager.run_cmd = AsyncMock(side_effect=lambda *a, **k: order.append("run") or ran)
    monkeypatch.setattr(host, "_prepare_live_session", prepare)
    monkeypatch.setattr(host, "_run_user_binding", lambda user: Binding())
    monkeypatch.setattr(host, "_live_session_mgr", lambda: manager)

    result = await host._run_one("x", timeout=3.0, log=LogMode.QUIET)
    assert result == ran
    assert order == ["prepare", "bind", "run", "unbind"]
    manager.run_cmd.assert_awaited_once_with(cmd="x", expects=None, timeout=3.0, log=LogMode.QUIET)


@pytest.mark.asyncio
async def test_a_refusal_lands_above_the_dry_run_arm(monkeypatch):
    """A family that cannot honour ``user`` refuses even under a dry run, before preparing."""
    from otto.host.element import Element
    from otto.host.embedded_host import ZephyrHost
    from otto.host.errors import UnsupportedOptionError

    host = ZephyrHost(ip="192.0.2.1", element=Element(name="z"), log=LogMode.QUIET)
    prepared = AsyncMock()
    monkeypatch.setattr(host, "_prepare_live_session", prepared)
    with active_context(dry_run=True), pytest.raises(UnsupportedOptionError, match="user="):
        await host.run("id", timeout=5.0, user="root")
    prepared.assert_not_awaited()


def _refusing_every_option() -> type:
    """A Zephyr family that also refuses ``expects=``: one refusing every option."""
    from dataclasses import replace

    from otto.host.capability_grid import OptionSupport
    from otto.host.embedded_host import ZephyrHost

    refused = ZephyrHost.capabilities.sudo.limitation
    assert refused is not None

    class Refusing(ZephyrHost):
        capabilities = replace(
            ZephyrHost.capabilities, expects=OptionSupport.refused(limitation=refused)
        )

    return Refusing


_OPTION_INPUTS = [
    pytest.param("true", {"expects": [("x", "y")]}, "expects", id="call-expects"),
    pytest.param(("true", [("x", "y")]), {}, "expects", id="command-expects"),
    pytest.param(("true", []), {"expects": [("x", "y")]}, None, id="command-overrides-call"),
    pytest.param("true", {"sudo": True}, "sudo", id="call-sudo"),
    pytest.param("true", {"user": "other"}, "user", id="call-user"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry", [False, True], ids=["live", "dry"])
@pytest.mark.parametrize("verb", ["run", "exec"])
@pytest.mark.parametrize(("command", "kwargs", "refused"), _OPTION_INPUTS)
async def test_run_and_exec_refuse_from_the_resolved_command_s_options(
    monkeypatch, verb, dry, command, kwargs, refused
):
    """Both verbs, live and dry, on the same input: refused by the same option, or run.

    A command's own ``expects`` replaces the call's, so a command that sets
    none overrides the call's pairs and nothing is refused.
    """
    from otto.host.element import Element
    from otto.host.errors import UnsupportedOptionError
    from otto.host.host import ShellCommand

    host = _refusing_every_option()(ip="192.0.2.1", element=Element(name="z"), log=LogMode.QUIET)
    ran = CommandResult(status=Status.Success, value="", command="true", retcode=0)
    monkeypatch.setattr(host, "_run_one", AsyncMock(return_value=ran))
    monkeypatch.setattr(host, "_exec_one", AsyncMock(return_value=ran))
    cmd = command if isinstance(command, str) else ShellCommand(cmd=command[0], expects=command[1])
    with active_context(dry_run=dry):
        if refused is None:
            await getattr(host, verb)(cmd, timeout=5.0, **kwargs)
            return
        with pytest.raises(UnsupportedOptionError) as raised:
            await getattr(host, verb)(cmd, timeout=5.0, **kwargs)
    assert raised.value.option == refused


@pytest.mark.asyncio
@pytest.mark.parametrize("dry", [False, True], ids=["live", "dry"])
@pytest.mark.parametrize(
    ("kwargs", "refused"),
    [
        pytest.param({"expects": [("x", "y")]}, "expects", id="call-expects"),
        pytest.param({"sudo": True}, "sudo", id="call-sudo"),
        pytest.param({"user": "other"}, "user", id="call-user"),
    ],
)
async def test_a_call_option_is_refused_with_no_commands_at_all(dry, kwargs, refused):
    """``run([])``: the refusal is about the call. Only ``run`` takes a list of commands."""
    from otto.host.element import Element
    from otto.host.errors import UnsupportedOptionError

    host = _refusing_every_option()(ip="192.0.2.1", element=Element(name="z"), log=LogMode.QUIET)
    with active_context(dry_run=dry), pytest.raises(UnsupportedOptionError) as raised:
        await host.run([], timeout=5.0, **kwargs)
    assert raised.value.option == refused
