"""Tests for ``_get_files_nc`` and ``_get_files_nc_tunneled``: GET path coverage.

These tests cover:
- ``_get_files_nc`` (non-tunnel happy path): socket server is started, the
  ``_on_connect`` callback is captured and invoked directly with a fake reader
  so the read loop writes bytes to the destination file, resolving the
  done-future with ``(Status.Success, "")``.

- ``_get_files_nc_tunneled`` (tunnel path): happy path plus three error
  branches — listener-wait ``ConnectionError``, forward+connect
  ``ConnectionError``, and listen-task timeout.

- ``TestNcGetTunneledCancellation`` (chaos hardening Plan 4, Task 7): external
  cancellation mid-GET must cancel+reap the remote ``nc -Nl`` listener, not
  leak it for ``listener_timeout`` seconds (30s by default — longer than the
  10s teardown deadline; ``todo/chaos-teardown-followups.md`` §1). Mirrors
  ``test_transfer_nc_put.py::TestNcPutCancellation`` for the reversed-listener
  GET path.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.result import CommandResult, Result
from otto.utils import Status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _only(per_file: dict, src: Path) -> tuple[Status, str]:
    """Unwrap the single per-file ``(status, msg)`` from a nc transfer's mapping."""
    r = per_file[src]
    return r.status, r.msg


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


