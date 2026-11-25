"""Local host implementation — runs commands on the machine otto itself is running on.

:class:`LocalHost` is a concrete :class:`~otto.host.host.BaseHost` that spawns
local shell sessions (a persistent one for ``run``, pooled ones for ``exec``), mirroring the API of
:class:`~otto.host.unix_host.UnixHost` without any network transport. File
transfers are handled by :class:`LocalFileTransfer` (a :mod:`shutil`-backed
:class:`~otto.host.transfer.BaseFileTransfer` subclass) so progress reporting
works uniformly across all host backends.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from typing_extensions import final, override

from ..models.base import Cred
from ..result import Result
from ..utils import Arg, Exclude, Opt, Status, cli_exposed, process_user
from .capability_grid import HostCapabilities, OptionSupport, SessionIdentity, UserSupport
from .file_ops import PosixFileOps
from .host import (
    CONCURRENT_HELP,
    PROBE_TIMEOUT_S,
    BaseHost,
    is_dry_run,
)
from .privilege import PosixPrivilege
from .session import (
    LocalSession,
    SessionManager,
)
from .transfer import BaseFileTransfer, ProgressGranularity, TransferProgressFactory

if TYPE_CHECKING:
    from .recursive_transfer import RemoteListing


class LocalFileTransfer(BaseFileTransfer):
    """File transfer for :class:`LocalHost` — a local copy via :func:`shutil.copy2`.

    Concrete :class:`~otto.host.transfer.BaseFileTransfer` so the ABC's
    progress contract holds uniformly across every backend in the host
    fleet (Unix's :class:`~otto.host.transfer.UnixFileTransfer`, embedded's
    :class:`~otto.host.transfer.EmbeddedFileTransfer`, and this
    one). Per-file completion is the granularity — ``shutil.copy2`` is a
    single blocking C call with no progress hook, the analogue of an
    embedded ``fs read``.
    """

    supports_mode = True
    """A local copy lands on the machine's own filesystem, so ``Path.chmod``
    applies the mode directly — no shell, no transport."""

    progress_granularity = ProgressGranularity(
        put=None,
        get=None,
        note=(
            "`shutil.copy2` is one blocking C call with no progress hook -- the "
            "one event arrives when the whole file has been copied"
        ),
    )

    async def _do_copy(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
        *,
        concurrent: bool,
    ) -> dict[Path, Result]:
        # A single-directory copy, keyed by the source path exactly as passed.
        # The destination directory is made ONCE, before any file: a failure
        # there is every file's failure, not the first one's. Past it, an
        # OSError (a missing source, say) is that file's own entry and the
        # files beside it still copy.
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {src: Result(status=Status.Error, msg=str(e)) for src in src_files}

        async def _copy_one(src: Path) -> Result:
            dest = dest_dir / src.name
            try:
                await asyncio.to_thread(shutil.copy2, src, dest)
                if progress_factory is not None:
                    # Inside the try deliberately: `dest.stat()` is a syscall
                    # of its own, and an OSError from it must carry the same
                    # bare `str(e)` this arm has always reported rather than
                    # the dispatcher's `f"{src}: {exc}"` fold.
                    size = dest.stat().st_size
                    progress_factory()(str(src), str(dest), size, size)
            except OSError as e:
                return Result(status=Status.Error, msg=str(e))
            return Result(status=Status.Success, value=dest)

        return await self.dispatch_per_file(
            src_files, transfer_one=_copy_one, concurrent=concurrent
        )

    @override
    async def run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        return await self._do_copy(src_files, dest_dir, progress_factory, concurrent=concurrent)

    @override
    async def run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        return await self._do_copy(src_files, dest_dir, progress_factory, concurrent=concurrent)

    @override
    async def apply_mode(self, dest_paths: list[Path], *, mode: int) -> Result:
        """Chmod the copied files directly, off the event loop.

        ``Path.chmod`` is a blocking syscall; the whole batch runs in one
        worker thread rather than one hop per file.
        """

        def _chmod_all() -> None:
            for path in dest_paths:
                path.chmod(mode)

        try:
            await asyncio.to_thread(_chmod_all)
        except OSError as e:
            return Result(status=Status.Error, msg=str(e))
        return Result(status=Status.Success)


logger = logging.getLogger(__name__)


@final
@dataclass(
    slots=True,
    kw_only=True,
)
class LocalHost(PosixPrivilege, PosixFileOps, BaseHost):
    """A host that runs commands on the local machine via a persistent shell session.

    Implements the full :class:`~otto.host.host.BaseHost` API (run, exec, put,
    get, open_session, send, expect, is_reachable) without any network transport.
    Shell state (working directory, environment variables) persists across ``run``
    calls through a :class:`~otto.host.session.SessionManager`-backed local
    session; ``exec`` bypasses it and runs on a pooled local shell of its own,
    as on a remote host, making concurrent calls safe. File transfers
    delegate to :class:`LocalFileTransfer`.

    The shells run as the invoking user. ``user=`` switches them exactly as on
    a remote host: with the target's credential from *creds* when there is
    one, otherwise as an ad-hoc ``su`` target; ``sudo`` is answered from the
    invoking user's credential when it asks.
    """

    capabilities = HostCapabilities(
        sudo=OptionSupport.honoured(),
        expects=OptionSupport.honoured(),
        user_refusal=None,
        run_user=UserSupport.switch,
        exec_user=UserSupport.switch,
        put_user=UserSupport.refused,
        get_user=UserSupport.refused,
        show_progress=False,
        session_identity=SessionIdentity.as_user_scoped,
        transfer="`shutil.copy2` on the machine's own filesystem, one file at a time",
        note=(
            "`run` and `exec` switch from the invoking user as a unix host "
            "switches from its login; local copies keep the invoking user's "
            "ownership, so `put` and `get` take no `user=`."
        ),
    )
    """What this family promises. See :class:`~otto.host.capability_grid.HostCapabilities`."""

    name: str = field(default="localhost", init=False)

    creds: list[Cred] | None = None
    """Credentials for switching user and answering ``sudo``, looked up as a
    unix host's are: the invoking user's answers ``sudo``, a target's answers
    the switch to it. ``None`` knows none: every target is an ad-hoc ``su``."""

    _file_transfer: LocalFileTransfer = field(init=False, repr=False)
    """Local copy via shutil, routed through BaseFileTransfer so progress
    reporting works uniformly across every host backend."""

    def __post_init__(self) -> None:
        self.id = "local"
        self._rebuild_connections()

    @override
    def _rebuild_connections(self) -> None:
        """Recreate the session manager and transfer backend, dropping the old ones unclosed.

        How a host moves to another event loop: the old shell's pipes belong
        to that loop and cannot be driven from another, so they are abandoned
        rather than closed. ``__post_init__`` builds through here too.
        """
        self._session_mgr = SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            fold_log=self._session_fold(),
            session_factory=LocalSession,
            creds=self._switch_creds(),
            host_id=self.id,
            types_into_terminal=LocalSession.reads_a_terminal,
            # The shells this host spawns run as the user otto runs as,
            # resolved once per manager; "" (unknown) for a user with no name.
            login_user=process_user() or "",
        )
        self._file_transfer = LocalFileTransfer(name=self.name)

    def _live_file_transfer(self) -> LocalFileTransfer:
        """Return the transfer backend, after claiming the running loop (see ``_claim_loop``)."""
        self._claim_loop()
        return self._file_transfer

    ####################
    #  Command execution
    ####################

    @override
    def _terminal_limitation(
        self,
        terminal: bool,
        *,
        verb: Literal["run", "exec", "open_session"],
    ) -> str | None:
        """Refuse a terminal: every session of the local host reads a pipe."""
        return (
            "is not available on the local host, whose sessions read a pipe" if terminal else None
        )

    ####################
    #  File transfer
    ####################

    @override
    @cli_exposed(success="Download complete.", dry_run_preview=True)
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
        *,
        user: Annotated[
            str | None,
            Opt(
                help="Not supported on this host type — containers chown, "
                "unix hosts authenticate as the user."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
        concurrent: Annotated[bool, Opt(help=CONCURRENT_HELP)] = True,
    ) -> Result:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer` so progress reporting
        flows through the same :class:`~otto.host.transfer.BaseFileTransfer`
        machinery as Unix and embedded backends.

        ``recursive`` transfers each directory among the sources as a tree;
        see :ref:`recursive-transfers`. ``concurrent`` bounds the files in
        flight; see :ref:`concurrent-transfers`.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: get(user=...) is not supported on LocalHost — "
                f"local transfers keep the invoking user's ownership"
            ) from None
        src_files = self._as_sources(src_files)
        if recursive:
            return await self._get_tree(
                src_files, dest_dir, user=user, show_progress=show_progress, concurrent=concurrent
            )
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        return await self._live_file_transfer().get_files(
            src_files=src_files,
            dest_dir=dest_dir,
            show_progress=show_progress,
            concurrent=concurrent,
        )

    @override
    @cli_exposed(success="Transfer complete.", dry_run_preview=True)
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
        *,
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
        user: Annotated[
            str | None,
            Opt(
                help="Not supported on this host type — containers chown, "
                "unix hosts authenticate as the user."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
        concurrent: Annotated[bool, Opt(help=CONCURRENT_HELP)] = True,
    ) -> Result:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer`; see :meth:`get`. *mode* sets
        the permission bits on the copies — an ``int`` (``0o755``) from
        Python, or a string always read as octal (``"755"``, ``"0755"``,
        ``"0o755"``). Without it, ``shutil.copy2`` preserves the source
        file's own permissions.

        ``recursive`` transfers each directory among the sources as a tree;
        see :ref:`recursive-transfers`. ``concurrent`` bounds the files in
        flight; see :ref:`concurrent-transfers`.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: put(user=...) is not supported on LocalHost — "
                f"local transfers keep the invoking user's ownership"
            ) from None
        src_files = self._as_sources(src_files)
        if recursive:
            return await self._put_tree(
                src_files,
                dest_dir,
                mode=mode,
                user=user,
                show_progress=show_progress,
                concurrent=concurrent,
            )
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir, mode)
        return await self._live_file_transfer().put_files(
            src_files=src_files,
            dest_dir=dest_dir,
            show_progress=show_progress,
            mode=mode,
            concurrent=concurrent,
        )

    @override
    async def _walk_remote(self, path: "str | Path") -> "RemoteListing":
        """Walk the filesystem directly — the runner IS the host, same rules as the shell script."""
        from .recursive_transfer import _CANNOT_TELL, ListingError, RemoteListing

        root = Path(path)
        if "\n" in root.name:
            raise ListingError(
                f"{root}: a name in this tree contains a newline; rename it and retry"
            )
        # Under a directory that cannot be searched, `[ -d ]` is just false;
        # Path.is_dir raises there before Python 3.14, so read it the shell's way.
        try:
            is_dir = root.is_dir()
        except PermissionError:
            is_dir = False
        if not is_dir:
            if os.path.lexists(root):
                return RemoteListing(kind="f", dirs=[], files=[])
            if not _nearest_existing_ancestor_searchable(root):
                raise ListingError(f"{root}: {_CANNOT_TELL}")
            return RemoteListing(kind="-", dirs=[], files=[])
        dirs: list[Path] = []
        files: list[Path] = []
        unreadable: list[OSError] = []
        walker = os.walk(root, followlinks=False, onerror=unreadable.append)
        for dirpath, dirnames, filenames in walker:
            here = Path(dirpath)
            for name in [*dirnames, *filenames]:
                if "\n" in name:
                    raise ListingError(
                        f"{root}: a name in this tree contains a newline; rename it and retry"
                    )
            dirs.extend(here / d for d in dirnames if not (here / d).is_symlink())
            files.extend(here / f for f in filenames if (here / f).is_file())
        if unreadable:
            raise ListingError(f"{root}: cannot read {unreadable[0].filename}")
        return RemoteListing(kind="d", dirs=dirs, files=files)

    @override
    async def _mkdir_all(self, paths: "list[Path]", *, user: str | None = None) -> Result:
        """Create the directories with no shell — the runner's own filesystem answers."""
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: _mkdir_all(user=...) is not supported on LocalHost — "
                f"local transfers keep the invoking user's ownership"
            ) from None
        if is_dry_run():
            names = ", ".join(str(p) for p in paths)
            self._log_command(f"[DRY RUN] mkdir -p: {names}")
            return Result(status=Status.NotRun, msg=f"[DRY RUN] mkdir -p: {names}")
        try:
            for p in paths:
                p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Result(status=Status.Error, msg=f"{exc.filename}: {exc.strerror}")
        return Result(status=Status.Success)

    ####################
    #  Power / reachability
    ####################

    @override
    async def is_reachable(self, *, timeout: float = PROBE_TIMEOUT_S, fresh: bool = False) -> bool:
        """Return ``True`` — the local machine is always reachable."""
        return True

    ####################
    #  Cleanup
    ####################

    @override
    async def _close(self) -> None:
        session_mgr = self._session_mgr
        await session_mgr.close_all()


def _nearest_existing_ancestor_searchable(path: Path) -> bool:
    """Whether the closest directory above *path* that exists may be searched.

    The shell walk's rule for "missing" (``recursive_transfer._ABSENT_OR_UNKNOWN``):
    a path that does not show is missing only when the directory it would sit
    in could be looked into.
    """
    ancestor = path.parent
    while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    return os.access(ancestor, os.X_OK)
