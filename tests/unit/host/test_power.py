"""Unit tests for the PowerController strategy, registry, and host power verbs."""

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from otto.host.power import (
    CommandPowerController,
    PowerController,
    PowerState,
    build_power_controller,
    power_control_from_spec,
)
from otto.utils import CommandStatus, Status


def _target_with_controller(runner):
    """Build a UnixHost whose lab resolves controller id 'hyp' to *runner*."""
    from otto.host.unix_host import UnixHost

    target = UnixHost(ip="10.0.0.9", element="vm", creds={"u": "p"}, name="vm1", log=LogMode.QUIET)

    class _FakeLab:
        hosts: ClassVar = {"hyp": runner}

    target._lab = _FakeLab()  # type: ignore[assignment]
    return target


@pytest.mark.asyncio
async def test_command_controller_on_runs_formatted_command_on_controller():
    runner = AsyncMock()
    runner.oneshot.return_value = CommandStatus("virsh start vm1", "", Status.Success, 0)
    target = _target_with_controller(runner)
    pc = CommandPowerController(
        on_cmd="virsh start {name}", off_cmd="virsh destroy {name}", controller="hyp"
    )
    status, _ = await pc.on(target)
    assert status is Status.Success
    runner.oneshot.assert_awaited_once_with("virsh start vm1")


@pytest.mark.asyncio
async def test_command_controller_status_parses_on_marker():
    runner = AsyncMock()
    runner.oneshot.return_value = CommandStatus("virsh domstate vm1", "running", Status.Success, 0)
    target = _target_with_controller(runner)
    pc = CommandPowerController(
        on_cmd="x",
        off_cmd="y",
        status_cmd="virsh domstate {name}",
        status_on="running",
        controller="hyp",
    )
    assert await pc.status(target) is PowerState.ON


@pytest.mark.asyncio
async def test_command_controller_status_none_when_no_status_command():
    pc = CommandPowerController(on_cmd="x", off_cmd="y")
    assert await pc.status(_target_with_controller(AsyncMock())) is None


@pytest.mark.asyncio
async def test_cycle_default_is_off_then_on():
    runner = AsyncMock()
    runner.oneshot.return_value = CommandStatus("c", "", Status.Success, 0)
    target = _target_with_controller(runner)
    pc = CommandPowerController(on_cmd="on {name}", off_cmd="off {name}", controller="hyp")
    status, _ = await pc.cycle(target)
    assert status is Status.Success
    issued = [c.args[0] for c in runner.oneshot.await_args_list]
    assert issued == ["off vm1", "on vm1"]


def test_registry_builtin_and_unknown():
    assert build_power_controller("command") is CommandPowerController
    with pytest.raises(ValueError, match="Unknown power controller"):
        build_power_controller("nope")


def test_power_control_from_spec_dict_builds_command_controller():
    pc = power_control_from_spec(
        {"type": "command", "on_cmd": "o", "off_cmd": "f", "controller": "hyp"}
    )
    assert isinstance(pc, CommandPowerController)
    assert pc.on_cmd == "o"
    assert pc.controller == "hyp"


def test_power_control_from_spec_passthrough_and_none():
    pc = CommandPowerController(on_cmd="o", off_cmd="f")
    assert power_control_from_spec(pc) is pc
    assert power_control_from_spec(None) is None


def test_unix_power_control_coerced_from_dict():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"u": "p"},
        log=LogMode.QUIET,
        power_control={"type": "command", "on_cmd": "o", "off_cmd": "f"},
    )
    assert isinstance(host.power_control, CommandPowerController)


def test_hosts_default_power_control_none():
    from otto.host.embedded_host import ZephyrHost
    from otto.host.local_host import LocalHost

    assert LocalHost().power_control is None
    assert ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET).power_control is None


class _FakeController(PowerController):
    type_name = "fake"

    def __init__(self, state=PowerState.OFF):
        self.state = state
        self.calls: list[str] = []

    async def on(self, host):
        self.calls.append("on")
        self.state = PowerState.ON
        return Status.Success, ""

    async def off(self, host):
        self.calls.append("off")
        self.state = PowerState.OFF
        return Status.Success, ""

    async def status(self, host):
        return self.state


def _local_with_controller(ctrl):
    from otto.host.local_host import LocalHost

    h = LocalHost()
    h.power_control = ctrl
    return h


@pytest.mark.asyncio
async def test_power_on_off_dispatch():
    ctrl = _FakeController()
    host = _local_with_controller(ctrl)
    await host.power("on")
    await host.power("off")
    assert ctrl.calls == ["on", "off"]


@pytest.mark.asyncio
async def test_power_toggle_uses_status():
    ctrl = _FakeController(state=PowerState.OFF)
    host = _local_with_controller(ctrl)
    await host.power()  # off → on
    assert ctrl.calls == ["on"]


@pytest.mark.asyncio
async def test_power_toggle_without_status_raises():
    ctrl = _FakeController()

    async def no_status(host):
        return None

    ctrl.status = no_status  # type: ignore[assignment]
    with pytest.raises(ValueError, match="status"):
        await _local_with_controller(ctrl).power()


