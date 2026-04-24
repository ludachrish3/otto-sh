"""
Remote host class

Remote hosts manage 2 main things:

- Command execution

  - SSH (via asyncssh)
  - telnet (via telnetlib3)

- File transfers

  - SCP (via asyncssh)
  - SFTP (via asyncssh)
  - FTP (via aioftp)
  - netcat (via commands on the client and host)

All kinds of host connections, for command execution and for file transfers, should be able to establish a
connection first, keep it open, and then use it for multiple commands and transfers. Host connections can
be explictly closed by calling the `.close()` method or in the destructor of the host object.

The `.run()` method runs a single command (str) or a list of commands. Depending on the `.term` value
the correct connection type (ssh or telnet) is used without being specified as an argument.

The `.put()` and `.get()` methods both take a single file or a list of files. Depending on the `.transfer`
value the correct connection type (scp, sftp, ftp, or netcat) is used without being specified as an argument.
"""

# TODO: Consider having a single function that takes a connection, and does the lower level asyncio stuff
# For example, run could dynamically dispatch to _runSshCmds(), which would pass along the _ssh_conn member
# Then the _ssh_conn would be the connection used in an "async with" block to issue the command.
# Main problem here is that eash library uses its own method names to run commands and put/get files
# Possibly make the homegrown TelnetClient class mirror asyncssh? that could really help with design symmetry.
import asyncio
import re
import socket
from dataclasses import (
    dataclass,
    field,
    replace,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    cast,
)

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

from ..logger import getOttoLogger
from ..utils import (
    CommandStatus,
    Status,
    is_literal,
)
from .connections import (
    ConnectionManager,
    TermType,
)
from .host import (
    BaseHost,
    Host,
    SuppressCommandOutput,
    isDryRun,
)
from .interact import run_ssh_login, run_telnet_login
from .telnet import TelnetClient
from .options import (
    FtpOptions,
    NcOptions,
    ScpOptions,
    SftpOptions,
    SshOptions,
    TelnetOptions,
)
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    SessionManager,
)
from .toolchain import Toolchain
from .transfer import (
    FileTransfer,
    FileTransferType,
)

logger = getOttoLogger()


