"""
Connection option classes for the network protocols otto speaks.

Each protocol (SSH, Telnet, SFTP, SCP, FTP, netcat) has an ``*Options``
dataclass that holds tunable connection parameters. Default-constructed
instances reproduce otto's pre-options behavior exactly, so callers who
do not care about configuration can ignore this module.

For SSH, curated fields cover the common knobs and an ``extra`` dict
plus a ``post_connect`` hook together forward the full power of asyncssh
(kwargs *and* post-connect method calls like port forwarding).

Example::

    host = UnixHost(
        ip="10.0.0.1",
        creds={"admin": "secret"},
        element="lab",
        ssh_options=SshOptions(port=2222, connect_timeout=5),
        telnet_options=TelnetOptions(auto_window_resize=True),
    )
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass as pydantic_dataclass

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from .transfer import NcListenerCheck, NcPortStrategy


_FORWARD_CONFIG = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


@pydantic_dataclass(frozen=True, config=_FORWARD_CONFIG)
class LocalPortForward:
    """An SSH local port forward: listen locally, send to host:port via the remote."""

    listen_host: str
    listen_port: int
    dest_host: str
    dest_port: int


@pydantic_dataclass(frozen=True, config=_FORWARD_CONFIG)
class RemotePortForward:
    """An SSH remote port forward: listen on the remote, send to host:port locally."""

    listen_host: str
    listen_port: int
    dest_host: str
    dest_port: int


@pydantic_dataclass(frozen=True, config=_FORWARD_CONFIG)
class SocksForward:
    """A dynamic SOCKS forward listening on the given local address."""

    listen_host: str
    listen_port: int


@dataclass(slots=True)
class SshOptions:
    """Connection options for asyncssh-backed SSH sessions.

    Covers the common connection knobs as curated fields plus two escape
    hatches (``extra`` and ``post_connect``) that together give access to
    every asyncssh feature.

    Default-constructed ``SshOptions()`` reproduces otto's historical
    behavior: port 22 with host-key verification disabled.
    """

    port: int = 22
    """TCP port for the SSH connection."""

    known_hosts: Any = None
    """asyncssh known_hosts. ``None`` disables host-key verification
    (otto's historical default). Pass a path, a list of keys, or the
    string ``'~/.ssh/known_hosts'`` to enable checking."""

    connect_timeout: float | None = None
    """Seconds to wait for the TCP + SSH handshake. ``None`` = asyncssh default."""

    keepalive_interval: float | None = None
    """Seconds between SSH-level keepalives. ``None`` = asyncssh default (disabled)."""

    keepalive_count_max: int | None = None
    """Missed keepalives before asyncssh closes the connection. ``None`` = default."""

    client_keys: list[str] | None = None
    """Private-key paths for public-key auth. ``None`` lets asyncssh auto-discover."""

    client_host_keys: list[str] | None = None
    """Host-key paths for host-based auth."""

    agent_forwarding: bool = False
    """Forward the local SSH agent to the remote side."""

    preferred_auth: str | list[str] | None = None
    """Authentication methods to attempt, in order (e.g. ``'publickey,password'``)."""

    encryption_algs: list[str] | None = None
    """Allowed symmetric ciphers."""

    server_host_key_algs: list[str] | None = None
    """Allowed server host-key algorithms."""

    compression_algs: list[str] | None = None
    """Allowed compression algorithms."""

    local_forwards: list[LocalPortForward] = field(default_factory=list)
    """Local port forwards to set up after the connection is established."""

    remote_forwards: list[RemotePortForward] = field(default_factory=list)
    """Remote port forwards to set up after the connection is established."""

    socks_forwards: list[SocksForward] = field(default_factory=list)
    """Dynamic SOCKS forwards to set up after the connection is established."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Arbitrary extra kwargs forwarded directly to ``asyncssh.connect()``.

    Anything kwarg-shaped on asyncssh's connect (``config``, ``proxy_command``,
    ``x509_trusted_certs``, ``gss_host``, etc.) can be set here. Values in
    ``extra`` override curated fields on conflict."""

    post_connect: Callable[["SSHClientConnection"], Awaitable[None]] | None = None
    """Optional async hook called with the freshly opened connection,
    after the structured forward lists have been applied. Use for anything
    not expressible as a kwarg or a structured forward — e.g. UNIX-socket
    forwards, X11, custom subsystems."""

    def _kwargs(self) -> dict[str, Any]:
        """Build the kwargs dict passed to ``asyncssh.connect()``.

        Includes only fields the caller explicitly set, so asyncssh's own
        defaults apply for everything we did not override. ``known_hosts``
        is always included (default ``None``) since that is the historical
        otto behavior callers rely on.
        """
        kw: dict[str, Any] = {
            "port": self.port,
            "known_hosts": self.known_hosts,
        }
        if self.connect_timeout is not None:
            kw["connect_timeout"] = self.connect_timeout
        if self.keepalive_interval is not None:
            kw["keepalive_interval"] = self.keepalive_interval
        if self.keepalive_count_max is not None:
            kw["keepalive_count_max"] = self.keepalive_count_max
        if self.client_keys is not None:
            kw["client_keys"] = self.client_keys
        if self.client_host_keys is not None:
            kw["client_host_keys"] = self.client_host_keys
        if self.agent_forwarding:
            kw["agent_forwarding"] = True
        if self.preferred_auth is not None:
            kw["preferred_auth"] = self.preferred_auth
        if self.encryption_algs is not None:
            kw["encryption_algs"] = self.encryption_algs
        if self.server_host_key_algs is not None:
            kw["server_host_key_algs"] = self.server_host_key_algs
        if self.compression_algs is not None:
            kw["compression_algs"] = self.compression_algs
        kw.update(self.extra)
        return kw

    async def _apply_post_connect(self, conn: "SSHClientConnection") -> None:
        """Apply structured forwards and run the ``post_connect`` hook."""
        for f in self.local_forwards:
            await conn.forward_local_port(f.listen_host, f.listen_port, f.dest_host, f.dest_port)
        for f in self.remote_forwards:
            await conn.forward_remote_port(f.listen_host, f.listen_port, f.dest_host, f.dest_port)
        for s in self.socks_forwards:
            await conn.forward_socks(s.listen_host, s.listen_port)
        if self.post_connect is not None:
            await self.post_connect(conn)


# ---------------------------------------------------------------------------
# Telnet
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TelnetOptions:
    """Connection options for telnetlib3-backed telnet sessions.

    Default-constructed ``TelnetOptions()`` reproduces otto's historical
    behavior: port 23, bytes mode, cols=400, and a 3-second ECHO
    negotiation timeout.
    """

    port: int = 23
    """TCP port for the telnet connection."""

    write_chunk_size: int = 0
    """Split each command write into chunks of at most this many bytes. ``0``
    (default) writes the whole payload in one call — correct for a
    host-terminated telnet shell (x86 + E1000). A positive value paces the
    write so a UART-backed RTOS shell behind a ``-serial telnet:`` bridge
    doesn't overrun its console RX FIFO on a multi-KB ``llext load_hex`` line."""

    write_chunk_delay: float = 0.0
    """Seconds to pause between chunked writes (see :attr:`write_chunk_size`).
    Ignored when ``write_chunk_size`` is 0."""

    cols: int = 400
    """Initial terminal width reported to the remote side. otto historically
    used 400 to avoid line-wrap artifacts in automation output."""

    rows: int = 24
    """Initial terminal height reported to the remote side."""

    encoding: str | bool = False
    """Text encoding. ``False`` = bytes mode (otto default)."""

    connect_timeout: float | None = None
    """Seconds to wait for the telnet TCP handshake. ``None`` = no timeout."""

    echo_negotiation_timeout: float = 3.0
    """Seconds to wait for the remote to honor ``DONT ECHO`` during connect."""

    login_prompt: bytes = b":"
    """Byte delimiter that terminates the login/password prompts. Anything
    ending in a colon matches ``login:``, ``Username:``, ``Password:``, etc."""

    login: bool = True
    """Whether ``connect()`` performs a telnet login (waits for the
    login/password prompts and sends credentials). Set ``False`` for a shell
    with no login step — e.g. a bare-metal/RTOS telnet shell — where waiting
    for a ``login:`` prompt that never arrives would hang the connection."""

    single_client_console: bool = False
    """When True, this connection targets a single-client console — an RTOS
    telnet shell that serves one client at a time (e.g. Zephyr ``shell_telnet``
    reached over a ``-serial telnet:`` bridge). The transport is registered in a
    process-local set so the embedded test teardown can force-release the slot if
    a timed-out test left it half-open (see
    :func:`otto.host.telnet.abort_console_transports`). Unix telnet (multi-session
    telnetd) leaves this False, so it is never registered or aborted."""

    auto_window_resize: bool = False
    """When True and stdin is a TTY, install a SIGWINCH handler that sends
    a NAWS update on every resize so remote TUIs reflow. Off by default;
    opt in for interactive telnet sessions."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Extra kwargs forwarded to ``telnetlib3.open_connection()``."""

    def _open_kwargs(self) -> dict[str, Any]:
        """Build the kwargs dict passed to ``telnetlib3.open_connection()``."""
        kw: dict[str, Any] = {
            "port": self.port,
            "encoding": self.encoding,
            "cols": self.cols,
            "rows": self.rows,
        }
        kw.update(self.extra)
        return kw


