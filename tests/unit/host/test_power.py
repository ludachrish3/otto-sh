"""Unit tests for the PowerController strategy, registry, and host power verbs."""

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from otto.host.login_proxy import Cred
from otto.host.power import (
    CommandPowerController,
    PowerController,
    PowerState,
    build_power_controller,
    power_control_from_spec,
)
from otto.result import CommandResult, Result
from otto.utils import Status


def _fake_command_result(command: str, output: str, status: Status, retcode: int) -> CommandResult:
    return CommandResult(status=status, value=output, command=command, retcode=retcode)


def _target_with_controller(runner):
    """Build a UnixHost whose lab resolves controller id 'hyp' to *runner*."""
    from otto.host.unix_host import UnixHost

    target = UnixHost(
        ip="10.0.0.9",
        element="vm",
        creds=[Cred(login="u", password="p")],
        name="vm1",
        log=LogMode.QUIET,
    )

    class _FakeLab:
        hosts: ClassVar = {"hyp": runner}

    target._lab = _FakeLab()  # type: ignore[assignment]
    return target


@pytest.mark.asyncio
async def test_command_controller_on_runs_formatted_command_on_controller():
    runner = AsyncMock()
    runner.exec.return_value = _fake_command_result("virsh start vm1", "", Status.Success, 0)
    target = _target_with_controller(runner)
    pc = CommandPowerController(
        on_cmd="virsh start {name}", off_cmd="virsh destroy {name}", controller="hyp"
    )
    result = await pc.on(target)
    assert result.status is Status.Success
    runner.exec.assert_awaited_once_with("virsh start vm1")


@pytest.mark.asyncio
async def test_command_controller_status_parses_on_marker():
    runner = AsyncMock()
    runner.exec.return_value = _fake_command_result(
        "virsh domstate vm1", "running", Status.Success, 0
    )
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
    runner.exec.return_value = _fake_command_result("c", "", Status.Success, 0)
    target = _target_with_controller(runner)
    pc = CommandPowerController(on_cmd="on {name}", off_cmd="off {name}", controller="hyp")
    result = await pc.cycle(target)
    assert result.status is Status.Success
    issued = [c.args[0] for c in runner.exec.await_args_list]
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
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        power_control={"type": "command", "on_cmd": "o", "off_cmd": "f"},
    )
    assert isinstance(host.power_control, CommandPowerController)


def test_hosts_default_power_control_none():
    from otto.host.embedded_host import ZephyrHost
    from otto.host.local_host import LocalHost

    assert LocalHost().power_control is None
    zephyr = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
    assert zephyr.power_control is None


class _FakeController(PowerController):
    type_name = "fake"

    def __init__(self, state=PowerState.OFF):
        self.state = state
        self.calls: list[str] = []

    async def on(self, host):
        self.calls.append("on")
        self.state = PowerState.ON
        return Result(Status.Success)

    async def off(self, host):
        self.calls.append("off")
        self.state = PowerState.OFF
        return Result(Status.Success)

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

from otto.logger.mode import LogMode
from otto.result import Results


@pytest.mark.asyncio
async def test_unix_soft_reboot_issues_reboot_sudo():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
    )
    with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = Results.collect([])
        result = await host.reboot()
    mock_run.assert_awaited_once_with("reboot", sudo=True, timeout=10.0)
    assert result.status is Status.Success


@pytest.mark.asyncio
async def test_zephyr_soft_reboot_issues_kernel_reboot():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
    with patch.object(ZephyrHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = Results.collect([])
        result = await host.reboot()
    mock_run.assert_awaited_once_with("kernel reboot cold", timeout=10.0)
    assert result.status is Status.Success


@pytest.mark.asyncio
async def test_hard_reboot_cycles_controller():
    ctrl = _FakeController(state=PowerState.ON)
    host = _local_with_controller(ctrl)
    result = await host.reboot(hard=True)
    assert result.status is Status.Success
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

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
    )
    with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = Results.collect([])
        result = await host.shutdown()
    mock_run.assert_awaited_once_with("shutdown -h now", sudo=True, timeout=10.0)
    assert result.status is Status.Success


@pytest.mark.asyncio
async def test_localhost_is_reachable_true():
    from otto.host.local_host import LocalHost

    assert await LocalHost().is_reachable() is True


