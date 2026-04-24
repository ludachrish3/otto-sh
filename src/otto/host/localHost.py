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

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .host import BaseHost, isDryRun
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    LocalSession,
    SessionManager,
)

logger = getOttoLogger()


@dataclass(
    slots=True,
)
class LocalHost(BaseHost):

    name: str = field(default='localhost', init=False)

    log: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout and log files."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages persistent shell sessions for this host."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Manages periodic background command tasks for this host."""

    def __post_init__(self) -> None:
        self._session_mgr = SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=LocalSession,
            oneshot_factory=self._exec_subprocess,
        )
        self._repeater = RepeatRunner(run_cmds=self.run)

    ####################
    #  Command execution
    ####################

    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
    ) -> CommandStatus:
        """Execute a command via the persistent local shell session.

        Shell state (working directory, environment variables) persists between
        calls, matching RemoteHost behavior.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout)

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Run a command in a fresh subprocess (stateless, concurrent-safe).

        Each call spawns an independent process — no state persists between
        calls, and multiple oneshot() calls can run concurrently via
        asyncio.gather().
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._exec_subprocess(cmd, timeout)

    async def _exec_subprocess(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Fire-and-forget subprocess execution."""
        status = Status.Error
        lines: list[str] = []

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
        if isDryRun():
            self._log_command(f"[DRY RUN] open_session({name!r})")
        return await self._session_mgr.open_session(name)

    async def send(self, text: str) -> None:
        """Send raw text to the host's persistent session."""
        if isDryRun():
            self._log_command(f"[DRY RUN] send({text!r})")
            return
        await self._session_mgr.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        if isDryRun():
            self._log_command("[DRY RUN] expect() skipped — pattern would never match without a live session")
            return ""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        """Copy files to dest_dir on the local filesystem."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if isDryRun():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for src in src_files:
                await asyncio.to_thread(shutil.copy2, src, dest_dir / src.name)
            return Status.Success, ""
        except Exception as e:
            return Status.Error, str(e)

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        """Copy files to dest_dir on the local filesystem."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if isDryRun():
            return self._dry_run_transfer("PUT", src_files, dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for src in src_files:
                await asyncio.to_thread(shutil.copy2, src, dest_dir / src.name)
            return Status.Success, ""
        except Exception as e:
            return Status.Error, str(e)

    ####################
    #  Cleanup
    ####################

    async def close(self) -> None:
        await self._repeater.stop_all()
        await self._session_mgr.close_all()
