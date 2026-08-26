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
  cancellation mid-GET must cancel+reap the remote ``nc -l -p`` listener, not
  leak it for ``listener_timeout`` seconds (30s by default — longer than the
  10s teardown deadline; ``todo/chaos-teardown-followups.md`` §1). Mirrors
  ``test_transfer_nc_put.py::TestNcPutCancellation`` for the reversed-listener
  GET path.
"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions, UserlandOptions
from otto.host.transfer import NcFileTransfer
from otto.host.userland import Userland
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
    userland: "Userland | None" = None,
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
        userland=userland,
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
                return _ok("5 regular file\n")
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
        """Multiple chunks are concatenated in the destination file.

        The stat answers the payload's real length because the read loop
        terminates on it — a size is the transfer's bound now, not a progress
        decoration (see :class:`TestThePlainGetIsSizeTerminated`).
        """
        src_remote = Path("/remote/multi.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        ft = _make_ft(AsyncMock(return_value=_ok("9\n")), has_tunnel=False)

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
                return _ok("9 regular file\n")
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
                # Non-zero, or the loop owes no bytes and never reads at all —
                # the raise this test is about would not happen.
                return _ok("5 regular file\n")
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
                return _ok("0 regular empty file\n")
            # The nc sender: keyed on its redirection, because the spelling no
            # longer carries a `-N` to recognise it by.
            if " < " in cmd:
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
                return _ok("10 regular file\n")
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


class _SilenceAfterPayload:
    """*payload* in ``read``-sized bites, then SILENCE — never EOF.

    The shape of an ``-N``-less sender that has reached EOF on its input and
    is waiting for the RECEIVER to close (measured 2026-08-25 on all six
    userlands). ``FakeReader`` cannot stand in for it: its trailing ``b""``
    hands the loop the EOF this sender never sends.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, n: int) -> bytes:
        if self._payload:
            block, self._payload = self._payload[:n], self._payload[n:]
            return block
        await asyncio.Event().wait()  # still connected, saying nothing
        raise AssertionError("unreachable")  # pragma: no cover


def _fake_server(port: int) -> MagicMock:
    """An ``asyncio.start_server`` stand-in reporting a local bind on *port*."""
    server = MagicMock()
    server.sockets = [MagicMock()]
    server.sockets[0].getsockname.return_value = ("0.0.0.0", port)
    server.close = MagicMock()
    server.wait_closed = AsyncMock(return_value=None)
    return server


def _fake_writer() -> MagicMock:
    """A ``StreamWriter`` stand-in whose ``close`` is observable."""
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock(return_value=None)
    return writer


async def _captured_callback(captured: "list[Any]") -> Any:
    """The ``_on_connect`` the patched ``start_server`` registered, once it has.

    The same spin the older non-tunnel tests inline: yield to the loop until
    ``_get_one`` has reached its ``start_server`` call.
    """
    for _ in range(20):
        await asyncio.sleep(0)
        if captured:
            return captured[0]
    raise AssertionError("start_server was never called")


async def _abandon(*tasks: "asyncio.Task[Any]") -> None:
    """Cancel and join *tasks* — a red run must not strand a pending task."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestThePlainGetIsSizeTerminated:
    """The plain GET reads exactly the stat-reported size, then closes.

    Nothing in the stream says "done" any more. The sender carries no ``-N``
    — BusyBox's applet rejects the option outright — so it holds the socket
    open after its last byte and waits for the receiver's close (measured
    2026-08-25 on all six userlands). The stat prefetch, which used to
    decorate a progress bar, is therefore the read loop's terminator, and
    each of its failure modes becomes a verdict rather than a cosmetic
    degrade: EOF before N is a short read, and a stat that could not answer
    is a refusal for that file rather than a 0 that reads nothing and calls
    it success.
    """

    @pytest.mark.asyncio
    async def test_a_short_read_is_an_error_not_a_truncated_success(self, tmp_path: Path) -> None:
        """EOF before the stat-reported size must FAIL, naming got/expected.

        Read-to-EOF made a clean FIN mid-transfer (a killed sender, or the
        ``-w`` idle-kill measured 2026-08-25) indistinguishable from
        completion: the silent-truncation shape. Against a known N, short is
        an error. Mutation check: revert the loop to read-to-EOF and this
        test reports a truncated file as Success — it must be RED then.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("64 regular file\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54330)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            # Half the file, then FIN: a killed sender, a dropped link, the
            # `-w` idle-kill. Whatever caused it, 32 of 64 bytes arrived.
            await on_connect(FakeReader([b"x" * 32]), _fake_writer())
            status, msg = _only(await asyncio.wait_for(get_task, timeout=5.0), src_remote)

        assert status is Status.Error, msg
        assert "short read" in msg, msg
        assert "64" in msg, f"the error must name what was expected: {msg}"
        assert "32" in msg, f"the error must name what arrived: {msg}"

    @pytest.mark.asyncio
    async def test_a_failed_stat_refuses_the_file_before_any_listener_exists(
        self, tmp_path: Path
    ) -> None:
        """stat rc!=0 must produce a per-file Error and never open a server.

        It used to degrade to ``total=0`` — cosmetic, progress only. Under
        size-termination a 0 reads zero bytes and "succeeds", so the degrade
        arm becomes a refusal, and it fires before ``asyncio.start_server``:
        the stat was issued and NO local server was created.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return CommandResult(command=cmd, value="", status=Status.Error, retcode=1)
            return _ok()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd)
        started: list = []

        async def fake_start_server(callback, host, port):
            started.append(callback)

            # Where the degraded 0 became a ghost success: a connection
            # delivering nothing reads to EOF at once and resolves Success
            # over an empty file. Nothing may reach here now — `started` is
            # the assertion, this is what makes reaching it observable.
            async def _connect() -> None:
                await callback(FakeReader([]), _fake_writer())

            asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(_connect()))
            return _fake_server(54331)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            per_file = await asyncio.wait_for(ft._get_files_nc([src_remote], dst_dir), timeout=5.0)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Error, msg
        assert "could not stat" in msg, msg
        assert "rc=1" in msg, msg
        assert not started, "a local server was bound for a file with no size to bound it"
        assert any("stat -L -c '%s %F'" in c.args[0] for c in exec_cmd.await_args_list), (
            "the stat never ran, so this test's premise never held"
        )
        assert not (dst_dir / "data.bin").exists(), (
            "a destination file was created for a transfer that was refused"
        )

    @pytest.mark.asyncio
    async def test_exactly_n_bytes_is_success_and_the_connection_is_closed(
        self, tmp_path: Path
    ) -> None:
        """N bytes then silence (NO EOF) must complete: the close is otto's.

        Measured 2026-08-25: on every userland the receiver's close is what
        terminates the ``-N``-less sender, so a loop still waiting for EOF
        waits for something nobody sends. The outer ``wait_for`` is this
        test's red — a hang, not an assertion, is how that regression shows.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("5 regular file\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54332)

        writer = _fake_writer()

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            connect_task = asyncio.create_task(on_connect(_SilenceAfterPayload(b"hello"), writer))
            try:
                per_file = await asyncio.wait_for(get_task, timeout=2.0)
            finally:
                await _abandon(connect_task, get_task)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Success, msg
        assert (dst_dir / "data.bin").read_bytes() == b"hello"
        writer.close.assert_called_once()  # the close IS the sender's terminator

    @pytest.mark.asyncio
    async def test_a_stalled_sender_fails_at_the_bound_instead_of_hanging(
        self, tmp_path: Path
    ) -> None:
        """Fewer than N bytes and then SILENCE must fail, not park forever.

        The plain twin of the tunnelled arm's zero-progress bound, and it only
        became necessary with size-termination: "when do we stop" moved from
        the sender's FIN to otto's close, so a sender that delivers fewer than
        `total` bytes and then waits for that close -- what every netcat
        measured 2026-08-25 does at stdin EOF -- parks `reader.read` with no
        deadline of its own. The close is then never reached, the remote
        sender is stranded, and `await send_task` is unbounded by design. The
        outer `wait_for` is this test's red: the regression is a HANG, not an
        assertion.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        closed = asyncio.Event()
        writer = _fake_writer()
        writer.close = MagicMock(side_effect=closed.set)

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("5 regular file\n")
            if " < " in cmd:
                await closed.wait()  # the `-N`-less sender exits on our close
                return _ok()
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54335)

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.2),
            patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server),
        ):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            # 3 of the 5 bytes the stat promised, then a sender that says
            # nothing and never closes: a file truncated between the stat and
            # the read (log rotation is the ordinary case).
            connect_task = asyncio.create_task(on_connect(_SilenceAfterPayload(b"abc"), writer))
            try:
                per_file = await asyncio.wait_for(get_task, timeout=3.0)
            finally:
                await _abandon(connect_task, get_task)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Error, msg
        assert "no data for" in msg, msg
        assert "3 bytes received" in msg, f"the error must name what arrived: {msg}"
        writer.close.assert_called_once()
        assert closed.is_set(), "the sender was left connected to a receiver that never closed"

    @pytest.mark.asyncio
    async def test_a_file_that_grew_after_the_stat_delivers_the_measured_bytes(
        self, tmp_path: Path
    ) -> None:
        """The plain twin of the tunnelled clamp: 100 measured, 150 offered.

        UP is the direction a pre-transfer stat can be wrong in without
        failing -- a live log grows between the measurement and the read -- and
        the `min()` in the read's length is what stops the loop AT the
        measurement, the close discarding the rest. Without it the loop would
        take the whole 8 KiB block on offer and write 150 bytes for a file
        vouched at 100, so the destination's contents are what makes the clamp
        observable; the sender here honours the requested length, which is what
        lets it be.
        """
        src_remote = Path("/remote/growing.log")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("100 regular file\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54336)

        writer = _fake_writer()

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            connect_task = asyncio.create_task(on_connect(_SilenceAfterPayload(b"a" * 150), writer))
            try:
                per_file = await asyncio.wait_for(get_task, timeout=3.0)
            finally:
                await _abandon(connect_task, get_task)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Success, msg
        assert (dst_dir / "growing.log").read_bytes() == b"a" * 100, (
            "the read did not stop at the measured size"
        )
        writer.close.assert_called_once()  # the close is what discards the excess

    @pytest.mark.asyncio
    async def test_a_read_that_raises_still_closes_on_the_sender(self, tmp_path: Path) -> None:
        """The error arm must close too, or the failure arrives as a hang.

        The close is the sender's only terminator now, and the spawn is
        deliberately unbounded (``timeout=float("inf")``) because its duration
        is the transfer. So a close that happens only where the read loop
        finished normally turns a mid-stream read error into a parked
        ``await send_task``. The scripted sender here does what every userland
        measured 2026-08-25 does: having written its bytes, it waits for the
        receiver — and the outer ``wait_for`` is the red.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        class BrokenReader:
            """A reader whose ``read()`` raises to simulate an I/O error."""

            async def read(self, _n: int) -> bytes:
                raise OSError("simulated read failure")

        closed = asyncio.Event()
        writer = _fake_writer()
        writer.close = MagicMock(side_effect=closed.set)

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("5 regular file\n")
            if " < " in cmd:
                await closed.wait()  # the `-N`-less sender exits on our close
                return _ok()
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54334)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            connect_task = asyncio.create_task(on_connect(BrokenReader(), writer))
            try:
                per_file = await asyncio.wait_for(get_task, timeout=2.0)
            finally:
                await _abandon(connect_task, get_task)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Error, msg
        assert "simulated read failure" in msg, msg
        assert closed.is_set(), "the sender was left connected to a receiver that never closed"

    @pytest.mark.asyncio
    async def test_the_sender_command_is_the_universal_spelling(self, tmp_path: Path) -> None:
        """``nc IP PORT < FILE`` — no ``-N``, asserted as the whole command.

        The full string including the redirections, so a reintroduced ``-N``
        (BusyBox: ``unrecognized option``, and the spawn sends the stderr
        saying so to /dev/null) or a ``-w`` (the measured mid-transfer
        idle-kill) each redden this by name. The transfer's terminator is
        otto's close, never an option.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if "stat" in cmd:
                return _ok("5 regular file\n")
            return _ok()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd)
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54333)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            await on_connect(FakeReader([b"hello"]), _fake_writer())
            status, msg = _only(await asyncio.wait_for(get_task, timeout=5.0), src_remote)

        assert status is Status.Success, msg
        sends = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert sends == ["nc 127.0.0.1 54333 < /remote/data.bin 2>/dev/null"], sends
        assert " -N" not in sends[0], f"-N is rejected by every BusyBox row: {sends[0]}"
        assert " -w " not in sends[0], f"-w is the measured mid-transfer idle-kill: {sends[0]}"

    @pytest.mark.asyncio
    async def test_the_prefetch_stat_follows_symlinks_and_asks_the_file_type(
        self, tmp_path: Path
    ) -> None:
        """``stat -L -c '%s %F' SRC`` — the whole command, both halves earned.

        Measured 2026-08-25 on every pinned BusyBox applet (1.16.1, 1.21.1,
        1.28.1, 1.31.0, 1.35.0) and the system coreutils ``stat``: a symlink
        to a 12-byte file answers 23 to ``stat -c %s`` — the LINK's own length
        — and 12 to ``stat -L -c %s``. The sender's ``< SRC`` follows the
        link, so the unadorned spelling vouched 23 bytes against a stream
        carrying 12, and on THIS arm that is the stall bound above, not an
        error anybody could read. ``%F`` rides in the same call because the
        size is the read's terminator and only a regular file delivers
        ``st_size`` bytes down the pipe.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("5 regular file\n")
            return _ok()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd)
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54337)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            await on_connect(FakeReader([b"hello"]), _fake_writer())
            status, msg = _only(await asyncio.wait_for(get_task, timeout=5.0), src_remote)

        assert status is Status.Success, msg
        stats = [c.args[0] for c in exec_cmd.await_args_list if c.args[0].startswith("stat")]
        assert stats == ["stat -L -c '%s %F' /remote/data.bin"], stats

    @pytest.mark.asyncio
    async def test_a_non_regular_file_is_refused_before_anything_is_bound(
        self, tmp_path: Path
    ) -> None:
        """A directory's ``st_size`` is not what ``< SRC`` delivers: refuse it.

        4096 bytes vouched, a shell's error down the pipe or nothing at all,
        and a read loop terminating on a number the stream will never reach —
        the stall bound's shape rather than a legible verdict. The type came
        back in the same call as the size, so the refusal is free, and it
        lands where the failed-stat refusal already does: before
        ``start_server`` binds anything and before a sender is spawned.
        """
        src_remote = Path("/remote/logs")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("4096 directory\n")
            return _ok()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd)
        started: list = []

        async def fake_start_server(callback, host, port):
            started.append(callback)
            return _fake_server(54338)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            per_file = await asyncio.wait_for(ft._get_files_nc([src_remote], dst_dir), timeout=5.0)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Error, msg
        assert "not a regular file (directory)" in msg, msg
        assert "shell backend" in msg, f"the refusal must name the way through: {msg}"
        assert not started, "a local server was bound for a file the nc backend cannot transfer"
        sends = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert not sends, f"a sender was spawned for a directory: {sends}"
        assert not (dst_dir / "logs").exists(), (
            "a destination file was created for a transfer that was refused"
        )

    @pytest.mark.asyncio
    async def test_a_stat_answer_without_a_type_is_refused_as_unreadable(
        self, tmp_path: Path
    ) -> None:
        """``12`` alone -- a ``stat`` that ignored ``%F`` -- is a refusal, not a 12-byte GET.

        The parse wants "SIZE TYPE"; a ``stat`` that printed only the size
        (a format string half-honoured) or something that is not a size at
        all lands on the unparsable arm. That arm is the one an unmeasured
        userland reaches, so it must be a named refusal quoting what came
        back, and it must land where the other refusals do: before anything
        is bound or spawned. Injected: the fake answers ``"12\n"``.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("12\n")
            return _ok()

        exec_cmd = AsyncMock(side_effect=exec_side)
        ft = _make_ft(exec_cmd)
        started: list = []

        async def fake_start_server(callback, host, port):
            started.append(callback)
            return _fake_server(54339)

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            per_file = await asyncio.wait_for(ft._get_files_nc([src_remote], dst_dir), timeout=5.0)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Error, msg
        assert "could not stat the remote file" in msg, msg
        assert "'12'" in msg, f"the refusal must quote the answer it could not read: {msg}"
        assert not started, "a local server was bound on an answer that named no size"
        sends = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert not sends, f"a sender was spawned on an unreadable stat answer: {sends}"

    @pytest.mark.asyncio
    async def test_an_empty_regular_file_is_still_a_success(self, tmp_path: Path) -> None:
        """``0 regular empty file`` transfers; only the TYPE arm may refuse.

        The size arm cannot be the one that refuses a zero: an empty regular
        file is a legitimate GET, and ``%F`` names it as one. It is also the
        answer a procfs pseudo-file gives (``/proc/version`` → ``0 regular
        empty file``, measured), which is exactly why those are documented
        rather than refused — nothing in the stat separates them.
        """
        src_remote = Path("/remote/empty.log")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("0 regular empty file\n")
            return _ok()

        ft = _make_ft(AsyncMock(side_effect=exec_side))
        captured: list = []

        async def fake_start_server(callback, host, port):
            captured.append(callback)
            return _fake_server(54339)

        writer = _fake_writer()

        with patch.object(transfer_mod.asyncio, "start_server", new=fake_start_server):
            get_task = asyncio.create_task(ft._get_files_nc([src_remote], dst_dir))
            on_connect = await _captured_callback(captured)
            connect_task = asyncio.create_task(on_connect(_SilenceAfterPayload(b""), writer))
            try:
                per_file = await asyncio.wait_for(get_task, timeout=3.0)
            finally:
                await _abandon(connect_task, get_task)
        status, msg = _only(per_file, src_remote)

        assert status is Status.Success, msg
        assert (dst_dir / "empty.log").read_bytes() == b""
        writer.close.assert_called_once()  # a `total` of 0 closes at once


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
            if cmd.startswith("stat"):
                # Remote file-size stat: return size.
                return _ok("5 regular file\n")
            if " -l -p " in cmd:
                # The listener: complete immediately — data transferred via FakeReader.
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
            if cmd.startswith("stat"):
                return _ok("0 regular empty file\n")
            if " -l -p " in cmd:
                # The listener: block until cancelled (orphaned — the wait raises first).
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
            if cmd.startswith("stat"):
                return _ok("0 regular empty file\n")
            if " -l -p " in cmd:
                # The listener: block until cancelled (the connect error fires first).
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

        The listen_task is the asyncio.Task wrapping the ``nc -l -p`` exec.
        We make that exec block forever (orphaned listener) so that the
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
            if cmd.startswith("stat"):
                return _ok("0 regular empty file\n")
            if " -l -p " in cmd:
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
            if cmd.startswith("stat"):
                return _ok("10 regular file\n")
            if " -l -p " in cmd:
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


