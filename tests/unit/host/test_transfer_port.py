"""Tests for netcat port-finding and listener-check strategies."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.connections import ConnectionManager
from otto.host.errors import CommandValueError, HostCommandError, HostUnreachableError
from otto.host.exec_route import ExecRoute
from otto.host.exit_status import no_return_code_result
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.host.transfer import nc as nc_module
from otto.host.transfer.nc import (
    NcListenerAnswerError,
    NcListenerToolError,
    NcPortRungError,
    NcPortSharedError,
    NcRungToolError,
    read_rung_answer,
)
from otto.host.transfer.nc_scripts import TOOL_FAILED
from otto.host.value_reader import Integer
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.nc_listings import COMPLAINT
from tests._fixtures.nc_listings import tool_failed as _tool_failed


def make_ft(
    *,
    nc_port_strategy: str = "auto",
    nc_port_cmd: str | None = None,
    nc_listener_check: str = "auto",
    nc_listener_cmd: str | None = None,
    nc_port: int = 9000,
    exec_cmd: AsyncMock | None = None,
    term: str = "ssh",
) -> NcFileTransfer:
    """Build a NcFileTransfer with mocked dependencies for strategy tests."""
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = False
    mock_connections.ip = "10.0.0.1"
    mock_connections.term = term
    return NcFileTransfer(
        connections=mock_connections,
        name="test",
        transfer="nc",
        nc_options=NcOptions(
            exec_name="nc",
            port=nc_port,
            port_strategy=nc_port_strategy,  # type: ignore[arg-type]
            port_cmd=nc_port_cmd,
            listener_check=nc_listener_check,  # type: ignore[arg-type]
            listener_cmd=nc_listener_cmd,
        ),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=exec_cmd or AsyncMock(),
        userland=None,
    )


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _fail(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Failed, retcode=1)


def _timed_out(output: str = "") -> CommandResult:
    """A probe killed by its timeout — retcode -1, which is why it needs a flag.

    ``retcode`` cannot express this: -1 also means "never ran" and "skipped".
    """
    return CommandResult(command="", value=output, status=Status.Failed, retcode=-1, timed_out=True)


# ============================================================================
# Port-finding strategies
# ============================================================================


class TestFindFreePortSs:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok("9001\n"))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9001
        assert port in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail("ss: command not found"))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        with pytest.raises(HostCommandError, match="ss port scan failed"):
            await ft._find_free_port()

    @pytest.mark.asyncio
    async def test_timed_out_scan_is_unreachable_not_a_command_failure(self):
        """A scan that never came back says nothing about the host's ports.

        Same message either way — the TYPE is what tells a caller whether to
        look at the remote sockets or at the connection to the host.
        """
        mock_exec = AsyncMock(return_value=_timed_out(""))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        with pytest.raises(HostUnreachableError, match="ss port scan failed"):
            await ft._find_free_port()


class TestFindFreePortNetstat:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok("9002\n"))
        ft = make_ft(nc_port_strategy="netstat", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9002

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail(""))
        ft = make_ft(nc_port_strategy="netstat", exec_cmd=mock_exec)
        with pytest.raises(HostCommandError, match="netstat port scan failed"):
            await ft._find_free_port()


class TestFindFreePortPython:
    @pytest.mark.asyncio
    async def test_python_succeeds(self):
        """First try (``python``) succeeds."""
        mock_exec = AsyncMock(return_value=_ok("54321\n"))
        ft = make_ft(nc_port_strategy="python", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 54321
        # Should have called `python -c ...`
        cmd = mock_exec.call_args_list[0][0][0]
        assert cmd.startswith("python -c")

    @pytest.mark.asyncio
    async def test_python_fails_python3_succeeds(self):
        """``python`` fails, ``python3`` succeeds."""
        mock_exec = AsyncMock(side_effect=[_fail("not found"), _ok("12345\n")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 12345
        assert mock_exec.call_count == 2
        cmd2 = mock_exec.call_args_list[1][0][0]
        assert cmd2.startswith("python3 -c")

    @pytest.mark.asyncio
    async def test_both_fail(self):
        mock_exec = AsyncMock(side_effect=[_fail(""), _fail("")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=mock_exec)
        with pytest.raises(HostCommandError, match="python port discovery failed"):
            await ft._find_free_port()

    @pytest.mark.asyncio
    async def test_both_time_out(self):
        mock_exec = AsyncMock(side_effect=[_timed_out(""), _timed_out("")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=mock_exec)
        with pytest.raises(HostUnreachableError, match="python port discovery failed"):
            await ft._find_free_port()


class TestFindFreePortProc:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok("9003\n"))
        ft = make_ft(nc_port_strategy="proc", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 9003

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_exec = AsyncMock(return_value=_fail(""))
        ft = make_ft(nc_port_strategy="proc", exec_cmd=mock_exec)
        with pytest.raises(HostCommandError, match="/proc/net/tcp port scan failed"):
            await ft._find_free_port()


class TestFindFreePortCustom:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_exec = AsyncMock(return_value=_ok("7777\n"))
        ft = make_ft(nc_port_strategy="custom", nc_port_cmd="my_port_finder", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port == 7777
        mock_exec.assert_awaited_once_with("my_port_finder")

    @pytest.mark.asyncio
    async def test_none_cmd_raises(self):
        ft = make_ft(nc_port_strategy="custom", nc_port_cmd=None)
        with pytest.raises(ValueError, match="nc_port_cmd is None"):
            await ft._find_free_port()


class TestFindFreePortAuto:
    @pytest.mark.asyncio
    async def test_probe_picks_strategy_and_caches(self):
        """Compound probe resolves `python` on a host without ss/netstat."""

        async def mock_exec(cmd, **kw):
            if "command -v" in cmd:
                # Host has python but not ss/netstat.
                return _ok("python proc\n")
            if cmd.startswith("python -c"):
                return _ok("33333\n")
            return _fail("unexpected: " + cmd)

        ft = make_ft(nc_port_strategy="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        port = await ft._find_free_port()
        assert port == 33333
        assert ft._resolved_port_strategy == "python"

    @pytest.mark.asyncio
    async def test_uses_cached_strategy(self):
        """Second call uses the cached strategy directly — no re-probe."""
        mock_exec = AsyncMock(return_value=_ok("44444\n"))
        ft = make_ft(nc_port_strategy="auto", exec_cmd=mock_exec)
        ft._resolved_port_strategy = "ss"
        port = await ft._find_free_port()
        assert port == 44444
        # Only one call: the ss port scan. No probe, no cascade.
        assert mock_exec.call_count == 1
        assert "command -v" not in mock_exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_probe_failure_falls_back_to_cascade(self):
        """If the compound probe can't resolve, cascade still works."""
        call_log: list[str] = []

        async def mock_exec(cmd, **kw):
            call_log.append(cmd)
            if "command -v" in cmd:
                return _fail("probe blew up")
            if "ss -tln" in cmd:
                return _fail("no ss")
            if "netstat -tln" in cmd:
                return _fail("no netstat")
            if cmd.startswith("python -c"):
                return _ok("55555\n")
            return _fail("unexpected: " + cmd)

        ft = make_ft(nc_port_strategy="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        port = await ft._find_free_port()
        assert port == 55555
        assert ft._resolved_port_strategy == "python"

    @pytest.mark.asyncio
    async def test_all_fail_raises(self):
        """All strategies fail — raises HostCommandError naming each attempt."""
        mock_exec = AsyncMock(return_value=_fail("nope"))
        ft = make_ft(nc_port_strategy="auto", exec_cmd=mock_exec)
        with pytest.raises(HostCommandError, match="All port-finding strategies failed"):
            await ft._find_free_port()


# ============================================================================
# Reserved ports
# ============================================================================


class TestReservedPorts:
    @pytest.mark.asyncio
    async def test_port_reserved_after_find(self):
        mock_exec = AsyncMock(return_value=_ok("9000\n"))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        assert port in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_release_port(self):
        mock_exec = AsyncMock(return_value=_ok("9000\n"))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        port = await ft._find_free_port()
        ft._release_port(port)
        assert port not in ft._reserved_ports

    @pytest.mark.asyncio
    async def test_reserved_ports_injected_into_script(self):
        """Reserved ports appear in the shell script sent to the remote."""
        mock_exec = AsyncMock(return_value=_ok("9002\n"))
        ft = make_ft(nc_port_strategy="ss", exec_cmd=mock_exec)
        ft._reserved_ports = {9000, 9001}
        await ft._find_free_port()
        cmd = mock_exec.call_args[0][0]
        # The reserved ports should appear in the script.
        assert "9000" in cmd
        assert "9001" in cmd


# ============================================================================
# Listener-check strategies
# ============================================================================


class TestListenerCheckSs:
    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok("1\n"))
        ft = make_ft(nc_listener_check="ss", exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        assert "ss -tln" in cmd
        assert "8080" in cmd

    @pytest.mark.asyncio
    async def test_success_after_retry(self):
        mock_exec = AsyncMock(side_effect=[_fail(), _fail(), _ok("1\n")])
        ft = make_ft(nc_listener_check="ss", exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert mock_exec.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check="ss", exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match="not ready within"):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckNetstat:
    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok("1\n"))
        ft = make_ft(nc_listener_check="netstat", exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        assert "netstat -tln" in cmd

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check="netstat", exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match="not ready within"):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckProc:
    @pytest.mark.asyncio
    async def test_immediate_success(self):
        mock_exec = AsyncMock(return_value=_ok("1\n"))
        ft = make_ft(nc_listener_check="proc", exec_cmd=mock_exec)
        await ft._wait_for_remote_listener(8080)
        cmd = mock_exec.call_args[0][0]
        # Port 8080 = 0x1F90
        assert "1F90" in cmd
        assert "/proc/net/tcp" in cmd

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        mock_exec = AsyncMock(return_value=_fail())
        ft = make_ft(nc_listener_check="proc", exec_cmd=mock_exec)
        with pytest.raises(ConnectionError, match="not ready within"):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)


