"""
Connection management for remote hosts.

ConnectionManager owns all raw transport connections (SSH, SFTP, FTP, Telnet)
for a single remote host. It provides lazy-connect coroutines that create the
connection on first call and reuse it thereafter.

When a ``HopTransport`` is provided (via the *hop* parameter), all
connections are routed through the hop's SSH tunnel:

- SSH connections use asyncssh's native ``tunnel`` parameter.
- Telnet connections use SSH local port forwarding to reach the target
  through the tunnel.
- SFTP piggybacks on the (already tunneled) SSH connection.
- FTP uses ``TunneledFtpClient``, which forwards the control port and
  dynamically forwards each PASV data port through the tunnel.
- Netcat transfers use ``forward_port`` to reach the remote ``nc``
  listener through the tunnel (both PUT and GET directions).

Inject a subclass via ``RemoteHost._connection_factory`` to replace the real
transport with a test double — no monkeypatching of library functions needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

import aioftp
import asyncssh
from asyncssh import SFTPClient, SSHClientConnection
from asyncssh import connect as ssh_connect

from ..logger import getOttoLogger
from .options import FtpOptions, SftpOptions, SshOptions, TelnetOptions
from .telnet import TelnetClient

if TYPE_CHECKING:
    from .transport import HopTransport

TermType = Literal['ssh', 'telnet']

logger = getOttoLogger()


class TunneledFtpClient(aioftp.Client):
    """aioftp Client that routes FTP data connections through an SSH hop.

    FTP passive mode announces dynamic data ports via PASV responses.
    This subclass intercepts each *data* connection attempt and creates
    a corresponding SSH port forward so the data flows through the
    tunnel alongside the control connection.

    The control connection (port 21) is already forwarded by
    ``ConnectionManager`` before ``connect()`` is called, so the tunnel
    override is only activated after the control connection is established.
    """

    def __init__(self, hop: HopTransport, dest_host: str, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._hop = hop
        self._dest_host = dest_host
        self._tunnel_data = False

    async def connect(self, host: str, port: int = aioftp.DEFAULT_PORT) -> list[str]:  # type: ignore[override]
        # Control connection is already forwarded — connect normally.
        info = await super().connect(host, port)
        # Enable tunnel override for subsequent PASV data connections.
        self._tunnel_data = True
        return info

    async def _open_connection(self, host, port):  # type: ignore[no-untyped-def, override]
        if not self._tunnel_data:
            return await super()._open_connection(host, port)
        local_port = await self._hop.forward_port(self._dest_host, port)
        return await super()._open_connection('localhost', local_port)


class ConnectionManager:
    """Owns all raw transport connections for a single remote host.

    Connections are created lazily and reused across calls. Call ``close()``
    to release all open connections.

    When a ``HopTransport`` is provided (via the *hop* parameter), an SSH
    tunnel to the hop host is established lazily on first use. All protocol
    connections are then routed through this tunnel rather than connecting
    directly to the target IP.

    Subclass and inject via ``RemoteHost._connection_factory`` to swap in test
    doubles without monkeypatching library functions::

        class FakeConnections(ConnectionManager):
            def __init__(self, ip, creds, user, term, name):
                self._ssh_conn = AsyncMock(spec=SSHClientConnection)
                self._sftp_conn = None
                self._ftp_conn = None
                self._telnet_conn = None

            async def ssh(self):
                return self._ssh_conn

        host = RemoteHost(..., _connection_factory=FakeConnections)
    """

    def __init__(
        self,
        ip: str,
        creds: dict[str, str],
        user: Optional[str],
        term: TermType,
        name: str,
        hop: HopTransport | None = None,
        ssh_options: SshOptions | None = None,
        telnet_options: TelnetOptions | None = None,
        sftp_options: SftpOptions | None = None,
        ftp_options: FtpOptions | None = None,
    ) -> None:
        self._ip = ip
        self._creds_dict = creds
        self._user = user
        self._term = term
        self._name = name
        self._hop = hop
        self._ssh_options = ssh_options or SshOptions()
        self._telnet_options = telnet_options or TelnetOptions()
        self._sftp_options = sftp_options or SftpOptions()
        self._ftp_options = ftp_options or FtpOptions()

        self._ssh_conn: SSHClientConnection | None = None
        self._sftp_conn: SFTPClient | None = None
        self._ftp_conn: aioftp.Client | None = None
        self._telnet_conn: TelnetClient | None = None

    @property
    def telnet_options(self) -> TelnetOptions:
        """Expose the stored ``TelnetOptions`` so callers that build their
        own ``TelnetClient`` (e.g. ``SessionManager.open_session``) can
        honor the same configuration."""
        return self._telnet_options

    @property
    def credentials(self) -> tuple[str, str]:
        """Return the active (username, password) pair."""
        if self._user is None:
            return next(iter(self._creds_dict.items()))
        return self._user, self._creds_dict[self._user]

    @property
    def ip(self) -> str:
        return self._ip

    @property
    def term(self) -> TermType:
        return self._term

    @term.setter
    def term(self, value: TermType) -> None:
        self._term = value

    @property
    def connected(self) -> bool:
        """Whether any raw connection is currently open."""
        return bool(
            self._ssh_conn
            or self._telnet_conn
            or self._sftp_conn
            or self._ftp_conn
        )

    @property
    def has_tunnel(self) -> bool:
        """Whether this connection manager is configured to use a tunnel."""
        return self._hop is not None

    async def _ensure_tunnel(self) -> SSHClientConnection:
        """Return the tunnel SSH connection, creating it via the hop transport if needed."""
        assert self._hop is not None
        logger.debug(f"Establishing SSH tunnel for {self._name}")
        tunnel = await self._hop.get_tunnel()
        logger.debug(f"SSH tunnel established for {self._name}")
        return tunnel

    async def _forward_port(self, dest_port: int) -> int:
        """Forward a local ephemeral port to ``self._ip:dest_port`` through the tunnel.

        Returns the local port number to connect to.
        """
        assert self._hop is not None
        local_port = await self._hop.forward_port(self._ip, dest_port)
        logger.debug(f"Forwarding localhost:{local_port} -> {self._ip}:{dest_port} for {self._name}")
        return local_port

    async def ssh(self) -> SSHClientConnection:
        """Return the live SSH connection, opening it if needed."""
        if self._ssh_conn is None:
            user, password = self.credentials
            logger.debug(f"Connecting to {self._name} via SSH")
            tunnel = None
            if self._hop is not None:
                tunnel = await self._ensure_tunnel()
            self._ssh_conn = await ssh_connect(
                self._ip,
                username=user,
                password=password,
                tunnel=tunnel,
                **self._ssh_options._kwargs(),
            )
            await self._ssh_options._apply_post_connect(self._ssh_conn)
            logger.debug(f"Connected to {self._name} via SSH")
        return self._ssh_conn

    async def sftp(self) -> SFTPClient:
        """Return the live SFTP client, opening it (and SSH if needed) first."""
        if self._sftp_conn is None:
            conn = await self.ssh()
            logger.debug(f"Starting SFTP client for {self._name}")
            self._sftp_conn = await conn.start_sftp_client(**self._sftp_options._kwargs())
            logger.debug(f"SFTP client connected for {self._name}")
        return self._sftp_conn

    async def ftp(self) -> aioftp.Client:
        """Return the live FTP client, opening it if needed."""
        if self._ftp_conn is None:
            user, password = self.credentials
            ftp_port = self._ftp_options.port
            client_kwargs = self._ftp_options._client_kwargs()
            if self._hop is not None:
                local_port = await self._forward_port(ftp_port)
                self._ftp_conn = TunneledFtpClient(
                    hop=self._hop, dest_host=self._ip,
                    **client_kwargs,
                )
                logger.debug(f"Connecting to {self._name} via FTP (tunneled)")
                await self._ftp_conn.connect('localhost', local_port)
            else:
                self._ftp_conn = aioftp.Client(**client_kwargs)
                logger.debug(f"Connecting to {self._name} via FTP")
                await self._ftp_conn.connect(self._ip, ftp_port)
            await self._ftp_conn.login(user, password)
            logger.debug(f"FTP connected to {self._name}")
        return self._ftp_conn

    async def telnet(self) -> TelnetClient:
        """Return the live TelnetClient, opening it if needed."""
        if self._telnet_conn is None:
            user, password = self.credentials
            remote_port = self._telnet_options.port
            if self._hop is not None:
                local_port = await self._forward_port(remote_port)
                connect_host = 'localhost'
                connect_port = local_port
            else:
                connect_host = self._ip
                connect_port = remote_port
            logger.debug(f"Connecting to {self._name} via telnet")
            self._telnet_conn = TelnetClient(
                connect_host,
                user=user,
                password=password,
                options=self._telnet_options,
                connect_port=connect_port,
            )
            await self._telnet_conn.connect()
            logger.debug(f"Connected to {self._name} via telnet")
        return self._telnet_conn

    async def forward_port(self, dest_port: int) -> int:
        """Forward a local ephemeral port to ``self._ip:dest_port`` through the tunnel.

        This is the public interface for protocols (like netcat) that need
        additional port forwards beyond the standard ones managed internally.

        Returns the local port number to connect to.

        Raises ``RuntimeError`` if no tunnel is configured.
        """
        if self._hop is None:
            raise RuntimeError(f"{self._name}: forward_port requires a tunnel (hop)")
        return await self._forward_port(dest_port)

    async def close(self) -> None:
        """Close all open connections, port forwards, and the tunnel."""
        if self._sftp_conn:
            self._sftp_conn.exit()
            self._sftp_conn = None

        if self._ssh_conn:
            self._ssh_conn.close()
            await self._ssh_conn.wait_closed()
            self._ssh_conn = None

        if self._ftp_conn:
            await self._ftp_conn.quit()
            self._ftp_conn = None

        if self._telnet_conn:
            await self._telnet_conn.close()
            self._telnet_conn = None

        if self._hop is not None:
            await self._hop.close()
