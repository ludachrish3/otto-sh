"""Tests for per-command options via ``ShellCommand`` and the unified
``RunResult`` return type of :meth:`BaseHost.run`.

These cover the inheritance rules (per-``ShellCommand`` field > run-kwarg >
``None``), budget-cap interaction for list-form calls, scalar ``Expect``
normalization, and the ``RunResult.only`` convenience accessor.
"""

import pytest
from unittest.mock import AsyncMock, patch

from otto.host import RunResult, ShellCommand
from otto.host.remoteHost import RemoteHost
from otto.utils import CommandStatus, Status


@pytest.fixture
def host() -> RemoteHost:
    return RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)


@pytest.fixture
def ok() -> CommandStatus:
    return CommandStatus(command='cmd', output='ok', status=Status.Success, retcode=0)


class TestShellCommandConstruction:

    def test_defaults(self):
        sc = ShellCommand(cmd='ls')
        assert sc.cmd == 'ls'
        assert sc.expects is None
        assert sc.timeout is None

    def test_with_timeout(self):
        sc = ShellCommand(cmd='reboot', timeout=120.0)
        assert sc.timeout == 120.0

    def test_with_expects_scalar(self):
        sc = ShellCommand(cmd='sudo ls', expects=('Password:', 'pw\n'))
        assert sc.expects == ('Password:', 'pw\n')

    def test_with_expects_list(self):
        sc = ShellCommand(cmd='sudo ls', expects=[('Password:', 'pw\n')])
        assert sc.expects == [('Password:', 'pw\n')]


class TestRunResultOnly:

    def test_only_returns_single_status(self):
        cs = CommandStatus('x', '', Status.Success, 0)
        result = RunResult(status=Status.Success, statuses=[cs])
        assert result.only is cs

    def test_only_raises_when_empty(self):
        result = RunResult(status=Status.Success, statuses=[])
        with pytest.raises(ValueError):
            result.only

    def test_only_raises_when_multiple(self):
        cs1 = CommandStatus('a', '', Status.Success, 0)
        cs2 = CommandStatus('b', '', Status.Success, 0)
        result = RunResult(status=Status.Success, statuses=[cs1, cs2])
        with pytest.raises(ValueError):
            result.only


class TestRunInputForms:

    @pytest.mark.asyncio
    async def test_run_string_single(self, host: RemoteHost, ok: CommandStatus):
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run('ls')
        mock.assert_called_once_with('ls', expects=None, timeout=None)
        assert isinstance(result, RunResult)
        assert len(result.statuses) == 1
        assert result.only is ok

    @pytest.mark.asyncio
    async def test_run_shell_command_single(self, host: RemoteHost, ok: CommandStatus):
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run(ShellCommand(cmd='ls'))
        mock.assert_called_once_with('ls', expects=None, timeout=None)
        assert len(result.statuses) == 1
        assert result.only is ok

    @pytest.mark.asyncio
    async def test_run_shell_command_list(self, host: RemoteHost, ok: CommandStatus):
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            result = await host.run([ShellCommand(cmd='a'), ShellCommand(cmd='b')])
        assert mock.call_count == 2
        assert [c.args[0] for c in mock.call_args_list] == ['a', 'b']
        assert len(result.statuses) == 2

    @pytest.mark.asyncio
    async def test_run_mixed_list(self, host: RemoteHost, ok: CommandStatus):
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(['a', ShellCommand(cmd='b', timeout=2.0)])
        assert mock.call_count == 2
        assert mock.call_args_list[0].kwargs['timeout'] is None
        # Run-level timeout is None → only ShellCommand's own timeout applies
        assert mock.call_args_list[1].kwargs['timeout'] == 2.0


class TestTimeoutInheritance:

    @pytest.mark.asyncio
    async def test_shell_command_inherits_run_kwarg(self, host: RemoteHost, ok: CommandStatus):
        """ShellCommand.timeout=None → run-kwarg timeout is used."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd='x'), timeout=5.0)
        mock.assert_called_once_with('x', expects=None, timeout=5.0)

    @pytest.mark.asyncio
    async def test_shell_command_overrides_run_kwarg(self, host: RemoteHost, ok: CommandStatus):
        """ShellCommand.timeout=2 beats run-kwarg timeout=5 in single-cmd form."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd='x', timeout=2.0), timeout=5.0)
        mock.assert_called_once_with('x', expects=None, timeout=2.0)

    @pytest.mark.asyncio
    async def test_budget_caps_per_command_timeout(self, host: RemoteHost, ok: CommandStatus):
        """In list form, ShellCommand.timeout is bounded by remaining budget."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd='x', timeout=100.0)], timeout=1.0)
        actual = mock.call_args.kwargs['timeout']
        assert actual is not None
        assert 0 < actual <= 1.0, f'expected timeout bounded by 1.0s budget, got {actual}'

    @pytest.mark.asyncio
    async def test_none_timeout_everywhere(self, host: RemoteHost, ok: CommandStatus):
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd='x')])
        mock.assert_called_once_with('x', expects=None, timeout=None)


class TestExpectsInheritance:

    @pytest.mark.asyncio
    async def test_run_level_expects_inherits_to_commands_without_own(
        self, host: RemoteHost, ok: CommandStatus
    ):
        """Run-level expects is a default that each command without its own inherits."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(
                [
                    ShellCommand(cmd='a'),
                    ShellCommand(cmd='b', expects=[('P:', 'y\n')]),
                ],
                expects=[('P:', 'x\n')],
            )
        assert mock.call_args_list[0].kwargs['expects'] == [('P:', 'x\n')]
        assert mock.call_args_list[1].kwargs['expects'] == [('P:', 'y\n')]

    @pytest.mark.asyncio
    async def test_scalar_expects_wrapped_for_run_one(
        self, host: RemoteHost, ok: CommandStatus
    ):
        """A scalar Expect tuple passed to run() is normalized to a list."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run('sudo ls', expects=('Password:', 'pw\n'))
        mock.assert_called_once_with(
            'sudo ls', expects=[('Password:', 'pw\n')], timeout=None
        )

    @pytest.mark.asyncio
    async def test_scalar_expects_on_shell_command(
        self, host: RemoteHost, ok: CommandStatus
    ):
        """A scalar Expect tuple on a ShellCommand is normalized too."""
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(ShellCommand(cmd='x', expects=('P:', 'y\n')))
        mock.assert_called_once_with('x', expects=[('P:', 'y\n')], timeout=None)
