"""Local host implementation — runs commands on the machine otto itself is running on.

:class:`LocalHost` is a concrete :class:`~otto.host.host.BaseHost` that spawns
subprocesses and manages a persistent local shell session, mirroring the API of
:class:`~otto.host.unix_host.UnixHost` without any network transport. File
transfers are handled by :class:`LocalFileTransfer` (a :mod:`shutil`-backed
:class:`~otto.host.transfer.BaseFileTransfer` subclass) so progress reporting
works uniformly across all host backends.
"""

import asyncio
import contextlib
import logging
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

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import Arg, Exclude, Opt, Status, cli_exposed
from .dev_tool import DevTool
from .file_ops import PosixFileOps
from .host import _EXEC_REAP_TIMEOUT, BaseHost, is_dry_run
from .lab_info import LabInfo
from .power import PowerController
from .privilege import PosixPrivilege
from .product import Product
from .session import (
    Expect,
    HostSession,
    LocalSession,
    SessionManager,
)
from .toolchain import Toolchain
from .transfer import BaseFileTransfer, ProgressGranularity, TransferProgressFactory
from .transfer.base import mark_skipped


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
    ) -> dict[Path, Result]:
        # Sequential single-directory copy: an OSError (e.g. a missing source)
        # stops the loop and every not-yet-copied file is marked Skipped. Keyed
        # by the source path exactly as passed.
        per_file: dict[Path, Result] = {}
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {src: Result(Status.Error, msg=str(e)) for src in src_files}
        for i, src in enumerate(src_files):
            dest = dest_dir / src.name
            try:
                await asyncio.to_thread(shutil.copy2, src, dest)
                if progress_factory is not None:
                    size = dest.stat().st_size
                    progress_factory()(str(src), str(dest), size, size)
            except OSError as e:
                per_file[src] = Result(Status.Error, msg=str(e))
                mark_skipped(per_file, src_files[i + 1 :])
                break
            per_file[src] = Result(Status.Success, value=dest)
        return per_file

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._do_copy(src_files, dest_dir, progress_factory)

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._do_copy(src_files, dest_dir, progress_factory)

    @override
    async def _apply_mode(self, dest_paths: list[Path], mode: int) -> Result:
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
            return Result(Status.Error, msg=str(e))
        return Result(Status.Success)


logger = logging.getLogger(__name__)


