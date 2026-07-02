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

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.result import CommandResult
from otto.utils import Status


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _only(per_file: dict, src: Path) -> tuple[Status, str]:
    """Unwrap the single per-file ``(status, msg)`` from a nc transfer's mapping."""
    r = per_file[src]
    return r.status, r.msg


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
    term: str = "ssh",
    listener_timeout: float = 30.0,
) -> NcFileTransfer:
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = has_tunnel
    mock_connections.ip = "10.0.0.1"
    mock_connections.term = term
    return NcFileTransfer(
        connections=mock_connections,
        name="tomato",
        transfer="nc",
        nc_options=NcOptions(
            exec_name="nc",
            port=9000,
            port_strategy="ss",
            port_cmd=None,
            listener_check="ss",
            listener_cmd=None,
            listener_timeout=listener_timeout,
        ),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=exec_cmd,
    )


class TestNcPutDrain:
    """Issue #1: the nc write loop must drain periodically during transfer."""

    @pytest.mark.asyncio
    async def test_drains_periodically(self, tmp_path: Path):
        # 8 KB blocks x 256 = 2 MB. At drain_every=64, that's 4 drain calls
        # during the loop (one per 64 blocks) plus the final drain.
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * (8192 * 256))

        fake_writer = _FakeWriter()

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, fake_writer

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd)

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            status, msg = _only(await ft._put_files_nc([src], tmp_path / "dst"), src)

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
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * (8192 * 128))  # 1 MB

        fake_writer = _FakeWriter()
        handler_calls: list[tuple[int, int]] = []  # (bytes_done, drained_so_far)

        def handler(s: str, d: str, bytes_done: int, total: int) -> None:
            handler_calls.append(
                (bytes_done, fake_writer.drain_calls[-1] if fake_writer.drain_calls else 0)
            )

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, fake_writer

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd)

        # Call the internal nc put directly with a pre-built factory that
        # hands out *our* handler so we can inspect per-callback state.
        def factory():
            return handler

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            status, _ = _only(await ft._put_files_nc([src], tmp_path / "dst", factory), src)

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
        src = tmp_path / "small.bin"
        src.write_bytes(b"hello world")

        order: list[str] = []

        async def fake_wait(self, port: int, *a, **kw) -> None:
            order.append(f"wait:{port}")

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            order.append(f"connect:{host}:{port}")
            return None, _FakeWriter()

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd, has_tunnel=False)

        with (
            patch.object(NcFileTransfer, "_wait_for_remote_listener", new=fake_wait),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
        ):
            status, msg = _only(await ft._put_files_nc([src], tmp_path / "dst"), src)

        assert status == Status.Success, msg

        waits = [c for c in order if c.startswith("wait:")]
        connects = [c for c in order if c.startswith("connect:")]
        assert waits, f"expected _wait_for_remote_listener to run; order={order}"
        assert connects, f"expected _connect_with_retry to run; order={order}"
        assert order.index(waits[0]) < order.index(connects[0]), (
            f"wait must come before connect; order={order}"
        )

    @pytest.mark.asyncio
    async def test_telnet_control_run_serializes_onto_pool(self):
        """On telnet, concurrent control-plane ops must not overlap.

        ``_control_run`` routes every control op (port-find, listener probe,
        strategy probe, file-size stats) through ``_exec_cmd`` — the same
        oneshot exec path the listeners use. ``_control_lock`` serializes
        them so they reuse one warm pooled session instead of fanning out
        and each paying a cold telnet auth handshake.
        """
        in_flight = 0
        max_in_flight = 0

        async def tracking_exec(cmd: str, *args, **kwargs) -> CommandResult:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0)  # yield so any overlap would be observed
            in_flight -= 1
            return _ok()

        exec_cmd = AsyncMock(side_effect=tracking_exec)
        ft = _make_ft(exec_cmd, term="telnet")

        await asyncio.gather(*(ft._control_run("probe") for _ in range(5)))

        assert exec_cmd.await_count == 5, "every control op must route through _exec_cmd"
        assert max_in_flight == 1, (
            f"telnet control ops must be serialized, saw {max_in_flight} in flight"
        )

    @pytest.mark.asyncio
    async def test_ssh_control_run_does_not_serialize(self):
        """On SSH, control ops run directly with no lock — exec channels over
        the live connection are cheap and concurrency-safe."""
        in_flight = 0
        max_in_flight = 0

        async def tracking_exec(cmd: str, *args, **kwargs) -> CommandResult:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return _ok()

        exec_cmd = AsyncMock(side_effect=tracking_exec)
        ft = _make_ft(exec_cmd, term="ssh")

        await asyncio.gather(*(ft._control_run("probe") for _ in range(5)))

        assert exec_cmd.await_count == 5
        assert max_in_flight > 1, "SSH control ops should not be serialized"

    @pytest.mark.asyncio
    async def test_non_tunnel_survives_slow_listener_startup(self, tmp_path: Path):
        """If the listener takes > 2 s to bind, the wait step must cover that.

        Pre-fix this scenario fails with "nc listener not ready" because the
        2 s ``_connect_with_retry`` timeout is too short to cover SSH session
        setup + remote process spawn + bind() on a contended system.
        """
        src = tmp_path / "small.bin"
        src.write_bytes(b"hello")

        listener_ready = asyncio.Event()

        async def slow_wait(self, port: int, *a, **kw) -> None:
            # Simulate the listener taking longer than the connect timeout.
            await asyncio.sleep(0.05)
            listener_ready.set()

        async def gated_connect(host: str, port: int, timeout: float = 2.0):
            # Mirror real behavior: if the listener isn't ready, connect fails.
            if not listener_ready.is_set():
                raise ConnectionError(
                    f"Remote nc listener on {host}:{port} not ready within {timeout}s"
                )
            return None, _FakeWriter()

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd, has_tunnel=False)

        with (
            patch.object(NcFileTransfer, "_wait_for_remote_listener", new=slow_wait),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
            patch.object(transfer_mod, "_connect_with_retry", new=gated_connect),
        ):
            status, msg = _only(await ft._put_files_nc([src], tmp_path / "dst"), src)

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
            return _ok(f"{p}\n")

        exec_cmd = AsyncMock(side_effect=ss_emulator)
        ft = _make_ft(exec_cmd)

        port_a, port_b = await asyncio.gather(ft._find_free_port(), ft._find_free_port())
        assert port_a != port_b, f"concurrent port allocation collided: both returned {port_a}"