class TestTheTunneledGetIsSizeTerminated:
    """The hop-forwarded GET reads exactly the stat-reported size, then closes.

    The plain arm's argument (``TestThePlainGetIsSizeTerminated``) carried
    through a port forward: the remote listener carries no ``-N`` — BusyBox's
    applet has no such option — so it holds the connection open after its last
    byte and ends on the RECEIVER's close, measured 2026-08-25 on all six
    userlands. The size the prefetch measured is the read loop's terminator
    here too.

    What that replaces is a documented leniency, not an oversight: this path
    used to fail an EOF at ZERO bytes against a known size and accept every
    other short read, because a pre-transfer stat cannot tell a file that grew
    from a transfer that was severed. Against a known N the two separate — N
    bytes arrive and the rest is discarded by the close; fewer than N is a
    short read — so the truncations that leniency waved through (a reaped
    listener, a hop that dropped, the ``-w`` idle-kill this wave removes) stop
    arriving as Success over a partial file.
    """

    @pytest.mark.asyncio
    async def test_a_short_read_is_an_error_not_a_truncated_success(self, tmp_path: Path) -> None:
        """EOF at 32 of 64 bytes must FAIL, naming got and expected.

        The case the old check was deliberately narrowed AWAY from: non-zero,
        so ``bytes_done == 0`` never fired, and the caller was handed Success
        over half a file. Both attempts see the same truncation, so the retry
        cannot mask it.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("64 regular file\n")
            return _ok("9000\n")

        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=True, listener_timeout=5.0)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"x" * 32]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        assert "short read" in msg, msg
        assert "64" in msg, f"the error must name what was expected: {msg}"
        assert "32" in msg, f"the error must name what arrived: {msg}"

    @pytest.mark.asyncio
    async def test_a_failed_stat_refuses_the_file_before_any_listener_exists(
        self, tmp_path: Path
    ) -> None:
        """stat rc!=0 must fail the file with NOTHING spawned and NOTHING bound.

        The stronger half of the plain arm's twin: this path spawns a REMOTE
        process and opens a port forward, so a size that cannot bound the
        transfer has to be a verdict before either exists. The old encoding
        made the same failure invisible — ``None`` skipped the empty check, so
        a stat failure plus a dropped connection returned Success over a
        zero-byte file.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return CommandResult(command=cmd, value="", status=Status.Error, retcode=1)
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
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        assert "could not stat" in msg, msg
        assert "rc=1" in msg, msg
        spawns = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert not spawns, f"a remote listener was spawned for a file with no size: {spawns}"
        ft._connections.forward_port.assert_not_awaited()
        assert any("stat -L -c '%s %F'" in c.args[0] for c in exec_cmd.await_args_list), (
            "the stat never ran, so this test's premise never held"
        )
        assert not (dst_dir / "data.bin").exists(), (
            "a destination file was created for a transfer that was refused"
        )

    @pytest.mark.asyncio
    async def test_exactly_n_bytes_is_success_and_ottos_close_ends_the_listener(
        self, tmp_path: Path
    ) -> None:
        """N bytes then SILENCE (no EOF) must complete, and complete by closing.

        The scripted listener here is the measured one: having written its
        bytes it returns only once the receiver closes. A loop still waiting
        for EOF therefore reaches the stall bound instead of finishing —
        patched short so the regression is an assertion rather than a wait.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        closed = asyncio.Event()
        writer = _fake_writer()
        writer.close = MagicMock(side_effect=closed.set)

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("5 regular file\n")
            if " < " in cmd:  # the remote listener, serving the file
                await closed.wait()  # ends on OUR close and on nothing else
                return _ok()
            return _ok("9000\n")

        ft = _make_ft(AsyncMock(side_effect=exec_side), has_tunnel=True, listener_timeout=5.0)
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(transfer_mod, "_NC_STALL_TIMEOUT", 0.3),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (_SilenceAfterPayload(b"hello"), writer)),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        assert (dst_dir / "data.bin").read_bytes() == b"hello"
        assert closed.is_set(), "the listener was left waiting on a receiver that never closed"

    @pytest.mark.asyncio
    async def test_the_listener_spawn_is_the_universal_spelling(self, tmp_path: Path) -> None:
        """``nc -l -p PORT < FILE`` — asserted whole, so a re-added option reds.

        ``-l -p`` is the one listener spelling every measured netcat accepts;
        ``-N`` is the option BusyBox's applet rejects outright (and this spawn
        sends the ``unrecognized option`` saying so to /dev/null, which is how
        the rejection used to arrive as a hang); ``-w`` is the mid-transfer
        idle-kill measured on five of six userlands, rc 0 and a partial file.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("5 regular file\n")
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
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"hello"]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        spawns = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert spawns == ["nc -l -p 9000 < /remote/data.bin 2>/dev/null"], spawns
        assert " -N" not in spawns[0], f"-N is rejected by every BusyBox row: {spawns[0]}"
        assert " -w " not in spawns[0], f"-w is the measured mid-transfer idle-kill: {spawns[0]}"

    @pytest.mark.asyncio
    async def test_get_on_a_busybox_userland_is_no_longer_refused(self, tmp_path: Path) -> None:
        """A GET against a BusyBox-class userland must transfer, not refuse.

        The refusal that used to sit at the top of ``_get_files_nc`` — ABOVE
        the tunnel dispatch, so it refused both arms — was predicated on a
        ``nc_dash_n`` probe of ``rejected``, which is what every BusyBox row
        measured. Both the guard and the capability it read are gone: no spawn
        asks for ``-N`` any more, so there is no option left for a device to
        reject. What is pinned here is the OUTCOME, which is why the guard
        survives its own predicate: an ``ash`` userland carrying the applet set
        the matrix measured reaches the transfer and succeeds. Entered through
        ``_get_files_nc`` rather than the tunnelled emitter directly, because
        the removed call was on that side of the dispatch.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def _unreachable(cmd: str, **_kw: object) -> CommandResult:
            raise OSError(f"no device in this test: {cmd!r}")

        # Declared, not probed: the values are the ones every matrix row
        # measures, and a scripted device is not needed to hold them. The
        # runner raises, so a declaration that failed to take would leave the
        # capability unsettled rather than quietly probing something else.
        userland = Userland(UserlandOptions(shell_dialect="ash", applet_nc="present"), _unreachable)
        await userland.resolve()
        assert userland.shell_dialect == "ash", "the premise never held"
        assert userland.has_applet("nc") == "present", "the premise never held"

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("5 regular file\n")
            return _ok("9000\n")

        ft = _make_ft(
            AsyncMock(side_effect=exec_side),
            has_tunnel=True,
            listener_timeout=5.0,
            userland=userland,
        )
        ft._connections.forward_port = AsyncMock(return_value=15000)

        with (
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"hello"]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(ft._get_files_nc([src_remote], dst_dir), timeout=5.0),
                src_remote,
            )

        assert status is Status.Success, msg
        assert (dst_dir / "data.bin").read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_the_prefetch_stat_follows_symlinks_and_asks_the_file_type(
        self, tmp_path: Path
    ) -> None:
        """``stat -L -c '%s %F' SRC`` here too, asserted as the whole command.

        Same measurement as the plain arm's twin (2026-08-25, five pinned
        BusyBox applets plus the system coreutils ``stat``): a symlink to a
        12-byte file answers 23 to ``stat -c %s`` and 12 to ``stat -L -c %s``,
        while the remote listener's ``< SRC`` follows the link. Pinned on BOTH
        arms because both prefetches are their own call site — a fix applied
        to one and not the other is exactly the drift this file exists to
        catch.
        """
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("5 regular file\n")
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
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([b"hello"]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Success, msg
        stats = [c.args[0] for c in exec_cmd.await_args_list if c.args[0].startswith("stat")]
        assert stats == ["stat -L -c '%s %F' /remote/data.bin"], stats

    @pytest.mark.asyncio
    async def test_a_non_regular_file_is_refused_before_anything_is_spawned(
        self, tmp_path: Path
    ) -> None:
        """The stronger half: a directory must cost no listener and no forward.

        This arm spawns a REMOTE process and opens a port forward, so a file
        whose ``st_size`` is not what ``< SRC`` will deliver has to be a
        verdict before either exists — the same place, and for the same
        reason, as the failed-stat refusal beside it.
        """
        src_remote = Path("/remote/logs")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        async def exec_side(cmd: str, timeout=None, **kw):
            if cmd.startswith("stat"):
                return _ok("4096 directory\n")
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
                AsyncMock(side_effect=lambda *a, **k: (FakeReader([]), _fake_writer())),
            ),
        ):
            status, msg = _only(
                await asyncio.wait_for(
                    ft._get_files_nc_tunneled([src_remote], dst_dir), timeout=5.0
                ),
                src_remote,
            )

        assert status is Status.Error, msg
        assert "not a regular file (directory)" in msg, msg
        assert "shell backend" in msg, f"the refusal must name the way through: {msg}"
        spawns = [c.args[0] for c in exec_cmd.await_args_list if " < " in c.args[0]]
        assert not spawns, f"a remote listener was spawned for a directory: {spawns}"
        ft._connections.forward_port.assert_not_awaited()
        assert not (dst_dir / "logs").exists(), (
            "a destination file was created for a transfer that was refused"
        )


# ============================================================================
# TestNcGetTunneledCancellation — chaos hardening Plan 4, Task 7
# ============================================================================


class TestNcGetTunneledCancellation:
    """External cancellation mid-GET must reap the remote ``nc -l -p`` listener.

    ``_get_files_nc_tunneled``'s inner ``_get_one`` spawns the remote listener
    (``nc -l -p <port> < <src>``) as an ``asyncio.Task`` and only joins it on
    its normal success / ``ConnectionError`` / timeout branches. A caller-side
    cancellation skips all of those, so — mirroring
    ``test_transfer_nc_put.py::TestNcPutCancellation`` for the put path's
    ``_attempt`` — ``_get_one`` must cancel the listener task and reap the
    remote ``nc -l`` itself. Pre-fix there is no ``except
    asyncio.CancelledError`` handler at all in ``_get_one``, so the listener
    is left running until something else ends it — the remote ``timeout``
    prefix, an hour later — which outlives the 10s teardown deadline
    (``todo/chaos-teardown-followups.md`` §1; chaos spec success-criterion #1).
    """

    @pytest.mark.asyncio
    async def test_cancellation_reaps_listener(self, tmp_path: Path) -> None:
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()

        listener_started = asyncio.Event()

        async def exec_side_effect(cmd: str, timeout: "float | None" = None, **kw: object):
            if " -l -p " in cmd:
                # The remote listener "runs" until its task is cancelled.
                listener_started.set()
                await asyncio.Event().wait()
            if cmd.startswith("stat"):
                return _ok("9000 regular file\n")
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
            if " -l -p " in cmd:
                listener_started.set()
                await asyncio.Event().wait()  # the remote listener runs until reaped
            if cmd.startswith("stat"):
                return _ok("9000 regular file\n")
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
        if cmd.startswith("stat"):
            return _ok("3 regular file\n")
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
        accept window. Pre-fix this returned Success with an empty file — and
        it is now the zero end of the size-terminated read's short-read arm
        rather than a check of its own, which is why the message it asserts is
        the same one a partial delivery gets."""
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
        assert "short read" in msg, msg
        assert "3 bytes" in msg, msg
        assert "after 0" in msg, msg

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
    the size semantics (finding 4) are pinned in BOTH directions: a file that
    grew delivers the measured bytes and succeeds, while short of the measured
    size is an error (``TestTheTunneledGetIsSizeTerminated``)."""

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
        if cmd.startswith("stat"):
            return _ok("100 regular file\n")
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
            if cmd.startswith("stat"):
                return _ok("3 regular file\n")
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
    async def test_a_file_that_grew_after_the_stat_delivers_the_measured_bytes(
        self, tmp_path: Path
    ):
        """The pre-transfer stat is a snapshot, and UP is the direction it can
        be wrong in without failing: a growing remote file — a live log through
        a hop — offers more than was measured, and the read stops at the
        measurement with the excess discarded by the close. Review finding 4
        had to accept every mismatch because nothing could tell a grown file
        from a severed transfer; terminating ON the size is what separates
        them, so this direction stays a Success while short is now an error.
        The reader here honours the requested length, which is what makes
        "never read the excess" observable as the file's contents."""
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
                AsyncMock(
                    side_effect=lambda *a, **k: (
                        _SilenceAfterPayload(b"a" * 150),
                        self._writer(),
                    )
                ),
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
        assert (dst_dir / "growing.log").read_bytes() == b"a" * 100, (
            "the read did not stop at the measured size"
        )
