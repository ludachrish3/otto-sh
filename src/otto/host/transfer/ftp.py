"""FTP file transfer backend for UnixHost.

``otto.host.transfer.registry`` registers it as ``ftp``, by reference.
"""

import asyncio
import os
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aioftp

    from ..connections import ConnectionManager

import logging

from typing_extensions import final, override

from ...result import CommandResult, Result
from ...utils import Status
from .base import (
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
)
from .shell import staged_temp_name
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)

# aioftp's own block size, mirrored rather than imported. Both directions use it:
# PUT reads `f.read(aioftp.DEFAULT_BLOCK_SIZE)` per `stream.write`, and
# GET's `stream.iter_by_block()` defaults to `count=DEFAULT_BLOCK_SIZE`.
# `aioftp` is imported LAZILY inside the transfer methods, and host-class
# discovery must never load it (a row in tests/unit/test_import_contracts.py),
# so reading the constant at class-body time would pull the package into every
# `otto host` invocation. The pin
# against the real constant lives in the unit tests instead
# (`test_transfer_registry.py::test_the_ftp_stride_is_aioftps_own_block_size`).
_FTP_BLOCK_SIZE = 8192


async def _ftp_size(ftp_conn: "aioftp.Client", path: str) -> int:
    """Return remote file size via the SIZE command, or 0 if unsupported.

    Avoids `aioftp.Client.stat()`, whose MLST→LIST fallback leaks a passive
    StreamWriter on servers that 500 MLSD (e.g. vsftpd).
    """
    try:
        _code, info = await ftp_conn.command(f"SIZE {path}", "213")
        return int(info[0].strip()) if info else 0
    except Exception:  # noqa: BLE001 — FTP SIZE command may fail for various protocol reasons; 0 is safe fallback
        return 0


# How long a refused transfer waits for its data connection to finish closing.
_FTP_DATA_CLOSE_WAIT = 1.0


async def _open_transfer_stream(
    ftp_conn: "aioftp.Client", command: str
) -> "aioftp.DataConnectionThrottleStreamIO":
    """Open the data connection, send ``command`` (STOR/RETR), return the stream.

    Mirrors aioftp 0.28's ``Client.get_stream`` step for step (passive
    connection of type ``I``, the command expecting ``1xx``, the same stream
    class over the client's throttle and socket timeout; ``upload_stream`` and
    ``download_stream`` are that function with a fixed command) with the one
    thing it lacks: ``get_stream`` opens the data connection BEFORE sending the
    command, so a refused STOR/RETR raises with the connection already open and
    unreferenced, and only the garbage collector closes it (a ``ResourceWarning``
    that pytest escalates into the next test's failure). aioftp offers no option
    or public sequence that closes it. Here any exception, cancellation
    included, closes the data writer first, the way aioftp closes its own
    (``writer.close()``), then waits a bounded time for it to finish. Delete
    this when aioftp's ``get_stream`` closes its connection on a refused command.
    """
    import aioftp

    reader, writer = await ftp_conn.get_passive_connection("I")
    try:
        await ftp_conn.command(command, "1xx")
        return aioftp.DataConnectionThrottleStreamIO(
            ftp_conn,
            reader,
            writer,
            throttles={"_": ftp_conn.throttle},
            timeout=ftp_conn.socket_timeout,
        )
    except BaseException:
        writer.close()
        with suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), _FTP_DATA_CLOSE_WAIT)
        raise


@contextmanager
def _name_refusal(what: str) -> Generator[None, None, None]:
    """Re-raise a server refusal as an ``OSError`` that says what was refused.

    The server's status line names no path, so ``what`` supplies the words in
    front of it. The dispatcher already prefixes every per-file failure with the
    source path, so a get's ``what`` does not repeat it; a put's names the
    destination, which the dispatcher does not print.
    """
    import aioftp

    try:
        yield
    except aioftp.StatusCodeError as exc:
        raise OSError(f"{what}: {exc}") from exc


