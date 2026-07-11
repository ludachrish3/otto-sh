"""SFTP file transfer backend for UnixHost.

Registers ``sftp`` into the shared transfer registry on import.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connections import ConnectionManager

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status
from .base import (
    TransferContext,
    TransferProgressFactory,
)
from .progress import _make_sftp_progress
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)


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
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
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
        if ctx.connections is None:
            raise ValueError(
                "SftpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("SftpFileTransfer requires exec_cmd on the transfer context")
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
        return await self._get_files_sftp(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._put_files_sftp(src_files, dest_dir, progress_factory)

    async def _get_files_sftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        sftp_conn = await self._connections.sftp()

        async def _get_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP get {src} -> {dest_dir}")
            await sftp_conn.get(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_get_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _put_files_sftp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        sftp_conn = await self._connections.sftp()

        async def _put_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP put {src} -> {dest_dir}")
            await sftp_conn.put(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_put_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file


register_transfer_backend("sftp", SftpFileTransfer)