class FakeReader:
    """Minimal ``asyncio.StreamReader`` stand-in that yields queued chunks then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = [*list(chunks), b""]

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0)


# ---------------------------------------------------------------------------
# _get_files_nc — non-tunnel happy path
# ---------------------------------------------------------------------------


class TestGetFilesNcNonTunnel:
    """``_get_files_nc`` (non-tunnel): server started, callback invoked, file written."""

    @pytest.mark.asyncio
    async def test_happy_path_writes_file(self, tmp_path: Path) -> None:
        """The read loop writes all chunks to dst; result is (Status.Success, "")."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        # The stat call returns the byte count of our fake content.
        exec_cmd = AsyncMock(return_value=_ok("5\n"))
        ft = _make_ft(exec_cmd, has_tunnel=False)

        # Fake server whose sockets[0].getsockname() returns ("0.0.0.0", 54321).
        fake_server = MagicMock()
        fake_server.sockets = [MagicMock()]
        fake_server.sockets[0].getsockname.return_value = ("0.0.0.0", 54321)
        fake_server.close = MagicMock()
        fake_server.wait_closed = AsyncMock(return_value=None)

        captured_callback: list = []

        async def fake_start_server(callback, host, port):
            captured_callback.append(callback)
            return fake_server

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("5\n")
            # nc sender command — return quickly so await send_task doesn't block.
            return _ok()

        ft._exec_cmd = AsyncMock(side_effect=exec_side)  # type: ignore[method-assign]

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            # Run the GET in a task so we can inject the callback concurrently.
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))

            # Yield until fake_start_server has been called and registered the callback.
            for _ in range(20):
                await asyncio.sleep(0)
                if captured_callback:
                    break

            assert captured_callback, "start_server was never called"
            on_connect = captured_callback[0]

            # Invoke the callback directly — simulates a client connecting.
            fake_writer = MagicMock()
            fake_writer.close = MagicMock()
            fake_writer.wait_closed = AsyncMock(return_value=None)
            await on_connect(FakeReader([b"hello"]), fake_writer)

            status, msg = _only(await get_task, src_remote)

        assert status is Status.Success, msg
        assert msg == ""
        dst_file = dst_dir / "data.bin"
        assert dst_file.exists(), "destination file was not created"
        assert dst_file.read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_happy_path_multiple_chunks(self, tmp_path: Path) -> None:
        """Multiple chunks are concatenated in the destination file."""
        src_remote = Path("/remote/multi.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        ft = _make_ft(AsyncMock(return_value=_ok("0\n")), has_tunnel=False)

        fake_server = MagicMock()
        fake_server.sockets = [MagicMock()]
        fake_server.sockets[0].getsockname.return_value = ("0.0.0.0", 54322)
        fake_server.close = MagicMock()
        fake_server.wait_closed = AsyncMock(return_value=None)

        captured_callback: list = []

        async def fake_start_server(callback, host, port):
            captured_callback.append(callback)
            return fake_server

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("0\n")
            return _ok()

        ft._exec_cmd = AsyncMock(side_effect=exec_side)  # type: ignore[method-assign]

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))

            for _ in range(20):
                await asyncio.sleep(0)
                if captured_callback:
                    break

            assert captured_callback
            on_connect = captured_callback[0]
            fake_writer = MagicMock()
            fake_writer.close = MagicMock()
            fake_writer.wait_closed = AsyncMock(return_value=None)
            await on_connect(FakeReader([b"foo", b"bar", b"baz"]), fake_writer)

            status, msg = _only(await get_task, src_remote)

        assert status is Status.Success, msg
        dst_file = dst_dir / "multi.bin"
        assert dst_file.read_bytes() == b"foobarbaz"

    @pytest.mark.asyncio
    async def test_on_connect_exception_sets_error_result(self, tmp_path: Path) -> None:
        """Lines 630-631: exception inside _on_connect → done resolved with Status.Error.

        A reader that raises on ``read()`` exercises the ``except Exception`` block
        at lines 630-631.  The exception message propagates as the error string.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        class BrokenReader:
            """A reader whose ``read()`` raises to simulate an I/O error."""

            async def read(self, _n: int) -> bytes:
                raise OSError("simulated read failure")

        fake_server = MagicMock()
        fake_server.sockets = [MagicMock()]
        fake_server.sockets[0].getsockname.return_value = ("0.0.0.0", 54323)
        fake_server.close = MagicMock()
        fake_server.wait_closed = AsyncMock(return_value=None)

        captured_callback: list = []

        async def fake_start_server(callback, host, port):
            captured_callback.append(callback)
            return fake_server

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("0\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=False)
        ft._exec_cmd = AsyncMock(side_effect=exec_side)  # type: ignore[method-assign]

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))

            for _ in range(20):
                await asyncio.sleep(0)
                if captured_callback:
                    break

            assert captured_callback, "start_server was never called"
            on_connect = captured_callback[0]

            fake_writer = MagicMock()
            fake_writer.close = MagicMock()
            fake_writer.wait_closed = AsyncMock(return_value=None)
            await on_connect(BrokenReader(), fake_writer)

            status, msg = _only(await get_task, src_remote)

        assert status is Status.Error, msg
        assert "simulated read failure" in msg

    @pytest.mark.asyncio
    async def test_send_task_failure_propagates_to_done_future(self, tmp_path: Path) -> None:
        """Lines 649-651: if send_task raises before _on_connect fires, done gets Status.Error.

        We arrange for the nc sender exec to raise an OSError.  Since we never
        invoke _on_connect, ``done`` is not yet resolved when the callback fires,
        so lines 649-651 set the error result on the future.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        fake_server = MagicMock()
        fake_server.sockets = [MagicMock()]
        fake_server.sockets[0].getsockname.return_value = ("0.0.0.0", 54324)
        fake_server.close = MagicMock()
        fake_server.wait_closed = AsyncMock(return_value=None)

        async def fake_start_server(callback, host, port):
            # Capture but never invoke — the send_task failure drives done instead.
            return fake_server

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("0\n")
            # nc sender command ("-N"): raise to simulate a transport failure.
            if "-N " in cmd:
                raise OSError("send transport failed")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=False)
        ft._exec_cmd = AsyncMock(side_effect=exec_side)  # type: ignore[method-assign]

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            status, msg = _only(await ft._get_files_nc([src_remote], dst_dir), src_remote)

        assert status is Status.Error, msg
        assert "send transport failed" in msg

    @pytest.mark.asyncio
    async def test_progress_handler_called_during_read(self, tmp_path: Path) -> None:
        """Line 627: progress handler fires for each chunk read inside _on_connect."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        progress_calls: list[tuple[int, int]] = []

        def handler(src: str, dst: str, bytes_done: int, total: int) -> None:
            progress_calls.append((bytes_done, total))

        def factory():
            return handler

        fake_server = MagicMock()
        fake_server.sockets = [MagicMock()]
        fake_server.sockets[0].getsockname.return_value = ("0.0.0.0", 54325)
        fake_server.close = MagicMock()
        fake_server.wait_closed = AsyncMock(return_value=None)

        captured_callback: list = []

        async def fake_start_server(callback, host, port):
            captured_callback.append(callback)
            return fake_server

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("10\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=False)
        ft._exec_cmd = AsyncMock(side_effect=exec_side)  # type: ignore[method-assign]

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir, factory))

            for _ in range(20):
                await asyncio.sleep(0)
                if captured_callback:
                    break

            assert captured_callback
            on_connect = captured_callback[0]
            fake_writer = MagicMock()
            fake_writer.close = MagicMock()
            fake_writer.wait_closed = AsyncMock(return_value=None)
            await on_connect(FakeReader([b"hello", b"world"]), fake_writer)

            status, msg = _only(await get_task, src_remote)

        assert status is Status.Success, msg
        assert len(progress_calls) == 2
        assert progress_calls[0] == (5, 10)
        assert progress_calls[1] == (10, 10)

    @pytest.mark.asyncio
    async def test_get_files_nc_dispatches_to_tunneled_when_has_tunnel(
        self, tmp_path: Path
    ) -> None:
        """Line 594: ``_get_files_nc`` with ``has_tunnel=True`` dispatches to tunneled.

        When ``_connections.has_tunnel`` is True, ``_get_files_nc`` calls
        ``_get_files_nc_tunneled`` immediately.  We verify by patching the tunneled
        method and confirming it receives the call.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        exec_cmd = AsyncMock(return_value=_ok("0\n"))
        ft = _make_ft(exec_cmd, has_tunnel=True)

        with patch.object(
            NcFileTransfer,
            "_get_files_nc_tunneled",
            new=AsyncMock(return_value={src_remote: Result(Status.Success, value=dst_dir)}),
        ) as mock_tunneled:
            status, msg = _only(await ft._get_files_nc([src_remote], dst_dir), src_remote)

        assert status is Status.Success, msg
        mock_tunneled.assert_awaited_once()


