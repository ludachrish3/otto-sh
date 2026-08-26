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


class _FakeTransport:
    """Transport stub: scripted write-buffer sizes + abort tracking.

    ``buffer_sizes`` is consumed left-to-right by
    ``get_write_buffer_size()``; the last value repeats — a constant list
    models a fully stalled channel (zero progress), a decreasing list a
    slow-but-moving one.
    """

    def __init__(self, buffer_sizes: list[int] | None = None) -> None:
        self.buffer_sizes = list(buffer_sizes) if buffer_sizes else [0]
        self.aborted = False

    def get_write_buffer_size(self) -> int:
        if len(self.buffer_sizes) > 1:
            return self.buffer_sizes.pop(0)
        return self.buffer_sizes[0]

    def abort(self) -> None:
        self.aborted = True


class _FakeWriter:
    """Minimal ``asyncio.StreamWriter`` stand-in for nc put tests.

    Tracks every ``write`` and ``drain`` so tests can assert when draining
    actually happens relative to writes.
    """

    def __init__(self, drain_delay: float = 0.0) -> None:
        self.written_bytes: int = 0
        self.drain_calls: list[int] = []
        self.closed: bool = False
        self.transport = _FakeTransport()
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
        name="test2",
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
        userland=None,
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
        exec path the listeners use. ``_control_lock`` serializes
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
    async def test_listener_command_is_the_universal_spelling(self, tmp_path: Path):
        """The one listener spelling every measured netcat accepts: `-l -p PORT`.

        Asserted as the FULL command tail, not option-by-option: a reintroduced
        `-w` (the mid-transfer idle-kill, rc 0 with a partial file — measured
        2026-08-25 on five of six userlands) or a dropped `-p` (BusyBox parses
        the bare port as HOST: `bad address`) must each redden this by name.
        The orphan bound is the `timeout` prefix plus `_cancel_and_reap`, not
        a netcat option.
        """
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
        for cmd in listen_cmds:
            assert " -l -p " in cmd, f"not the universal listener spelling: {cmd}"
            assert " -w " not in cmd, f"-w is the measured mid-transfer idle-kill: {cmd}"
            assert " -N" not in cmd, f"-N is rejected by every BusyBox row: {cmd}"


