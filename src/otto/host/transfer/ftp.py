"""FTP file transfer backend for UnixHost.

Registers ``ftp`` into the shared transfer registry on import.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aioftp

    from ..connections import ConnectionManager

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status
from .base import (
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
)
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)

# aioftp's own block size, mirrored rather than imported. Both arms use it:
# PUT reads `f.read(aioftp.DEFAULT_BLOCK_SIZE)` per `stream.write`, and
# GET's `stream.iter_by_block()` defaults to `count=DEFAULT_BLOCK_SIZE`.
# `aioftp` is imported LAZILY inside the transfer methods and is absent from
# the `host` import-budget snapshot, so reading the constant at class-body
# time would pull the package into every `otto host` invocation. The pin
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


class FtpFileTransfer(UnixFileTransfer):
    """FTP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from :class:`BaseFileTransfer` and
    unix scaffolding (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``)
    from :class:`UnixFileTransfer`; implements ``_run_put`` / ``_run_get``
    directly for the FTP protocol.
    """

    host_families = frozenset({"unix"})

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
    def create(cls, ctx: "TransferContext") -> "FtpFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "FtpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("FtpFileTransfer requires exec_cmd on the transfer context")
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
        )

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
        *,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        return await self._get_files_ftp(
            src_files, dest_dir, progress_factory, concurrent=concurrent
        )

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
        *,
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
            # The lock is per FILE now, and it serializes the files THIS
            # instance moves over its own client: aioftp's client carries one
            # data channel, so two overlapping host.get() calls on this object
            # must not interleave a STOR/RETR exchange. The base semaphore
            # (`concurrency_limit` 1) already serializes them; the lock stays
            # because the semaphore does not reach across two calls that each
            # bring their own. Two DIFFERENT backend objects sharing one
            # client are not covered by either -- pre-existing, and not this
            # change's to settle.
            async with self._ftp_lock:
                if progress_factory is None:
                    # `write_into=True` for the SAME JOIN REASON as the PUT
                    # arm below -- and only that reason; the two arms differ
                    # after it, so this defers for the mechanism and states
                    # its own consequences.
                    #
                    # Shared: aioftp's `download` carries the identical
                    # `if not write_into: destination = destination / name`
                    # branch. `dst` is already `dest_dir / src.name`, so the
                    # default joined the name a second time.
                    #
                    # GET-specific, and QUIETER than PUT's and so worse: the
                    # following `path_io.mkdir(destination.parent)` runs on
                    # the LOCAL filesystem, so the default turned the
                    # CALLER'S OWN destination into a directory and wrote
                    # the bytes one level inside it -- while the `Result`
                    # below still said Success and named `dst`. A caller
                    # checking `is_ok` believed a file had arrived; one
                    # reading `.value` got IsADirectoryError. PUT failed
                    # loudly one step later; this said nothing at all.
                    # Pinned in
                    # tests/unit/host/transfer/test_ftp_transfer_destination.py,
                    # which observes the landed file rather than restating
                    # this rule.
                    #
                    # NOT shared -- do not read PUT's caveats onto this arm:
                    # * Directory sources. PUT's caveat says they stay
                    #   broken; on `download` they do not. Its `is_dir`
                    #   branch computes children as
                    #   `destination_path / name.relative_to(source)` and
                    #   recurses with `write_into=True`, i.e. relative to the
                    #   DESTINATION, so `write_into=True` is strictly better
                    #   here rather than merely harmless.
                    # * The dest_dir asymmetry runs the same way but through
                    #   different code: `download` mkdirs `dest_dir` for us,
                    #   while the progress arm's `dst.open("wb")` below
                    #   raises FileNotFoundError if it is missing. Same
                    #   split question as PUT's, recorded here because it is
                    #   reachable by a different path. Pre-existing, and not
                    #   this fix's to settle.
                    await ftp_conn.download(str(src), str(dst), write_into=True)
                else:
                    handler = progress_factory()
                    # Use SIZE rather than aioftp's `stat()`: stat() falls back
                    # to LIST when MLST is unsupported (e.g. vsftpd returns 500),
                    # but `Client.get_stream` opens the passive data connection
                    # *before* sending MLSD — when MLSD then 500s, the suppressed
                    # StatusCodeError leaves the data StreamWriter unreferenced.
                    # Python 3.11+ surfaces that as a ResourceWarning that pytest's
                    # unraisable plugin escalates into a test failure.
                    total = await _ftp_size(ftp_conn, str(src))
                    bytes_done = 0
                    async with ftp_conn.download_stream(str(src)) as stream:
                        with dst.open("wb") as f:
                            async for block in stream.iter_by_block():
                                f.write(block)
                                bytes_done += len(block)
                                handler(str(src), str(dst), bytes_done, total)
            return Result(Status.Success, value=dst)

        return await self._dispatch_per_file(src_files, _get_one, concurrent=concurrent)

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
            # The lock is per FILE now, and covers exactly what _get_files_ftp's
            # own comment says it covers -- including its limit.
            async with self._ftp_lock:
                if progress_factory is None:
                    # `write_into=True` is load-bearing, not a tidy-up. aioftp's
                    # `upload` defaults to `write_into=False`, which means
                    # "write source INTO destination" -- it appends
                    # `source.name` to whatever it is given and mkdirs the
                    # parent. `dst` is ALREADY `dest_dir / src.name`, so the
                    # default resolved `<dest>/<name>/<name>` and left a
                    # directory wearing the file's own name. Measured on the
                    # bed 2026-08-26 across all four GNU hosts: the follow-up
                    # `get` of the caller's path then failed `550 Failed to
                    # open file`. True flag = "this destination IS the file",
                    # which is what the progress arm's `upload_stream(dst)`
                    # below has always meant. Pinned in
                    # tests/unit/host/transfer/test_ftp_transfer_destination.py.
                    #
                    # SCOPE: "this destination IS the file" is the right
                    # reading for a FILE source, which is all otto puts
                    # here. A DIRECTORY source stays broken either way --
                    # the top-level directory now lands correctly, but
                    # aioftp walks children relative to the FTP cwd rather
                    # than to the destination (its own bug,
                    # `client.py:1216-1222`), so this is not a fix for
                    # directory transfers and does not regress them. This
                    # caveat is PUT-ONLY: `download`'s dir branch resolves
                    # children against the destination, so the GET arm above
                    # really is fixed for directories.
                    #
                    # One asymmetry this does NOT close, pre-existing and
                    # left alone deliberately: `upload` still runs
                    # `make_directory(destination.parent)`, so this arm
                    # auto-creates `dest_dir` while the `upload_stream` arm
                    # below fails if it is missing. Same call, two answers
                    # to "must the destination directory exist?".
                    await ftp_conn.upload(str(src), str(dst), write_into=True)
                else:
                    handler = progress_factory()
                    total = src.stat().st_size
                    bytes_done = 0
                    async with ftp_conn.upload_stream(str(dst)) as stream:
                        with src.open("rb") as f:
                            while True:
                                block = f.read(aioftp.DEFAULT_BLOCK_SIZE)
                                if not block:
                                    break
                                await stream.write(block)
                                bytes_done += len(block)
                                handler(str(src), str(dst), bytes_done, total)
            return Result(Status.Success, value=dst)

        return await self._dispatch_per_file(src_files, _put_one, concurrent=concurrent)


register_transfer_backend("ftp", FtpFileTransfer)