# ---------------------------------------------------------------------------
# _get_files_nc_tunneled — happy path + error branches
# ---------------------------------------------------------------------------


class TestGetFilesNcTunneled:
    """``_get_files_nc_tunneled``: four scenarios."""

    @pytest.mark.asyncio
    async def test_happy_path_writes_file(self, tmp_path: Path) -> None:
        """Happy path: all seams succeed; dst file contains streamed bytes."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        fake_reader = FakeReader([b"hello"])
        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                # Remote file-size stat: return size.
                return _ok("5\n")
            if "-Nl" in cmd:
                # nc -Nl listener: complete immediately — data transferred via FakeReader.
                return _ok()
            # ss port-finding, warmup "true", etc.
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd, has_tunnel=True, listener_timeout=5.0)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(return_value=(fake_reader, fake_writer)),
            ),
        ):
            status, msg = _only(await ft._get_files_nc_tunneled([src_remote], dst_dir), src_remote)

        assert status is Status.Success, msg
        assert msg == ""
        dst_file = dst_dir / "data.bin"
        assert dst_file.exists(), "destination file was not created"
        assert dst_file.read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_listener_wait_error_returns_status_error(self, tmp_path: Path) -> None:
        """_wait_for_remote_listener raises ConnectionError → Status.Error."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                return _ok("0\n")
            if "-Nl" in cmd:
                # nc -Nl listener: block until cancelled (orphaned because wait raises first).
                await asyncio.Event().wait()
            # ss port-finding, warmup, etc. → return a valid port number.
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd, has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with patch.object(
            NcFileTransfer,
            "_wait_for_remote_listener",
            new=AsyncMock(side_effect=ConnectionError("probe failed")),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        # Exact message from line 712.
        assert "Remote nc listener on port" in msg
        assert "not ready" in msg

    @pytest.mark.asyncio
    async def test_forward_connect_error_returns_status_error(self, tmp_path: Path) -> None:
        """_connect_with_retry raises ConnectionError → Status.Error."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                return _ok("0\n")
            if "-Nl" in cmd:
                # nc -Nl listener: block until cancelled (connect error fires first).
                await asyncio.Event().wait()
            # ss port-finding, warmup, etc.
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd, has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=ConnectionError("refused")),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        # Exact message from the connect-failure branch.
        assert "nc listener on localhost:" in msg
        assert "not ready" in msg

    @pytest.mark.asyncio
    async def test_listen_task_timeout_returns_status_error(self, tmp_path: Path) -> None:
        """listen_task exceeds listener_timeout → Status.Error with 'orphaned'.

        The listen_task is the asyncio.Task wrapping the ``nc -Nl`` exec.
        We make the nc -Nl exec block forever (orphaned listener) so that the
        ``asyncio.wait_for(listen_task, timeout=...)`` fires. ``_get_one``
        retries the attempt once on a fresh port (the listener-readiness-race
        recovery), so the streams are built fresh per attempt and the final
        result is the SECOND attempt's identical error.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        listener_blocked = asyncio.Event()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                return _ok("0\n")
            if "-Nl" in cmd:
                # Orphaned listener: nc never exits, simulating a port-collision scenario.
                await listener_blocked.wait()
                return _ok()  # pragma: no cover
            # ss port-finding, warmup, etc.
            return _ok("9000\n")

        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        exec_cmd = AsyncMock(side_effect=exec_side)
        # Very short listener_timeout so the listen-task join fires quickly.
        ft = _make_ft(exec_cmd, has_tunnel=True, listener_timeout=0.05)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"x"]), fake_writer)),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        # Exact message from lines 753-756.
        assert "did not exit within" in msg
        assert "orphaned listener" in msg

    @pytest.mark.asyncio
    async def test_progress_handler_called_during_read(self, tmp_path: Path) -> None:
        """Progress handler fires for each chunk inside the tunneled read loop."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        progress_calls: list[tuple[int, int]] = []

        def handler(src: str, dst: str, bytes_done: int, total: int) -> None:
            progress_calls.append((bytes_done, total))

        def factory():
            return handler

        fake_reader = FakeReader([b"hello", b"world"])
        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                return _ok("10\n")
            if "-Nl" in cmd:
                return _ok()
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd, has_tunnel=True, listener_timeout=5.0)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(return_value=(fake_reader, fake_writer)),
            ),
        ):
            status, msg = _only(
                await ft._get_files_nc_tunneled([src_remote], dst_dir, factory), src_remote
            )

        assert status is Status.Success, msg
        assert len(progress_calls) == 2
        assert progress_calls[0] == (5, 10)
        assert progress_calls[1] == (10, 10)


# ============================================================================
# TestNcGetTunneledCancellation — chaos hardening Plan 4, Task 7
# ============================================================================


class TestNcGetTunneledCancellation:
    """External cancellation mid-GET must reap the remote ``nc -Nl`` listener.

    ``_get_files_nc_tunneled``'s inner ``_get_one`` spawns the remote listener
    (``nc -Nl -w <listener_timeout> <port> < <src>``) as an ``asyncio.Task``
    and only joins it on its normal success / ``ConnectionError`` / timeout
    branches. A caller-side cancellation skips all of those, so — mirroring
    ``test_transfer_nc_put.py::TestNcPutCancellation`` for the put path's
    ``_attempt`` — ``_get_one`` must cancel the listener task and reap the
    remote ``nc -l`` itself. Pre-fix there is no ``except
    asyncio.CancelledError`` handler at all in ``_get_one``, so the listener
    is left running until its own ``-w`` timeout (30s default), which
    outlives the 10s teardown deadline (``todo/chaos-teardown-followups.md``
    §1; chaos spec success-criterion #1).
    """

    @pytest.mark.asyncio
    async def test_cancellation_reaps_listener(self, tmp_path: Path) -> None:
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        listener_started = asyncio.Event()

        async def exec_side_effect(cmd: str, timeout: "float | None" = None, **kw: object):
            if "-Nl" in cmd:
                # The remote listener "runs" until its task is cancelled.
                listener_started.set()
                await asyncio.Event().wait()
            return _ok("9000\n")

        exec_cmd = AsyncMock(side_effect=exec_side_effect)
        ft = _make_ft(exec_cmd, has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        reap_calls: list[int] = []

        async def fake_reap(port: int) -> None:
            reap_calls.append(port)

        async def block_forever(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        # _wait_for_remote_listener blocks forever, so the cancellation lands
        # after listen_task is spawned but before the forward/connect step —
        # the window the fix targets.
        with (
            patch.object(ft, "_reap_nc_listener", new=fake_reap),
            patch.object(
                NcFileTransfer,
                "_wait_for_remote_listener",
                new=AsyncMock(side_effect=block_forever),
            ),
        ):
            task = asyncio.create_task(ft._get_files_nc_tunneled([src_remote], dst_dir))
            await asyncio.wait_for(listener_started.wait(), timeout=2.0)
            await asyncio.sleep(0)  # let _get_one reach _wait_for_remote_listener
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert reap_calls == [9000], (
            f"cancellation must reap the remote nc GET listener, got {reap_calls}"
        )

    @pytest.mark.asyncio
    async def test_a_reap_that_never_returns_is_abandoned_at_the_bound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """GET's reap is bounded from the call, exactly as PUT's is.

        Same argument, same constant
        (:data:`otto.host.transfer.nc._INTERRUPTED_REAP_TIMEOUT`), same
        failure it prevents: a reap that cannot finish -- a wedged hop, a
        forward that never comes up -- would hold teardown past the deadline
        the interrupt promised, which is the very overrun the listener reap
        exists to end. Patched to 0.05 s so this states the property rather
        than measuring the constant; ``reap_cancelled`` is the direct
        observation that the bound took the work down rather than walking
        away from it. The mirror of
        ``test_transfer_nc_put.py::TestNcPutCancellationReap``'s bound test.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        listener_started = asyncio.Event()
        reap_started = asyncio.Event()
        reap_cancelled: "list[bool]" = []

        async def exec_side_effect(cmd: str, timeout: "float | None" = None, **kw: object):
            if "-Nl" in cmd:
                listener_started.set()
                await asyncio.Event().wait()  # the remote listener runs until reaped
            return _ok("9000\n")

        async def parked_reap(port: int) -> None:
            reap_started.set()
            try:
                await asyncio.Event().wait()  # a reap that never returns
            except asyncio.CancelledError:
                reap_cancelled.append(True)
                raise

        async def block_forever(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        ft = _make_ft(AsyncMock(side_effect=exec_side_effect), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", 0.05),
            patch.object(ft, "_reap_nc_listener", new=parked_reap),
            patch.object(
                NcFileTransfer,
                "_wait_for_remote_listener",
                new=AsyncMock(side_effect=block_forever),
            ),
            caplog.at_level("WARNING", logger="otto.lifecycle"),
        ):
            task = asyncio.create_task(ft._get_files_nc_tunneled([src_remote], dst_dir))
            await asyncio.wait_for(listener_started.wait(), timeout=2.0)
            await asyncio.sleep(0)  # let _get_one reach _wait_for_remote_listener
            task.cancel()
            # Runaway guard, not a measurement: an unbounded reap parks
            # forever and would otherwise burn the suite's cap.
            _done, pending = await asyncio.wait({task}, timeout=10.0)
            assert not pending, "the reap bound never fired: the cancelled get never finished"
            with pytest.raises(asyncio.CancelledError):
                task.result()

        assert reap_started.is_set(), "no reap was attempted at all"
        assert reap_cancelled == [True], (
            "the abandoned reap was left running instead of being cancelled with the bound"
        )
        assert any("nc get listener reap" in r.message for r in caplog.records), (
            "the abandonment was never announced: the bound is compensate's, and compensate "
            f"names what it gave up on -- got {[r.message for r in caplog.records]}"
        )


class TestNcGetStallBoundAndRetry:
    """The GET face of the LISTEN-vs-accept race (the hop-nc transfer hang,
    root-caused live in the 2026-08 test-infra Wave 2): an accepted-but-unserviced
    forward parks ``read()`` forever (probed live: >20s), and a connection
    dropped in the accept window closes cleanly at 0 bytes — a ghost success.
    Reads are idle-bounded, the received size is verified, and ``_get_one``
    retries once on a fresh port, mirroring ``_put_one``."""

    def _ft(self, exec_side) -> NcFileTransfer:
        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)
        return ft

    @staticmethod
    async def _exec_ok(cmd: str, timeout=None, **kw):
        if "stat -c" in cmd:
            return _ok("3\n")
        return _ok("9000\n")

    @pytest.mark.asyncio
    async def test_a_stalled_read_fails_the_attempt_instead_of_hanging(self, tmp_path: Path):
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        class _StalledReader:
            async def read(self, _n: int) -> bytes:
                await asyncio.Event().wait()  # accepted but never serviced
                raise AssertionError("unreachable")  # pragma: no cover

        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.1),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (_StalledReader(), fake_writer)),
            ),
        ):
            # Outer bound = the pin's own harness: pre-fix code hangs forever.
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(self._exec_ok)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Error
        assert "no data for" in msg, msg

    @pytest.mark.asyncio
    async def test_a_short_read_is_an_error_not_a_ghost_success(self, tmp_path: Path):
        """EOF at 0 of 3 expected bytes = the connection was dropped in the
        accept window. Pre-fix this returned Success with an empty file."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([]), fake_writer)),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(self._exec_ok)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Error
        assert "empty transfer" in msg, msg
        assert "3 bytes" in msg, msg

    @pytest.mark.asyncio
    async def test_the_fresh_port_retry_recovers_the_race(self, tmp_path: Path):
        """Attempt 1 loses the accept-window race (short read); attempt 2
        delivers. The designed recovery — previously PUT-only — must turn
        this into a Success with the full payload on disk."""
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        # Exactly the two transfer attempts. The reap is patched out below, so
        # nothing else draws from this list — see the patch stack for why.
        attempts: list[FakeReader] = [FakeReader([]), FakeReader([b"abc"])]

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (attempts.pop(0), fake_writer)),
            ),
            # The reap is patched out because it would draw from `attempts`
            # NON-DETERMINISTICALLY. `_cancel_and_reap` skips a listener task
            # that is already done, and whether it is done at that instant is
            # event-loop scheduling: measured 2026-08-10, 3.10 and 3.11 skip the
            # reap here while 3.12, 3.13 and 3.14 perform it, so a fixed-length
            # stub list passes on two interpreters and fails on three. That is a
            # property of this stub, not of the code under test — this test is
            # about the fresh-port retry, and the reap has its own tests in
            # test_transfer_nc_listener_reap.py.
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(self._exec_ok)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        assert (dst_dir / "data.bin").read_bytes() == b"abc"
        assert not attempts, "both prepared attempts must have been consumed"


class TestNcGetStallSemantics:
    """GET twins of the stall-semantics pins (interim review finding 5): the
    forward-setup bound and the wedged-close abort existed on GET but were
    unpinned — the PUT twin alone leaves the GET copy free to regress — and
    the size-check semantics (finding 4) are pinned in BOTH directions:
    empty-vs-known-size fails, changed-size and unknown-size do not."""

    def _ft(self, exec_side) -> NcFileTransfer:
        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(return_value=15000)
        return ft

    @staticmethod
    def _writer():
        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)
        return fake_writer

    @staticmethod
    async def _exec_sized(cmd: str, timeout=None, **kw):
        if "stat -c" in cmd:
            return _ok("100\n")
        return _ok("9000\n")

    @staticmethod
    async def _exec_stat_fails(cmd: str, timeout=None, **kw):
        if "stat -c" in cmd:
            return CommandResult(command=cmd, value="", status=Status.Error, retcode=1)
        return _ok("9000\n")

    @pytest.mark.asyncio
    async def test_a_stalled_forward_setup_fails_the_attempt(self, tmp_path: Path):
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def parked_forward(port: int) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        ft = _make_ft(AsyncMock(side_effect=self._exec_sized), has_tunnel=True)
        ft._connections.forward_port = parked_forward

        with (
            patch.object(transfer_mod, "_NC_FORWARD_SETUP_TIMEOUT", 0.1),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Error
        assert "port-forward setup" in msg, msg

    @pytest.mark.asyncio
    async def test_a_wedged_close_is_aborted_not_leaked(self, tmp_path: Path):
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        fake_writer = MagicMock()
        fake_writer.close = MagicMock()

        async def wedged_wait_closed():
            await asyncio.Event().wait()

        fake_writer.wait_closed = wedged_wait_closed
        fake_writer.transport = MagicMock()

        async def exec_three(cmd: str, timeout=None, **kw):
            if "stat -c" in cmd:
                return _ok("3\n")
            return _ok("9000\n")

        with (
            patch.object(transfer_mod, "_NC_CLOSE_TIMEOUT", 0.1),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"abc"]), fake_writer)),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(exec_three)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        fake_writer.transport.abort.assert_called()

    @pytest.mark.asyncio
    async def test_a_changed_remote_file_is_not_failed_by_the_stale_stat(self, tmp_path: Path):
        """The pre-transfer stat is a snapshot; a growing/changed remote file
        legitimately delivers a different byte count (review finding 4: the
        first cut hard-failed any mismatch — a live log through a hop would
        have errored where pre-fix it succeeded)."""
        src_remote = Path("/remote/growing.log")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"abc"]), self._writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(self._exec_sized)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        assert (dst_dir / "growing.log").read_bytes() == b"abc"

    @pytest.mark.asyncio
    async def test_an_unknown_size_cannot_vouch_so_the_empty_check_skips(self, tmp_path: Path):
        """stat failure means no size is known — the empty-transfer check
        deliberately skips rather than guesses. This residual ghost window
        (empty file delivered when the stat failed) is a DOCUMENTED
        acceptance; this pin keeps it deliberate, so closing it is a design
        change, not a drive-by."""
        src_remote = Path("/remote/unknown.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([]), self._writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    self._ft(self._exec_stat_fails)._get_files_nc_tunneled([src_remote], dst_dir),
                    timeout=5.0,
                ),
                src_remote,
            )

        assert status is Status.Success, msg
