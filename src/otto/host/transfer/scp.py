"""SCP file transfer backend for UnixHost.

Registers ``scp`` into the shared transfer registry on import.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..options import ScpOptions

from typing_extensions import override

from ...logger import get_logger
from ...result import CommandResult, Result
from ...utils import Status
from .base import (
    TransferContext,
    TransferProgressFactory,
)
from .progress import _make_sftp_progress
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = get_logger()


class ScpFileTransfer(UnixFileTransfer):
    """SCP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from :class:`BaseFileTransfer` and
    unix scaffolding (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``)
    from :class:`UnixFileTransfer`; implements ``_run_put`` / ``_run_get``
    directly for the SCP protocol.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        scp_options: "ScpOptions",
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self._scp_options = scp_options

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ScpFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "ScpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("ScpFileTransfer requires exec_cmd on the transfer context")
        if ctx.scp_options is None:
            raise ValueError("ScpFileTransfer requires scp_options on the transfer context")
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            scp_options=ctx.scp_options,
            max_filename_len=ctx.max_filename_len,
        )

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._get_files_scp(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._put_files_scp(src_files, dest_dir, progress_factory)

    async def _get_files_scp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        import asyncssh

        ssh_conn = await self._connections.ssh()

        async def _get_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SCP get {src} -> {dest_dir}")
            await asyncssh.scp(
                (ssh_conn, str(src)),
                dest_dir,
                progress_handler=_progress,
                **self._scp_options._kwargs(),  # noqa: SLF001 — intra-package access to ScpOptions._kwargs
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

    async def _put_files_scp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        import asyncssh

        ssh_conn = await self._connections.ssh()

        async def _put_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SCP put {src} -> {dest_dir}")
            await asyncssh.scp(
                str(src),
                (ssh_conn, str(dest_dir)),
                progress_handler=_progress,
                **self._scp_options._kwargs(),  # noqa: SLF001 — intra-package access to ScpOptions._kwargs
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


register_transfer_backend("scp", ScpFileTransfer)
