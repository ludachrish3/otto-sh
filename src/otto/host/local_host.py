"""Local host implementation — runs commands on the machine otto itself is running on.

:class:`LocalHost` is a concrete :class:`~otto.host.host.BaseHost` that spawns
local shell sessions (a persistent one for ``run``, pooled ones for ``exec``), mirroring the API of
:class:`~otto.host.unix_host.UnixHost` without any network transport. File
transfers are handled by :class:`LocalFileTransfer` (a :mod:`shutil`-backed
:class:`~otto.host.transfer.BaseFileTransfer` subclass) so progress reporting
works uniformly across all host backends.
"""

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import (
    dataclass,
    field,
)
from errno import (
    EIO,
)
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from typing_extensions import final, override

from ..logger.mode import LogMode
from ..models.base import Cred
from ..result import CommandResult, Result
from ..utils import Arg, Exclude, Opt, Status, cli_exposed, process_user
from . import process_stop
from .capability_grid import HostCapabilities, OptionSupport, SessionIdentity, UserSupport
from .exec_route import ExecRoute
from .exit_status import exit_status_of, no_return_code_result
from .file_ops import PosixFileOps
from .host import (
    CONCURRENT_HELP,
    PROBE_TIMEOUT_S,
    BaseHost,
    CredentialExpect,
    is_dry_run,
)
from .output_codec import READ_CHUNK, output_decoder
from .privilege import PosixPrivilege
from .session import (
    ExecOutput,
    ExecPlan,
    Expect,
    LocalSession,
    SessionManager,
    begin_exec_log,
    drain_and_wait,
    timed_out_result,
)
from .transfer import BaseFileTransfer, ProgressGranularity, TransferProgressFactory

if TYPE_CHECKING:
    from .command_frame import ExecInput
    from .recursive_transfer import RemoteListing


