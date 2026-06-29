"""
Unix host class.

Unix hosts (Linux being the concrete kernel today; macOS/BSD trivially
compatible) accessed over the network via SSH or Telnet, with bash as the
remote shell. Manages two responsibilities:

- Command execution

  - SSH (via asyncssh)
  - telnet (via telnetlib3)

- File transfers

  - SCP (via asyncssh)
  - SFTP (via asyncssh)
  - FTP (via aioftp)
  - netcat (via commands on the client and host)

All kinds of host connections, for command execution and for file transfers, should be able to
establish a connection first, keep it open, and then use it for multiple commands and transfers.
Host connections can be explicitly closed by calling the `.close()` method (or via ``async with``),
and any still-open connections are closed automatically when the host's context scope exits.

The `.run()` method runs a single command (str) or a list of commands. Depending on the `.term`
value the correct connection type (ssh or telnet) is used without being specified as an argument.

The `.put()` and `.get()` methods both take a single file or a list of files. Depending on the
`.transfer` value the correct connection type (scp, sftp, ftp, or netcat) is used without being
specified as an argument.

History: this class was originally named ``RemoteHost``. With the introduction of
:class:`~otto.host.embedded_host.EmbeddedHost` for bare-metal/RTOS targets, ``RemoteHost`` is
now an abstract base for any network-reached host and the bash-on-SSH/Telnet concrete class lives
here as ``UnixHost``.
"""

# TODO: Consider having a single function that takes a connection, and does the lower level asyncio stuff  # noqa: E501 — TODO comment
# For example, run could dynamically dispatch to _runSshCmds(), which would pass along the _ssh_conn member  # noqa: E501 — TODO comment
# Then the _ssh_conn would be the connection used in an "async with" block to issue the command.
# Main problem here is that eash library uses its own method names to run commands and put/get files
# Possibly make the homegrown TelnetClient class mirror asyncssh? that could really help with design symmetry.  # noqa: E501 — TODO comment
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
    Annotated,
    cast,
)

from typing_extensions import override

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

from ..utils import (
    Arg,
    CommandStatus,
    Exclude,
    Opt,
    Status,
    cli_exposed,
)
from .capability import TERM_RESOLVER, TRANSFER_RESOLVER
from .command_frame import CommandFrame, build_command_frame
from .connections import (
    ConnectionManager,
    TermContext,
    build_term_backend,
)
from .file_ops import PosixFileOps
from .host import (
    Host,
    SuppressCommandOutput,
    is_dry_run,
)
from .interact import run_ssh_login, run_telnet_login
from .options import (
    FtpOptions,
    NcOptions,
    ScpOptions,
    SftpOptions,
    SnmpOptions,
    SshOptions,
    TelnetOptions,
)
from .power import PowerController, power_control_from_spec
from .privilege import PosixPrivilege
from .product import Product
from .remote_host import OsType, RemoteHost
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    SessionManager,
)
from .telnet import TelnetClient
from .toolchain import Toolchain
from .transfer import (
    TransferContext,
    UnixFileTransfer,
    build_transfer_backend,
)


