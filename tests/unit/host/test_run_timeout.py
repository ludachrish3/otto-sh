"""Tests for the cumulative timeout parameter of BaseHost.run().

The deadline-based budget distributes a single timeout across sequential
commands: each command receives the remaining time, and fast commands
donate surplus to slower ones.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from otto.host.localHost import LocalHost
from otto.host.remoteHost import RemoteHost
from otto.utils import CommandStatus, Status


# ---------------------------------------------------------------------------
# Unit tests (mocked — fast, deterministic)
# ---------------------------------------------------------------------------

@pytest.fixture
def host() -> RemoteHost:
    """Bare RemoteHost, no connections established."""
    return RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)


class TestRunTimeout:
    """Unit tests for deadline-based timeout propagation."""

    @pytest.mark.asyncio
    async def test_no_timeout_passes_none_to_run_one(self, host: RemoteHost):
        """Without timeout, _run_one receives no explicit timeout."""
        ok = CommandStatus('echo hi', 'hi', Status.Success, 0)
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(['echo hi'])
        mock.assert_called_once_with('echo hi', expects=None, timeout=None)

    @pytest.mark.asyncio
    async def test_timeout_passes_remaining_to_run_one(self, host: RemoteHost):
        """With a timeout, each _run_one receives the remaining budget."""
        ok = CommandStatus('cmd', 'ok', Status.Success, 0)
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(['cmd1', 'cmd2'], timeout=10.0)

        assert mock.call_count == 2
        # First call should get ~10s, second should get slightly less
        first_timeout = mock.call_args_list[0].kwargs['timeout']
        second_timeout = mock.call_args_list[1].kwargs['timeout']
        assert first_timeout > 9.0  # nearly full budget
        assert second_timeout > 0   # still has remaining time
        assert first_timeout > second_timeout  # budget decreases

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_remaining_commands(self, host: RemoteHost):
        """When the budget runs out, remaining commands are skipped."""
        async def slow_cmd(cmd, **kwargs):
            # Simulate a command that takes nearly all the budget
            await asyncio.sleep(0.3)
            return CommandStatus(cmd, 'ok', Status.Success, 0)

        with patch.object(host, '_run_one', new_callable=AsyncMock, side_effect=slow_cmd):
            result = await host.run(
                ['slow1', 'slow2', 'skipped'],
                timeout=0.5,
            )

        # First two might run; third should be skipped with Status.Error
        assert result.status == Status.Error
        skipped = [r for r in result.statuses if 'budget exhausted' in r.output]
        assert len(skipped) >= 1

    @pytest.mark.asyncio
    async def test_fast_commands_donate_surplus(self, host: RemoteHost):
        """Fast commands leave surplus for later commands."""
        call_timeouts: list[float] = []

        async def track_timeout(cmd, **kwargs):
            call_timeouts.append(kwargs.get('timeout'))
            return CommandStatus(cmd, 'ok', Status.Success, 0)

        with patch.object(host, '_run_one', new_callable=AsyncMock, side_effect=track_timeout):
            await host.run(['fast1', 'fast2', 'fast3'], timeout=5.0)

        # All three should get nearly the full budget since each is instant
        assert len(call_timeouts) == 3
        for t in call_timeouts:
            assert t > 4.5, f'Expected > 4.5s remaining, got {t}'


# ---------------------------------------------------------------------------
# Integration tests (LocalHost — real session, real shell)
# ---------------------------------------------------------------------------

class TestRunTimeoutIntegration:
    """Integration tests using real LocalHost shell sessions."""

    @pytest.mark.asyncio
    async def test_all_commands_complete_within_budget(self):
        """Fast commands all complete when given a generous budget."""
        host = LocalHost()
        try:
            result = await host.run(
                ['echo one', 'echo two', 'echo three'],
                timeout=10.0,
            )
            assert result.status == Status.Success
            assert len(result.statuses) == 3
            assert all(r.status == Status.Success for r in result.statuses)
            assert 'one' in result.statuses[0].output
            assert 'two' in result.statuses[1].output
            assert 'three' in result.statuses[2].output
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_slow_command_times_out_and_session_recovers(self):
        """A slow command triggers timeout recovery, and the session stays usable."""
        host = LocalHost()
        try:
            # sleep 10 will exceed the 1s budget — _run_one's wait_for fires,
            # triggers Ctrl+C recovery, returns Status.Error
            result = await host.run(
                ['sleep 10', 'echo after'],
                timeout=1.0,
            )
            assert result.statuses[0].status == Status.Error
            assert 'timed out' in result.statuses[0].output.lower()

            # 'echo after' should be skipped (budget exhausted) or timed out
            if len(result.statuses) > 1:
                assert result.statuses[1].status == Status.Error

            # Session should still be healthy — verify by running another command
            result2 = (await host.run('echo recovered')).only
            assert result2.status == Status.Success
            assert 'recovered' in result2.output
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_no_timeout_preserves_default_behavior(self):
        """Without timeout, run behaves exactly as before."""
        host = LocalHost()
        try:
            result = await host.run(['echo hello', 'echo world'])
            assert result.status == Status.Success
            assert len(result.statuses) == 2
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_surplus_time_donated_to_later_commands(self):
        """Fast early commands leave enough budget for a slightly slow later one."""
        host = LocalHost()
        try:
            # 3s total budget: two instant commands + one 0.5s sleep
            # Without donation, each would get 1s — still enough
            # but the point is the later command gets nearly the full 3s
            result = await host.run(
                ['echo fast1', 'echo fast2', 'sleep 0.5 && echo done'],
                timeout=3.0,
            )
            assert result.status == Status.Success
            assert len(result.statuses) == 3
            assert all(r.status == Status.Success for r in result.statuses)
            assert 'done' in result.statuses[2].output
        finally:
            await host.close()