@final
class _FtpFileTransfer(UnixFileTransfer):
    """FTP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from :class:`BaseFileTransfer` and
    unix scaffolding (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``)
    from :class:`UnixFileTransfer`; implements ``run_put`` / ``run_get``
    directly for the FTP protocol.
    """

    host_families = frozenset({"unix"})

    authenticates = True

    progress_granularity = ProgressGranularity(put=_FTP_BLOCK_SIZE, get=_FTP_BLOCK_SIZE)

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        # Serializes all FTP ops on the shared aioftp.Client. The client uses
        # one control connection with one data channel per transfer; concurrent
        # callers stomp on each other's STOR/RETR exchanges, surfacing as
        # "Connect first" or stuck data channels. FTP is inherently sequential
        # at the protocol layer, so this lock just enforces that.
        self._ftp_lock = asyncio.Lock()

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "_FtpFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "_FtpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("_FtpFileTransfer requires exec_cmd on the transfer context")
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
        )

    @override
    async def run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        return await self._get_files_ftp(
            src_files, dest_dir, progress_factory, concurrent=concurrent
        )

    @override
    async def run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        return await self._put_files_ftp(
            src_files, dest_dir, progress_factory, concurrent=concurrent
        )

    async def _get_files_ftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
        *,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        # FTP moves one file at a time: aioftp.Client uses a single control
        # connection with one data channel per transfer, so concurrent ops on
        # the same client are not supported -- `concurrency_limit` stays one,
        # and `concurrent=True` is a documented no-op here. Every file is
        # attempted whatever its siblings do; a failure is that file's entry.
        #
        # The connection is acquired ONCE for the batch, ABOVE the dispatch,
        # the way scp and sftp acquire theirs: a failure to connect is a
        # failure of the whole transfer, not of one file, and it must reach
        # the caller as the exception it is. Fetched inside the per-file
        # closure it would land in the dispatcher's per-file `except
        # Exception` and be reported as N file errors with no way to raise.
        ftp_conn = await self._connections.ftp()

        async def _get_one(src: Path) -> Result:
            dst = dest_dir / src.name
            _logger.debug(f"{self._name}: FTP get {src} -> {dst}")
            # The lock is per FILE, and it serializes the files THIS instance
            # moves over its own client: aioftp's client carries one data
            # channel, so two overlapping host.get() calls on this object must
            # not interleave a STOR/RETR exchange. The base semaphore
            # (`concurrency_limit` 1) already serializes them; the lock stays
            # because the semaphore does not reach across two calls that each
            # bring their own. Two DIFFERENT backend objects sharing one
            # client are not covered by either.
            #
            # One path whether or not a progress handler exists: the stream
            # calls write exactly `dst`, create no directory and do not recurse,
            # so a missing local directory or a remote directory source fails
            # this file -- creating directories is the recursive transfer's job.
            async with self._ftp_lock:
                # EVERY local decision is made BEFORE the server is asked to start
                # the transfer. aioftp's stream context manager reads the final
                # reply only on a clean exit; exited by an exception it closes the
                # data connection and leaves that reply unread, which breaks the
                # next command on this connection. So the destination is checked,
                # and a temporary sibling is opened, before RETR, and the bytes
                # reach `dst` only by `Path.replace` once the transfer completed.
                # A failure while blocks are moving (a read or write raising
                # mid-stream) still leaves that reply unread -- it is not
                # repaired here; the later files on this connection then fail.
                if dst.is_dir():
                    raise IsADirectoryError(f"cannot write {dst}: it is an existing directory")
                if not dst.parent.is_dir():
                    why = "is not a directory" if dst.parent.exists() else "does not exist"
                    raise FileNotFoundError(
                        f"cannot write {dst}: destination directory {dst.parent} {why}"
                    )
                temp = dst.parent / staged_temp_name(dst.name, self._max_filename_len)
                handler = progress_factory() if progress_factory is not None else None
                # Use SIZE rather than aioftp's `stat()`: stat() falls back
                # to LIST when MLST is unsupported (e.g. vsftpd returns 500),
                # but `Client.get_stream` opens the passive data connection
                # *before* sending MLSD — when MLSD then 500s, the suppressed
                # StatusCodeError leaves the data StreamWriter unreferenced.
                # Python 3.11+ surfaces that as a ResourceWarning that pytest's
                # unraisable plugin escalates into a test failure. Only asked
                # for when a handler will use it.
                total = await _ftp_size(ftp_conn, str(src)) if handler is not None else 0
                bytes_done = 0
                try:
                    with (
                        temp.open("xb") as f,
                        _name_refusal("the server refused to send it"),
                    ):
                        async with await _open_transfer_stream(ftp_conn, f"RETR {src}") as stream:  # ty: ignore[invalid-context-manager]
                            async for block in stream.iter_by_block():
                                f.write(block)
                                bytes_done += len(block)
                                if handler is not None:
                                    handler(str(src), str(dst), bytes_done, total)
                    temp.replace(dst)
                except BaseException:
                    temp.unlink(missing_ok=True)
                    raise
            return Result(status=Status.Success, value=dst)

        return await self.dispatch_per_file(src_files, transfer_one=_get_one, concurrent=concurrent)

    async def _put_files_ftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
        *,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        import aioftp

        # One file at a time for the same reason as _get_files_ftp (a single
        # data channel), every file attempted for the same reason too, and the
        # connection acquired once above the dispatch for the same reason too.
        ftp_conn = await self._connections.ftp()

        async def _put_one(src: Path) -> Result:
            dst = dest_dir / src.name
            _logger.debug(f"{self._name}: FTP put {src} -> {dst}")
            # The lock covers exactly what _get_files_ftp's own comment says it
            # covers -- including its limit -- and the one path runs with or
            # without a progress handler, for the reason given there.
            async with self._ftp_lock:
                handler = progress_factory() if progress_factory is not None else None
                bytes_done = 0
                # The source is opened BEFORE STOR: STOR has already created or
                # truncated the remote file when it is accepted, and an exception
                # leaving aioftp's stream leaves the final reply unread (see
                # _get_one). A mid-stream read or write failure still does; it is
                # not repaired here.
                with src.open("rb") as f:
                    total = os.fstat(f.fileno()).st_size if handler is not None else 0
                    with _name_refusal(f"cannot write {dst}"):
                        async with await _open_transfer_stream(ftp_conn, f"STOR {dst}") as stream:  # ty: ignore[invalid-context-manager]
                            while True:
                                block = f.read(aioftp.DEFAULT_BLOCK_SIZE)
                                if not block:
                                    break
                                await stream.write(block)
                                bytes_done += len(block)
                                if handler is not None:
                                    handler(str(src), str(dst), bytes_done, total)
            return Result(status=Status.Success, value=dst)

        return await self.dispatch_per_file(src_files, transfer_one=_put_one, concurrent=concurrent)