class TestNcPutCancellation:
    """External cancellation mid-transfer must reap the remote ``nc -l``.

    ``_attempt`` spawns the listener as an ``asyncio.Task`` and only joins it
    on its normal success / error branches. A caller-side cancellation skips
    those, so ``_attempt`` must cancel the task and reap the remote listener
    itself — otherwise the ``nc -l`` lingers until the remote ``timeout``
    prefix's hard cap an hour later. Nothing shorter ends it: the listener
    spelling carries no ``-w``, which never bounded an unconnected listener
    anyway and kills a stalled TRANSFER (measured 2026-08-25).
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


class TestNcPutCancellationReap:
    """A cancelled put must reap its remote listener even under a second cancel.

    And must not spend the whole teardown window doing it: the reap runs
    inside the graceful window an interrupt promised, and its own sub-bounds
    stack to 8 s (forward setup + connect + close), so it is handed
    ``compensate``'s opt-in ``timeout=`` -- see
    :data:`otto.host.transfer.nc._INTERRUPTED_REAP_TIMEOUT`. The two tests
    here are the two halves of that: the shield must hold a second cancel,
    and the bound must fire when the reap cannot finish.
    """

    @pytest.mark.asyncio
    async def test_cancel_mid_put_reaps_listener_despite_second_cancel(self, tmp_path: Path):
        src = tmp_path / "small.bin"
        src.write_bytes(b"hello world")

        connect_reached = asyncio.Event()
        reap_started = asyncio.Event()
        reap_done: "list[bool]" = []

        async def scripted_exec(cmd: str, timeout: "float | None" = None, **kw: object):
            if " -l " in cmd:
                await asyncio.Event().wait()  # the remote listener runs until reaped
            return _ok("9000\n")

        async def parked_connect(host: str, port: int, timeout: float = 2.0):
            connect_reached.set()
            await asyncio.Event().wait()  # park until the test cancels the put
            raise AssertionError("unreachable")

        async def recording_reap(self, port: int) -> None:
            reap_started.set()
            await asyncio.sleep(0)  # a real suspension: a torn reap stops HERE
            reap_done.append(True)

        exec_cmd = AsyncMock(side_effect=scripted_exec)
        ft = _make_ft(exec_cmd)

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=parked_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=recording_reap),
        ):
            task = asyncio.ensure_future(ft._put_files_nc([src], tmp_path / "dst"))
            await connect_reached.wait()
            task.cancel()  # 1st cancel: tears the transfer, triggers the reap
            await reap_started.wait()
            task.cancel()  # 2nd cancel: lands during the reap — must be held
            with pytest.raises(asyncio.CancelledError):
                await task

        assert reap_done == [True], "the second cancellation tore the listener reap"

    @pytest.mark.asyncio
    async def test_a_reap_that_never_returns_is_abandoned_at_the_bound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The reap is bounded FROM THE CALL, with no second interrupt needed to arm it.

        The listener leak this handler exists to close was measured (todo,
        chaos-teardown-followups §1) as a remote ``nc -l`` outliving the 10 s
        teardown deadline by up to 20 s. A reap that itself cannot finish --
        a wedged hop, a forward that never comes up -- would recreate exactly
        that overrun in the fix, so ``compensate`` is given
        ``_INTERRUPTED_REAP_TIMEOUT`` and the bound is patched to 0.05 s here
        to state the property rather than measure the constant.

        ``reap_cancelled`` is the direct observation: the parked reap was
        CANCELLED, so the bound took its work down rather than walking away
        from it and leaving fresh network I/O running behind the interrupt.
        The ``otto.lifecycle`` warning pins WHOSE bound it is -- reverting the
        adoption leaves the handler unbounded and reddens the whole test.
        """
        src = tmp_path / "small.bin"
        src.write_bytes(b"hello world")

        connect_reached = asyncio.Event()
        reap_started = asyncio.Event()
        reap_cancelled: "list[bool]" = []

        async def scripted_exec(cmd: str, timeout: "float | None" = None, **kw: object):
            if " -l " in cmd:
                await asyncio.Event().wait()  # the remote listener runs until reaped
            return _ok("9000\n")

        async def parked_connect(host: str, port: int, timeout: float = 2.0):
            connect_reached.set()
            await asyncio.Event().wait()  # park until the test cancels the put
            raise AssertionError("unreachable")

        async def parked_reap(self, port: int) -> None:
            reap_started.set()
            try:
                await asyncio.Event().wait()  # a reap that never returns
            except asyncio.CancelledError:
                reap_cancelled.append(True)
                raise

        ft = _make_ft(AsyncMock(side_effect=scripted_exec))

        with (
            patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", 0.05),
            patch.object(transfer_mod, "_connect_with_retry", new=parked_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=parked_reap),
            caplog.at_level("WARNING", logger="otto.lifecycle"),
        ):
            task = asyncio.ensure_future(ft._put_files_nc([src], tmp_path / "dst"))
            await connect_reached.wait()
            task.cancel()
            # Runaway guard, not a measurement: an unbounded reap parks
            # forever and would otherwise burn the suite's cap.
            _done, pending = await asyncio.wait({task}, timeout=10.0)
            assert not pending, "the reap bound never fired: the cancelled put never finished"
            with pytest.raises(asyncio.CancelledError):
                task.result()

        assert reap_started.is_set(), "no reap was attempted at all"
        assert reap_cancelled == [True], (
            "the abandoned reap was left running instead of being cancelled with the bound"
        )
        assert any("nc listener reap" in r.message for r in caplog.records), (
            "the abandonment was never announced: the bound is compensate's, and compensate "
            f"names what it gave up on -- got {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_cancel_and_reap_propagates_cancellation_instead_of_swallowing_it(self):
        """suppress(BaseException) around the listen_task join must not eat a
        genuine CancelledError landing there (e.g. compensate()'s deadline-
        fired task.cancel() racing this method mid-join): swallowing it lets
        the reap start FRESH network I/O after the deadline already fired,
        which the caller's single follow-up cancel then can't kill (force
        path stall)."""
        listen_task_started = asyncio.Event()

        async def _parked_listener() -> None:
            listen_task_started.set()
            await asyncio.Event().wait()  # never resolves except via cancellation

        listen_task = asyncio.ensure_future(_parked_listener())
        reap = AsyncMock()
        ft = _make_ft(AsyncMock(return_value=_ok("9000\n")))

        with patch.object(NcFileTransfer, "_reap_nc_listener", new=reap):
            outer = asyncio.ensure_future(ft._cancel_and_reap(listen_task, 9000))
            await listen_task_started.wait()  # listen_task parked; outer now suspended in the join
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer

        reap.assert_not_awaited()


class TestNcPutStallBound:
    """The LISTEN-vs-accept race (the hop-nc transfer hang, root-caused live
    in the 2026-08 test-infra Wave 2): a connection accepted into the kernel backlog
    but never read parks ``drain()`` on flow control with no deadline —
    probed live: >20s with zero progress through an SSH hop. Every
    data-phase step must be idle-bounded so the stall fails THIS attempt
    while the caller still has budget for the fresh-port retry."""

    @pytest.mark.asyncio
    async def test_a_stalled_drain_fails_the_attempt_instead_of_hanging(self, tmp_path: Path):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * (8192 * 128))

        class _StalledWriter(_FakeWriter):
            async def drain(self) -> None:
                await asyncio.Event().wait()  # accepted-but-unread: never progresses

        stalled_writer = _StalledWriter()

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, stalled_writer

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd, has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.1),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
        ):
            # The outer bound is the pin's own harness: pre-fix code hangs
            # here forever, which must read as a fast failure, not a hang.
            status, msg = _only(
                await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), timeout=5.0),
                src,
            )

        assert status is Status.Error
        assert "no send progress" in msg, msg
        assert stalled_writer.closed, "the stalled writer must still be closed"

    @pytest.mark.asyncio
    async def test_a_stalled_forward_setup_fails_the_attempt(self, tmp_path: Path):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)

        async def parked_forward(port: int) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        exec_cmd = AsyncMock(return_value=_ok("9000\n"))
        ft = _make_ft(exec_cmd, has_tunnel=True)
        ft._connections.forward_port = parked_forward

        with (
            patch.object(transfer_mod, "_NC_FORWARD_SETUP_TIMEOUT", 0.1),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), timeout=5.0),
                src,
            )

        assert status is Status.Error
        assert "port-forward setup" in msg, msg


