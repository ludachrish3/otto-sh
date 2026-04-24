"""Tests for netcat port-finding and listener-check strategies."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import RunResult
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions, ScpOptions
from otto.host.transfer import FileTransfer
from otto.utils import CommandStatus, Status


def make_ft(
    *,
    nc_port_strategy: str = 'auto',
    nc_port_cmd: str | None = None,
    nc_listener_check: str = 'auto',
    nc_listener_cmd: str | None = None,
    nc_port: int = 9000,
    exec_cmd: AsyncMock | None = None,
) -> FileTransfer:
    """Build a FileTransfer with mocked dependencies for strategy tests."""
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = False
    mock_connections.ip = '10.0.0.1'
    mock_connections.term = 'ssh'
    return FileTransfer(
        connections=mock_connections,
        name='test',
        transfer='nc',
        nc_options=NcOptions(
            exec_name='nc',
            port=nc_port,
            port_strategy=nc_port_strategy,  # type: ignore[arg-type]
            port_cmd=nc_port_cmd,
            listener_check=nc_listener_check,  # type: ignore[arg-type]
            listener_cmd=nc_listener_cmd,
        ),
        scp_options=ScpOptions(),
        get_local_ip=lambda: '127.0.0.1',
        open_session=AsyncMock(),
        exec_cmd=exec_cmd or AsyncMock(),
    )


def _ok(output: str = '') -> CommandStatus:
    return CommandStatus(command='', output=output, status=Status.Success, retcode=0)


def _fail(output: str = '') -> CommandStatus:
    return CommandStatus(command='', output=output, status=Status.Failed, retcode=1)


# ============================================================================
# Port-finding strategies
# ============================================================================


class TestFindFreePortSs:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok('9001\n'))
        ft = make_ft(nc_port_strategy='ss', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9001
        assert port in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail('ss: command not found'))
        ft = make_ft(nc_port_strategy='ss', exec_cmd=mock_exec)
        with pytest.raises(RuntimeError, match='ss port scan failed'):
            await ft._find_free_port()


class TestFindFreePortNetstat:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok('9002\n'))
        ft = make_ft(nc_port_strategy='netstat', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9002

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail(''))
        ft = make_ft(nc_port_strategy='netstat', exec_cmd=mock_exec)
        with pytest.raises(RuntimeError, match='netstat port scan failed'):
            await ft._find_free_port()


class TestFindFreePortPython:

    @pytest.mark.asyncio
    async def test_python_succeeds(self):
        """First try (``python``) succeeds."""
        mock_exec = AsyncMock(return_value=_ok('54321\n'))
        ft = make_ft(nc_port_strategy='python', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 54321
        # Should have called `python -c ...`
        cmd = mock_exec.call_args_list[0][0][0]
        assert cmd.startswith('python -c')

    @pytest.mark.asyncio
    async def test_python_fails_python3_succeeds(self):
        """``python`` fails, ``python3`` succeeds."""
        mock_exec = AsyncMock(side_effect=[_fail('not found'), _ok('12345\n')])
        ft = make_ft(nc_port_strategy='python', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 12345
        assert mock_exec.call_count == 2
        cmd2 = mock_exec.call_args_list[1][0][0]
        assert cmd2.startswith('python3 -c')

    @pytest.mark.asyncio
    async def test_both_fail(self):
        mock_exec = AsyncMock(side_effect=[_fail(''), _fail('')])
        ft = make_ft(nc_port_strategy='python', exec_cmd=mock_exec)
        with pytest.raises(RuntimeError, match='python port discovery failed'):
            await ft._find_free_port()


class TestFindFreePortProc:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok('9003\n'))
        ft = make_ft(nc_port_strategy='proc', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9003

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail(''))
        ft = make_ft(nc_port_strategy='proc', exec_cmd=mock_exec)
        with pytest.raises(RuntimeError, match='/proc/net/tcp port scan failed'):
            await ft._find_free_port()


class TestFindFreePortCustom:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok('7777\n'))
        ft = make_ft(nc_port_strategy='custom', nc_port_cmd='my_port_finder', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 7777
        mock_exec.assert_awaited_once_with('my_port_finder')

    @pytest.mark.asyncio
    async def test_none_cmd_raises(self):
        ft = make_ft(nc_port_strategy='custom', nc_port_cmd=None)
        with pytest.raises(ValueError, match="nc_port_cmd is None"):
            await ft._find_free_port()


class TestFindFreePortAuto:

    @pytest.mark.asyncio
    async def test_probe_picks_strategy_and_caches(self):
        """Compound probe resolves `python` on a host without ss/netstat."""
        async def mock_exec(cmd, **kw):
            if 'command -v' in cmd:
                # Host has python but not ss/netstat.
                return _ok('python proc\n')
            if cmd.startswith('python -c'):
                return _ok('33333\n')
            return _fail('unexpected: ' + cmd)

        ft = make_ft(nc_port_strategy='auto', exec_cmd=AsyncMock(side_effect=mock_exec))
        port = await ft._find_free_port()
        assert port == 33333
        assert ft._resolved_port_strategy == 'python'

    @pytest.mark.asyncio
    async def test_uses_cached_strategy(self):
        """Second call uses the cached strategy directly — no re-probe."""
        mock_exec = AsyncMock(return_value=_ok('44444\n'))
        ft = make_ft(nc_port_strategy='auto', exec_cmd=mock_exec)
        ft._resolved_port_strategy = 'ss'
        port = await ft._find_free_port()
        assert port == 44444
        # Only one call: the ss port scan. No probe, no cascade.
        assert mock_exec.call_count == 1
        assert 'command -v' not in mock_exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_probe_failure_falls_back_to_cascade(self):
        """If the compound probe can't resolve, cascade still works."""
        call_log: list[str] = []

        async def mock_exec(cmd, **kw):
            call_log.append(cmd)
            if 'command -v' in cmd:
                return _fail('probe blew up')
            if 'ss -tln' in cmd:
                return _fail('no ss')
            if 'netstat -tln' in cmd:
                return _fail('no netstat')
            if cmd.startswith('python -c'):
                return _ok('55555\n')
            return _fail('unexpected: ' + cmd)

        ft = make_ft(nc_port_strategy='auto', exec_cmd=AsyncMock(side_effect=mock_exec))
        port = await ft._find_free_port()
        assert port == 55555
        assert ft._resolved_port_strategy == 'python'

    @pytest.mark.asyncio
    async def test_all_fail_raises(self):
        """All strategies fail — raises RuntimeError."""
        mock_exec = AsyncMock(return_value=_fail('nope'))
        ft = make_ft(nc_port_strategy='auto', exec_cmd=mock_exec)
        with pytest.raises(RuntimeError, match='All port-finding strategies failed'):
            await ft._find_free_port()