class TestListenerCheckCustom:
    @pytest.mark.asyncio
    async def test_uses_port_placeholder(self):
        mock_exec = AsyncMock(return_value=_ok("1\n"))
        ft = make_ft(
            nc_listener_check="custom", nc_listener_cmd="check_port {port}", exec_cmd=mock_exec
        )
        await ft._wait_for_remote_listener(9999)
        cmd = mock_exec.call_args[0][0]
        assert cmd == "check_port 9999"

    @pytest.mark.asyncio
    async def test_none_cmd_raises(self):
        ft = make_ft(nc_listener_check="custom", nc_listener_cmd=None)
        with pytest.raises(ValueError, match="nc_listener_cmd is None"):
            await ft._wait_for_remote_listener(9999)


class TestListenerCheckAuto:
    @pytest.mark.asyncio
    async def test_resolves_ss_when_available(self):
        """Compound probe picks `ss` when the host has it."""

        async def mock_exec(cmd, **kw):
            if "command -v" in cmd:
                return _ok("ss ss\n")
            return _ok("1\n")  # listener check succeeds immediately

        ft = make_ft(nc_listener_check="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == "ss"

    @pytest.mark.asyncio
    async def test_falls_back_to_netstat(self):
        """Host has netstat but not ss — probe returns netstat."""

        async def mock_exec(cmd, **kw):
            if "command -v" in cmd:
                return _ok("netstat netstat\n")
            return _ok("1\n")

        ft = make_ft(nc_listener_check="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == "netstat"

    @pytest.mark.asyncio
    async def test_falls_back_to_proc(self):
        """Neither ss nor netstat — probe returns proc for the listener side."""

        async def mock_exec(cmd, **kw):
            if "command -v" in cmd:
                # Port side could be python; listener falls back to proc.
                return _ok("python proc\n")
            return _ok("1\n")

        ft = make_ft(nc_listener_check="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        await ft._wait_for_remote_listener(8080)
        assert ft._resolved_listener_check == "proc"

    @pytest.mark.asyncio
    async def test_caches_resolved_strategy(self):
        """Second call uses the cached strategy without re-probing."""
        mock_exec = AsyncMock(return_value=_ok("1\n"))
        ft = make_ft(nc_listener_check="auto", exec_cmd=mock_exec)
        ft._resolved_listener_check = "netstat"
        await ft._wait_for_remote_listener(8080)
        # Cache hit means no probe at all.
        for call in mock_exec.call_args_list:
            assert "command -v" not in call[0][0]
            assert not call[0][0].startswith("type ")


# ============================================================================
# prepare() + warm-up  # noqa: ERA001 — section divider comment
# ============================================================================


class TestPrepareProbe:
    """`prepare()` runs a single compound probe and caches both strategies."""

    @pytest.mark.asyncio
    async def test_single_probe_caches_both(self):
        mock_exec = AsyncMock(return_value=_ok("ss ss\n"))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy == "ss"
        assert ft._resolved_listener_check == "ss"
        # Exactly one probe call.
        assert mock_exec.call_count == 1
        cmd = mock_exec.call_args[0][0]
        assert "command -v" in cmd

    @pytest.mark.asyncio
    async def test_mixed_strategies(self):
        """Probe can return different strategies for port vs listener."""
        mock_exec = AsyncMock(return_value=_ok("python proc\n"))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy == "python"
        assert ft._resolved_listener_check == "proc"

    @pytest.mark.asyncio
    async def test_idempotent(self):
        """Second `prepare()` is a no-op when caches are populated."""
        mock_exec = AsyncMock(return_value=_ok("ss ss\n"))
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
            nc_port_strategy="ss",
            nc_listener_check="netstat",
            exec_cmd=mock_exec,
        )
        await ft.prepare()
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_failure_leaves_caches_unset(self):
        """A failing probe must not poison the caches — cascade takes over."""
        mock_exec = AsyncMock(return_value=_fail("/bin/sh: command: not found"))
        ft = make_ft(exec_cmd=mock_exec)
        await ft.prepare()
        assert ft._resolved_port_strategy is None
        assert ft._resolved_listener_check is None

    @pytest.mark.asyncio
    async def test_concurrent_callers_probe_once(self):
        """Two coroutines calling prepare() in parallel share a single probe."""
        mock_exec = AsyncMock(return_value=_ok("ss ss\n"))
        ft = make_ft(exec_cmd=mock_exec)
        await asyncio.gather(ft.prepare(), ft.prepare(), ft.prepare())
        assert mock_exec.call_count == 1


class TestWarmupForTransfer:
    """`_warmup_for_transfer` runs prepare + pool warming concurrently."""

    @pytest.mark.asyncio
    async def test_ssh_runs_only_prepare(self):
        """On SSH the exec pool doesn't exist, so no `true` calls are needed."""
        mock_exec = AsyncMock(return_value=_ok("ss ss\n"))
        ft = make_ft(exec_cmd=mock_exec)  # defaults to ssh
        await ft._warmup_for_transfer(file_count=3)
        # Just the one compound probe — no `true` pre-warming on ssh.
        assert mock_exec.call_count == 1
        assert "command -v" in mock_exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_telnet_prewarms_pool_concurrently_with_probe(self):
        """Probe and `true` warmup calls overlap in wall-clock time."""
        order: list[str] = []
        started: asyncio.Event = asyncio.Event()

        async def mock_exec(cmd, **kw):
            order.append(f"start:{cmd[:20]}")
            started.set()
            # Yield so peers can start before we finish.
            await asyncio.sleep(0.01)
            order.append(f"end:{cmd[:20]}")
            if "command -v" in cmd:
                return _ok("ss ss\n")
            return _ok()

        # Use telnet term so warmup fires pool pre-opens.
        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "telnet"

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="auto",
                port_cmd=None,
                listener_check="auto",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=AsyncMock(side_effect=mock_exec),
            userland=None,
        )

        await ft._warmup_for_transfer(file_count=2)

        # 1 probe + 2 `true` calls, all via the exec pool = 3 invocations.
        total = len(order) // 2  # each cmd has a start + end event
        assert total == 3, f"expected 3 invocations, saw {total}: {order}"

        # At least one peer starts before the first one ends — i.e. the
        # probe and the `true` calls overlap, not run back-to-back.
        first_end = next(i for i, e in enumerate(order) if e.startswith("end:"))
        starts_before_first_end = sum(1 for e in order[:first_end] if e.startswith("start:"))
        assert starts_before_first_end >= 2, f"warmup didn't overlap; order={order}"

    @pytest.mark.asyncio
    async def test_telnet_warmup_swallows_warmup_failures(self):
        """A failed `true` pre-warm must not take down the transfer."""

        async def mock_exec(cmd, **kw):
            if "command -v" in cmd:
                return _ok("ss ss\n")
            raise RuntimeError("pool open failed")

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "telnet"

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="auto",
                port_cmd=None,
                listener_check="auto",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=AsyncMock(side_effect=mock_exec),
            userland=None,
        )

        # Should not raise — prepare succeeded, pool warmup failures are
        # best-effort.
        await ft._warmup_for_transfer(file_count=2)
        assert ft._resolved_port_strategy == "ss"