@pytest.mark.asyncio
async def test_remote_is_reachable_reflects_verify_connection(monkeypatch):
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
    )
    monkeypatch.setattr(
        host,
        "verify_connection",
        AsyncMock(
            return_value=CommandResult(
                status=Status.Success, value="ok", command="connect", retcode=0
            )
        ),
    )
    assert await host.is_reachable() is True
    monkeypatch.setattr(
        host,
        "verify_connection",
        AsyncMock(
            return_value=CommandResult(
                status=Status.Error, value="no", command="connect", retcode=1
            )
        ),
    )
    assert await host.is_reachable() is False


@pytest.mark.asyncio
async def test_probe_less_remote_subclass_fails_with_a_named_error():
    """verify_connection's template must convert a missing probe into a
    DIAGNOSABLE Error result, not an empty one.

    The template's ``except`` turns the abstract ``_probe_connection`` raise
    into ``CommandResult(Error, value=str(e))`` — a bare ``NotImplementedError``
    would surface as an empty message, which is exactly wrong for the one
    audience that hits this path (subclass authors who forgot the override).
    """
    from otto.host.remote_host import RemoteHost

    class ProbeLess(RemoteHost):
        def _log_command(self, msg, log=None):
            pass

    host = ProbeLess()
    result = await host.verify_connection()
    assert result.status is Status.Error
    assert "ProbeLess" in result.value
    assert "_probe_connection" in result.value
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

    async def went_down(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(host), "wait_until_down", went_down)

    async def never_up(self, timeout, interval=2.0):
        return False

    monkeypatch.setattr(type(host), "wait_until_up", never_up)
    result = await host.reboot(hard=True, wait=True, timeout=0.0)
    assert result.status is Status.Failed
    assert "reachable" in result.msg
    assert ctrl.calls == ["off", "on"]  # the reboot still happened


@pytest.mark.asyncio
async def test_reboot_wait_success_keeps_status(monkeypatch):
    ctrl = _FakeController(state=PowerState.ON)
    host = _local_with_controller(ctrl)

    async def went_down(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(host), "wait_until_down", went_down)

    async def comes_up(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(host), "wait_until_up", comes_up)
    result = await host.reboot(hard=True, wait=True, timeout=0.0)
    assert result.status is Status.Success


# ===========================================================================
# `shutdown` picks the spelling the device has
# ===========================================================================
#
# The gap registry's `shutdown-command` record used to describe a hole:
# `UnixHost.shutdown` emitted `shutdown -h now` on every host, BusyBox has no
# such applet on any of the five matrix rows, and the non-ok result was thrown
# away. Both halves are fixed here, and both are pinned below -- the choice by
# `TestShutdownPicksTheSpellingTheDeviceHas`, the discarded result by
# `TestShutdownReportsWhatTheDeviceAnswered`.
#
# EVERY USERLAND IN THIS BLOCK DECLARES ITS APPLETS, so no test here depends on
# a probe round: `_resolve_once` skips a declared capability, and the runner
# these are built with refuses to answer anything, which is what proves the
# applets came from the declaration. The six non-applet capabilities are left
# undeclared on purpose -- they go unasked and fall back to their assumed
# values, exactly as they would on a host that refused a probe, so a test that
# accidentally started depending on one of them would be depending on a guess.

from otto.host import userland as userland_module
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.options import UserlandOptions
from otto.host.unix_host import BUSYBOX_POWEROFF, GNU_SHUTDOWN, UnixHost, shutdown_command
from otto.host.userland import (
    APPLET_ABSENT,
    APPLET_PRESENT,
    PROBED_APPLETS,
    UNTESTED,
    Gap,
    Userland,
    applet_capability,
    gap_for,
)


async def _no_device(cmd, **kwargs):
    """A runner that cannot answer, so only DECLARED capabilities settle.

    `Userland._send` turns an exception into "could not be asked" rather than
    letting it escape, so this is the cheap way to build a resolved userland
    that reached no network at all.
    """
    raise OSError(f"no device in this test: {cmd!r}")


async def _userland_declaring(**applets: str) -> Userland:
    """A resolved `Userland` whose applet answers are the ones passed in.

    Every name in `PROBED_APPLETS` is declared -- the ones not named default to
    present -- so the applet batch is never issued and `is_settled` answers
    True for all of them.
    """
    declared = {applet_capability(a): applets.get(a, APPLET_PRESENT) for a in PROBED_APPLETS}
    userland = Userland(UserlandOptions(**declared), _no_device)
    await userland.resolve()
    return userland