@pytest.mark.asyncio
async def test_power_without_controller_raises():
    from otto.host.local_host import LocalHost

    with pytest.raises(ValueError, match="no power_control"):
        await LocalHost().power("on")


from unittest.mock import patch

from otto.host.host import RunResult
from otto.logger.mode import LogMode


@pytest.mark.asyncio
async def test_unix_soft_reboot_issues_reboot_sudo():
    from otto.host.unix_host import UnixHost

    host = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=LogMode.QUIET)
    with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = RunResult(Status.Success, [])
        status, _ = await host.reboot()
    mock_run.assert_awaited_once_with("reboot", sudo=True, timeout=10.0)
    assert status is Status.Success


@pytest.mark.asyncio
async def test_zephyr_soft_reboot_issues_kernel_reboot():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET)
    with patch.object(ZephyrHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = RunResult(Status.Success, [])
        status, _ = await host.reboot()
    mock_run.assert_awaited_once_with("kernel reboot cold", timeout=10.0)
    assert status is Status.Success


@pytest.mark.asyncio
async def test_hard_reboot_cycles_controller():
    ctrl = _FakeController(state=PowerState.ON)
    host = _local_with_controller(ctrl)
    status, _ = await host.reboot(hard=True)
    assert status is Status.Success
    assert ctrl.calls == ["off", "on"]  # cycle default


@pytest.mark.asyncio
async def test_localhost_soft_reboot_raises():
    from otto.host.local_host import LocalHost

    with pytest.raises(NotImplementedError, match="reboot"):
        await LocalHost().reboot()


@pytest.mark.asyncio
async def test_localhost_shutdown_raises():
    from otto.host.local_host import LocalHost

    with pytest.raises(NotImplementedError, match="shutdown"):
        await LocalHost().shutdown()


@pytest.mark.asyncio
async def test_unix_shutdown_issues_shutdown_sudo():
    from otto.host.unix_host import UnixHost

    host = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=LogMode.QUIET)
    with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = RunResult(Status.Success, [])
        status, _ = await host.shutdown()
    mock_run.assert_awaited_once_with("shutdown -h now", sudo=True, timeout=10.0)
    assert status is Status.Success


@pytest.mark.asyncio
async def test_localhost_is_reachable_true():
    from otto.host.local_host import LocalHost

    assert await LocalHost().is_reachable() is True


@pytest.mark.asyncio
async def test_remote_is_reachable_reflects_verify_connection(monkeypatch):
    from otto.host.unix_host import UnixHost

    host = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=LogMode.QUIET)
    monkeypatch.setattr(
        host,
        "verify_connection",
        AsyncMock(return_value=CommandStatus("connect", "ok", Status.Success, 0)),
    )
    assert await host.is_reachable() is True
    monkeypatch.setattr(
        host,
        "verify_connection",
        AsyncMock(return_value=CommandStatus("connect", "no", Status.Error, 1)),
    )
    assert await host.is_reachable() is False


@pytest.mark.asyncio
async def test_wait_until_up_returns_true_when_reachable(monkeypatch):
    from otto.host.local_host import LocalHost

    host = LocalHost()
    seq = iter([False, True])

    async def fake(timeout=10.0):
        return next(seq)

    monkeypatch.setattr(type(host), "is_reachable", fake)
    assert await host.wait_until_up(timeout=5.0, interval=0.0) is True


@pytest.mark.asyncio
async def test_wait_until_up_times_out(monkeypatch):
    from otto.host.local_host import LocalHost

    host = LocalHost()

    async def never(timeout=10.0):
        return False

    monkeypatch.setattr(type(host), "is_reachable", never)
    assert await host.wait_until_up(timeout=0.05, interval=0.01) is False


@pytest.mark.asyncio
async def test_wait_until_down_returns_true_when_unreachable(monkeypatch):
    from otto.host.local_host import LocalHost

    host = LocalHost()
    seq = iter([True, False])

    async def fake(timeout=10.0):
        return next(seq)

    monkeypatch.setattr(type(host), "is_reachable", fake)
    assert await host.wait_until_down(timeout=5.0, interval=0.0) is True


@pytest.mark.asyncio
async def test_reboot_wait_timeout_downgrades_to_failed(monkeypatch):
    ctrl = _FakeController(state=PowerState.ON)
    host = _local_with_controller(ctrl)

    async def never_up(self, timeout, interval=2.0):
        return False

    monkeypatch.setattr(type(host), "wait_until_up", never_up)
    status, msg = await host.reboot(hard=True, wait=True, timeout=0.0)
    assert status is Status.Failed
    assert "reachable" in msg
    assert ctrl.calls == ["off", "on"]  # the reboot still happened


@pytest.mark.asyncio
async def test_reboot_wait_success_keeps_status(monkeypatch):
    ctrl = _FakeController(state=PowerState.ON)
    host = _local_with_controller(ctrl)

    async def comes_up(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(host), "wait_until_up", comes_up)
    status, _ = await host.reboot(hard=True, wait=True, timeout=0.0)
    assert status is Status.Success