# ---------------------------------------------------------------------------
# SFTP
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SftpOptions:
    """Connection options for asyncssh-backed SFTP clients.

    SFTP rides on the underlying SSH connection, so connection-level
    tuning belongs in ``SshOptions``. These knobs configure the SFTP
    subsystem itself.
    """

    env: dict[str, str] | None = None
    """Environment variables to set in the remote SFTP process."""

    send_env: list[str] | None = None
    """Local env vars to forward to the remote SFTP process."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Extra kwargs forwarded to ``SSHClientConnection.start_sftp_client()``."""

    def _kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if self.env is not None:
            kw["env"] = self.env
        if self.send_env is not None:
            kw["send_env"] = self.send_env
        kw.update(self.extra)
        return kw


# ---------------------------------------------------------------------------
# SCP
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScpOptions:
    """Connection options for ``asyncssh.scp`` file transfers.

    Only protocol-level knobs live here; the SSH connection itself is
    configured via ``SshOptions``.
    """

    preserve: bool = False
    """Preserve mtime/atime/mode on transferred files."""

    recurse: bool = True
    """Recurse into directories."""

    block_size: int = 16384
    """Chunk size for SCP transfers. Larger = faster on fast links, more RAM."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Extra kwargs forwarded to ``asyncssh.scp()``."""

    def _kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "preserve": self.preserve,
            "recurse": self.recurse,
            "block_size": self.block_size,
        }
        kw.update(self.extra)
        return kw


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FtpOptions:
    """Connection options for aioftp-backed FTP clients.

    Default-constructed ``FtpOptions()`` reproduces otto's historical
    behavior: port 21, UTF-8 encoding, aioftp defaults for everything else.
    """

    port: int = 21
    """TCP port for the FTP control connection."""

    encoding: str = "utf-8"
    """Text encoding for FTP commands and paths."""

    socket_timeout: float | None = None
    """Socket-level read/write timeout."""

    connection_timeout: float | None = None
    """Handshake timeout."""

    path_timeout: float | None = None
    """Timeout for path-level operations (list/stat/etc.)."""

    read_speed_limit: int | None = None
    """Bytes/sec cap on downloads. ``None`` = unlimited."""

    write_speed_limit: int | None = None
    """Bytes/sec cap on uploads. ``None`` = unlimited."""

    ssl: Any = None
    """``ssl.SSLContext``, ``True``, or ``None``. Use an SSLContext for FTPS."""

    passive_commands: tuple[str, ...] = ("epsv", "pasv")
    """Passive-mode commands to attempt, in order."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Extra kwargs forwarded to ``aioftp.Client()``. aioftp also accepts
    arbitrary ``siosocks_asyncio_kwargs`` which can be routed through here."""

    def _client_kwargs(self) -> dict[str, Any]:
        """Build the kwargs dict passed to ``aioftp.Client()``.

        Only includes fields the caller explicitly set so aioftp's defaults apply.
        """
        kw: dict[str, Any] = {}
        if self.socket_timeout is not None:
            kw["socket_timeout"] = self.socket_timeout
        if self.connection_timeout is not None:
            kw["connection_timeout"] = self.connection_timeout
        if self.path_timeout is not None:
            kw["path_timeout"] = self.path_timeout
        if self.read_speed_limit is not None:
            kw["read_speed_limit"] = self.read_speed_limit
        if self.write_speed_limit is not None:
            kw["write_speed_limit"] = self.write_speed_limit
        if self.ssl is not None:
            kw["ssl"] = self.ssl
        if self.encoding != "utf-8":
            kw["encoding"] = self.encoding
        if self.passive_commands != ("epsv", "pasv"):
            kw["passive_commands"] = self.passive_commands
        kw.update(self.extra)
        return kw


