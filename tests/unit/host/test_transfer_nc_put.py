"""Tests for ``_put_files_nc``: progress drain (#1) and listener wait (#3).

Issue #1 — the nc write loop used to buffer the entire source file into the
asyncio StreamWriter without ever awaiting ``drain()``.  The progress handler
fired at full speed while no bytes had actually left the process, producing
the fake ~400 MB/s "100% burst".  The fix drains every N blocks so
``bytes_done`` tracks drained bytes and the event loop gets to breathe.

Issue #3 — the non-tunnel branch of ``_put_files_nc`` skipped
``_wait_for_remote_listener`` and went straight into ``_connect_with_retry``
with a 2 s timeout.  On back-to-back transfers the remote ``nc -l`` hadn't
finished spawning yet.  The fix calls ``_wait_for_remote_listener`` before
connecting, regardless of tunnel state.

Port-collision race — ``_find_free_port`` had no synchronization, so two
concurrent ``_put_one`` calls could each ``ss``-scan and both see the same
"free" port.  Only one ``nc -l`` would win the bind; the loser's file would
later fail with "listener not ready" because by connect time the winner's
brief transfer had already closed the port.  The fix serializes port
allocation under a lock so the second caller sees the first's reservation.
"""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer as transfer_mod
from otto.host import RunResult
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions, ScpOptions
from otto.host.transfer import FileTransfer
from otto.utils import CommandStatus, Status


def _ok(output: str = '') -> CommandStatus:
    return CommandStatus(command='', output=output, status=Status.Success, retcode=0)


def _ok_result(output: str = '') -> RunResult:
    """Wrap ``_ok`` in a RunResult — for mocks of ``HostSession.run``."""
    return RunResult(status=Status.Success, statuses=[_ok(output)])


class _FakeWriter:
    """Minimal ``asyncio.StreamWriter`` stand-in for nc put tests.

    Tracks every ``write`` and ``drain`` so tests can assert when draining
    actually happens relative to writes.
    """

    def __init__(self, drain_delay: float = 0.0) -> None:
        self.written_bytes: int = 0
        self.drain_calls: list[int] = []
        self.closed: bool = False
        self._drain_delay = drain_delay

    def write(self, data: bytes) -> None:
        self.written_bytes += len(data)

    async def drain(self) -> None:
        if self._drain_delay:
            await asyncio.sleep(self._drain_delay)
        self.drain_calls.append(self.written_bytes)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _make_ft(
    exec_cmd: AsyncMock,
    *,
    has_tunnel: bool = False,
    term: str = 'ssh',
    open_session: AsyncMock | None = None,
) -> FileTransfer:
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = has_tunnel
    mock_connections.ip = '10.0.0.1'
    mock_connections.term = term
    return FileTransfer(
        connections=mock_connections,
        name='tomato',
        transfer='nc',
        nc_options=NcOptions(
            exec_name='nc',
            port=9000,
            port_strategy='ss',
            port_cmd=None,
            listener_check='ss',
            listener_cmd=None,
        ),
        scp_options=ScpOptions(),
        get_local_ip=lambda: '127.0.0.1',
        open_session=open_session or AsyncMock(),
        exec_cmd=exec_cmd,
    )