# ============================================================================
# _find_free_port_auto — cached-strategy self-heal
# ============================================================================


class TestFindFreePortAutoSelfHeal:
    @pytest.mark.asyncio
    async def test_self_heal_resets_and_cascades(self) -> None:
        """Cached strategy fails → every OTHER rung is tried, in the declared order.

        PORT_LADDER's order is ss, netstat, python, proc.
        With "netstat" pre-cached, the expected call sequence is:
          1. "netstat" — cached attempt (raises), never run a second time
          2. "ss"      — first in the order (raises)
          3. "python"  — next other rung (succeeds, returns 54321)
        """
        returned_port = 54321
        call_strategies: list[str] = []

        async def find_free_port_with_side_effect(strategy: str) -> int:
            call_strategies.append(strategy)
            if strategy in ("netstat", "ss"):
                raise RuntimeError(f"{strategy} failed")
            return returned_port

        ft = make_ft(nc_port_strategy="auto")
        ft._resolved_port_strategy = "netstat"
        ft._find_free_port_with = AsyncMock(side_effect=find_free_port_with_side_effect)

        port = await ft._find_free_port_auto()

        # Correct port returned from the cascade winner.
        assert port == returned_port

        # Exact call-strategy sequence: cached "netstat" fails once, then the
        # other rungs in declared order ("ss" fails, "python" succeeds).
        assert call_strategies == ["netstat", "ss", "python"], (
            f"unexpected call sequence: {call_strategies}"
        )

        # The new cached strategy is the cascade winner, not the old one.
        assert ft._resolved_port_strategy == "python"

    @pytest.mark.asyncio
    async def test_a_recovered_earlier_rung_is_used_after_the_cached_one_fails(self) -> None:
        """Across two calls: ss fails and netstat is cached; later netstat fails and ss works."""
        log: list[str] = []
        answers = {"probe": _ok("ss ss\n"), "ss": _tool_failed(), "netstat": _ok("40020\n")}
        ft = make_ft(exec_cmd=_host(answers, log))
        assert await ft._find_free_port() == 40020
        assert ft._resolved_port_strategy == "netstat"
        ft._release_port(40020)

        answers["netstat"] = _tool_failed()
        answers["ss"] = _ok("40021\n")
        log.clear()
        assert await ft._find_free_port() == 40021
        assert log == ["netstat", "ss"]
        assert ft._resolved_port_strategy == "ss"