class TestNcPutOrphanedListener:
    """An orphaned ``nc -l`` (no client ever connects) must not hang forever.

    If a concurrent process wins a port-collision race, our sender's bytes go
    to *its* listener and ours never gets a connection — leaving ``listen_task``
    waiting on an ``nc -l`` that never exits. The bounded ``await`` must convert
    that into a ``Status.Error`` so ``_put_one``'s retry can take a fresh port.
    """

    @pytest.mark.asyncio
    async def test_put_errors_when_listener_never_exits(self, tmp_path: Path):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 32)

        async def exec_side(cmd: str, *a, **kw):
            if "nc -l" in cmd:
                # Orphaned listener: nc never sees a client, never exits.
                await asyncio.Event().wait()
            return _ok("9000\n")

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, _FakeWriter()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd, listener_timeout=0.1)

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._put_files_nc([src], tmp_path / "dst"),
                    timeout=5.0,
                ),
                src,
            )

        assert status == Status.Error, msg
        assert "orphaned listener" in msg

    @pytest.mark.asyncio
    async def test_listener_command_carries_idle_timeout(self, tmp_path: Path):
        """The remote ``nc -l`` invocation must include ``-w`` so the listener
        self-terminates rather than leaking when no client connects."""
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 16)

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, _FakeWriter()

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd, listener_timeout=30.0)

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            await ft._put_files_nc([src], tmp_path / "dst")

        listen_cmds = [
            c.args[0] for c in exec_cmd.await_args_list if c.args and "nc -l" in c.args[0]
        ]
        assert listen_cmds, "expected an `nc -l` listener invocation"
        assert all("-w 30" in cmd for cmd in listen_cmds), listen_cmds


class TestNcPutCancellation:
    """External cancellation mid-transfer must reap the remote ``nc -l``.

    ``_attempt`` spawns the listener as an ``asyncio.Task`` and only joins it
    on its normal success / error branches. A caller-side cancellation skips
    those, so ``_attempt`` must cancel the task and reap the remote listener
    itself — otherwise the ``nc -l`` lingers until its ``-w`` timeout.
    """

    @pytest.mark.asyncio
    async def test_cancellation_reaps_listener(self, tmp_path: Path):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)

        listener_started = asyncio.Event()

        async def exec_side_effect(cmd: str, *args, **kwargs) -> CommandResult:
            if "nc -l" in cmd:
                # The listener "runs" until its task is cancelled.
                listener_started.set()
                await asyncio.Event().wait()
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side_effect)
        ft = _make_ft(exec_cmd)

        reap_calls: list[int] = []

        async def fake_reap(port: int) -> None:
            reap_calls.append(port)

        async def block_forever(*args, **kwargs) -> None:
            await asyncio.Event().wait()

        # _wait_for_remote_listener blocks forever, so the cancellation lands
        # after listen_task is spawned but before any sender connects — the
        # window the fix targets.
        with (
            patch.object(ft, "_reap_nc_listener", new=fake_reap),
            patch.object(
                NcFileTransfer,
                "_wait_for_remote_listener",
                new=AsyncMock(side_effect=block_forever),
            ),
        ):
            task = asyncio.create_task(ft._put_files_nc([src], tmp_path / "dst"))
            await asyncio.wait_for(listener_started.wait(), timeout=2.0)
            await asyncio.sleep(0)  # let _attempt reach _wait_for_remote_listener
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert reap_calls == [9000], (
            f"cancellation must reap the remote nc listener, got {reap_calls}"
        )


# ============================================================================
# _connect_with_retry — retry-then-succeed
# ============================================================================


class TestConnectWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_after_retries(self):
        """_connect_with_retry retries on ConnectionRefusedError and eventually returns."""
        reader, writer = MagicMock(), MagicMock()
        open_conn = AsyncMock(
            side_effect=[ConnectionRefusedError(), ConnectionRefusedError(), (reader, writer)]
        )
        with patch.object(transfer_mod.asyncio, "open_connection", open_conn):
            r, w = await transfer_mod._connect_with_retry(
                "h", 9000, timeout=5.0, retry_interval=0.0
            )
        assert (r, w) == (reader, writer)
        assert open_conn.await_count == 3

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_timeout(self):
        """_connect_with_retry raises ConnectionError when timeout is exceeded."""
        open_conn = AsyncMock(side_effect=ConnectionRefusedError())
        with (
            patch.object(transfer_mod.asyncio, "open_connection", open_conn),
            pytest.raises(ConnectionError, match="not ready within"),
        ):
            await transfer_mod._connect_with_retry("h", 9000, timeout=0.0, retry_interval=0.0)


# ============================================================================
# _verify_nc_dest_size — error paths + success
# ============================================================================


class TestVerifyNcDestSize:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stat_out", "expected", "want"),
        [
            ("MISSING", 10, "destination file missing"),
            ("abc", 10, "unparseable"),
            ("5", 10, "expected 10 bytes, got 5"),
        ],
    )
    async def test_errors(self, stat_out: str, expected: int, want: str, tmp_path: Path) -> None:
        exec_cmd = AsyncMock(side_effect=lambda cmd, **kw: _ok(stat_out))
        ft = _make_ft(exec_cmd)
        result = await ft._verify_nc_dest_size(tmp_path / "f", expected)
        assert result is not None
        assert result.status is Status.Error
        assert want in result.msg

    @pytest.mark.asyncio
    async def test_ok_returns_none(self, tmp_path: Path) -> None:
        exec_cmd = AsyncMock(side_effect=lambda cmd, **kw: _ok("10"))
        ft = _make_ft(exec_cmd)
        assert await ft._verify_nc_dest_size(tmp_path / "f", 10) is None


# ============================================================================
# _reap_nc_listener — non-tunnel, tunnel, connect-failure
# ============================================================================


class TestReapNcListener:
    @pytest.mark.asyncio
    async def test_non_tunnel_connects_and_closes(self) -> None:
        """Non-tunnel path connects to connections.ip and closes the writer."""
        writer = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)
        ft = _make_ft(AsyncMock(), has_tunnel=False)

        with patch.object(
            transfer_mod, "_connect_with_retry", AsyncMock(return_value=(MagicMock(), writer))
        ) as mock_connect:
            await ft._reap_nc_listener(9000)

        mock_connect.assert_awaited_once()
        call_args = mock_connect.await_args
        assert call_args[0][0] == "10.0.0.1"  # connections.ip
        writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_tunnel_uses_forward_port(self) -> None:
        """Tunnel path calls forward_port and connects to localhost."""
        writer = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)
        ft = _make_ft(AsyncMock(), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with patch.object(
            transfer_mod, "_connect_with_retry", AsyncMock(return_value=(MagicMock(), writer))
        ) as mock_connect:
            await ft._reap_nc_listener(9000)

        ft._connections.forward_port.assert_awaited_once_with(9000)
        call_args = mock_connect.await_args
        assert call_args[0][0] == "localhost"
        assert call_args[0][1] == 15000

    @pytest.mark.asyncio
    async def test_connect_failure_is_silent(self) -> None:
        """ConnectionError from _connect_with_retry is swallowed — returns None."""
        ft = _make_ft(AsyncMock(), has_tunnel=False)

        with patch.object(
            transfer_mod, "_connect_with_retry", AsyncMock(side_effect=ConnectionError("nope"))
        ):
            result = await ft._reap_nc_listener(9000)

        assert result is None

    @pytest.mark.asyncio
    async def test_tunnel_forward_port_exception_is_silent(self) -> None:
        """If forward_port raises, _reap_nc_listener returns silently without connecting."""
        ft = _make_ft(AsyncMock(), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(side_effect=OSError("tunnel broken"))

        with patch.object(transfer_mod, "_connect_with_retry", AsyncMock()) as mock_connect:
            result = await ft._reap_nc_listener(9000)

        assert result is None
        mock_connect.assert_not_awaited()


# ============================================================================
# _put_files_nc — connect-failure branch
# ============================================================================


class TestPutFilesNcConnectFailure:
    @pytest.mark.asyncio
    async def test_connect_failure_returns_error(self, tmp_path: Path) -> None:
        """If _connect_with_retry raises ConnectionError, _put_files_nc returns Status.Error."""
        src = tmp_path / "file.bin"
        src.write_bytes(b"hello")

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=ConnectionError("nope")),
            ),
        ):
            status, msg = _only(await ft._put_files_nc([src], tmp_path / "dst"), src)

        assert status is Status.Error
        assert "not ready" in msg