class TestNcPutDrain:
    """Issue #1: the nc write loop must drain periodically during transfer."""

    @pytest.mark.asyncio
    async def test_drains_periodically(self, tmp_path: Path):
        # 8 KB blocks × 256 = 2 MB. At drain_every=64, that's 4 drain calls
        # during the loop (one per 64 blocks) plus the final drain.
        src = tmp_path / 'payload.bin'
        src.write_bytes(b'x' * (8192 * 256))

        fake_writer = _FakeWriter()

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, fake_writer

        exec_cmd = AsyncMock(return_value=_ok('9000\n'))
        ft = _make_ft(exec_cmd)

        with patch.object(transfer_mod, '_connect_with_retry', new=fake_connect), \
             patch.object(FileTransfer, '_wait_for_remote_listener',
                          new=AsyncMock(return_value=None)), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)):
            status, msg = await ft._put_files_nc([src], tmp_path / 'dst')

        assert status == Status.Success, msg
        # Pre-fix the loop drained exactly once (at the end) — the whole file
        # was written in one synchronous burst.  With the fix we expect
        # multiple drain calls interleaved with the writes.
        assert len(fake_writer.drain_calls) >= 3, (
            f"drain() should fire periodically during the write loop, "
            f"got {len(fake_writer.drain_calls)} total: {fake_writer.drain_calls}"
        )
        # Drains happen at ascending byte counts (not all piled up at the end).
        assert fake_writer.drain_calls[0] < fake_writer.drain_calls[-1]
        # The final drain reports the full file size.
        assert fake_writer.drain_calls[-1] == src.stat().st_size

    @pytest.mark.asyncio
    async def test_progress_handler_reports_bounded_bytes(self, tmp_path: Path):
        """The handler's ``bytes_done`` must never exceed what's been drained.

        This is the user-visible symptom of Issue #1: ``bytes_done`` rockets
        to ``total`` before any drain happens, so Rich's TransferSpeedColumn
        reports an impossibly-fast speed.
        """
        src = tmp_path / 'payload.bin'
        src.write_bytes(b'x' * (8192 * 128))  # 1 MB

        fake_writer = _FakeWriter()
        handler_calls: list[tuple[int, int]] = []  # (bytes_done, drained_so_far)

        def handler(s: str, d: str, bytes_done: int, total: int) -> None:
            handler_calls.append((bytes_done, fake_writer.drain_calls[-1] if fake_writer.drain_calls else 0))

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, fake_writer

        exec_cmd = AsyncMock(return_value=_ok('9000\n'))
        ft = _make_ft(exec_cmd)

        # Call the internal nc put directly with a pre-built factory that
        # hands out *our* handler so we can inspect per-callback state.
        def factory():
            return handler

        with patch.object(transfer_mod, '_connect_with_retry', new=fake_connect), \
             patch.object(FileTransfer, '_wait_for_remote_listener',
                          new=AsyncMock(return_value=None)), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)):
            status, _ = await ft._put_files_nc([src], tmp_path / 'dst', factory)

        assert status == Status.Success
        # Progress callbacks must span more than one drain boundary — i.e.
        # the handler observes more than one distinct "drained bytes" value
        # while progress is ticking.  Pre-fix there's only one (0, because
        # drain happens after the whole loop), and every handler call sees
        # "drained=0" until the final drain.
        distinct_drain_snapshots = {drained for _, drained in handler_calls}
        assert len(distinct_drain_snapshots) > 1, (
            f"handler always saw the same drained-byte count "
            f"({distinct_drain_snapshots}); drains aren't interleaved with writes"
        )