# ============================================================================
# _resolve_listener_strategy — type cascade fallback
# ============================================================================


class TestResolveListenerStrategyCascade:
    @pytest.mark.asyncio
    async def test_type_cascade_falls_back_to_proc(self) -> None:
        """When ss and netstat are absent, cascade returns 'proc'."""

        async def mock_exec(cmd: str, **kw: object) -> CommandResult:
            if "type ss" in cmd:
                return _fail("not found")
            if "type netstat" in cmd:
                return _fail("not found")
            # compound probe fails too (command -v)
            return _fail("not found")

        ft = make_ft(nc_listener_check="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        # Force _resolved_listener_check to None so prepare() runs but probe fails
        ft._resolved_listener_check = None
        ft._resolved_port_strategy = "ss"  # so prepare() short-circuits the port part

        result = await ft._resolve_listener_strategy()

        assert result == "proc"

    @pytest.mark.asyncio
    async def test_type_cascade_picks_ss_when_available(self) -> None:
        """When ss's present test succeeds in the cascade, returns 'ss' and caches it."""

        async def mock_exec(cmd: str, **kw: object) -> CommandResult:
            if cmd == "command -v ss >/dev/null 2>&1":  # ss's present test
                return _ok("/usr/sbin/ss")
            # compound probe fails (command -v)
            return _fail("not found")

        ft = make_ft(nc_listener_check="auto", exec_cmd=AsyncMock(side_effect=mock_exec))
        ft._resolved_listener_check = None
        ft._resolved_port_strategy = "ss"

        result = await ft._resolve_listener_strategy()

        assert result == "ss"
        assert ft._resolved_listener_check == "ss"


# ============================================================================
# The port finder fails loudly
# ============================================================================

_COMPLAINT_AND_PORT = f"{COMPLAINT}\n10744"


def _rung_of(cmd: str) -> str:
    """Which rung a control command belongs to (``probe`` for the strategy probe)."""
    if "command -v" in cmd:
        return "probe"
    if "ss -tln" in cmd:
        return "ss"
    if "netstat -tln" in cmd:
        return "netstat"
    if cmd.startswith(("python -c", "python3 -c")):
        return "python"
    if "/proc/net/tcp" in cmd:
        return "proc"
    return "other"


def _host(answers: dict[str, CommandResult], log: list[str]) -> AsyncMock:
    """An exec mock that answers each rung from *answers* and records the rung it ran."""

    async def run(cmd: str, **kw: object) -> CommandResult:
        rung = _rung_of(cmd)
        log.append(rung)
        return answers.get(rung, _fail("unexpected: " + cmd))

    return AsyncMock(side_effect=run)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class TestPortFinderAutoIsLoud:
    @pytest.mark.asyncio
    async def test_the_field_case_warns_once_and_caches_the_next_rung(self, caplog):
        """The probe says netstat; netstat's tool failed; python's port is used and kept.

        ``ss`` is tried after the failed cached rung, as every other rung is.
        """
        log: list[str] = []
        exec_cmd = _host(
            {
                "probe": _ok("netstat netstat\n"),
                "ss": _tool_failed(),
                "netstat": _tool_failed(),
                "python": _ok("40001\n"),
            },
            log,
        )
        ft = make_ft(exec_cmd=exec_cmd)
        with caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"):
            assert await ft._find_free_port() == 40001
        assert ft._resolved_port_strategy == "python"
        netstat_warnings = [w for w in _warnings(caplog) if "'netstat'" in w]
        assert len(netstat_warnings) == 1, _warnings(caplog)
        assert "nc_options.port_strategy" in netstat_warnings[0]
        assert "test" in netstat_warnings[0]
        assert log == ["probe", "netstat", "ss", "python"]

        caplog.clear()
        log.clear()
        with caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"):
            assert await ft._find_free_port() == 40001
        assert _warnings(caplog) == []
        assert log == ["python"]

    @pytest.mark.asyncio
    async def test_a_failing_cached_rung_is_run_once(self):
        log: list[str] = []
        exec_cmd = _host({"ss": _tool_failed(), "netstat": _ok("40002\n")}, log)
        ft = make_ft(exec_cmd=exec_cmd)
        ft._resolved_port_strategy = "ss"
        assert await ft._find_free_port() == 40002
        assert log == ["ss", "netstat"]

    @pytest.mark.asyncio
    async def test_every_rung_failing_names_each_rung_its_reason_and_the_pin(self):
        log: list[str] = []
        exec_cmd = _host(
            {
                "probe": _fail("probe blew up"),
                "ss": _tool_failed(),
                "netstat": _ok(_COMPLAINT_AND_PORT),
                "python": _fail("no python here"),
                "proc": _fail("proc said no"),
            },
            log,
        )
        ft = make_ft(exec_cmd=exec_cmd)
        with pytest.raises(HostCommandError) as info:
            await ft._find_free_port()
        message = str(info.value)
        assert "All port-finding strategies failed on test" in message
        for rung in ("ss", "netstat", "python", "proc"):
            assert f"{rung}:" in message
        assert TOOL_FAILED in message or "missing or failed" in message
        assert "No such file or directory" in message
        assert "no python here" in message
        assert "proc said no" in message
        assert "nc_options.port_strategy" in message

    @pytest.mark.asyncio
    async def test_a_timed_out_first_rung_stops_the_ladder_without_a_warning(self, caplog):
        log: list[str] = []
        exec_cmd = _host({"ss": _timed_out("")}, log)
        ft = make_ft(exec_cmd=exec_cmd)
        ft._resolved_port_strategy = "ss"
        with (
            caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"),
            pytest.raises(HostUnreachableError),
        ):
            await ft._find_free_port()
        assert _warnings(caplog) == []
        assert log == ["ss"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [asyncio.CancelledError(), NcPortSharedError("shared")],
        ids=["cancelled", "port-shared"],
    )
    async def test_errors_that_stop_the_ladder_propagate(self, error, caplog):
        ft = make_ft()
        ft._resolved_port_strategy = "ss"
        ft._find_free_port_with = AsyncMock(side_effect=error)
        with (
            caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"),
            pytest.raises(type(error)),
        ):
            await ft._find_free_port()
        assert ft._find_free_port_with.await_count == 1
        assert _warnings(caplog) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["probe", "cache", "cascade"])
    async def test_a_failing_rung_reports_the_same_on_every_path(self, path, caplog):
        """The probe's fresh choice, the cache and the cascade give one report."""
        answers = {"netstat": _ok(_COMPLAINT_AND_PORT), "python": _ok("40003\n")}
        if path == "probe":
            answers["probe"] = _ok("netstat netstat\n")
            answers["ss"] = _fail("no ss")
        elif path == "cascade":
            answers["probe"] = _fail("probe blew up")
            answers["ss"] = _fail("no ss")
        log: list[str] = []
        ft = make_ft(exec_cmd=_host(answers, log))
        if path == "cache":
            ft._resolved_port_strategy = "netstat"
            answers["ss"] = _fail("no ss")
        with caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"):
            assert await ft._find_free_port() == 40003
        netstat_warnings = [w for w in _warnings(caplog) if "'netstat'" in w]
        assert netstat_warnings == [
            (
                "test: the netcat port finder's 'netstat' rung failed (netstat free port: "
                "expected a base-10 integer >= 1 and <= 65535, got "
                f"{_COMPLAINT_AND_PORT!r}); trying the next rung. "
                "Set nc_options.port_strategy to pin another."
            )
        ]
        assert log.count("netstat") == 1


