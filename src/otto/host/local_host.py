import asyncio
import re
import shutil
from dataclasses import (
    dataclass,
    field,
)
from errno import (
    EIO,
    ERANGE,
)
from pathlib import Path
from typing import Annotated

from ..logger import get_otto_logger
from ..utils import Arg, CommandStatus, Exclude, Status, cli_exposed
from .file_ops import PosixFileOps
from .host import BaseHost, is_dry_run
from .power import PowerController
from .privilege import PosixPrivilege
from .product import Product
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    LocalSession,
    SessionManager,
)
from .transfer import BaseFileTransfer, TransferProgressFactory


class LocalFileTransfer(BaseFileTransfer):
    """File transfer for :class:`LocalHost` — a local copy via
    :func:`shutil.copy2`.

    Concrete :class:`~otto.host.transfer.BaseFileTransfer` so the ABC's
    progress contract holds uniformly across every backend in the host
    fleet (Unix's :class:`~otto.host.transfer.UnixFileTransfer`, embedded's
    :class:`~otto.host.transfer.EmbeddedFileTransfer`, and this
    one). Per-file completion is the granularity — ``shutil.copy2`` is a
    single blocking C call with no progress hook, the analogue of an
    embedded ``fs read``.
    """

    async def _do_copy(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for src in src_files:
                dest = dest_dir / src.name
                await asyncio.to_thread(shutil.copy2, src, dest)
                if progress_factory is not None:
                    size = dest.stat().st_size
                    progress_factory()(str(src), str(dest), size, size)
            return Status.Success, ''
        except Exception as e:
            return Status.Error, str(e)

    async def _run_put(self, src_files, dest_dir, progress_factory):
        return await self._do_copy(src_files, dest_dir, progress_factory)

    async def _run_get(self, src_files, dest_dir, progress_factory):
        return await self._do_copy(src_files, dest_dir, progress_factory)

logger = get_otto_logger()


@dataclass(
    slots=True,
)
class LocalHost(PosixPrivilege, PosixFileOps, BaseHost):

    name: str = field(default='localhost', init=False)

    id: str = field(default='local', init=False)
    """Stable identifier for the local host — always ``"local"``."""

    log: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout and log files."""

    resources: set[str] = field(default_factory=set, repr=False)
    """Resources required to reserve this host — always empty for LocalHost."""

    products: list[Product] = field(default_factory=list, repr=False)
    """Software-under-test deployed to this host. Default empty."""

    power_control: 'PowerController | None' = field(default=None, repr=False)
    """Always None — LocalHost/DockerContainerHost are not power-controlled."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages persistent shell sessions for this host."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Manages periodic background command tasks for this host."""

    _file_transfer: LocalFileTransfer = field(init=False, repr=False)
    """Local copy via shutil, routed through BaseFileTransfer so progress
    reporting works uniformly across every host backend."""

    def __post_init__(self) -> None:
        self._session_mgr = SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=LocalSession,
            oneshot_factory=self._exec_subprocess,
        )
        self._repeater = RepeatRunner(run_cmds=self.run)
        self._file_transfer = LocalFileTransfer(name=self.name)

    ####################
    #  Command execution
    ####################

    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: bool = True,
    ) -> CommandStatus:
        """Execute a command via the persistent local shell session.

        Shell state (working directory, environment variables) persists between
        calls, matching UnixHost behavior.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout, log=log)

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        """Run a command in a fresh subprocess (stateless, concurrent-safe).

        Each call spawns an independent process — no state persists between
        calls, and multiple oneshot() calls can run concurrently via
        asyncio.gather().
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._exec_subprocess(cmd, timeout, log=log)

    async def _exec_subprocess(
        self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        """Fire-and-forget subprocess execution."""
        status = Status.Error
        lines: list[str] = []

        if log:
            self._log_command(cmd)

        proc = await asyncio.create_subprocess_shell(
            cmd=cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        if proc.stdout is None:
            return CommandStatus(command=cmd, output="Failed to set up stdout", retcode=EIO, status=status)

        try:
            while True:
                data = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not len(data):
                    break
                line = data.decode().rstrip()
                lines.append(line)
                if log:
                    self._log_output(line)
        except asyncio.TimeoutError:
            proc.terminate()
            return CommandStatus(
                command=cmd,
                output=f"Command timed out after {timeout}s\n" + "\n".join(lines),
                status=Status.Error,
                retcode=-1,
            )

        await proc.wait()
        if proc.returncode is None:
            return CommandStatus(command=cmd, output="Process did not provide a return code",
                                 retcode=ERANGE, status=status)

        if proc.returncode == 0:
            status = Status.Success
        else:
            status = Status.Failed

        return CommandStatus(command=cmd, output="\n".join(lines), retcode=proc.returncode, status=status)

    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session."""
        if is_dry_run():
            self._log_command(f"[DRY RUN] open_session({name!r})")
        return await self._session_mgr.open_session(name)

    async def send(self, text: str, log: bool = True) -> None:
        """Send raw text to the host's persistent session."""
        if is_dry_run():
            if log:
                self._log_command(f"[DRY RUN] send({text!r})")
            return
        await self._session_mgr.send(text, log=log)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        if is_dry_run():
            self._log_command("[DRY RUN] expect() skipped — pattern would never match without a live session")
            return ""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  File transfer
    ####################

    @cli_exposed(success="Download complete.")
    async def get(
        self,
        src_files: Annotated[list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Remote file(s) to download.")],
        dest_dir: Path,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> tuple[Status, str]:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer` so progress reporting
        flows through the same :class:`~otto.host.transfer.BaseFileTransfer`
        machinery as Unix and embedded backends."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        return await self._file_transfer.get_files(
            src_files, dest_dir, show_progress,
        )

    @cli_exposed(success="Transfer complete.")
    async def put(
        self,
        src_files: Annotated[list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")],
        dest_dir: Path,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> tuple[Status, str]:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer`; see :meth:`get`."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir)
        return await self._file_transfer.put_files(
            src_files, dest_dir, show_progress,
        )

    ####################
    #  Power / reachability
    ####################

    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Return ``True`` — the local machine is always reachable."""
        return True

    ####################
    #  Cleanup
    ####################

    async def close(self) -> None:
        await self._repeater.stop_all()
        await self._session_mgr.close_all()
