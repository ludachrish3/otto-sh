"""Tests for per-command options via ``ShellCommand`` and the unified
``Results`` return type of :meth:`BaseHost.run`.

These cover the inheritance rules (per-``ShellCommand`` field > run-kwarg >
``None``), budget-cap interaction for list-form calls, scalar ``Expect``
normalization, and the ``Results.only`` convenience accessor.
"""

from unittest.mock import AsyncMock, patch

import pytest

from otto.host import Results, ShellCommand
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


@pytest.fixture
def host() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="user", password="pass")], log=LogMode.QUIET
    )


@pytest.fixture
def ok() -> CommandResult:
    return CommandResult(command="cmd", value="ok", status=Status.Success, retcode=0)


class TestShellCommandConstruction:
    def test_defaults(self):
        sc = ShellCommand(cmd="ls")
        assert sc.cmd == "ls"
        assert sc.expects is None
        assert sc.timeout is None

    def test_with_timeout(self):
        sc = ShellCommand(cmd="reboot", timeout=120.0)
        assert sc.timeout == 120.0

    def test_with_expects_scalar(self):
        sc = ShellCommand(cmd="sudo ls", expects=("Password:", "pw\n"))
        assert sc.expects == ("Password:", "pw\n")

    def test_with_expects_list(self):
        sc = ShellCommand(cmd="sudo ls", expects=[("Password:", "pw\n")])
        assert sc.expects == [("Password:", "pw\n")]


class TestResultsOnly:
    def test_only_returns_single_status(self):
        cs = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        result = Results.collect([cs])
        assert result.only is cs

    def test_only_raises_when_empty(self):
        result = Results.collect([])
        with pytest.raises(ValueError, match="exactly 1 command result"):
            _ = result.only

    def test_only_raises_when_multiple(self):
        cs1 = CommandResult(status=Status.Success, value="", command="a", retcode=0)
        cs2 = CommandResult(status=Status.Success, value="", command="b", retcode=0)
        result = Results.collect([cs1, cs2])
        with pytest.raises(ValueError, match="exactly 1 command result"):
            _ = result.only


class TestRunInputForms:
    @pytest.mark.asyncio
    async def test_run_string_single(self, host: UnixHost, ok: CommandResult):
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run("ls")
        mock.assert_called_once_with(
            "ls", expects=None, timeout=DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL, user=None
        )
        assert isinstance(result, Results)
        assert len(result) == 1
        assert result.only is ok

    @pytest.mark.asyncio
    async def test_run_shell_command_single(self, host: UnixHost, ok: CommandResult):
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run(ShellCommand(cmd="ls"))
        mock.assert_called_once_with(
            "ls", expects=None, timeout=DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL, user=None
        )
        assert len(result) == 1
        assert result.only is ok

    @pytest.mark.asyncio
    async def test_run_shell_command_list(self, host: UnixHost, ok: CommandResult):
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run([ShellCommand(cmd="a"), ShellCommand(cmd="b")])
        assert mock.call_count == 2
        assert [c.args[0] for c in mock.call_args_list] == ["a", "b"]
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_run_mixed_list(self, host: UnixHost, ok: CommandResult):
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(["a", ShellCommand(cmd="b", timeout=2.0)])
        assert mock.call_count == 2
        actual0 = mock.call_args_list[0].kwargs["timeout"]
        # No run-level timeout given → list form budgets DEFAULT_COMMAND_TIMEOUT,
        # so "a" (no per-command timeout) receives a budget-derived value just
        # under the full default, never the default itself.
        assert 0 < actual0 <= DEFAULT_COMMAND_TIMEOUT
        # ShellCommand's own timeout (2.0) is well within that budget, so it wins.
        assert mock.call_args_list[1].kwargs["timeout"] == 2.0


