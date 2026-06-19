"""File transfer backends for otto hosts — one package, both host families.

Public API (also re-exported from ``otto.host``): ``register_transfer_backend``,
``build_transfer_backend``, the Rich progress helpers, and the ``Nc*`` selector
Literals. Importing this package registers every built-in backend.
"""
from .base import (
    BaseFileTransfer,
    NcListenerCheck,
    NcPortStrategy,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
    _first_error,
    validate_filename_lengths,
)
from .console import ConsoleFileTransfer  # registers console on import
from .embedded_base import EmbeddedFileTransfer
from .ftp import FtpFileTransfer  # registers ftp on import
from .nc import NcFileTransfer  # registers nc on import
from .progress import (
    _acquire_shared_progress,
    _make_sftp_progress,
    make_rich_progress_factory,
    make_rich_progress_handler,
    make_transfer_progress,
)
from .registry import (
    _TRANSFER_BACKENDS,
    build_transfer_backend,
    register_transfer_backend,
)
from .scp import ScpFileTransfer  # registers scp on import
from .sftp import SftpFileTransfer  # registers sftp on import
from .tftp import TftpFileTransfer  # registers tftp on import
from .unix_base import UnixFileTransfer

__all__ = [
    "BaseFileTransfer", "TransferContext", "validate_filename_lengths",
    "TransferProgressHandler", "TransferProgressFactory",
    "NcPortStrategy", "NcListenerCheck",
    "make_rich_progress_handler", "make_rich_progress_factory",
    "make_transfer_progress",
    "register_transfer_backend", "build_transfer_backend",
    "NcFileTransfer", "UnixFileTransfer", "EmbeddedFileTransfer",
    "ConsoleFileTransfer", "TftpFileTransfer",
    "ScpFileTransfer", "SftpFileTransfer", "FtpFileTransfer",
]