def _busybox_host(userland: Userland) -> UnixHost:
    """A UnixHost wired to *userland*, so nothing builds one against a real device."""
    host = UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
    )
    host._userland_cache = userland
    return host


class TestShutdownPicksTheSpellingTheDeviceHas:
    """The choice, at the function that makes it.

    Three arms and a fallback, and the two that matter most are the ends: a GNU
    host must be UNCHANGED by this (the expensive direction is otto adapting a
    host that never needed it), and a device with neither name must be refused
    rather than sent something that cannot run.
    """

    @pytest.mark.asyncio
    async def test_a_gnu_host_keeps_the_spelling_it_always_had(self):
        userland = await _userland_declaring(shutdown=APPLET_PRESENT)
        assert shutdown_command(userland) == "shutdown -h now"

    @pytest.mark.asyncio
    async def test_a_device_without_shutdown_is_powered_off_with_poweroff(self):
        """The measured case: `shutdown` absent, `poweroff` present, all five rows."""
        userland = await _userland_declaring(shutdown=APPLET_ABSENT, poweroff=APPLET_PRESENT)
        assert shutdown_command(userland) == "poweroff"

    @pytest.mark.asyncio
    async def test_a_device_with_neither_spelling_is_refused_from_the_record(self):
        """The residue, and the message has to be the record's rather than a copy."""
        userland = await _userland_declaring(shutdown=APPLET_ABSENT, poweroff=APPLET_ABSENT)
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            shutdown_command(userland, host="box")
        message = str(exc_info.value)
        gap = gap_for("shutdown-command")
        assert gap is not None
        assert gap.reason in message
        assert gap.docs_anchor in message
        assert "box" in message

    @pytest.mark.asyncio
    async def test_downgrading_the_record_stops_the_refusal(self, monkeypatch):
        """The half that makes this the TABLE's verdict and not the guard's.

        The caller decides the host is in the measured class; the record decides
        whether that class is refused at all. Downgraded, the device gets the
        command otto sent before any of this existed -- which is the honest
        fallback, because "we no longer claim this is broken" cannot mean
        "refuse anyway".
        """
        gap = gap_for("shutdown-command")
        assert gap is not None
        downgraded = Gap(
            surface=gap.surface,
            status=UNTESTED,
            reason=gap.reason,
            measured_on="",
            queued_for=gap.queued_for,
        )
        monkeypatch.setattr(userland_module, "GAPS", [downgraded])
        userland = await _userland_declaring(shutdown=APPLET_ABSENT, poweroff=APPLET_ABSENT)
        assert shutdown_command(userland) == GNU_SHUTDOWN

    @pytest.mark.asyncio
    async def test_an_unsettled_absence_degrades_rather_than_refusing(self, monkeypatch):
        """The `is_settled` gate, made able to fire.

        An applet currently defaults to `present` when its batch could not be
        asked, so today an unsettled host takes the first arm and this gate is
        structurally redundant. It is written anyway because what makes it
        redundant is a VALUE: flip the default the other way -- exactly what
        this does -- and an sshd that refused otto an exec channel would look
        like a device with no way to power off. Degrading is the answer, because
        a probe that could not be asked is not a measurement.
        """
        monkeypatch.setattr(
            userland_module,
            "_UNASKABLE_DEFAULTS",
            {
                **{
                    k: v
                    for k, v in userland_module._UNASKABLE_DEFAULTS.items()
                    if not k.startswith("applet_")
                },
                **{applet_capability(a): APPLET_ABSENT for a in PROBED_APPLETS},
            },
        )
        # Nothing declared, and the runner answers nothing: every applet is
        # absent AND unsettled, which is the shape the gate is about.
        userland = Userland(UserlandOptions(), _no_device)
        await userland.resolve()
        assert userland.has_applet("shutdown") == APPLET_ABSENT
        assert not userland.is_settled(applet_capability("shutdown"))
        assert shutdown_command(userland) == GNU_SHUTDOWN

    def test_the_two_spellings_are_named_once_each(self):
        """The constants are the product's, so a test cannot pass against its own copy."""
        assert GNU_SHUTDOWN == "shutdown -h now"
        assert BUSYBOX_POWEROFF == "poweroff"


