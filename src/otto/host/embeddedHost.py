"""
Embedded (bare-metal / RTOS) host class.

An :class:`EmbeddedHost` is a network-reached target whose "OS" is a real-time
kernel or bare-metal firmware rather than a POSIX system — Zephyr is the first
concrete example. It is exposed through the *same* :class:`~otto.host.host.Host`
API as :class:`~otto.host.unixHost.UnixHost` (``run``/``oneshot``/``send``/
``expect``/``put``/``get``) so test code does not care whether a target is a
Linux box or a microcontroller.

What makes an embedded target different from a Unix host:

- **One console.** A Zephyr device exposes a *single* shell over telnet. There
  is no second channel and no stateless exec primitive, so ``oneshot`` shares
  the one persistent session with ``run`` and is therefore **not**
  concurrency-safe (it is on :class:`UnixHost`).
- **No bash.** No ``$?``, no command substitution, no ``scp``/``ftp``/``nc``.
  Command framing and file transfer cannot reuse the Unix machinery.
- **Telnet only.** The shell is reached over telnet (optionally through an SSH
  hop), never SSH directly.

Command execution speaks the Zephyr shell: the :class:`SessionManager` is
wired with :class:`~otto.host.zephyr.ZephyrSession`, which frames each command
for the RTOS shell (see that module). Console file transfer (``get``/``put``)
lands in a later phase and currently raises :class:`NotImplementedError`, as
does the interactive bridge (``_interact``).
"""

import asyncio
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .connections import ConnectionManager
from .host import isDryRun
from .options import TelnetOptions
from .remoteHost import OsType, RemoteHost
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    SessionManager,
)
from .zephyr import ZephyrSession

logger = getOttoLogger()


