"""File transfer backends for otto hosts — one package, both host families.

Public API (also re-exported from ``otto.host``): ``register_transfer_backend``,
``build_transfer_backend``, the Rich progress helpers, and the ``Nc*`` selector
Literals.

The backend classes and the progress helpers are exported lazily (PEP 562):
importing this package imports only the shared base types and the registry,
whose built-in entries name each backend by reference, so a caller pays for the
one backend module it actually names or builds.
"""

from typing import TYPE_CHECKING

from .base import (
    MAX_FILE_MODE,
    BaseFileTransfer,
    NcListenerCheck,
    NcPortStrategy,
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
    aggregate_transfer,
    chmod_command,
    parse_file_mode,
    validate_filename_lengths,
)
from .registry import (
    TRANSFER_BACKENDS,
    build_transfer_backend,
    register_transfer_backend,
)

if TYPE_CHECKING:
    from .console import ConsoleFileTransfer as ConsoleFileTransfer
    from .embedded_base import EmbeddedFileTransfer as EmbeddedFileTransfer
    from .ftp import FtpFileTransfer as FtpFileTransfer
    from .nc import NcFileTransfer as NcFileTransfer
    from .progress import _acquire_shared_progress as _acquire_shared_progress
    from .progress import _make_sftp_progress as _make_sftp_progress
    from .progress import make_rich_progress_factory as make_rich_progress_factory
    from .progress import make_rich_progress_handler as make_rich_progress_handler
    from .progress import make_transfer_progress as make_transfer_progress
    from .scp import ScpFileTransfer as ScpFileTransfer
    from .sftp import SftpFileTransfer as SftpFileTransfer
    from .shell import ShellFileTransfer as ShellFileTransfer
    from .tftp import TftpFileTransfer as TftpFileTransfer
    from .unix_base import UnixFileTransfer as UnixFileTransfer

_LAZY_ATTRS: dict[str, str] = {
    "ConsoleFileTransfer": "otto.host.transfer.console",
    "EmbeddedFileTransfer": "otto.host.transfer.embedded_base",
    "FtpFileTransfer": "otto.host.transfer.ftp",
    "NcFileTransfer": "otto.host.transfer.nc",
    "ScpFileTransfer": "otto.host.transfer.scp",
    "SftpFileTransfer": "otto.host.transfer.sftp",
    "ShellFileTransfer": "otto.host.transfer.shell",
    "TftpFileTransfer": "otto.host.transfer.tftp",
    "UnixFileTransfer": "otto.host.transfer.unix_base",
    "_acquire_shared_progress": "otto.host.transfer.progress",
    "_make_sftp_progress": "otto.host.transfer.progress",
    "make_rich_progress_factory": "otto.host.transfer.progress",
    "make_rich_progress_handler": "otto.host.transfer.progress",
    "make_transfer_progress": "otto.host.transfer.progress",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.host.transfer's backend and progress exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion, alongside the eager names."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "MAX_FILE_MODE",
    "TRANSFER_BACKENDS",
    "BaseFileTransfer",
    "ConsoleFileTransfer",
    "EmbeddedFileTransfer",
    "FtpFileTransfer",
    "NcFileTransfer",
    "NcListenerCheck",
    "NcPortStrategy",
    "ProgressGranularity",
    "ScpFileTransfer",
    "SftpFileTransfer",
    "ShellFileTransfer",
    "TftpFileTransfer",
    "TransferContext",
    "TransferProgressFactory",
    "TransferProgressHandler",
    "UnixFileTransfer",
    "aggregate_transfer",
    "build_transfer_backend",
    "chmod_command",
    "make_rich_progress_factory",
    "make_rich_progress_handler",
    "make_transfer_progress",
    "parse_file_mode",
    "register_transfer_backend",
    "validate_filename_lengths",
]