# ---------------------------------------------------------------------------
# Netcat
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NcOptions:
    """Connection options for netcat-based file transfers.

    Bundles all nc-specific knobs that previously lived on ``UnixHost``
    into a single object so that ``NcOptions()`` (with defaults) produces
    the same behavior as the old individual fields.
    """

    exec_name: str = "nc"
    """Netcat executable on both sides (e.g. ``nc``, ``ncat``, ``netcat``).
    Listener syntax is assumed to be OpenBSD-style (``nc -l PORT``)."""

    port: int = 9000
    """Base port for netcat transfers. Used as the scan-start for the
    ss/netstat/python/proc port-finding strategies."""

    port_strategy: "NcPortStrategy" = "auto"
    """Strategy for finding free ports on the remote host. ``'auto'``
    probes ss → netstat → python → proc and caches the first that works."""

    port_cmd: str | None = None
    """Shell command that prints a free port to stdout. Only used when
    ``port_strategy == 'custom'``."""

    listener_check: "NcListenerCheck" = "auto"
    """Strategy for verifying that a remote ``nc`` listener is ready
    before sending data. ``'auto'`` probes ss → netstat → proc."""

    listener_cmd: str | None = None
    """Shell command (using ``{port}`` as placeholder) that exits 0 when
    a port is listening. Only used when ``listener_check == 'custom'``."""

    listener_timeout: float = 30.0
    """Seconds a remote ``nc -l`` listener may wait for a client before it
    self-terminates (passed as ``nc -w``), and the ceiling on the post-transfer
    wait for that listener to exit. Bounds the orphaned-listener hang: if a
    concurrent process wins a port-collision race, our sender's bytes land in
    *its* listener and ours never gets a client — without this it would block
    forever. A transfer whose connection is established stays unaffected; this
    only caps the wait for a client that never arrives."""


