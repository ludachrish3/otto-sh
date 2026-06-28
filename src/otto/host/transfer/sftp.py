"""SFTP file transfer backend for UnixHost.

Registers ``sftp`` into the shared transfer registry on import.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connections import ConnectionManager

from typing_extensions import override

from ...logger import get_otto_logger
from ...utils import CommandStatus, Status
from .base import (
    TransferContext,
    TransferProgressFactory,
    _first_error,
)
from .progress import _make_sftp_progress
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = get_otto_logger()


class SftpFileTransfer(UnixFileTransfer):
    """SFTP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from :class:`BaseFileTransfer` and
    unix scaffolding (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``)
    from :class:`UnixFileTransfer`; implements ``_run_put`` / ``_run_get``
    directly for the SFTP protocol.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "SftpFileTransfer":
        assert ctx.connections is not None and ctx.exec_cmd is not None
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
    ) -> tuple[Status, str]:
        return await self._get_files_sftp(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        return await self._put_files_sftp(src_files, dest_dir, progress_factory)

    async def _get_files_sftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        sftp_conn = await self._connections.sftp()

        async def _get_one(src: Path) -> tuple[Status, str]:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP get {src} -> {dest_dir}")
            await sftp_conn.get(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
            )
            return Status.Success, ""

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in src_files), return_exceptions=True
        )
        return _first_error(results)

    async def _put_files_sftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        sftp_conn = await self._connections.sftp()

        async def _put_one(src: Path) -> tuple[Status, str]:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP put {src} -> {dest_dir}")
            await sftp_conn.put(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
            )
            return Status.Success, ""

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_put_one(src) for src in src_files), return_exceptions=True
        )
        return _first_error(results)


register_transfer_backend("sftp", SftpFileTransfer)
