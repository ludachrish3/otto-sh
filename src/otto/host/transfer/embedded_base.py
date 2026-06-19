"""Shared embedded scaffolding for console and TFTP transfer backends.

:class:`EmbeddedFileTransfer` holds the common embedded fields (exec_cmd,
filesystem, mount state) shared by :class:`~otto.host.transfer.ConsoleFileTransfer`
and :class:`~otto.host.transfer.TftpFileTransfer`. Subclasses implement
``_run_get`` / ``_run_put`` and self-register their selector on import.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from ...utils import CommandStatus
from ..embedded_filesystem import EmbeddedFileSystem, NoFileSystem
from .base import BaseFileTransfer, TransferContext


class EmbeddedFileTransfer(BaseFileTransfer):
    """Shared base for embedded file-transfer backends (console, tftp).

    Subclasses :class:`~otto.host.transfer.BaseFileTransfer`, inheriting
    its ``put_files`` / ``get_files`` API (filename-length validation,
    shared Rich progress acquisition). Holds the fields common to all
    embedded selectors: the shell runner (``exec_cmd``), the on-device
    filesystem model, and the idempotent mount-done flag. Concrete
    subclasses implement ``_run_put`` / ``_run_get``.
    """

    host_families = frozenset({"embedded"})

    def __init__(
        self,
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]] | None = None,
        filesystem: EmbeddedFileSystem | None = None,
        max_filename_len: int = 255,
        # Legacy positional-compat: the old EmbeddedFileTransfer accepted
        # ``transfer`` as its first positional argument. Accept and ignore it
        # here so existing test call-sites that pass it by keyword still work;
        # concrete subclasses (ConsoleFileTransfer, TftpFileTransfer) are
        # selected by the registry, not by this field.
        transfer: str | None = None,
    ) -> None:
        super().__init__(name=name, max_filename_len=max_filename_len)
        # Stored even when None so subclasses that don't need exec_cmd
        # (e.g. TftpFileTransfer's stub) can still be constructed without it.
        self._exec_cmd = exec_cmd
        # The host's on-device filesystem variant. Source of truth for the
        # mount command, the no-FS short-circuit (``supports_transfer``),
        # and the command-formation hooks (``read_command`` etc.) that this
        # class drives. ``None`` is accepted for backward compatibility with
        # callers that pre-date the EmbeddedFileSystem refactor and is
        # treated as :class:`NoFileSystem`.
        self._filesystem: EmbeddedFileSystem = filesystem or NoFileSystem()
        # ``_mount_done`` makes ``_ensure_mounted`` idempotent within a host's
        # lifetime; a "already mounted" error from the real ``fs mount`` is
        # silently accepted by ``_ensure_mounted``.
        self._mount_done = False

    @classmethod
    def create(cls, ctx: "TransferContext") -> "EmbeddedFileTransfer":
        """Construct from a :class:`TransferContext`.

        Subclasses override this to read selector-specific ctx fields.
        """
        assert ctx.exec_cmd is not None
        return cls(
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            filesystem=ctx.filesystem,
            max_filename_len=ctx.max_filename_len,
        )