class TestPortFinderPinnedIsNamed:
    @pytest.mark.asyncio
    async def test_netstat_answering_a_complaint_and_a_port(self):
        log: list[str] = []
        ft = make_ft(
            nc_port_strategy="netstat", exec_cmd=_host({"netstat": _ok(_COMPLAINT_AND_PORT)}, log)
        )
        with pytest.raises(NcPortRungError) as info:
            await ft._find_free_port()
        assert isinstance(info.value, HostCommandError)
        message = str(info.value)
        assert "port finder" in message
        assert "'netstat'" in message
        assert "test" in message
        assert repr(_COMPLAINT_AND_PORT) in message
        assert log == ["netstat"]

    @pytest.mark.asyncio
    async def test_custom_answering_a_port_out_of_range(self):
        exec_cmd = AsyncMock(return_value=_ok("70000\n"))
        ft = make_ft(nc_port_strategy="custom", nc_port_cmd="finder", exec_cmd=exec_cmd)
        with pytest.raises(NcPortRungError, match="70000"):
            await ft._find_free_port()

    @pytest.mark.asyncio
    async def test_ss_answering_the_sentinel_is_named_not_internal(self):
        ft = make_ft(nc_port_strategy="ss", exec_cmd=AsyncMock(return_value=_tool_failed()))
        with pytest.raises(NcPortRungError) as info:
            await ft._find_free_port()
        assert not isinstance(info.value, NcRungToolError)
        message = str(info.value)
        assert "'ss'" in message
        assert "missing or failed" in message
        assert TOOL_FAILED not in message, "the internal sentinel is not shown to the user"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("strategy", "answer"),
        [("netstat", _COMPLAINT_AND_PORT), ("custom", "70000\n")],
        ids=["pinned", "custom"],
    )
    async def test_an_unreadable_answer_is_still_a_value_error(self, strategy, answer):
        """``except ValueError`` caught an unreadable answer before the rung had a name."""
        ft = make_ft(
            nc_port_strategy=strategy,
            nc_port_cmd="finder",
            exec_cmd=AsyncMock(return_value=_ok(answer)),
        )
        with pytest.raises(ValueError, match=strategy if strategy != "custom" else "70000"):
            await ft._find_free_port()
        with pytest.raises(HostCommandError):
            await ft._find_free_port()

    @pytest.mark.asyncio
    async def test_a_digit_string_too_long_to_convert_is_the_rung_error(self):
        ft = make_ft(nc_port_strategy="ss", exec_cmd=AsyncMock(return_value=_ok("9" * 5000)))
        with pytest.raises(NcPortRungError, match="'ss'"):
            await ft._find_free_port()


