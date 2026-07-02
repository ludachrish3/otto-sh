"""FTP file transfer backend for UnixHost.

Registers ``ftp`` into the shared transfer registry on import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aioftp

    from ..connections import ConnectionManager

from typing_extensions import override

from ...logger import get_otto_logger
from ...result import CommandResult, Result
from ...utils import Status
from .base import (
    TransferContext,
    TransferProgressFactory,
    mark_skipped,
)
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = get_otto_logger()


async def _ftp_size(ftp_conn: aioftp.Client, path: str) -> int:
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
    ) -> dict[Path, Result]:
        return await self._get_files_ftp(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._put_files_ftp(src_files, dest_dir, progress_factory)

    async def _get_files_ftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        # FTP transfers are sequential: aioftp.Client uses a single control
        # connection with one data channel per transfer, so concurrent ops on
        # the same client are not supported.  _ftp_lock serializes external
        # callers so concurrent host.get() invocations queue rather than
        # collide on the shared client. A failure stops the loop and every
        # not-yet-attempted file is marked Skipped.
        per_file: dict[Path, Result] = {}
        async with self._ftp_lock:
            ftp_conn = await self._connections.ftp()
            for i, src in enumerate(src_files):
                dst = dest_dir / src.name
                _logger.debug(f"{self._name}: FTP get {src} -> {dst}")
                try:
                    if progress_factory is None:
                        await ftp_conn.download(str(src), str(dst))
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
                except Exception as e:  # noqa: BLE001 — FTP get can fail via network/protocol/IO; all map to Error
                    per_file[src] = Result(Status.Error, msg=f"{src}: {e}")
                    mark_skipped(per_file, src_files[i + 1 :])
                    break
                per_file[src] = Result(Status.Success, value=dst)
        return per_file

    async def _put_files_ftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        import aioftp

        # Sequential for the same reason as _get_files_ftp (single data channel).
        per_file: dict[Path, Result] = {}
        async with self._ftp_lock:
            ftp_conn = await self._connections.ftp()
            for i, src in enumerate(src_files):
                dst = dest_dir / src.name
                _logger.debug(f"{self._name}: FTP put {src} -> {dst}")
                try:
                    if progress_factory is None:
                        await ftp_conn.upload(str(src), str(dst))
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
                except Exception as e:  # noqa: BLE001 — FTP put can fail via network/protocol/IO; all map to Error
                    per_file[src] = Result(Status.Error, msg=f"{src}: {e}")
                    mark_skipped(per_file, src_files[i + 1 :])
                    break
                per_file[src] = Result(Status.Success, value=dst)
        return per_file


register_transfer_backend("ftp", FtpFileTransfer)