@dataclass(slots=True)
class EmbeddedHost(RemoteHost):
    """A bare-metal / RTOS host reached over telnet (Zephyr being the first kind)."""

    ip: str
    """IP address of the host's telnet shell."""

    ne: str = field(repr=False)
    """Network element to which this host belongs."""

    osType: OsType = 'embedded'
    """OS family of this host. Always ``embedded`` for an :class:`EmbeddedHost`."""

    osName: Optional[str] = 'Zephyr'
    """Kernel/OS name. Defaults to ``Zephyr``, the first supported RTOS."""

    osVersion: Optional[str] = None
    """OS/kernel version string, or None if unspecified."""

    name: str = None  # type: ignore
    """Human readable name to represent the host. Automatically generated if not provided."""

    creds: dict[str, str] = field(default_factory=dict)
    """Users and their respective passwords. Optional — the Zephyr telnet shell
    backend has no login step, so this is empty for a stock Zephyr target."""

    user: Optional[str] = None
    """User with which to log in, if the shell requires one. Usually unset."""

    neId: Optional[int] = field(default=None, repr=False)
    """Network element identifier to which this host belongs."""

    board: Optional[str] = field(default=None, repr=False)
    """Name of the board type to which this host belongs."""

    slot: Optional[int] = field(default=None, repr=False)
    """Physical slot number of the board to which this host belongs."""

    is_virtual: bool = False
    """Determines whether a host is a VM/emulator (e.g. QEMU) or not."""

    telnet_options: TelnetOptions = field(default_factory=TelnetOptions, repr=False)
    """Connection options for the telnet shell (port, cols/rows, etc.)."""

    hop: Optional[str] = None
    """Host ID of the intermediate SSH hop used to reach this host, or None."""

    resources: set[str] = field(default_factory=set[str])
    """Names of resources required to use this host."""

    log: bool = field(default=True, repr=False)
    """Whether this host should log its output to stdout and log files."""

    log_stdout: bool = field(default=True, repr=False)
    """Whether this host should log its output to stdout."""

    id: str = field(init=False, repr=False)
    """Unique identifier for this host."""

    _connection_factory: type[ConnectionManager] | None = field(default=None, init=True, repr=False)
    """Optional ConnectionManager subclass for dependency injection (test doubles)."""

    _connections: ConnectionManager = field(init=False, repr=False)
    """Manages the raw telnet transport for this host."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Manages periodic background command tasks for this host."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages the persistent shell session for this host."""

    def __post_init__(self) -> None:

        self.id = self._generateId()
        if self.name is None:
            self.name = self._generateName()

        hop_transport = self._build_hop_transport() if self.hop else None

        # An RTOS telnet shell has no login step — force ``login=False`` so the
        # connection never blocks waiting for a ``login:`` prompt that the
        # device will never send.
        factory = self._connection_factory or ConnectionManager
        self._connections = factory(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term='telnet',
            name=self.name,
            hop=hop_transport,
            telnet_options=replace(self.telnet_options, login=False),
        )
        self._repeater = RepeatRunner(run_cmds=self.run)
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            telnet_session_cls=ZephyrSession,
        )

    @property
    def _connected(self) -> bool:
        """Whether the host has any current connections or live sessions."""
        return self._session_mgr.has_live_sessions or self._connections.connected

    ####################
    #  Connection
    ####################

    async def verify_connection(self) -> CommandStatus:
        """Attempt to open the telnet shell without running commands (dry-run)."""
        try:
            await self._connections.telnet()
            self._log_command("[DRY RUN] Connection verified")
            return CommandStatus(command="connect", output="Connection successful", status=Status.Success, retcode=0)
        except Exception as e:
            self._log_command(f"[DRY RUN] Connection FAILED: {e}")
            return CommandStatus(command="connect", output=str(e), status=Status.Error, retcode=1)

    async def close(self) -> None:
        await self._repeater.stop_all()
        await self._session_mgr.close_all()
        await self._connections.close()

    def __del__(self):
        """Best-effort cleanup on garbage collection. Call close() explicitly for reliable cleanup."""

        # Guard against partially-constructed instances (e.g. __post_init__ threw)
        if getattr(self, '_connections', None) is None:
            return

        if not self._connected:
            return

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.close())
        except RuntimeError:
            try:
                asyncio.run(self.close())
            except (RuntimeError, TypeError):
                pass  # Loop is closed or mocks can't be awaited; OS will clean up

    ####################
    #  Command execution
    ####################

    async def _interact(self) -> None:
        """Open an interactive shell bridged to the local terminal.

        Not yet implemented for embedded hosts — the telnet bridge for a
        login-less RTOS shell lands in a later phase.
        """
        raise NotImplementedError(
            "Interactive sessions for embedded hosts are not yet implemented"
        ) from None

    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
    ) -> CommandStatus:
        """Execute a single command on the embedded host via the persistent shell session.

        Like :meth:`UnixHost._run_one`, the session is stateful and **sequential
        only** — the embedded target has a single console, so concurrent
        ``run()`` calls would corrupt the session.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout)

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Run a single command on the embedded host.

        Unlike :meth:`UnixHost.oneshot`, this is **not** concurrency-safe: an
        embedded target exposes a single console with no stateless exec
        primitive, so ``oneshot`` runs on the same persistent session as
        :meth:`run`. It exists for API parity; use :meth:`run` for stateful
        workflows.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, timeout=timeout)

    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session.

        Note: an embedded target has a single console. Opening a second named
        session opens a second telnet connection to the device, which most
        RTOS shell backends do not accept concurrently. Prefer the default
        session via :meth:`run`.
        """
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
            self._log_command("[DRY RUN] expect() skipped — pattern would never match without a live connection")
            return ""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        """Transfer files from the embedded host to the local machine.

        Not yet implemented — console-based file transfer over the RTOS shell
        lands in a later phase.
        """
        raise NotImplementedError(
            "File transfer for embedded hosts is not yet implemented"
        ) from None

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        """Transfer files from the local machine to the embedded host.

        Not yet implemented — console-based file transfer over the RTOS shell
        lands in a later phase.
        """
        raise NotImplementedError(
            "File transfer for embedded hosts is not yet implemented"
        ) from None