class TestPythonRung:
    @pytest.mark.parametrize(("strategy", "draws"), [("python", 0), ("ss", 1)])
    @pytest.mark.asyncio
    async def test_only_a_scanning_rung_draws_a_scan_start(
        self, monkeypatch: pytest.MonkeyPatch, strategy: str, draws: int
    ):
        """The kernel picks python's port, so no random start is drawn for it."""
        drawn: list[int] = []

        def randrange(stop: int) -> int:
            drawn.append(stop)
            return 0

        monkeypatch.setattr(nc_module.random, "randrange", randrange)
        ft = make_ft(nc_port_strategy=strategy, exec_cmd=AsyncMock(return_value=_ok("40006\n")))
        assert await ft._find_free_port() == 40006
        assert len(drawn) == draws

    @pytest.mark.asyncio
    async def test_python_fails_python3_answers(self):
        exec_cmd = AsyncMock(side_effect=[_fail("not found"), _ok("40004\n")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=exec_cmd)
        assert await ft._find_free_port() == 40004

    @pytest.mark.asyncio
    async def test_python_timing_out_still_tries_python3(self):
        exec_cmd = AsyncMock(side_effect=[_timed_out(""), _ok("40005\n")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=exec_cmd)
        assert await ft._find_free_port() == 40005
        assert exec_cmd.await_count == 2

    @pytest.mark.asyncio
    async def test_both_interpreters_timing_out_is_unreachable(self):
        exec_cmd = AsyncMock(side_effect=[_timed_out(""), _timed_out("")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=exec_cmd)
        with pytest.raises(HostUnreachableError):
            await ft._find_free_port()

    @pytest.mark.asyncio
    async def test_a_silent_failure_says_the_exit_status_and_that_nothing_was_printed(self):
        exec_cmd = AsyncMock(side_effect=[_fail(""), _fail("")])
        ft = make_ft(nc_port_strategy="python", exec_cmd=exec_cmd)
        with pytest.raises(NcPortRungError) as info:
            await ft._find_free_port()
        message = str(info.value)
        assert "exit status 1" in message
        assert "printed nothing" in message
        assert not message.endswith(": ")


class TestReadRungAnswer:
    def test_the_sentinel_is_a_tool_failure(self):
        with pytest.raises(NcRungToolError, match="ss"):
            read_rung_answer(f"{TOOL_FAILED}\n", rung="ss", what="ss free port", kind=Integer(1))

    def test_text_is_a_command_value_error(self):
        with pytest.raises(CommandValueError):
            read_rung_answer("nope", rung="ss", what="ss free port", kind=Integer(1))

    def test_a_number_is_read(self):
        assert read_rung_answer(" 12\n", rung="ss", what="count", kind=Integer()) == 12


# ============================================================================
# The listener check fails loudly
# ============================================================================

_COMPLAINT_AND_COUNT = f"{COMPLAINT}\n1"


class TestListenerCheckIsLoud:
    @pytest.mark.asyncio
    async def test_the_sentinel_is_a_tool_error_after_one_command(self):
        exec_cmd = AsyncMock(return_value=_tool_failed())
        ft = make_ft(nc_listener_check="netstat", exec_cmd=exec_cmd)
        with pytest.raises(NcListenerToolError) as info:
            await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 1, "a failed tool is not something to wait out"
        message = str(info.value)
        assert "'netstat'" in message
        assert "test" in message
        assert "nc_options.listener_check" in message
        assert isinstance(info.value, ConnectionError)

    @pytest.mark.asyncio
    async def test_text_that_is_not_a_count_is_an_answer_error_quoting_it(self):
        exec_cmd = AsyncMock(return_value=_ok(_COMPLAINT_AND_COUNT))
        ft = make_ft(nc_listener_check="netstat", exec_cmd=exec_cmd)
        with pytest.raises(NcListenerAnswerError) as info:
            await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 1
        assert repr(_COMPLAINT_AND_COUNT) in str(info.value)
        assert isinstance(info.value, ConnectionError)

    @pytest.mark.asyncio
    async def test_a_digit_string_too_long_to_convert_is_an_answer_error(self):
        exec_cmd = AsyncMock(return_value=_ok("9" * 5000))
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        with pytest.raises(NcListenerAnswerError):
            await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "never_ran",
        [
            no_return_code_result(cmd="check", route=ExecRoute.SSH_CHANNEL),
            CommandResult(
                status=Status.Error,
                value="Session died unexpectedly (EOF)",
                command="check",
                retcode=-1,
            ),
        ],
        ids=["no-return-code", "session-died"],
    )
    async def test_a_check_that_never_ran_is_no_answer_and_is_polled_again(self, never_ran):
        exec_cmd = AsyncMock(side_effect=[never_ran, _ok("1\n")])
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 2

    @pytest.mark.asyncio
    async def test_a_zero_count_with_a_non_zero_exit_is_polled(self):
        exec_cmd = AsyncMock(side_effect=[_fail("0\n"), _ok("1\n")])
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("partial", ["1\n", f"{TOOL_FAILED}\n"], ids=["one", "sentinel"])
    async def test_a_timed_out_check_is_no_answer_whatever_it_printed(self, partial):
        exec_cmd = AsyncMock(side_effect=[_timed_out(partial), _ok("1\n")])
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 2

    @pytest.mark.asyncio
    async def test_a_check_that_never_answered_says_so_and_names_the_stage(self):
        exec_cmd = AsyncMock(return_value=_fail(""))
        ft = make_ft(nc_listener_check="proc", exec_cmd=exec_cmd)
        with pytest.raises(ConnectionError) as info:
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)
        assert type(info.value) is ConnectionError
        message = str(info.value)
        assert "remote readiness" in message
        assert "'proc'" in message
        assert "nc_options.listener_check" in message
        assert "never answered" in message
        assert "8080" in message

    @pytest.mark.asyncio
    async def test_a_check_that_answered_zero_does_not_claim_it_never_answered(self):
        exec_cmd = AsyncMock(return_value=_fail("0\n"))
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        with pytest.raises(ConnectionError) as info:
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01)
        assert "never answered" not in str(info.value)

    @pytest.mark.asyncio
    async def test_the_timeout_says_whether_the_listener_had_exited(self):
        async def done() -> None:
            return None

        listener = asyncio.ensure_future(done())
        await listener
        exec_cmd = AsyncMock(return_value=_fail("0\n"))
        ft = make_ft(nc_listener_check="ss", exec_cmd=exec_cmd)
        with pytest.raises(ConnectionError, match="had already exited"):
            await ft._wait_for_remote_listener(8080, timeout=0.05, interval=0.01, listener=listener)

    @pytest.mark.asyncio
    async def test_a_custom_check_exiting_non_zero_is_polled(self):
        exec_cmd = AsyncMock(side_effect=[_fail(f"{TOOL_FAILED}\n"), _fail("junk"), _ok("")])
        ft = make_ft(nc_listener_check="custom", nc_listener_cmd="chk {port}", exec_cmd=exec_cmd)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 3