class TestTimeoutInheritance:
    @pytest.mark.asyncio
    async def test_shell_command_inherits_run_kwarg(self, host: UnixHost, ok: CommandResult):
        """ShellCommand.timeout=None → run-kwarg timeout is used."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd="x"), timeout=5.0)
        mock.assert_called_once_with("x", expects=None, timeout=5.0, log=LogMode.NORMAL, user=None)

    @pytest.mark.asyncio
    async def test_shell_command_overrides_run_kwarg(self, host: UnixHost, ok: CommandResult):
        """ShellCommand.timeout=2 beats run-kwarg timeout=5 in single-cmd form."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd="x", timeout=2.0), timeout=5.0)
        mock.assert_called_once_with("x", expects=None, timeout=2.0, log=LogMode.NORMAL, user=None)

    @pytest.mark.asyncio
    async def test_budget_caps_per_command_timeout(self, host: UnixHost, ok: CommandResult):
        """In list form, ShellCommand.timeout is bounded by remaining budget."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd="x", timeout=100.0)], timeout=1.0)
        actual = mock.call_args.kwargs["timeout"]
        assert actual is not None
        assert 0 < actual <= 1.0, f"expected timeout bounded by 1.0s budget, got {actual}"

    @pytest.mark.asyncio
    async def test_default_timeout_everywhere(self, host: UnixHost, ok: CommandResult):
        """No timeout anywhere → the default becomes the cumulative budget."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd="x")])
        actual = mock.call_args.kwargs["timeout"]
        assert 0 < actual <= DEFAULT_COMMAND_TIMEOUT


