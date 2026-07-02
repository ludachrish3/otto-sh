"""Base classes and shared utilities for file-transfer backends.

Defines the abstract :class:`BaseFileTransfer` that every transfer backend
must subclass, the :class:`TransferContext` frozen data class (the uniform
construction seam for registered backends), :func:`validate_filename_lengths`
(guards against filesystem ``NAME_MAX`` violations before any bytes move), and
the :data:`TransferProgressHandler` / :data:`TransferProgressFactory` type
aliases consumed by the progress-bar wiring layer.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ...result import CommandResult, Result
from ...utils import Status

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..embedded_filesystem import EmbeddedFileSystem
    from ..options import NcOptions, ScpOptions

# (src_path, dst_path, bytes_done, bytes_total)  # noqa: ERA001 — signature doc
# Mirrors asyncssh's progress_handler signature exactly.
TransferProgressHandler = Callable[[str, str, int, int], None]

# Factory that creates a fresh, isolated TransferProgressHandler per file.
# Used for concurrent transfers so each coroutine has independent progress state.
TransferProgressFactory = Callable[[], TransferProgressHandler]


@dataclass(frozen=True)
class TransferContext:
    """Construction inputs a host provides to build its file transfer backend.

    The frozen public seam for custom transfer backends. Carries the union of what any family's
    built-ins receive at their call sites; a unix backend reads the unix fields, an embedded
    backend the embedded ones. Selector validation (host-family applicability) runs before
    construction, so a backend never sees a ctx missing the fields it needs.
    """

    transfer: str
    host_name: str
    max_filename_len: int = 255
    exec_cmd: "Callable[..., Coroutine[Any, Any, CommandResult]] | None" = None
    # unix-family fields
    connections: "ConnectionManager | None" = None
    nc_options: "NcOptions | None" = None
    scp_options: "ScpOptions | None" = None
    get_local_ip: "Callable[[], str] | None" = None
    # embedded-family fields
    filesystem: "EmbeddedFileSystem | None" = None


def validate_filename_lengths(
    files: list[Path],
    limit: int,
    host_name: str,
) -> Result:
    """Reject files whose basename exceeds the host's filesystem cap.

    Shared by :class:`~otto.host.transfer.UnixFileTransfer` (Unix) and
    :class:`~otto.host.transfer.EmbeddedFileTransfer` (embedded) so every backend
    surfaces the same self-explaining error. Without this guard the
    failure modes are:

    - Unix SCP/SFTP/FTP: server returns ``File name too long`` (errno 36),
      mid-transfer, after the local file is already read.
    - Embedded FAT (8.3, no LFN) or LittleFS over ``NAME_MAX``: device
      fails ``fs_open`` with ``-ENOENT``, giving no hint that the *name*
      was the problem.

    Returns an ok :class:`~otto.result.Result` when every basename fits, or a
    failing one whose ``msg`` names the offending file.
    """
    for path in files:
        name = path.name
        if len(name) > limit:
            return Result(
                Status.Error,
                msg=(
                    f"filename {name!r} ({len(name)} chars) exceeds the "
                    f"{limit}-character basename limit for host "
                    f"{host_name!r}. The target filesystem cannot open longer "
                    f"names — rename the file or raise the firmware/filesystem "
                    f"limit (``CONFIG_FS_FATFS_MAX_LFN`` for FAT, "
                    f"``CONFIG_FS_LITTLEFS_NAME_MAX`` for LittleFS; ``NAME_MAX`` "
                    f"on POSIX)."
                ),
            )
    return Result(Status.Success)


def aggregate_transfer(per_file: dict[Path, Result]) -> Result:
    """Fold a per-file mapping into the aggregate transfer Result.

    Aggregate status is the first non-ok entry's status (Skipped counts as
    ok); aggregate msg joins each non-ok entry's diagnostic. The mapping is
    carried through unchanged as :attr:`~otto.result.Result.value`, keyed by
    the source paths exactly as passed.
    """
    status = next((r.status for r in per_file.values() if not r.is_ok), Status.Success)
    msg = "; ".join(r.msg for r in per_file.values() if not r.is_ok and r.msg)
    return Result(status=status, value=per_file, msg=msg)


def mark_skipped(per_file: dict[Path, Result], remaining: list[Path]) -> None:
    """Mark each not-yet-attempted source path Skipped after a sequential backend stops.

    A sequential backend (ftp/console/nc) stops on the first failure; the
    files it never reached are recorded ``Status.Skipped`` (which
    :attr:`~otto.result.Result.is_ok` treats as passing, so a trailing run of
    Skipped never fails the aggregate on its own). Keyed by the source path
    exactly as passed.
    """
    for src in remaining:
        per_file[src] = Result(Status.Skipped, msg="not attempted (earlier failure)")


class BaseFileTransfer(ABC):
    """Shared API + progress plumbing for any file-transfer backend.

    The public ``put_files`` / ``get_files`` surface (filename-length
    validation, shared Rich progress acquisition) is owned by this base.
    Concrete backends (Unix's :class:`~otto.host.transfer.UnixFileTransfer`
    subclasses (:class:`~otto.host.transfer.ScpFileTransfer`,
    :class:`~otto.host.transfer.SftpFileTransfer`,
    :class:`~otto.host.transfer.FtpFileTransfer`,
    :class:`~otto.host.transfer.NcFileTransfer`), embedded's
    :class:`~otto.host.transfer.EmbeddedFileTransfer` subclasses
    (:class:`~otto.host.transfer.ConsoleFileTransfer`,
    :class:`~otto.host.transfer.TftpFileTransfer`), and any
    future ones) implement two abstract methods —
    ``_run_put`` and ``_run_get`` — both of which receive a
    :data:`TransferProgressFactory` and are responsible for invoking it
    at least once per source file, terminating with
    ``bytes_done == bytes_total`` to mark completion.

    The progress-bar capability is enforced at the *type system* level:
    ``abc.abstractmethod`` refuses to instantiate a subclass that omits
    either method, so a new backend cannot be defined without supplying a
    way to report progress. The runtime contract test
    (``TestTransferProgressContract``) verifies the factory is actually
    invoked, not just that the methods exist.
    """

    host_families: frozenset[str] = frozenset()
    """
    Host-family selectors this backend serves — a subset of ``{'unix', 'embedded'}``.
    Subclasses declare it; the spec field_validator rejects a backend on a host
    of the wrong family. A backend with an empty set can never validate and is
    rejected at registration.
    """

    @classmethod
    def create(cls, ctx: "TransferContext") -> "BaseFileTransfer":
        """Build a transfer backend from a :class:`TransferContext`.

        The uniform construction seam (WS#4). Concrete backends override this to
        run their exact construction against the ctx fields they need. Not an
        ``abstractmethod`` deliberately: only registered built-ins are ever
        constructed through ``create``, and test doubles that subclass
        ``BaseFileTransfer`` only to exercise the progress contract must not be
        forced to implement it.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not implement create(); a registered transfer "
            f"backend must override create(cls, ctx)."
        )

    def __init__(self, name: str, max_filename_len: int = 255) -> None:
        self._name = name
        self._max_filename_len = max_filename_len

    async def put_files(
        self,
        src_files: list[Path],
        dest_dir: Path,
        show_progress: bool = True,
    ) -> Result:
        """Upload *src_files* to *dest_dir*, validating filenames and driving progress display.

        Rejects over-limit basenames up front (see :func:`validate_filename_lengths`),
        then acquires the process-wide shared Rich progress bar (if *show_progress*)
        and delegates to the concrete backend's ``_run_put`` implementation. Returns
        the aggregate :class:`~otto.result.Result` whose ``value`` maps each source
        path (exactly as passed) to its per-file :class:`~otto.result.Result`.
        """
        from .progress import _acquire_shared_progress, make_rich_progress_factory

        name_check = validate_filename_lengths(
            src_files,
            self._max_filename_len,
            self._name,
        )
        if not name_check.is_ok:
            return aggregate_transfer(
                {f: Result(name_check.status, msg=name_check.msg) for f in src_files}
            )
        if not show_progress:
            return aggregate_transfer(await self._run_put(src_files, dest_dir, None))
        async with _acquire_shared_progress() as progress:
            return aggregate_transfer(
                await self._run_put(
                    src_files,
                    dest_dir,
                    make_rich_progress_factory(progress, self._name),
                )
            )

    async def get_files(
        self,
        src_files: list[Path],
        dest_dir: Path,
        show_progress: bool = True,
    ) -> Result:
        """Download *src_files* into *dest_dir*, validating filenames and driving progress display.

        Same validation and shared-progress contract as :meth:`put_files`,
        but delegates to the concrete backend's ``_run_get`` implementation.
        Returns the aggregate :class:`~otto.result.Result` whose ``value`` maps
        each source path (exactly as passed) to its per-file
        :class:`~otto.result.Result`.
        """
        from .progress import _acquire_shared_progress, make_rich_progress_factory

        name_check = validate_filename_lengths(
            src_files,
            self._max_filename_len,
            self._name,
        )
        if not name_check.is_ok:
            return aggregate_transfer(
                {f: Result(name_check.status, msg=name_check.msg) for f in src_files}
            )
        if not show_progress:
            return aggregate_transfer(await self._run_get(src_files, dest_dir, None))
        async with _acquire_shared_progress() as progress:
            return aggregate_transfer(
                await self._run_get(
                    src_files,
                    dest_dir,
                    make_rich_progress_factory(progress, self._name),
                )
            )

    @abstractmethod
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Backend-specific put implementation.

        Returns a per-file mapping keyed by the source paths exactly as passed:
        each value is a :class:`~otto.result.Result` carrying ``value=dest_path``
        on success, a per-file ``msg`` on failure, or ``Status.Skipped`` for a
        file a sequential backend stopped short of attempting.

        For each src file the implementation must call
        ``progress_factory()`` (if not ``None``) to obtain a fresh
        :data:`TransferProgressHandler`, then invoke that handler as bytes
        complete — at minimum once with ``bytes_done == bytes_total`` so
        the file's progress bar reaches 100%.
        """

    @abstractmethod
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Backend-specific get implementation.

        Same per-file mapping and progress contract as :meth:`_run_put`.
        """