class TestShutdownEmitsTheChosenCommand:
    """End to end through `UnixHost.shutdown`, because the function above is only half.

    A correct chooser wired to nothing would pass every assertion in the class
    above while `shutdown()` went on emitting `shutdown -h now`.
    """

    @pytest.mark.asyncio
    async def test_a_busybox_device_is_sent_poweroff(self):
        host = _busybox_host(
            await _userland_declaring(shutdown=APPLET_ABSENT, poweroff=APPLET_PRESENT)
        )
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = Results.collect([])
            result = await host.shutdown()
        mock_run.assert_awaited_once_with("poweroff", sudo=True, timeout=10.0)
        assert result.status is Status.Success

    @pytest.mark.asyncio
    async def test_a_gnu_host_is_sent_what_it_always_was(self):
        host = _busybox_host(await _userland_declaring(shutdown=APPLET_PRESENT))
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = Results.collect([])
            result = await host.shutdown()
        mock_run.assert_awaited_once_with("shutdown -h now", sudo=True, timeout=10.0)
        assert result.status is Status.Success

    @pytest.mark.asyncio
    async def test_a_device_with_neither_spelling_sends_nothing_at_all(self):
        """Refused BEFORE anything is emitted -- which is what the exception means."""
        host = _busybox_host(
            await _userland_declaring(shutdown=APPLET_ABSENT, poweroff=APPLET_ABSENT)
        )
        with (
            patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run,
            pytest.raises(UnsupportedOnUserlandError),
        ):
            await host.shutdown()
        mock_run.assert_not_awaited()


class TestShutdownReportsWhatTheDeviceAnswered:
    """The second bug on that line: the result was discarded and Success returned.

    `_soft_reboot` tolerates a failed round trip because issuing the command
    races the transport being torn down, and shutdown races it identically --
    so the tolerance stays. What does NOT stay is tolerating an answer: a
    completed round trip that exited non-zero is the device saying no, and
    `shutdown()` has no down-wait behind it to catch that later the way
    `reboot(wait=True)` does.
    """

    @pytest.mark.asyncio
    async def test_a_non_zero_exit_is_reported_as_failure(self):
        host = _busybox_host(await _userland_declaring(shutdown=APPLET_PRESENT))
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = Results.collect(
                [
                    _fake_command_result(
                        "shutdown -h now", "sudo: a password is required", Status.Failed, 1
                    )
                ]
            )
            result = await host.shutdown()
        assert result.status is Status.Failed
        assert "still running" in result.msg
        assert "password is required" in result.msg

    @pytest.mark.asyncio
    async def test_a_transport_error_is_the_disconnect_race_and_still_succeeds(self):
        """`Status.Error` is what a timeout, an EOF or a lost connection becomes.

        `SessionManager.run_cmd` returns all three as `Error` rather than
        raising, and none of them is evidence the device disobeyed -- the
        command it was obeying is what killed the transport. Reporting these as
        failures would fail every successful shutdown on a fast host.
        """
        host = _busybox_host(await _userland_declaring(shutdown=APPLET_PRESENT))
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = Results.collect(
                [_fake_command_result("shutdown -h now", "EOF", Status.Error, -1)]
            )
            result = await host.shutdown()
        assert result.status is Status.Success

    @pytest.mark.asyncio
    async def test_a_dropped_connection_still_succeeds(self):
        host = _busybox_host(await _userland_declaring(shutdown=APPLET_PRESENT))
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = ConnectionResetError("transport gone")
            result = await host.shutdown()
        assert result.status is Status.Success

    @pytest.mark.asyncio
    async def test_a_refused_elevation_is_not_swallowed(self):
        """The same clause `_soft_reboot` carries, and for the same reason.

        A userland with no elevation raises before anything is sent. Folding
        that into the disconnect handler would report a shutdown on a host that
        is still running -- and there is no `wait` here for anything downstream
        to notice.
        """
        host = _busybox_host(await _userland_declaring(shutdown=APPLET_PRESENT))
        with patch.object(UnixHost, "run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = UnsupportedOnUserlandError("no elevation on this userland")
            with pytest.raises(UnsupportedOnUserlandError):
                await host.shutdown()
