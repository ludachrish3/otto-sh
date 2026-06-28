"""The reserved ``tftp`` embedded transfer backend (deferred)."""

from pathlib import Path

from typing_extensions import override

from ...utils import Status
from .base import TransferContext, TransferProgressFactory
from .embedded_base import EmbeddedFileTransfer
from .registry import register_transfer_backend


class TftpFileTransfer(EmbeddedFileTransfer):
    """Reserved: TFTP transfer for embedded hosts is not yet implemented."""

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "TftpFileTransfer":
        return cls(name=ctx.host_name, max_filename_len=ctx.max_filename_len)

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        raise NotImplementedError(
            "TFTP transfer for embedded hosts is not yet implemented"
        ) from None

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        raise NotImplementedError(
            "TFTP transfer for embedded hosts is not yet implemented"
        ) from None


register_transfer_backend("tftp", TftpFileTransfer)