class TestPortFinderTriesEveryOtherRungAfterTheCachedOne:
    @pytest.mark.asyncio
    async def test_the_probe_said_netstat_and_netstat_failed(self, caplog):
        """The cached rung runs once; then every other rung, in the declared order."""
        log: list[str] = []
        exec_cmd = _host(
            {
                "probe": _ok("netstat netstat\n"),
                "ss": _fail("no ss"),
                "netstat": _tool_failed(),
                "python": _ok("40011\n"),
            },
            log,
        )
        ft = make_ft(exec_cmd=exec_cmd)
        with caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"):
            assert await ft._find_free_port() == 40011
        assert len(_warnings(caplog)) == 2, _warnings(caplog)
        assert log == ["probe", "netstat", "ss", "python"]

    @pytest.mark.asyncio
    async def test_the_all_fail_error_names_every_rung_and_its_reason(self):
        log: list[str] = []
        exec_cmd = _host(
            {
                "ss": _fail("no ss"),
                "netstat": _fail("no netstat"),
                "python": _fail("no python here"),
                "proc": _fail("proc said no"),
            },
            log,
        )
        ft = make_ft(exec_cmd=exec_cmd)
        ft._resolved_port_strategy = "python"
        with pytest.raises(HostCommandError) as info:
            await ft._find_free_port()
        message = str(info.value)
        assert log == ["python", "python", "ss", "netstat", "proc"]
        for reason in ("no ss", "no netstat", "no python here", "proc said no"):
            assert reason in message
        assert "skipped" not in message

    @pytest.mark.asyncio
    async def test_only_a_rung_with_another_after_it_says_the_next_is_tried(self, caplog):
        exec_cmd = _host(
            {
                "probe": _fail("probe blew up"),
                "ss": _fail("no ss"),
                "netstat": _fail("no netstat"),
                "python": _fail("no python"),
                "proc": _fail("proc said no"),
            },
            [],
        )
        ft = make_ft(exec_cmd=exec_cmd)
        with (
            caplog.at_level(logging.WARNING, logger="otto.host.transfer.nc"),
            pytest.raises(HostCommandError),
        ):
            await ft._find_free_port()
        by_rung = {
            rung: [w for w in _warnings(caplog) if f"'{rung}' rung failed" in w]
            for rung in ("ss", "netstat", "python", "proc")
        }
        for rung in ("ss", "netstat", "python"):
            assert len(by_rung[rung]) == 1
            assert "trying the next rung" in by_rung[rung][0]
        assert len(by_rung["proc"]) == 1
        assert "trying the next rung" not in by_rung["proc"][0]
        assert by_rung["proc"][0].endswith("). Set nc_options.port_strategy to pin another.")

    @pytest.mark.asyncio
    async def test_a_non_zero_exit_is_cut_in_the_error(self):
        long_text = "x" * 1000
        ft = make_ft(nc_port_strategy="ss", exec_cmd=AsyncMock(return_value=_fail(long_text)))
        with pytest.raises(NcPortRungError) as info:
            await ft._find_free_port()
        assert "x" * 200 in str(info.value)
        assert "x" * 201 not in str(info.value)