@dataclass(slots=True)
class UnixHost(PosixPrivilege, PosixFileOps, RemoteHost):
    """Unix host accessed via SSH or Telnet, with bash as the remote shell."""

    ip: str
    """IP address of the host."""

    creds: dict[str, str]
    """Users and their respective passwords for this host."""

    element: str = field(repr=False)
    """Network element to which this host belongs."""

    os_type: OsType = "unix"
    """Default profile selector for a bare :class:`UnixHost`. A custom
    unix-based profile (e.g. ``ubuntu-22.04``) records its own name here."""

    os_name: str | None = "Linux"
    """Kernel/OS name. Defaults to ``Linux`` (the concrete Unix kernel today)."""

    os_version: str | None = None
    """OS/kernel version string, or None if unspecified."""

    name: str = None  # type: ignore
    """Human readable name to represent the host. Automatically generated if not provided."""

    user: str | None = None
    """User with which to log in. If not provided, the first user in the `creds` dict will be used."""  # noqa: E501 — long field docstring

    element_id: int | None = field(default=None, repr=False)
    """Network element identifier to which this host belongs.
    None indicates there are no other NEs of this type and a number is not needed."""

    board: str | None = field(default=None, repr=False)
    """Name of the board type to which this host belongs."""

    slot: int | None = field(default=None, repr=False)
    """Phyiscal slot number of the board to which this host belongs."""

    hw_version: str | None = None
    """Hardware version description."""

    sw_version: str | None = None
    """Software version description."""

    term: str = "ssh"
    """Protocol used to issue terminal commands."""

    is_virtual: bool = False
    """Determines whether a host is a VM or not."""

    docker_capable: bool = False
    """Whether this host can run Docker containers (i.e., has a docker daemon
    and the configured user can talk to it). Containers declared by projects
    are scheduled onto docker-capable hosts; non-capable hosts are skipped."""

    transfer: str = "scp"
    """Protocol used to transfer files."""

    valid_terms: list[str] = field(default_factory=lambda: ["ssh", "telnet"])
    """Closed menu of term backends this host supports (active is ``term``)."""

    valid_transfers: list[str] = field(default_factory=lambda: ["scp", "sftp", "ftp", "nc"])
    """Closed menu of transfer backends this host supports (active is ``transfer``)."""

    default_dest_dir: Path = field(default_factory=Path)
    """Default landing directory for ``put`` / ``get`` when the caller
    supplies an empty or relative ``dest_dir``. Defaults to ``Path()``,
    which preserves the existing behavior — SCP/SFTP resolve a relative
    destination against the SSH user's home directory. Override per-host
    to land transfers in a fixed location regardless of the caller's
    argument. See :attr:`~otto.host.remote_host.RemoteHost.default_dest_dir`."""

    max_filename_len: int = 255
    """Upper bound on the basename length (including extension) accepted by
    the target's filesystem. Defaults to ``255`` — the Linux ``NAME_MAX``,
    also the cap for ext4 / XFS / Btrfs / NTFS. Lower it for hosts on a
    tighter filesystem; see :attr:`~otto.host.remote_host.RemoteHost.max_filename_len` for details.
    Over-limit names are rejected by :meth:`put` / :meth:`get` with a
    self-explaining error instead of an opaque ``File name too long``
    midway through the transfer."""

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

    command_frame: CommandFrame | None = None
    """Shell-framing dialect for this host's bash console. ``None`` (the
    default) lets the :class:`~otto.host.session.SessionManager` use its
    built-in :class:`~otto.host.command_frame.BashFrame`, preserving the
    historical behavior exactly. Lab data may name a registered frame by string
    (resolved in ``__post_init__``); a profile or subclass may supply an
    instance. Promoted to a common field in Phase A so any host can declare its
    dialect — see :attr:`~otto.host.embedded_host.EmbeddedHost.command_frame`."""

    snmp: SnmpOptions | None = field(default=None, repr=False)
    """Optional SNMP polling config (lab ``snmp`` block). When set, otto's
    monitor collects this host's metrics over SNMP instead of running shell
    commands. SNMP monitoring is not embedded-only — a Unix host may use it to
    poll a real SNMP agent. See :class:`~otto.host.options.SnmpOptions`."""

    hop: str | None = None
    """Host ID of the intermediate hop used to reach this host, or None for direct connection."""

    resources: set[str] = field(default_factory=set[str])
    """Names of resources required to use this host."""

    interfaces: dict[str, str] = field(default_factory=dict, repr=False)
    """Named secondary interface addresses
    (see :attr:`~otto.host.remote_host.RemoteHost.interfaces`).
    Resolve with :meth:`~otto.host.remote_host.RemoteHost.address_for`."""

    products: list["Product"] = field(default_factory=list)
    """Software-under-test deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.products`."""

    power_control: "PowerController | None" = None
    """Pluggable power backend. Lab data declares it by string (a config-free
    controller type) or a ``[power]`` table (``{type, on_cmd, off_cmd, ...}``);
    ``__post_init__`` coerces it to an instance. None → power()/reboot(hard=True)
    fail loud. See :attr:`~otto.host.host.BaseHost.power_control`."""

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

    _lab: "Lab | None" = field(default=None, compare=False, repr=False, kw_only=True)
    """Back-reference to the owning Lab, wired by Lab.add_host. Lets hop
    resolution use self._lab.hosts[...] instead of ambient state."""

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

    _file_transfer: UnixFileTransfer = field(init=False, repr=False)
    """Handles all file transfer protocols for this host."""

    ####################
    #  Privilege
    ####################

    @override
    def _sudo_password(self) -> str | None:
        """Return the login user's password, used for ``sudo -S``."""
        _user, password = self._connections.credentials
        return password or None

    @override
    def _user_password(self, user: str) -> str | None:
        """Password for ``su <user>`` from this host's creds, if present."""
        return self.creds.get(user)

    def __post_init__(self) -> None:

        self.id = self._generate_id()
        if self.name is None:
            self.name = self._generate_name()

        # Lab JSON serializes ``default_dest_dir`` as a string; coerce so
        # ``_resolve_dest`` can use Path arithmetic uniformly.
        if not isinstance(self.default_dest_dir, Path):
            self.default_dest_dir = Path(self.default_dest_dir)

        # Lab JSON declares the frame dialect by name; coerce a string to the
        # registered instance. None is left as-is (SessionManager applies bash).
        if isinstance(self.command_frame, str):
            self.command_frame = build_command_frame(self.command_frame)

        self.power_control = power_control_from_spec(self.power_control)

        TERM_RESOLVER.validate_choice(self.valid_terms, self.term)
        TRANSFER_RESOLVER.validate_choice(self.valid_transfers, self.transfer)

        self._connections = self._build_connections()
        self._repeater = RepeatRunner(run_cmds=self.run)
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            user_password=self._user_password,
        )
        self._file_transfer = self._build_file_transfer()

    @property
    def _creds(self) -> tuple[str, str]:
        """Provide the (username, password) pair from creds. Delegates to ConnectionManager."""
        return self._connections.credentials

    ####################
    #  Connection
    ####################

    def rebuild_connections(self) -> None:
        """Recreate the ConnectionManager and dependents.

        Useful after changing ``hop`` or when the host must reconnect on a
        new event loop (e.g. after ``pytest.main()`` returns and coverage
        collection starts in a fresh ``asyncio.run()``).
        """
        self._connections = self._build_connections()
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            user_password=self._user_password,
        )
        self._file_transfer = self._build_file_transfer()

    def _build_connections(self) -> ConnectionManager:
        """Construct the connection backend for the current ``term`` via the registry seam.

        Honors the ``_connection_factory`` test override. Shared by ``__post_init__`` /
        ``rebuild_connections`` (and the override-copy seam, via ``dataclasses.replace``) so a
        custom term backend builds the right class.
        """
        hop_transport = self._build_hop_transport() if self.hop else None
        term_ctx = TermContext(
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
        conn_cls = self._connection_factory or build_term_backend(self.term)
        return conn_cls.create(term_ctx)

    def _build_file_transfer(self) -> UnixFileTransfer:
        """Construct the transfer backend for the current ``transfer`` via the registry seam.

        Uses ``self._connections``. Shared by ``__post_init__`` / ``rebuild_connections``
        (and the override-copy seam, via ``dataclasses.replace``) so a custom
        transfer backend builds the right class.
        """
        return cast(
            "UnixFileTransfer",
            build_transfer_backend(self.transfer).create(
                TransferContext(
                    transfer=self.transfer,
                    host_name=self.name,
                    connections=self._connections,
                    nc_options=self.nc_options,
                    scp_options=self.scp_options,
                    get_local_ip=lambda: self._get_local_ip(),  # noqa: PLW0108 — late-bind self for monkeypatching
                    exec_cmd=lambda *a, **kw: self.oneshot(*a, **kw),  # noqa: PLW0108 — late-bind self for monkeypatching
                    max_filename_len=self.max_filename_len,
                )
            ),
        )

    def _get_local_ip(self) -> str:
        """Return the local IP address used to reach this host, via OS routing lookup.

        Opens an unconnected UDP socket and uses the OS routing table to determine
        which local interface would be used to reach ``self.ip``. No packets are sent.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.ip, 80))
            return s.getsockname()[0]

    @override
    async def verify_connection(self) -> CommandStatus:
        """Attempt to connect without running any commands. Used by dry-run mode."""
        try:
            if self.term == "ssh":
                await self._connections.ssh()
            else:
                await self._connections.telnet()

            if self.transfer == "ftp":
                await self._connections.ftp()

            self._log_command("[DRY RUN] Connection verified")
            return CommandStatus(
                command="connect", output="Connection successful", status=Status.Success, retcode=0
            )
        except Exception as e:  # noqa: BLE001 — verify_connection probes all failure modes
            self._log_command(f"[DRY RUN] Connection FAILED: {e}")
            return CommandStatus(command="connect", output=str(e), status=Status.Error, retcode=1)

    @override
    async def close(self) -> None:

        await self._repeater.stop_all()
        await self._session_mgr.close_all()
        await self._connections.close()

    ####################
    #  Command execution
    ####################

    # TODO: Make sync versions of cmd and file methods that just wraps the async def

    @override
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
        if self.term == "ssh":
            conn = await self._connections.ssh()
            await run_ssh_login(conn=conn, host_name=self.name)
            return

        user, password = self._connections.credentials
        interactive_options = replace(self.telnet_options, auto_window_resize=True)
        remote_port = interactive_options.port
        if self._connections.has_tunnel:
            local_port = await self._connections._forward_port(remote_port)  # noqa: SLF001 — intra-package access to HostConnections._forward_port for tunnel setup
            connect_host = "localhost"
            connect_port: int | None = local_port
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

    @override
    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: bool = True,
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
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout, log=log)

    @override
    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        """Run a single command concurrent-safely, independent of the persistent shell.

        Unlike :meth:`~otto.host.host.BaseHost.run`, this method is **concurrent-safe**: multiple
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
          the independence guarantee while avoiding the 1-2 s handshake on
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
            :meth:`~otto.host.host.BaseHost.run`: stateful, sequential alternative
            with expect support.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._session_mgr.oneshot(cmd, timeout=timeout, log=log)

    @override
    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session.

        Unlike :meth:`~otto.host.host.BaseHost.run`, which uses a single default session,
        this method
        creates an additional named session that can run commands concurrently
        with the default session (or other named sessions).

        The session is established eagerly — any connection errors surface here.
        Call :meth:`~otto.host.session.HostSession.close` when done, or use the async context
        manager protocol::

            async with await host.open_session("monitor") as mon:
                result = await mon.run("stat /tmp/file.bin")

        Args:
            name: Identifier for this session. Reusing an existing name returns
                the existing session if it is still alive, or replaces it if dead.

        Returns:
            A :class:`~otto.host.session.HostSession` proxy exposing ``run``, ``send``,
            ``expect``, and ``close``.

        See Also:
            :meth:`~otto.host.unix_host.UnixHost.oneshot`: stateless one-shot alternative.
            :meth:`~otto.host.host.BaseHost.run`: default persistent session.
        """
        if is_dry_run():
            self._log_command(f"[DRY RUN] open_session({name!r})")
        return await self._session_mgr.open_session(name)

    @override
    async def send(self, text: str, log: bool = True) -> None:
        """Send raw text to the host's persistent session."""
        if is_dry_run():
            if log:
                self._log_command(f"[DRY RUN] send({text!r})")
            return
        await self._session_mgr.send(text, log=log)

    @override
    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        if is_dry_run():
            self._log_command(
                "[DRY RUN] expect() skipped — pattern would never match without a live connection"
            )
            return ""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  File transfer
    ####################

    @override
    @cli_exposed(success="Download complete.")
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(variadic=True, elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> tuple[Status, str]:
        """Transfer files from remote host to the local machine."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.get_files(src_files, dest_dir, show_progress)

    # TODO: Look into a way to batch a single list of files that goes to different hosts
    # The main use case is lists of products or tools. These are the same binaries, and
    # go to multiple hosts. It would be most efficient if they could all be done in a
    # single asyncio.gather() rather than multiple.
    @override
    @cli_exposed(success="Transfer complete.")
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> tuple[Status, str]:
        """Transfer files from local machine to remote host."""
        if not isinstance(src_files, list):
            src_files = [src_files]
        dest_dir = self._resolve_dest(dest_dir)
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.put_files(src_files, dest_dir, show_progress)

    ####################
    #  Kernel modules
    ####################

    @cli_exposed
    async def lsmod(self) -> list[str]:
        """List the kernel modules currently loaded on the host."""
        return await self._loaded_modules()

    async def _loaded_modules(self) -> list[str]:
        """Return loaded module names, read from ``/proc/modules`` — the source ``lsmod`` formats.

        World-readable (no sudo), no ``lsmod`` binary dependency; column
        one is the module name, already ``-``→``_`` normalized by the kernel.
        Returns ``[]`` under dry-run (the live module set is unknowable, and the
        skipped read would otherwise echo the dry-run banner). ``log=False``
        keeps the (potentially long) module dump out of the console/log.
        """
        if is_dry_run():
            return []
        result = await self.oneshot("cat /proc/modules", log=False)
        if not result.status.is_ok:
            return []
        return [line.split()[0] for line in result.output.splitlines() if line.strip()]

    @cli_exposed(success="Module loaded.")
    async def load(
        self,
        file: Annotated[Path, Arg(help="Kernel module .ko to insert.")],
        name: Annotated[str | None, Opt(help="Module name; defaults to the file stem.")] = None,
        dest_dir: Annotated[Path, Exclude] = Path("/tmp"),  # noqa: S108 — deliberate staging path
        show_progress: Annotated[bool, Exclude] = False,
    ) -> tuple[Status, str]:
        """Insert a kernel module: stage the .ko to the host, then ``insmod`` it.

        ``put`` lands the .ko on the target (as the login/transfer user); the
        ``insmod`` runs in the shell session — under ``sudo`` unless the session
        is already root (Spec A's ``current_user``). The staged file is removed
        afterward (the module lives in kernel memory once inserted). ``name``
        defaults to the file stem (``-``→``_``) and is used in error text.
        """
        resolved = (name or file.stem).replace("-", "_")
        dest = dest_dir / file.name
        status, put_msg = await self.put(file, dest_dir, show_progress=show_progress)
        if not status.is_ok:
            return status, f"staging {file} failed: {put_msg}"
        need_sudo = self.current_user != "root"
        result = await self.run(f"insmod {self._q(dest)}", sudo=need_sudo)
        await self.rm(dest, force=True)  # best-effort cleanup
        if result.status.is_ok:
            return Status.Success, ""
        return Status.Error, f"insmod {resolved} failed: {result.only.output.strip()}"

    @cli_exposed(success="Module unloaded.")
    async def unload(
        self,
        name: Annotated[str, Arg(help="Module name to remove.")],
    ) -> tuple[Status, str]:
        """Remove a kernel module (``rmmod``).

        Idempotent: removing a module that is not resident succeeds without running ``rmmod``
        (mirrors :meth:`~otto.host.embedded_host.EmbeddedHost.unload`).
        """
        resolved = name.replace("-", "_")
        if not is_dry_run() and resolved not in await self._loaded_modules():
            return Status.Success, ""
        need_sudo = self.current_user != "root"
        result = await self.run(f"rmmod {self._q(resolved)}", sudo=need_sudo)
        if result.status.is_ok:
            return Status.Success, ""
        return Status.Error, f"rmmod {resolved} failed: {result.only.output.strip()}"

    ####################
    #  Power / reboot
    ####################

    @override
    async def _soft_reboot(self) -> tuple[Status, str]:
        await self.run("reboot", sudo=True, timeout=10.0)
        return Status.Success, ""

    @override
    @cli_exposed
    async def shutdown(self) -> tuple[Status, str]:
        await self.run("shutdown -h now", sudo=True, timeout=10.0)
        return Status.Success, ""