class TestNcPutStallSemantics:
    """The zero-progress semantics and cleanup guarantees of the stall bounds
    (interim review findings 1, 3, 5): drain's bound must be an actual
    zero-progress window (never a throughput floor), the final drain must be
    guarded for payloads too small to hit the loop drain, a wedged close must
    abort the transport (not leak fd + buffer), and the constants must fit
    the integration wrapper's budget twice (once per attempt)."""

    def _tunneled_ft(self) -> NcFileTransfer:
        ft = _make_ft(AsyncMock(return_value=_ok("9000\n")), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)
        return ft

    @pytest.mark.asyncio
    async def test_a_slow_but_moving_link_is_not_a_stall(self, tmp_path: Path):
        """drain() resolving slower than the window is NOT a stall while the
        write buffer keeps shrinking — a plain wait_for here would impose a
        throughput floor that an impaired lab link legitimately undercuts
        (review finding 1: measured false-stall at 400 KiB/s)."""
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)

        class _SlowWriter(_FakeWriter):
            async def drain(self) -> None:
                # Parks past every (patched) window while the transport's
                # buffer numbers keep decreasing — progress, not a stall —
                # and resolves once the scripted buffer reaches zero, like
                # a real transport crossing its low-water mark.
                if self.transport.buffer_sizes == [0]:
                    self.drain_calls.append(self.written_bytes)
                    return
                await asyncio.sleep(10)
                raise AssertionError("unreachable")  # pragma: no cover

        slow_writer = _SlowWriter()
        slow_writer.transport = _FakeTransport([900, 700, 500, 300, 0])

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, slow_writer

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.1),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._tunneled_ft()._put_files_nc([src], tmp_path / "dst"), timeout=5.0
                ),
                src,
            )

        assert status is Status.Success, msg

    @pytest.mark.asyncio
    async def test_a_stalled_final_drain_fails_a_small_payload(self, tmp_path: Path):
        """A payload under _NC_DRAIN_EVERY blocks never runs the loop drain —
        the FINAL drain is then the only stall guard, and it was unpinned
        (review finding 5: the one bound the live soak exercised)."""
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)  # far below 64 blocks

        class _StalledWriter(_FakeWriter):
            async def drain(self) -> None:
                await asyncio.Event().wait()

        stalled_writer = _StalledWriter()
        stalled_writer.transport = _FakeTransport([1024])  # never shrinks

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, stalled_writer

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.1),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._tunneled_ft()._put_files_nc([src], tmp_path / "dst"), timeout=5.0
                ),
                src,
            )

        assert status is Status.Error
        assert "no send progress" in msg, msg

    @pytest.mark.asyncio
    async def test_a_wedged_close_is_aborted_not_leaked(self, tmp_path: Path):
        """A stalled channel never completes its graceful close — the bounded
        close must ABORT the transport (review finding 3: close()+suppress
        left the fd open with 32 MB buffered)."""
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)

        class _WedgedCloseWriter(_FakeWriter):
            async def wait_closed(self) -> None:
                await asyncio.Event().wait()

        wedged_writer = _WedgedCloseWriter()

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, wedged_writer

        with (
            patch.object(transfer_mod, "_NC_CLOSE_TIMEOUT", 0.1),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._tunneled_ft()._put_files_nc([src], tmp_path / "dst"), timeout=5.0
                ),
                src,
            )

        assert status is Status.Success, msg
        assert wedged_writer.closed
        assert wedged_writer.transport.aborted, (
            "a close that cannot complete must abort the transport, not leak it"
        )

    def test_data_path_bounds_fit_the_integration_budget_twice(self):
        """Every error path retries once on a fresh port, so the data-path
        bounds must fit twice inside the integration wrapper's transfer
        budget with setup headroom — pins the MAGNITUDE of the constants
        (review finding 5: 10.0 -> 1000.0 previously passed everything;
        'do not paper with a longer timeout' had no teeth)."""
        from tests.integration.host._transfer_retry import DEFAULT_TRANSFER_TIMEOUT

        per_attempt = (
            transfer_mod._NC_STALL_TIMEOUT
            + transfer_mod._NC_FORWARD_SETUP_TIMEOUT
            + transfer_mod._NC_CLOSE_TIMEOUT
        )
        assert 2 * per_attempt <= DEFAULT_TRANSFER_TIMEOUT, (
            f"two attempts of bounded data-path steps ({2 * per_attempt}s) must fit "
            f"the {DEFAULT_TRANSFER_TIMEOUT}s integration transfer budget — a bigger "
            f"bound papers over the stall instead of surfacing it while retry "
            f"budget remains"
        )

    @pytest.mark.asyncio
    async def test_progress_once_does_not_rearm_forever(self, tmp_path: Path):
        """The zero-progress baseline must ADVANCE: progress in window 1
        followed by zero progress thereafter must still fail. Deleting
        ``last = now`` re-arms forever off the stale baseline after a single
        byte of progress — the unbounded hang back through the side door
        (verification review, N1)."""
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 1024)

        class _StalledAfterOneWindow(_FakeWriter):
            async def drain(self) -> None:
                await asyncio.Event().wait()

        writer = _StalledAfterOneWindow()
        # One real decrease (1000 -> 900), then frozen at 900 forever.
        writer.transport = _FakeTransport([1000, 900])

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            return None, writer

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.1),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._tunneled_ft()._put_files_nc([src], tmp_path / "dst"), timeout=5.0
                ),
                src,
            )

        assert status is Status.Error
        assert "no send progress" in msg, msg