NcPortStrategy = Literal["auto", "ss", "netstat", "python", "proc", "custom"]
"""Strategy for finding free ports on the remote host for netcat transfers.

Available strategies:

- ``'auto'`` (default) — try each built-in strategy in order (ss → netstat →
  python → proc) and cache the first one that succeeds.
- ``'ss'`` — parse ``ss -tln`` output to find unused ports.
- ``'netstat'`` — parse ``netstat -tln`` output (fallback for hosts without ss).
- ``'python'`` — bind a socket to port 0 via a ``python``/``python3`` one-liner
  and let the OS assign a free port.
- ``'proc'`` — read ``/proc/net/tcp`` directly (Linux-only, always available as
  a last resort).
- ``'custom'`` — run the shell command specified in ``nc_port_cmd``; the command
  must print a free port number to stdout.
"""

NcListenerCheck = Literal["auto", "ss", "netstat", "proc", "custom"]
"""Strategy for checking if a remote nc listener is ready.

Available strategies:

- ``'auto'`` (default) — probe for ss, then netstat, falling back to proc.
  The first tool found is cached and reused for subsequent checks.
- ``'ss'`` — check for a LISTEN socket via ``ss -tln sport = :<port>``.
- ``'netstat'`` — grep ``netstat -tln`` output for the port.
- ``'proc'`` — scan ``/proc/net/tcp`` for LISTEN state (0A) on the port
  (Linux-only, always available as a last resort).
- ``'custom'`` — run the shell command specified in ``nc_listener_cmd`` with a
  ``{port}`` placeholder. Must exit 0 when the port is listening.
"""