@dataclass(
    slots=True,
)
class LocalHost(PosixPrivilege, PosixFileOps, BaseHost):
    """A host that runs commands on the local machine via a persistent shell session.

    Implements the full :class:`~otto.host.host.BaseHost` API (run, exec, put,
    get, open_session, send, expect, is_reachable) without any network transport.
    Shell state (working directory, environment variables) persists across ``run``
    calls through a :class:`~otto.host.session.SessionManager`-backed local
    session; ``exec`` bypasses it and spawns an independent subprocess, making
    concurrent calls safe. File transfers delegate to :class:`LocalFileTransfer`.
    """

    name: str = field(default="localhost", init=False)

    id: str = field(default="local", init=False)
    """Stable identifier for the local host — always ``"local"``."""

    has_bash: bool = True
    """Whether this host has a working ``bash`` a command can be tagged and
    exec'd through (``bash -c 'exec -a …'``). Tunnel discovery
    (:mod:`otto.tunnel.discovery`) scans only ``has_bash`` hosts. The local
    machine has bash by default."""

    log: LogMode = field(default=LogMode.NORMAL, repr=False)
    """Standing per-host logging disposition. ``QUIET`` keeps this host's command
    I/O in ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere
    (warnings/errors are unaffected)."""

    lab_info: LabInfo = field(default_factory=LabInfo, repr=False)
    """The resolved lab this host was registered into (empty unless a loader stamped it)."""

    debug_log_globs: list[str] = field(default_factory=list, repr=False)
    """Paths/glob patterns ``get_debug_logs`` fetches. Default empty. See
    :attr:`~otto.host.host.BaseHost.debug_log_globs`."""

    products: list[Product] = field(default_factory=list, repr=False)
    """Software-under-test deployed to this host. Default empty."""

    dev_tools: list[DevTool] = field(default_factory=list, repr=False)
    """Repo-internal tooling deployed to this host. Default empty."""

    toolchain: Toolchain = field(default_factory=Toolchain, repr=False)
    """Toolchain for this host's products — the system one by default, which is
    the right answer for the machine otto is already running on. See
    :attr:`~otto.host.host.BaseHost.toolchain`."""

    power_control: "PowerController | None" = field(default=None, repr=False)
    """Always None — LocalHost/DockerContainerHost are not power-controlled."""

    dry_run_exempt: bool = field(default=False, repr=False)
    """Opt this instance's ``run`` out of the ``--dry-run`` decline.

    **THE TEST, and all three parts must hold:** the commands this host will be
    asked to run contact **no device**, **mutate nothing**, and exist only for
    **otto's own bookkeeping** about the machine it is already running on.
    Anything else — anything an operator would recognise as work on a host —
    keeps the default and declines.

    It exists because ``LocalHost`` does double duty. It is a lab host (``otto
    host local run …``, which must decline under ``-n`` like any other host)
    AND it is otto's subprocess runner for questions about its own environment
    (:meth:`otto.config.repo.Repo.run_git_command` reads the SUT checkout's
    HEAD to stamp provenance on the run). The dry-run guard sits at the command
    boundary, so it fires on **which abstraction was used** rather than on
    **what was meant**, and it declined otto's own ``git log`` — a false
    positive of the contract, not enforcement of it. This flag is the one thing
    the two uses do not share, so it is what the policy is keyed on.

    Deliberately narrow, in four ways:

    * It is declared **at the construction site**, so an exemption is visible
      in the same statement that creates the host — and every exemption in the
      tree is one ``grep -rn dry_run_exempt src/`` away.
    * It lives on ``LocalHost`` and nowhere else. A remote host is a device by
      definition, so the exemption cannot even be spelled for one.
    * It is per-instance, not a context manager over a shared flag: the host it
      exempts is built for one call and thrown away, so there is no window in
      which a concurrent coroutine could run a real device command inside
      someone else's exemption.
    * It is read by ``_run_one`` alone. ``exec``, the transfer verbs,
      sessions and the power verbs all still decline on an exempt instance,
      because none of them is covered by the justification above.
    """

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages persistent shell sessions for this host."""

    _file_transfer: LocalFileTransfer = field(init=False, repr=False)
    """Local copy via shutil, routed through BaseFileTransfer so progress
    reporting works uniformly across every host backend."""

    def __post_init__(self) -> None:
        self._session_mgr = SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=LocalSession,
            exec_factory=self._exec_subprocess,
            creds=[],
            host_id=self.id,
        )
        self._file_transfer = LocalFileTransfer(name=self.name)

    ####################
    #  Command execution
    ####################

    @override
    async def _run_one(
        self,
        cmd: str,
        timeout: float,
        expects: list[Expect] | None = None,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Execute a command via the persistent local shell session.

        Shell state (working directory, environment variables) persists between
        calls, matching UnixHost behavior.

        The dry-run arm honours :attr:`dry_run_exempt` — the single seam that
        reads it. See that attribute for the three-part test an exemption has
        to pass; the default is to decline, and every host handed out by the
        fleet takes the default.
        """
        if is_dry_run() and not self.dry_run_exempt:
            return self._dry_run_result(cmd, log)
        return await self._session_mgr.run_cmd(
            cmd, expects=expects, timeout=timeout, log=self._effective_log(log)
        )

    @override
    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run a command in a fresh subprocess (stateless, concurrent-safe).

        Each call spawns an independent process — no state persists between
        calls, and multiple exec() calls can run concurrently via
        asyncio.gather().
        """
        return await self._exec_subprocess(cmd, timeout, log=self._effective_log(log))

    async def _exec_subprocess(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Fire-and-forget subprocess execution."""
        status = Status.Error
        lines: list[str] = []
        mode = log

        if mode is not LogMode.NEVER:
            self._log_command(cmd, mode)

        # Drive loop.subprocess_shell() directly rather than the higher-level
        # asyncio.create_subprocess_shell(), for the same reason
        # LocalSession._open() does: it hands back the transport, so the
        # `finally` below can release the pipe fds without reaching into the
        # private Process._transport. stdin=None is not a default — it is what
        # create_subprocess_shell passed, and loop.subprocess_shell would
        # otherwise give the child a pipe instead of our stdin.
        loop = asyncio.get_running_loop()

        def protocol_factory() -> asyncio.subprocess.SubprocessStreamProtocol:
            return asyncio.subprocess.SubprocessStreamProtocol(limit=2**16, loop=loop)

        transport, protocol = await loop.subprocess_shell(
            protocol_factory,
            cmd,
            stdin=None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        proc = asyncio.subprocess.Process(transport, protocol, loop)

        try:
            if proc.stdout is None:
                return CommandResult(
                    status=status, value="Failed to set up stdout", command=cmd, retcode=EIO
                )

            # Narrow once, outside the closure: proc.stdout is confirmed non-None
            # above, and this local is never reassigned, so the drain loop below
            # doesn't need its own None-check.
            stdout = proc.stdout

            async def _drain() -> None:
                while True:
                    data = await stdout.readline()
                    if not len(data):
                        break
                    line = data.decode().rstrip()
                    lines.append(line)
                    if mode is not LogMode.NEVER:
                        self._log_output(line, mode)

            try:
                # The whole drain is wrapped in a single wait_for so *timeout*
                # bounds the command, not each individual readline -- a command
                # that emits output more often than the timeout period (e.g.
                # `ping`) would otherwise never trip the per-line wait and run
                # forever.
                await asyncio.wait_for(_drain(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.terminate()
                # A process can trap SIGTERM, so bound the reap -- an unbounded wait
                # here would defeat the timeout it implements. If the bound fires,
                # escalate to SIGKILL (untrappable) rather than leaving the process
                # running with our pipes held open.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_EXEC_REAP_TIMEOUT)
                except asyncio.TimeoutError:
                    proc.kill()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=_EXEC_REAP_TIMEOUT)
                return CommandResult(
                    status=Status.Error,
                    value=f"Command timed out after {timeout}s\n" + "\n".join(lines),
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )

            await proc.wait()
            if proc.returncode is None:
                return CommandResult(
                    status=status,
                    value="Process did not provide a return code",
                    command=cmd,
                    retcode=ERANGE,
                )

            status = Status.Success if proc.returncode == 0 else Status.Failed

            return CommandResult(
                status=status, value="\n".join(lines), command=cmd, retcode=proc.returncode
            )
        finally:
            # asyncio retires a subprocess transport only once the child has
            # exited AND every pipe has reached EOF. The timeout path above
            # cancels the drain mid-stream, so stdout never reaches EOF: the
            # child is reaped, but the read-pipe transport, that pipe's fd and
            # the subprocess transport itself all survive until the garbage
            # collector complains -- three ResourceWarnings per timed-out
            # command. Under pytest those are unraisable exceptions billed to
            # whichever test the collector was running when it noticed, so the
            # cost landed on a stranger in a different file each run.
            #
            # In a `finally` rather than on the timeout branch alone so that
            # cancellation from outside is covered too: there the child is
            # still running, and close() kills it rather than orphaning it.
            # Free where nothing leaked -- close() returns immediately if the
            # transport already finished, which is every non-timeout path.
            transport.close()

    @override
    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session.

        Under a dry run no subprocess shell is spawned and the handle is a
        :class:`~otto.host.session.DeclinedSession` — see
        ``BaseHost._dry_run_session``.
        """
        if is_dry_run():
            return self._dry_run_session(name)
        return await self._session_mgr.open_session(name)

    @override
    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the host's persistent session."""
        effective = self._effective_log(log)
        if is_dry_run():
            # The folded mode, not the default NORMAL: a dry run must not put a
            # send on the console that a real run keeps off it. No NEVER guard
            # here on purpose -- `_log_command` returns before it logs on NEVER,
            # and that is the ONE home for the decision; a second copy here
            # reads as redundant and gets deleted, taking the real one's twin
            # with it. Building the f-string first costs nothing that matters:
            # `text` is already a live `str` and `repr` only copies it.
            self._log_command(f"[DRY RUN] send({text!r})", effective)
            return
        await self._session_mgr.send(text, log=effective)

    @override
    async def _expect_one(
        self,
        pattern: str | re.Pattern[str],
        timeout: float,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  File transfer
    ####################

    @override
    @cli_exposed(success="Download complete.", dry_run_preview=True)
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(variadic=True, elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> Result:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer` so progress reporting
        flows through the same :class:`~otto.host.transfer.BaseFileTransfer`
        machinery as Unix and embedded backends.
        """
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        return await self._file_transfer.get_files(
            src_files,
            dest_dir,
            show_progress,
        )

    @override
    @cli_exposed(success="Transfer complete.", dry_run_preview=True)
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> Result:
        """Copy files to dest_dir on the local filesystem.

        Delegates to :class:`LocalFileTransfer`; see :meth:`get`. *mode* sets
        the permission bits on the copies — an ``int`` (``0o755``) from
        Python, or a string always read as octal (``"755"``, ``"0755"``,
        ``"0o755"``). Without it, ``shutil.copy2`` preserves the source
        file's own permissions.
        """
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir, mode)
        return await self._file_transfer.put_files(
            src_files,
            dest_dir,
            show_progress,
            mode,
        )

    ####################
    #  Power / reachability
    ####################

    @override
    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Return ``True`` — the local machine is always reachable."""
        return True

    ####################
    #  Cleanup
    ####################

    @override
    async def close(self) -> None:
        await self._session_mgr.close_all()