# ---------------------------------------------------------------------------
# TFTP
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TftpOptions:
    """Connection options for TFTP file transfer to an embedded host.

    **Reserved.** TFTP is the deferred ``tftp`` embedded-transfer backend — a
    faster alternative to console ``fs`` transfer for targets whose firmware
    has a TFTP client. The backend itself is not yet implemented
    (:class:`~otto.host.transfer.EmbeddedFileTransfer` raises
    :class:`NotImplementedError` for ``tftp``); this dataclass exists so lab
    data and the host API can name the option table now, without a later
    breaking change when the backend lands.
    """

    port: int = 69
    """UDP port of the TFTP server."""

    server_ip: str | None = None
    """IP address otto's TFTP server binds to and the target transfers
    against. ``None`` auto-detects the local IP, as the netcat path does."""

    block_size: int = 512
    """TFTP block size (RFC 2348 ``blksize`` option). 512 is the protocol
    default; larger blocks reduce round-trips on a reliable link."""

    timeout: float = 5.0
    """Per-block retransmit timeout, in seconds."""


@dataclass(slots=True)
class SnmpOptions:
    """Per-host SNMP polling config — the acquisition half of SNMP monitoring.

    Declared in lab data as a host's ``snmp`` block; carries only *what to poll*
    and *how to reach the agent*. Presentation for each OID (chart/unit/scale)
    lives in the monitor module's descriptor registry, never here. The monitor
    factory turns this into a live
    :class:`~otto.monitor.snmp.SnmpClient` + :class:`~otto.monitor.snmp.SnmpSource`.

    A host carrying an ``snmp`` block is monitored over SNMP instead of by
    running shell commands — the way otto reaches an embedded target's metrics
    without contending for its single shell session. It is not embedded-only; a
    Unix host may declare one too.
    """

    oids: tuple[str, ...] = ()
    """OIDs to GET each tick (e.g. ``("1.3.6.1.2.1.1.3.0", ...)``)."""

    community: str = "public"
    """SNMP v2c community string."""

    port: int = 161
    """UDP port of the agent (or of a relay standing in for it)."""

    version: str = "2c"
    """SNMP version — ``"2c"`` (default) or ``"1"``."""

    address: str | None = None
    """Address otto sends SNMP to. ``None`` (the default) means use the host's
    own ``ip``; set it when the agent is reached at a different address than the
    host's primary interface — e.g. a relay endpoint, or (future) a named
    interface from a per-host interface map. See ``todo/multi_interface_hosts.md``."""