class TestNcPutListenerWait:
    """Issue #3: non-tunnel path must wait for the remote listener."""

    @pytest.mark.asyncio
    async def test_non_tunnel_calls_wait_before_connect(self, tmp_path: Path):
        """``_wait_for_remote_listener`` must run before ``_connect_with_retry``."""
        src = tmp_path / 'small.bin'
        src.write_bytes(b'hello world')

        order: list[str] = []

        async def fake_wait(self, port: int, *a, **kw) -> None:
            order.append(f'wait:{port}')

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            order.append(f'connect:{host}:{port}')
            return None, _FakeWriter()

        exec_cmd = AsyncMock(return_value=_ok('9000\n'))
        ft = _make_ft(exec_cmd, has_tunnel=False)

        with patch.object(FileTransfer, '_wait_for_remote_listener', new=fake_wait), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)), \
             patch.object(transfer_mod, '_connect_with_retry', new=fake_connect):
            status, msg = await ft._put_files_nc([src], tmp_path / 'dst')

        assert status == Status.Success, msg

        waits = [c for c in order if c.startswith('wait:')]
        connects = [c for c in order if c.startswith('connect:')]
        assert waits, f"expected _wait_for_remote_listener to run; order={order}"
        assert connects, f"expected _connect_with_retry to run; order={order}"
        assert order.index(waits[0]) < order.index(connects[0]), (
            f"wait must come before connect; order={order}"
        )

    @pytest.mark.asyncio
    async def test_telnet_reuses_one_monitor_session_across_files(self, tmp_path: Path):
        """Gate assertion: telnet opens ONE warm monitor session and reuses it.

        The earlier "poll session per file" design paid a telnet auth handshake
        (~1–2 s) before each file, defeating the optimization.  The fix is a
        single long-lived ``_nc_monitor`` session shared by every nc
        control-plane call (port-find, listener probe) on this host.

        Pairs two assertions so a future refactor can't widen the scope (open
        a monitor on SSH where exec channels are already cheap) or narrow it
        (reopen per-file on telnet, re-introducing the pause).
        """
        files = [tmp_path / f'f{i}.bin' for i in range(3)]
        for f in files:
            f.write_bytes(b'hello')

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, _FakeWriter()

        # --- telnet: open ONE '_nc_monitor', reuse across all files ---
        # On telnet, both port-find and listener-probe route through the
        # monitor's run. Port-find wants a number; listener-probe wants
        # retcode=0. _ok('9000\n') satisfies both.
        monitor_run = AsyncMock(return_value=_ok_result('9000\n'))
        monitor_session = MagicMock()
        monitor_session.alive = True
        monitor_session.run = monitor_run
        monitor_session.close = AsyncMock()
        telnet_open = AsyncMock(return_value=monitor_session)
        # With the new design, _exec_cmd is used only for the long-running
        # `nc -l` listener, not control-plane ops.
        exec_cmd = AsyncMock(return_value=_ok())
        ft_telnet = _make_ft(exec_cmd, term='telnet', open_session=telnet_open)

        with patch.object(transfer_mod, '_connect_with_retry', new=fake_connect), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)):
            status, msg = await ft_telnet._put_files_nc(files, tmp_path / 'dst')
        assert status == Status.Success, msg
        # Exactly one session opened, regardless of file count.
        assert telnet_open.await_count == 1, (
            f"telnet should reuse one monitor session, got {telnet_open.await_count} opens"
        )
        assert telnet_open.await_args.args[0] == '_nc_monitor'
        # Monitor persists across transfers — NOT closed in _put_files_nc.
        monitor_session.close.assert_not_called()
        # Control-plane probes routed through the monitor.
        assert monitor_run.await_count >= 1

        # --- ssh: must NOT open any control-plane session ---
        ssh_open = AsyncMock()
        exec_cmd_ssh = AsyncMock(return_value=_ok('9000\n'))
        ft_ssh = _make_ft(exec_cmd_ssh, term='ssh', open_session=ssh_open)

        with patch.object(transfer_mod, '_connect_with_retry', new=fake_connect), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)):
            status, msg = await ft_ssh._put_files_nc(files, tmp_path / 'dst')
        assert status == Status.Success, msg
        ssh_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_tunnel_survives_slow_listener_startup(self, tmp_path: Path):
        """If the listener takes > 2 s to bind, the wait step must cover that.

        Pre-fix this scenario fails with "nc listener not ready" because the
        2 s ``_connect_with_retry`` timeout is too short to cover SSH session
        setup + remote process spawn + bind() on a contended system.
        """
        src = tmp_path / 'small.bin'
        src.write_bytes(b'hello')

        listener_ready = asyncio.Event()

        async def slow_wait(self, port: int, *a, **kw) -> None:
            # Simulate the listener taking longer than the connect timeout.
            await asyncio.sleep(0.05)
            listener_ready.set()

        async def gated_connect(host: str, port: int, timeout: float = 2.0):
            # Mirror real behavior: if the listener isn't ready, connect fails.
            if not listener_ready.is_set():
                raise ConnectionError(f"Remote nc listener on {host}:{port} not ready within {timeout}s")
            return None, _FakeWriter()

        exec_cmd = AsyncMock(return_value=_ok('9000\n'))
        ft = _make_ft(exec_cmd, has_tunnel=False)

        with patch.object(FileTransfer, '_wait_for_remote_listener', new=slow_wait), \
             patch.object(FileTransfer, '_verify_nc_dest_size',
                          new=AsyncMock(return_value=None)), \
             patch.object(transfer_mod, '_connect_with_retry', new=gated_connect):
            status, msg = await ft._put_files_nc([src], tmp_path / 'dst')

        assert status == Status.Success, msg


class TestNcPutPortRace:
    """Concurrent ``_put_one`` calls must not allocate the same remote port.

    Pre-fix, two parallel ``_find_free_port`` coroutines each ran ``ss`` with
    ``reserved=""`` (the other's reservation hadn't been recorded yet) and
    both returned the same port number.  Downstream, one nc listener won the
    bind; the other's file failed with "listener not ready" mid-transfer.
    """

    @pytest.mark.asyncio
    async def test_concurrent_find_free_port_returns_unique_ports(self):
        """Two concurrent ``_find_free_port`` calls return distinct ports."""

        async def ss_emulator(cmd: str, *a, **kw):
            # Parse `reserved=" 9000 9001 "` out of the ss port script.
            m = re.search(r'reserved=" ([^"]*)"', cmd)
            reserved: set[int] = set()
            if m and m.group(1).strip():
                reserved = {int(p) for p in m.group(1).split()}
            # Yield so the other concurrent coroutine can interleave its ss
            # scan at the same "reserved" snapshot — this is what produced the
            # duplicate-port bug in real life.
            await asyncio.sleep(0)
            p = 9000
            while p in reserved:
                p += 1
            return _ok(f'{p}\n')

        exec_cmd = AsyncMock(side_effect=ss_emulator)
        ft = _make_ft(exec_cmd)

        port_a, port_b = await asyncio.gather(
            ft._find_free_port(), ft._find_free_port()
        )
        assert port_a != port_b, (
            f"concurrent port allocation collided: both returned {port_a}"
        )