class TestExpectsInheritance:
    @pytest.mark.asyncio
    async def test_run_level_expects_inherits_to_commands_without_own(
        self, host: UnixHost, ok: CommandResult
    ):
        """Run-level expects is a default that each command without its own inherits."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(
                [
                    ShellCommand(cmd="a"),
                    ShellCommand(cmd="b", expects=[("P:", "y\n")]),
                ],
                expects=[("P:", "x\n")],
            )
        assert mock.call_args_list[0].kwargs["expects"] == [("P:", "x\n")]
        assert mock.call_args_list[1].kwargs["expects"] == [("P:", "y\n")]

    @pytest.mark.asyncio
    async def test_scalar_expects_wrapped_for_run_one(self, host: UnixHost, ok: CommandResult):
        """A scalar Expect tuple passed to run() is normalized to a list."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run("sudo ls", expects=("Password:", "pw\n"))
        mock.assert_called_once_with(
            "sudo ls",
            expects=[("Password:", "pw\n")],
            timeout=DEFAULT_COMMAND_TIMEOUT,
            log=LogMode.NORMAL,
            user=None,
        )

    @pytest.mark.asyncio
    async def test_scalar_expects_on_shell_command(self, host: UnixHost, ok: CommandResult):
        """A scalar Expect tuple on a ShellCommand is normalized too."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd="x", expects=("P:", "y\n")))
        mock.assert_called_once_with(
            "x",
            expects=[("P:", "y\n")],
            timeout=DEFAULT_COMMAND_TIMEOUT,
            log=LogMode.NORMAL,
            user=None,
        )


from otto.host.host import _resolve_command


class TestRunForwardsLog:
    @pytest.mark.asyncio
    async def test_run_forwards_run_level_log(self, host, ok):
        with patch.object(host, "_run_one", new=AsyncMock(return_value=ok)) as m:
            await host.run("ls", log=LogMode.QUIET)
        _, kwargs = m.await_args
        assert kwargs["log"] is LogMode.QUIET

    @pytest.mark.asyncio
    async def test_run_per_command_log_in_batch(self, host, ok):
        with patch.object(host, "_run_one", new=AsyncMock(return_value=ok)) as m:
            await host.run([ShellCommand("a", log=LogMode.QUIET), "b"], log=LogMode.NORMAL)
        logs = [c.kwargs["log"] for c in m.await_args_list]
        assert logs == [LogMode.QUIET, LogMode.NORMAL]

    @pytest.mark.asyncio
    async def test_run_default_log_is_normal(self, host, ok):
        with patch.object(host, "_run_one", new=AsyncMock(return_value=ok)) as m:
            await host.run("ls")
        _, kwargs = m.await_args
        assert kwargs["log"] is LogMode.NORMAL


class TestShellCommandLog:
    def test_log_defaults_to_none(self):
        assert ShellCommand(cmd="ls").log is None

    def test_log_explicit_quiet(self):
        assert ShellCommand(cmd="dump", log=LogMode.QUIET).log is LogMode.QUIET

    def test_resolve_inherits_run_level_log(self):
        sc = _resolve_command("ls", None, None, default_log=LogMode.QUIET)
        assert sc.log is LogMode.QUIET

    def test_resolve_per_command_log_overrides_default(self):
        sc = _resolve_command(
            ShellCommand("ls", log=LogMode.NORMAL), None, None, default_log=LogMode.QUIET
        )
        assert sc.log is LogMode.NORMAL

    def test_resolve_none_log_falls_back_to_default(self):
        sc = _resolve_command(ShellCommand("ls"), None, None, default_log=LogMode.QUIET)
        assert sc.log is LogMode.QUIET


def test_shellcommand_log_defaults_to_none_and_inherits_normal():
    sc = _resolve_command("echo hi", None, None)
    assert sc.log is LogMode.NORMAL


def test_resolve_command_inherits_explicit_mode():
    sc = _resolve_command(ShellCommand("x", log=LogMode.QUIET), None, None)
    assert sc.log is LogMode.QUIET


def test_resolve_command_uses_default_mode():
    sc = _resolve_command("x", None, None, default_log=LogMode.NEVER)
    assert sc.log is LogMode.NEVER


class TestResolveCommandValidatesTimeout:
    """``ShellCommand`` is public and exported, so ``item.timeout`` is exactly
    the caller-supplied value a type checker never sees. ``_resolve_command``
    must reject a bad non-``None`` value rather than forwarding it straight
    to ``asyncio.wait_for``.
    """

    def test_nan_timeout_raises(self):
        with pytest.raises(ValueError, match="must not be NaN"):
            _resolve_command(ShellCommand("x", timeout=float("nan")), None, None)

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            _resolve_command(ShellCommand("x", timeout=-1), None, None)

    def test_none_timeout_passes_through_to_default(self):
        """Guard against over-tightening: None must keep meaning 'inherit'."""
        sc = _resolve_command(ShellCommand("x", timeout=None), None, 5.0)
        assert sc.timeout == 5.0

    def test_valid_timeout_is_preserved(self):
        sc = _resolve_command(ShellCommand("x", timeout=2.0), None, None)
        assert sc.timeout == 2.0


class TestShellCommandTimeoutValidationViaRun:
    """Same guard, exercised through ``Host.run`` in both call forms."""

    @pytest.mark.asyncio
    async def test_nan_timeout_raises_single_command(self, host: UnixHost):
        with (
            patch.object(host, "_run_one", new_callable=AsyncMock) as mock,
            pytest.raises(ValueError, match="must not be NaN"),
        ):
            await host.run(ShellCommand(cmd="x", timeout=float("nan")))
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_timeout_raises_single_command(self, host: UnixHost):
        with (
            patch.object(host, "_run_one", new_callable=AsyncMock) as mock,
            pytest.raises(ValueError, match="must be >= 0"),
        ):
            await host.run(ShellCommand(cmd="x", timeout=-1))
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nan_timeout_raises_list_form(self, host: UnixHost, ok: CommandResult):
        with (
            patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock,
            pytest.raises(ValueError, match="must not be NaN"),
        ):
            await host.run(["a", ShellCommand(cmd="b", timeout=float("nan"))])
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_timeout_raises_list_form(self, host: UnixHost, ok: CommandResult):
        with (
            patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock,
            pytest.raises(ValueError, match="must be >= 0"),
        ):
            await host.run(["a", ShellCommand(cmd="b", timeout=-1)])
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_timeout_still_inherits_single_command(
        self, host: UnixHost, ok: CommandResult
    ):
        """Guard against over-tightening: None must keep meaning 'inherit'."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd="x", timeout=None), timeout=5.0)
        mock.assert_called_once_with("x", expects=None, timeout=5.0, log=LogMode.NORMAL, user=None)

    @pytest.mark.asyncio
    async def test_none_timeout_still_inherits_list_form(self, host: UnixHost, ok: CommandResult):
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd="x", timeout=None)], timeout=5.0)
        actual = mock.call_args.kwargs["timeout"]
        assert 0 < actual <= 5.0