# ============================================================================
# Reserved ports
# ============================================================================


class TestReservedPorts:

    @pytest.mark.asyncio
    async def test_port_reserved_after_find(self):
        mock_exec = AsyncMock(return_value=_ok('9000\n'))
        ft = make_ft(nc_port_strategy='ss', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_release_port(self):
        mock_exec = AsyncMock(return_value=_ok('9000\n'))
        ft = make_ft(nc_port_strategy='ss', exec_cmd=mock_exec)
        port = await ft._find_free_port()
        ft._release_port(port)
        assert port not in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_reserved_ports_injected_into_script(self):
        """Reserved ports appear in the shell script sent to the remote."""
        mock_exec = AsyncMock(return_value=_ok('9002\n'))
        ft = make_ft(nc_port_strategy='ss', exec_cmd=mock_exec)
        ft._reserved_ports = {9000, 9001}
        await ft._find_free_port()
        cmd = mock_exec.call_args[0][0]
        # The reserved ports should appear in the script.
        assert '9000' in cmd
        assert '9001' in cmd


# ============================================================================
# Listener-check strategies
# ============================================================================


class TestListenerCheckSs:

    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok())
        ft = make_ft(nc_listener_check='ss', exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        assert 'ss -tln' in cmd
        assert '8080' in cmd

    @pytest.mark.asyncio
    async def test_success_after_retry(self):
        mock_exec = AsyncMock(side_effect=[_fail(), _fail(), _ok()])
        ft = make_ft(nc_listener_check='ss', exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert mock_exec.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check='ss', exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match='not ready within'):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckNetstat:

    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok())
        ft = make_ft(nc_listener_check='netstat', exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        assert 'netstat -tln' in cmd

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check='netstat', exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match='not ready within'):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckProc:

    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok())
        ft = make_ft(nc_listener_check='proc', exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        # Port 8080 = 0x1F90
        assert '1F90' in cmd
        assert '/proc/net/tcp' in cmd

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check='proc', exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match='not ready within'):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckCustom:

    @pytest.mark.asyncio
    async def test_uses_port_placeholder(self):
        mock_exec = AsyncMock(return_value=_ok())
        ft = make_ft(nc_listener_check='custom', nc_listener_cmd='check_port {port}', exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(9999)
        cmd = mock_exec.call_args[0][0]
        assert cmd == 'check_port 9999'

    @pytest.mark.asyncio
    async def test_none_cmd_raises(self):
        ft = make_ft(nc_listener_check='custom', nc_listener_cmd=None)
        with pytest.raises(ValueError, match="nc_listener_cmd is None"):
            await ft._wait_for_remote_listener(9999)


class TestListenerCheckAuto:

    @pytest.mark.asyncio
    async def test_resolves_ss_when_available(self):
        """Compound probe picks `ss` when the host has it."""
        async def mock_exec(cmd, **kw):
            if 'command -v' in cmd:
                return _ok('ss ss\n')
            return _ok()  # listener check succeeds immediately

        ft = make_ft(nc_listener_check='auto', exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == 'ss'

    @pytest.mark.asyncio
    async def test_falls_back_to_netstat(self):
        """Host has netstat but not ss — probe returns netstat."""
        async def mock_exec(cmd, **kw):
            if 'command -v' in cmd:
                return _ok('netstat netstat\n')
            return _ok()

        ft = make_ft(nc_listener_check='auto', exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == 'netstat'

    @pytest.mark.asyncio
    async def test_falls_back_to_proc(self):
        """Neither ss nor netstat — probe returns proc for the listener side."""
        async def mock_exec(cmd, **kw):
            if 'command -v' in cmd:
                # Port side could be python; listener falls back to proc.
                return _ok('python proc\n')
            return _ok()

        ft = make_ft(nc_listener_check='auto', exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == 'proc'

    @pytest.mark.asyncio
    async def test_caches_resolved_strategy(self):
        """Second call uses the cached strategy without re-probing."""
        mock_exec = AsyncMock(return_value=_ok())
        ft = make_ft(nc_listener_check='auto', exec_cmd=mock_exec)
        ft._resolved_listener_check = 'netstat'
        await ft._wait_for_remote_listener(8080)
        # Cache hit means no probe at all.
        for call in mock_exec.call_args_list:
            assert 'command -v' not in call[0][0]
            assert not call[0][0].startswith('type ')


# ============================================================================
# prepare() + warm-up
# ============================================================================


class TestPrepareProbe:
    """`prepare()` runs a single compound probe and caches both strategies."""

    @pytest.mark.asyncio
    async def test_single_probe_caches_both(self):
        mock_exec = AsyncMock(return_value=_ok('ss ss\n'))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy == 'ss'
        assert ft._resolved_listener_check == 'ss'
        # Exactly one probe call.
        assert mock_exec.call_count == 1
        cmd = mock_exec.call_args[0][0]
        assert 'command -v' in cmd

    @pytest.mark.asyncio
    async def test_mixed_strategies(self):
        """Probe can return different strategies for port vs listener."""
        mock_exec = AsyncMock(return_value=_ok('python proc\n'))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy == 'python'
        assert ft._resolved_listener_check == 'proc'

    @pytest.mark.asyncio
    async def test_idempotent(self):
        """Second `prepare()` is a no-op when caches are populated."""
        mock_exec = AsyncMock(return_value=_ok('ss ss\n'))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        await ft.prepare()
        await ft.prepare()
        assert mock_exec.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_when_caller_configured_strategies(self):
        """If both strategies are explicit (not 'auto'), prepare runs no probe."""
        mock_exec = AsyncMock()
        ft = make_ft(
            nc_port_strategy='ss',
            nc_listener_check='netstat',
            exec_cmd=mock_exec,
        )
        await ft.prepare()
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_failure_leaves_caches_unset(self):
        """A failing probe must not poison the caches — cascade takes over."""
        mock_exec = AsyncMock(return_value=_fail('/bin/sh: command: not found'))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy is None
        assert ft._resolved_listener_check is None

    @pytest.mark.asyncio
    async def test_concurrent_callers_probe_once(self):
        """Two coroutines calling prepare() in parallel share a single probe."""
        mock_exec = AsyncMock(return_value=_ok('ss ss\n'))
        ft = make_ft(exec_cmd=mock_exec)
        await asyncio.gather(ft.prepare(), ft.prepare(), ft.prepare())
        assert mock_exec.call_count == 1


class TestWarmupForTransfer:
    """`_warmup_for_transfer` runs prepare + pool warming concurrently."""

    @pytest.mark.asyncio
    async def test_ssh_runs_only_prepare(self):
        """On SSH the exec pool doesn't exist, so no `true` calls are needed."""
        mock_exec = AsyncMock(return_value=_ok('ss ss\n'))
        ft = make_ft(exec_cmd=mock_exec)  # defaults to ssh
        await ft._warmup_for_transfer(file_count=3)
        # Just the one compound probe — no `true` pre-warming on ssh.
        assert mock_exec.call_count == 1
        assert 'command -v' in mock_exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_telnet_prewarms_pool_concurrently_with_probe(self):
        """Probe and `true` warmup calls overlap in wall-clock time."""
        order: list[str] = []
        started: asyncio.Event = asyncio.Event()

        async def mock_exec(cmd, **kw):
            order.append(f'start:{cmd[:20]}')
            started.set()
            # Yield so peers can start before we finish.
            await asyncio.sleep(0.01)
            order.append(f'end:{cmd[:20]}')
            if 'command -v' in cmd:
                return _ok('ss ss\n')
            return _ok()

        # Use telnet term so warmup fires pool pre-opens.
        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'telnet'
        # On telnet, prepare() goes through _control_run → opens the monitor
        # session. Give it a working fake monitor so the probe can run.
        monitor_session = MagicMock()
        monitor_session.alive = True
        monitor_session.run = AsyncMock(side_effect=mock_exec)
        monitor_session.close = AsyncMock()

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='auto',
                port_cmd=None,
                listener_check='auto',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(return_value=monitor_session),
            exec_cmd=AsyncMock(side_effect=mock_exec),
        )

        await ft._warmup_for_transfer(file_count=2)

        # 1 probe via monitor + 2 `true` calls via exec pool = 3 invocations.
        total = len(order) // 2  # each cmd has a start + end event
        assert total == 3, f"expected 3 invocations, saw {total}: {order}"

        # At least one peer starts before the first one ends — i.e. the
        # probe and the `true` calls overlap, not run back-to-back.
        first_end = next(i for i, e in enumerate(order) if e.startswith('end:'))
        starts_before_first_end = sum(
            1 for e in order[:first_end] if e.startswith('start:')
        )
        assert starts_before_first_end >= 2, (
            f"warmup didn't overlap; order={order}"
        )

    @pytest.mark.asyncio
    async def test_telnet_warmup_swallows_warmup_failures(self):
        """A failed `true` pre-warm must not take down the transfer."""
        async def mock_exec(cmd, **kw):
            if 'command -v' in cmd:
                return _ok('ss ss\n')
            raise RuntimeError("pool open failed")

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'telnet'
        monitor_session = MagicMock()
        monitor_session.alive = True
        monitor_session.run = AsyncMock(
            return_value=RunResult(status=Status.Success, statuses=[_ok('ss ss\n')])
        )

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='auto',
                port_cmd=None,
                listener_check='auto',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(return_value=monitor_session),
            exec_cmd=AsyncMock(side_effect=mock_exec),
        )

        # Should not raise — prepare succeeded, pool warmup failures are
        # best-effort.
        await ft._warmup_for_transfer(file_count=2)
        assert ft._resolved_port_strategy == 'ss'