@dataclass(slots=True)
class RemoteHost(BaseHost):
    """
    Remote host accessed via SSH/network protocols.
    """

    ip: str
    """IP address of the host."""

    creds: dict[str, str]
    """Users and their respective passwords for this host."""

    ne: str = field(repr=False)
    """Network element to which this host belongs."""

    name: str = None # type: ignore
    """Human readable name to represent the host. Automatically generated if not provided."""

    user: Optional[str] = None
    """User with which to log in. If not provided, the first user in the `creds` dict will be used."""

    neId: Optional[int] = field(default=None, repr=False)
    """Network element identifier to which this host belongs.
    None indicates there are no other NEs of this type and a number is not needed."""

    board: Optional[str] = field(default=None, repr=False)
    """Name of the board type to which this host belongs."""

    slot: Optional[int] = field(default=None, repr=False)
    """Phyiscal slot number of the board to which this host belongs."""

    hw_version: Optional[str] = None
    """Hardware version description."""

    sw_version: Optional[str] = None
    """Software version description."""

    term: TermType = 'ssh'
    """Protocol used to issue terminal commands."""

    is_virtual: bool = False
    """Determines whether a host is a VM or not."""

    transfer: FileTransferType = 'scp'
    """Protocol used to transfer files."""

    ssh_options: SshOptions = field(default_factory=SshOptions, repr=False)
    """Connection options for SSH sessions (port, timeout, known_hosts,
    port-forwarding rules, etc.)."""

    telnet_options: TelnetOptions = field(default_factory=TelnetOptions, repr=False)
    """Connection options for telnet sessions (port, cols/rows, auto-resize, etc.)."""

    sftp_options: SftpOptions = field(default_factory=SftpOptions, repr=False)
    """Connection options for SFTP file transfers."""

    scp_options: ScpOptions = field(default_factory=ScpOptions, repr=False)
    """Connection options for SCP file transfers."""

    ftp_options: FtpOptions = field(default_factory=FtpOptions, repr=False)
    """Connection options for FTP file transfers (port, encoding, FTPS, etc.)."""

    nc_options: NcOptions = field(default_factory=NcOptions, repr=False)
    """Connection options for netcat file transfers (nc executable, port
    strategy, listener check, etc.)."""

    hop: Optional[str] = None
    """Host ID of the intermediate hop used to reach this host, or None for direct connection."""

    resources: set[str] = field(default_factory=set[str])
    """Names of resources required to use this host."""

    log: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout and log files.
    Setting this field to `False` effectively sets `log_stdout` to False as well."""

    log_stdout: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout.
    Commands and their output are still logged to log files if `log` is `True`."""

    toolchain: Toolchain = field(default_factory=Toolchain, repr=False)
    """Toolchain associated with this host's products.  Used by the
    coverage pipeline to select the correct ``gcov`` and ``lcov``
    binaries.  Defaults to system-installed tools."""

    id: str = field(init=False, repr=False)
    """Unique identifier for this host."""

    _connection_factory: type[ConnectionManager] | None = field(default=None, init=True, repr=False)
    """Optional ConnectionManager subclass for dependency injection (e.g. test doubles).
    When None, the real ConnectionManager is used."""

    _connections: ConnectionManager = field(init=False, repr=False)
    """Manages all raw transport connections for this host."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Manages periodic background command tasks for this host."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages persistent shell sessions for this host."""

    _file_transfer: FileTransfer = field(init=False, repr=False)
    """Handles all file transfer protocols for this host."""

    def __post_init__(self):

        self.id = self._generateId()
        if self.name is None:
            self.name = self._generateName()

        hop_transport = self._build_hop_transport() if self.hop else None

        factory = self._connection_factory or ConnectionManager
        self._connections = factory(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term=self.term,
            name=self.name,
            hop=hop_transport,
            ssh_options=self.ssh_options,
            telnet_options=self.telnet_options,
            sftp_options=self.sftp_options,
            ftp_options=self.ftp_options,
        )
        self._repeater = RepeatRunner(run_cmds=self.run)
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
        )
        self._file_transfer = FileTransfer(
            connections=self._connections,
            name=self.name,
            transfer=self.transfer,
            nc_options=self.nc_options,
            scp_options=self.scp_options,
            get_local_ip=lambda: self._get_local_ip(),
            open_session=lambda name: self.open_session(name),
            exec_cmd=lambda *a, **kw: self.oneshot(*a, **kw),
        )

    @property
    def _creds(self) -> tuple[str, str]:
        """Provide the (username, password) pair from creds. Delegates to ConnectionManager."""
        return self._connections.credentials

    @property
    def _connected(self) -> bool:
        """Whether the host has any current connections or live sessions."""
        return self._session_mgr.has_live_sessions or self._connections.connected

    ####################
    #  Connection
    ####################

    def rebuild_connections(self) -> None:
        """Recreate the ConnectionManager and dependents.

        Useful after changing ``hop`` or when the host must reconnect on a
        new event loop (e.g. after ``pytest.main()`` returns and coverage
        collection starts in a fresh ``asyncio.run()``).
        """
        hop_transport = self._build_hop_transport() if self.hop else None

        factory = self._connection_factory or ConnectionManager
        self._connections = factory(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term=self.term,
            name=self.name,
            hop=hop_transport,
            ssh_options=self.ssh_options,
            telnet_options=self.telnet_options,
            sftp_options=self.sftp_options,
            ftp_options=self.ftp_options,
        )
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
        )
        self._file_transfer = FileTransfer(
            connections=self._connections,
            name=self.name,
            transfer=self.transfer,
            nc_options=self.nc_options,
            scp_options=self.scp_options,
            get_local_ip=lambda: self._get_local_ip(),
            open_session=lambda name: self.open_session(name),
            exec_cmd=lambda *a, **kw: self.oneshot(*a, **kw),
        )

    def _build_hop_transport(self):
        """Build an ``SshHopTransport`` for reaching this host through its hop.

        The transport wraps a factory coroutine that lazily resolves the hop
        host ID via the config module and opens a dedicated SSH connection to
        it. Each target host gets its own tunnel connection (not shared with
        the hop's own connections).

        For multi-hop chains the transport holds a reference to the parent
        transport so that ``close()`` cascades down the chain.

        Cycle detection prevents infinite loops (e.g. A hops through B, B hops through A).
        """
        from ..configmodule import get_host
        from asyncssh import connect as _ssh_connect
        from .transport import SshHopTransport

        hop_id = self.hop
        host_name = self.name

        async def _create_tunnel(
            _visited: set[str] | None = None,
        ) -> 'SSHClientConnection':
            visited = _visited or set()
            if hop_id in visited:
                raise ValueError(f"Circular hop detected: {hop_id!r} already in chain {visited}")
            visited.add(hop_id)

            hop_host = get_host(hop_id)

            # If the hop itself has a hop, recursively build the tunnel chain.
            # We call the inner factory directly (not via SshHopTransport) so
            # that the _visited set propagates for cycle detection.
            parent_tunnel = None
            if hop_host.hop:
                parent_factory = hop_host._build_hop_transport()._factory
                parent_tunnel = await parent_factory(_visited=visited)

            user, password = next(iter(hop_host.creds.items())) if hop_host.user is None else (hop_host.user, hop_host.creds[hop_host.user])
            logger.debug(f"Opening SSH tunnel through {hop_id} for {host_name}")
            conn = await _ssh_connect(
                hop_host.ip,
                username=user,
                password=password,
                known_hosts=None,
                tunnel=parent_tunnel,
            )
            return conn

        return SshHopTransport(_create_tunnel)

    def set_term_type(self,
        term: Any,
    ):
        if is_literal(term, TermType): # type: ignore
            self.term = term
            self._connections.term = term

    def set_transfer_type(self,
        transfer: Any,
    ):
        if is_literal(transfer, FileTransferType): # type: ignore
            self.transfer = transfer
            self._file_transfer.transfer = transfer

    def _get_local_ip(self) -> str:
        """Return the local IP address used to reach this host, via OS routing lookup.

        Opens an unconnected UDP socket and uses the OS routing table to determine
        which local interface would be used to reach ``self.ip``. No packets are sent.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.ip, 80))
            return s.getsockname()[0]

    async def verify_connection(self) -> CommandStatus:
        """Attempt to connect without running any commands. Used by dry-run mode."""
        try:
            if self.term == 'ssh':
                await self._connections.ssh()
            else:
                await self._connections.telnet()

            if self.transfer == 'ftp':
                await self._connections.ftp()

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
            # A loop is running (we're inside an async context) — schedule cleanup on it
            loop.create_task(self.close())
        except RuntimeError:
            # No running loop — create one
            try:
                asyncio.run(self.close())
            except (RuntimeError, TypeError):
                pass  # Loop is closed or mocks can't be awaited; OS will clean up

    ####################
    #  Command execution
    ####################

    # TODO: Make sync versions of cmd and file methods that just wraps the async def

    async def _interact(self) -> None:
        """Open an interactive shell on this host, bridged to the local terminal.

        Dispatches on ``self.term``:

        - **ssh**: reuses the cached ``SSHClientConnection`` (asyncssh
          multiplexes channels, so opening a PTY-backed process on an
          existing connection is cheap). Works transparently through
          configured hops because the connection is already tunneled.
        - **telnet**: builds a *dedicated* :class:`TelnetClient` for
          this session with ``auto_window_resize=True`` and opens it in
          ``interactive=True`` mode so the remote shell echoes the
          user's keystrokes back (the normal connect path sends
          ``DONT ECHO`` to silence echoes for non-interactive capture).
          The cached telnet client, if any, is not reused — it may
          already be in non-echo mode. Hop tunnels are honored via the
          same port-forward helper the regular telnet path uses.

        See :mod:`otto.host.interact` for the bridge details.
        """
        if self.term == 'ssh':
            conn = await self._connections.ssh()
            await run_ssh_login(conn=conn, host_name=self.name)
            return

        user, password = self._connections.credentials
        interactive_options = replace(self.telnet_options, auto_window_resize=True)
        remote_port = interactive_options.port
        if self._connections.has_tunnel:
            local_port = await self._connections._forward_port(remote_port)
            connect_host = 'localhost'
            connect_port: Optional[int] = local_port
        else:
            connect_host = self._connections.ip
            connect_port = None  # TelnetClient will use options.port

        client = TelnetClient(
            host=connect_host,
            user=user,
            password=password,
            options=interactive_options,
            connect_port=connect_port,
            prompt=None,
        )
        try:
            await client.connect(interactive=True)
            await run_telnet_login(client=client, host_name=self.name)
        finally:
            await client.close()

    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
    ) -> CommandStatus:
        """Execute a single command on the remote host via the **persistent shell session**.

        Called by :meth:`run` for both the single-string and list forms. The session
        is stateful: working directory changes (``cd``), exported environment variables,
        and other shell state persist between calls, just as they would in an
        interactive terminal.

        Limitations:
            - **Sequential only.** The session is a single shell — calling ``run()``
              concurrently from multiple coroutines will corrupt the session output.
              Use :meth:`oneshot` instead when you need concurrent execution.
            - **Stateful.** Commands affect each other; a ``cd`` in one call changes
              the directory for the next.

        Args:
            cmd: Shell command to run. Passed to the remote shell as-is.
            expects: Optional list of ``(pattern, response)`` tuples for interactive
                prompts (e.g. sudo password, confirmation dialogs). Each pattern is
                matched against output as it arrives; the corresponding response is
                sent automatically.
            timeout: Seconds before the command is considered hung. On expiry,
                Ctrl+C is sent and ``Status.Error`` is returned. ``None`` disables
                the timeout (use for long-running commands).

        Returns:
            ``CommandStatus`` with the command, captured output, ``Status`` enum, and
            exit code. Exit code 0 → ``Status.Success``; non-zero → ``Status.Failed``.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout)

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Run a single command concurrent-safely, independent of the persistent shell.

        Unlike :meth:`run`, this method is **concurrent-safe**: multiple
        ``oneshot()`` calls can run simultaneously via ``asyncio.gather()`` or
        ``asyncio.create_task()`` without corrupting each other or the
        persistent shell session.

        Key differences from ``run()``:

        +------------------+------------------------------+----------------------------+
        | Property         | ``run()``                    | ``oneshot()``              |
        +==================+==============================+============================+
        | Shell state      | Persistent (cd, env persist) | Stateless (fresh each call)|
        | Concurrency      | Sequential only              | Safe for asyncio.gather()  |
        | Expect support   | Yes                          | No                         |
        | Connection cost  | Reuses existing session      | Reuses cached oneshot pool |
        | Best for         | Multi-step workflows, state  | One-off / parallel cmds    |
        +------------------+------------------------------+----------------------------+

        Implementation details:

        - **SSH**: runs via ``SSHClientConnection.create_process()`` — a
          lightweight exec channel on the existing TCP connection.  No new
          TCP handshake or authentication needed.
        - **Telnet**: telnet has no stateless exec primitive, so otto keeps
          a free-list pool of dedicated internal shell sessions.  Serial
          callers reuse one session (one TCP+auth handshake amortized over
          all calls); concurrent callers each pull their own session off
          the free-list, opening a new one if none are free.  This preserves
          the independence guarantee while avoiding the 1–2 s handshake on
          every call.

        Args:
            cmd: Shell command to run. Shell operators (``<``, ``>``, ``|``) work on
                SSH because asyncssh wraps the command in a shell; on telnet the
                command runs through the login shell of the new session.
            timeout: Seconds before the command is considered hung. ``None`` (the
                default) disables the timeout — appropriate for long-running commands
                such as a netcat listener waiting for a connection.

        Returns:
            ``CommandStatus`` with the command, captured output, ``Status`` enum, and
            exit code.

        See Also:
            :meth:`run`: stateful, sequential alternative with expect support.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        return await self._session_mgr.oneshot(cmd, timeout=timeout)

    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session.

        Unlike :meth:`run`, which uses a single default session, this method
        creates an additional named session that can run commands concurrently
        with the default session (or other named sessions).

        The session is established eagerly — any connection errors surface here.
        Call :meth:`HostSession.close` when done, or use the async context
        manager protocol::

            async with (await host.open_session("monitor")) as mon:
                result = await mon.run("stat /tmp/file.bin")

        Args:
            name: Identifier for this session. Reusing an existing name returns
                the existing session if it is still alive, or replaces it if dead.

        Returns:
            A :class:`HostSession` proxy exposing ``run``, ``send``,
            ``expect``, and ``close``.

        See Also:
            :meth:`oneshot`: stateless one-shot alternative.
            :meth:`run`: default persistent session.
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
            self._log_command(f"[DRY RUN] expect() skipped — pattern would never match without a live connection")
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
        """Transfer files from remote host to the local machine."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if isDryRun():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        with SuppressCommandOutput(host=cast(Host, self)):
            return await self._file_transfer.get_files(src_files, dest_dir, show_progress)

    # TODO: Look into a way to batch a single list of files that goes to different hosts
    # The main use case is lists of products or tools. These are the same binaries, and
    # go to multiple hosts. It would be most efficient if they could all be done in a
    # single asyncio.gather() rather than multiple.
    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        """Transfer files from local machine to remote host."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if isDryRun():
            return self._dry_run_transfer("PUT", src_files, dest_dir)
        with SuppressCommandOutput(host=cast(Host, self)):
            return await self._file_transfer.put_files(src_files, dest_dir, show_progress)

    ####################
    #  Naming
    ####################

    def _generateName(self):

        if not self.board:
            return f"{self.ne}{self._neIdStr}"

        return f"{self.ne}{self._neIdStr} {self.board}{self._slotStr}"

    def _generateId(self):

        neStr = f"{self.ne.lower()}{self._neIdStr}"

        if self.board is None:
            return neStr

        return f"{neStr}_{self.board.lower()}{self._slotStr}"

    @property
    def _neIdStr(self):

        if self.neId is None:
            return ''

        return f"{self.ne}"

    @property
    def _slotStr(self):

        if self.slot is None:
            return ''

        return f"{self.slot}"