async def _decoded_chunks(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield what *stream* carries, decoded, in the pieces it was read in.

    It reads in chunks rather than with ``readline``, so a line has no length
    limit: ``readline`` raises once a line outgrows the stream's buffer limit.
    A pipe carries bytes and the shared drain reads text: a byte that is not
    UTF-8 is replaced, never raised (the command-output contract), and the
    one incremental decoder keeps a character split across two reads whole.
    Lines are :class:`~otto.host.session.ExecOutput`'s to find.
    """
    decoder = output_decoder()
    while chunk := await stream.read(READ_CHUNK):
        if text := decoder.decode(chunk):
            yield text
    if tail := decoder.decode(b"", final=True):
        yield tail


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
        """Refuse a terminal: every session and subprocess of the local host reads a pipe."""
        return (
            "is not available on the local host, whose sessions read a pipe" if terminal else None
        )

    @override
    def _plan_exec(self, *, user: str | None, needs_shell: bool) -> ExecPlan:
        """Plan a local shell session for the call: ``POOLED_SHELL``, always.

        *user* is the call's effective user (an enclosing ``as_user()``
        included); the session is opened as them, and answers a prompt or an
        elevation as ``run``'s does. It reads a pipe, so the plan has one
        mode. The manager keeps no idle local
        session (``SessionManager._idle_exec_sessions_kept``): each call opens
        a shell for itself, which starts from the Python process's current
        directory and environment, and closes it after.
        """
        return ExecPlan(route=ExecRoute.POOLED_SHELL)

    @override
    async def _run_plan(
        self,
        plan: ExecPlan,
        *,
        cmd: str,
        timeout: float,
        log: LogMode,
        user: str | None,
        expects: "list[Expect | CredentialExpect] | None",
        exec_input: "ExecInput | None",
    ) -> CommandResult:
        """Run *cmd* on the route :meth:`_plan_exec` chose.

        The pooled shell is the session manager's, as on every family that
        plans one; each call has a shell of its own, so several ``exec()``
        calls can run at once via ``asyncio.gather()``. The subprocess route
        is still dispatched here for a plan that names it.
        """
        if plan.route is ExecRoute.LOCAL_SUBPROCESS:
            return await self._exec_subprocess(
                cmd, timeout, log=self._effective_log(log), exec_input=exec_input
            )
        return await self._exec_on_manager_route(
            plan.route,
            cmd=cmd,
            timeout=timeout,
            log=log,
            user=user,
            expects=expects,
            exec_input=exec_input,
            plain=plan.plain,
        )

    async def _exec_subprocess(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        *,
        exec_input: "ExecInput | None" = None,
    ) -> CommandResult:
        """Fire-and-forget subprocess execution; every result it returns records its route.

        No ``exec`` reaches it: a local ``exec`` runs on a pooled session.

        Logs the command and its output as ``resolve_exec_log`` answers for
        *log* and *exec_input*: on this host, or on a delegating host.
        """
        route = ExecRoute.LOCAL_SUBPROCESS.value
        status = Status.Error
        output = ExecOutput()
        xl = begin_exec_log(cmd, log, self._log_command, self._log_output, exec_input=exec_input)

        # Drive loop.subprocess_shell() directly rather than the higher-level
        # asyncio.create_subprocess_shell(), for the same reason
        # LocalSession.open_transport() does: it hands back the transport, so the
        # `finally` below can release the pipe fds without reaching into the
        # private Process._transport. stdin is end-of-file, as on the ssh
        # channel: a command that reads stdin must not read otto's. A new
        # session gives the command its own process group (id = its pid) and
        # no controlling terminal, so a stop reaches the command's group.
        loop = asyncio.get_running_loop()

        def protocol_factory() -> process_stop.LeaderExitProtocol:
            return process_stop.LeaderExitProtocol(limit=2**16, loop=loop)

        async def stop_started(
            started: "tuple[asyncio.SubprocessTransport, process_stop.LeaderExitProtocol]",
            exc: BaseException,
        ) -> None:
            # Cancelled while the spawn was still returning: the command is
            # already running, so it gets the same stop as a cancelled drain,
            # then the release the `finally` below gives.
            started_transport, started_protocol = started
            started_proc = asyncio.subprocess.Process(started_transport, started_protocol, loop)
            try:
                await process_stop.stop_interrupted(
                    process_stop.LocalStoppable(started_proc, started_protocol.leader_exited),
                    exc,
                    what=f"{self.name}: stop the interrupted command",
                )
            finally:
                with contextlib.suppress(ProcessLookupError):
                    started_transport.close()

        transport, protocol = await process_stop.started_or_stopped(
            loop.subprocess_shell(
                protocol_factory,
                cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            ),
            stop_started,
            what=f"{self.name}: start the command",
        )
        proc = asyncio.subprocess.Process(transport, protocol, loop)

        try:
            if proc.stdout is None:
                return CommandResult(
                    status=status,
                    value="Failed to set up stdout",
                    command=cmd,
                    retcode=EIO,
                    route=route,
                )

            stoppable = process_stop.LocalStoppable(proc, protocol.leader_exited)
            try:
                # One wait_for over the drain AND the wait for exit, so
                # *timeout* bounds the command, not each read, and not only
                # its output: a command that closes its output and keeps
                # running still times out (#1063).
                returncode = await asyncio.wait_for(
                    drain_and_wait(
                        _decoded_chunks(proc.stdout),
                        output,
                        proc.wait,
                        xl.context.log_output,
                        xl.context.mode,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await process_stop.stop_timed_out(
                    stoppable, what=f"{self.name}: stop the timed-out command"
                )
                return timed_out_result(
                    cmd=cmd, timeout=timeout, output=output, route=ExecRoute.LOCAL_SUBPROCESS
                )
            except BaseException as exc:
                await process_stop.stop_interrupted(
                    stoppable, exc, what=f"{self.name}: stop the interrupted command"
                )
                raise

            retcode = exit_status_of(returncode)
            if retcode is None:
                return no_return_code_result(cmd=cmd, route=ExecRoute.LOCAL_SUBPROCESS)

            status = Status.Success if retcode == 0 else Status.Failed

            return CommandResult(
                status=status, value=output.value(), command=cmd, retcode=retcode, route=route
            )
        finally:
            # asyncio retires a subprocess transport only once the child has
            # exited AND every pipe has reached EOF. The timeout and interrupt
            # paths cancel the drain mid-stream, so stdout never reaches EOF:
            # the child is reaped, but the read-pipe transport, that pipe's fd
            # and the subprocess transport itself all survive until the
            # garbage collector complains -- three ResourceWarnings per
            # timed-out command, billed under pytest to whichever test the
            # collector was running when it noticed. The stop above has
            # already signalled the command; close() only releases, and
            # returns at once where nothing leaked. It kills a leader the
            # bounded stop left unreaped, which may by then be gone.
            with contextlib.suppress(ProcessLookupError):
                transport.close()

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
