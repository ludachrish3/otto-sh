"""Tests for ``_get_files_nc`` and ``_get_files_nc_tunneled``: GET path coverage.

These tests cover:
- ``_get_files_nc`` (non-tunnel happy path): socket server is started, the
  ``_on_connect`` callback is captured and invoked directly with a fake reader
  so the read loop writes bytes to the destination file, resolving the
  done-future with ``(Status.Success, "")``.

- ``_get_files_nc_tunneled`` (tunnel path): happy path plus three error
  branches — listener-wait ``ConnectionError`` (lines 709-712),
  forward+connect ``ConnectionError`` (lines 722-725), and listen-task timeout
  (lines 750-757).
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.utils import Status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(output: str = "") -> "transfer_mod.CommandStatus":  # type: ignore[name-defined]
    from otto.utils import CommandStatus

    return CommandStatus(command="", output=output, status=Status.Success, retcode=0)


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

            status, msg = await get_task

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

            status, msg = await get_task

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

            status, msg = await get_task

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
            status, msg = await ft._get_files_nc([src_remote], dst_dir)

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

            status, msg = await get_task

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
            new=AsyncMock(return_value=(Status.Success, "")),
        ) as mock_tunneled:
            status, msg = await ft._get_files_nc([src_remote], dst_dir)

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
            status, msg = await ft._get_files_nc_tunneled([src_remote], dst_dir)

        assert status is Status.Success, msg
        assert msg == ""
        dst_file = dst_dir / "data.bin"
        assert dst_file.exists(), "destination file was not created"
        assert dst_file.read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_listener_wait_error_returns_status_error(self, tmp_path: Path) -> None:
        """Lines 709-712: _wait_for_remote_listener raises ConnectionError → Status.Error."""
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
            status, msg = await asyncio.wait_for(
                ft._get_files_nc_tunneled([src_remote], dst_dir),
                timeout=5.0,
            )

        assert status is Status.Error, msg
        # Exact message from line 712.
        assert "Remote nc listener on port" in msg
        assert "not ready" in msg

    @pytest.mark.asyncio
    async def test_forward_connect_error_returns_status_error(self, tmp_path: Path) -> None:
        """Lines 722-725: _connect_with_retry raises ConnectionError → Status.Error."""
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
            status, msg = await asyncio.wait_for(
                ft._get_files_nc_tunneled([src_remote], dst_dir),
                timeout=5.0,
            )

        assert status is Status.Error, msg
        # Exact message from line 725.
        assert "nc listener on localhost:" in msg
        assert "not ready" in msg

    @pytest.mark.asyncio
    async def test_listen_task_timeout_returns_status_error(self, tmp_path: Path) -> None:
        """Lines 750-757: listen_task exceeds listener_timeout → Status.Error with 'orphaned'.

        The listen_task is the asyncio.Task wrapping the ``nc -Nl`` exec.
        We make the nc -Nl exec block forever (orphaned listener) so that the
        ``asyncio.wait_for(listen_task, timeout=...)`` at line 746 fires.
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

        fake_reader = FakeReader([b"x"])
        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)

        exec_cmd = AsyncMock(side_effect=exec_side)
        # Very short listener_timeout so the wait_for at line 746 fires quickly.
        ft = _make_ft(exec_cmd, has_tunnel=True, listener_timeout=0.05)
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
            status, msg = await asyncio.wait_for(
                ft._get_files_nc_tunneled([src_remote], dst_dir),
                timeout=5.0,
            )

        assert status is Status.Error, msg
        # Exact message from lines 753-756.
        assert "did not exit within" in msg
        assert "orphaned listener" in msg

    @pytest.mark.asyncio
    async def test_progress_handler_called_during_read(self, tmp_path: Path) -> None:
        """Line 737: progress handler fires for each chunk inside the tunneled read loop."""
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
            status, msg = await ft._get_files_nc_tunneled([src_remote], dst_dir, factory)

        assert status is Status.Success, msg
        assert len(progress_calls) == 2
        assert progress_calls[0] == (5, 10)
        assert progress_calls[1] == (10, 10)
