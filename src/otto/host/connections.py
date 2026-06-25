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

Inject a subclass via ``UnixHost._connection_factory`` to replace the real
transport with a test double — no monkeypatching of library functions needed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logger import get_otto_logger
from .options import FtpOptions, SftpOptions, SshOptions, TelnetOptions
from .telnet import TelnetClient

if TYPE_CHECKING:
    import aioftp
    from asyncssh import SFTPClient, SSHClientConnection

    from .transport import HopTransport


@dataclass(frozen=True)
class TermContext:
    """Construction inputs a UnixHost provides to build its connection backend
    via :meth:`ConnectionManager.create`. The frozen public seam for custom
    term backends; carries only what the built-in already receives at its call
    site (no new coupling).
    """

    ip: str
    creds: dict[str, str]
    user: str | None
    term: str
    name: str
    hop: "HopTransport | None" = None
    ssh_options: SshOptions | None = None
    telnet_options: TelnetOptions | None = None
    sftp_options: SftpOptions | None = None
    ftp_options: FtpOptions | None = None


logger = get_otto_logger()


_tunneled_ftp_client_cls: type | None = None


def _build_tunneled_ftp_client_cls() -> type:
    """Build (once) the ``aioftp.Client`` subclass that routes FTP data
    connections through an SSH hop.

    Defined lazily — and cached — so merely importing this module does not pull
    in the heavy ``aioftp`` package. ``aioftp`` is only needed when an FTP
    connection is actually opened (or the class is introspected by a test). See
    ``tests/unit/host/test_lazy_network_imports.py``. The cached class is also
    surfaced as the module attribute ``TunneledFtpClient`` via ``__getattr__``,
    so ``from otto.host.connections import TunneledFtpClient`` and
    ``isinstance(...)`` checks remain stable.
    """
    global _tunneled_ftp_client_cls
    if _tunneled_ftp_client_cls is not None:
        return _tunneled_ftp_client_cls

    import aioftp

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
            # Open a direct SSH channel to the FTP server's data port instead of
            # opening a local listener and connecting through it. The listener
            # approach (via ``forward_local_port``) leaves both the listener and
            # the local-side accept socket in ``HopTransport._port_forwards``;
            # those linger until the hop closes, and the local socket pair
            # (127.0.0.1:X → 127.0.0.1:Y) hits asyncio's ``__del__`` after the
            # test ends, raising ``ResourceWarning`` which pytest's
            # ``[unraisable]`` plugin escalates into a flake on the *next* test.
            # ``conn.open_connection`` returns ``(SSHReader, SSHWriter)`` (duck-
            # compatible with asyncio's stream pair) tied directly to the SSH
            # channel — closes cleanly when aioftp closes the writer.
            conn = await self._hop.get_tunnel()
            return await conn.open_connection(self._dest_host, port)

    _tunneled_ftp_client_cls = TunneledFtpClient
    return _tunneled_ftp_client_cls


def __getattr__(name: str) -> Any:
    # PEP 562: expose ``TunneledFtpClient`` as a module attribute without
    # importing aioftp at module load. Triggered by
    # ``from otto.host.connections import TunneledFtpClient`` and any
    # ``otto.host.connections.TunneledFtpClient`` access.
    if name == "TunneledFtpClient":
        return _build_tunneled_ftp_client_cls()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def ssh_connect(*args: Any, **kwargs: Any) -> Any:
    """Lazy, patchable wrapper around :func:`asyncssh.connect`.

    Kept as a module-level seam (tests monkeypatch it) while deferring the heavy
    ``asyncssh`` import to connect-time — see
    ``tests/unit/host/test_lazy_network_imports.py``.
    """
    from asyncssh import connect
    return await connect(*args, **kwargs)


class ConnectionManager:
    """Owns all raw transport connections for a single remote host.

    Connections are created lazily and reused across calls. Call ``close()``
    to release all open connections.

    When a ``HopTransport`` is provided (via the *hop* parameter), an SSH
    tunnel to the hop host is established lazily on first use. All protocol
    connections are then routed through this tunnel rather than connecting
    directly to the target IP.

    Subclass and inject via ``UnixHost._connection_factory`` to swap in test
    doubles without monkeypatching library functions::

        class FakeConnections(ConnectionManager):
            def __init__(self, ip, creds, user, term, name):
                self._ssh_conn = AsyncMock(spec=SSHClientConnection)
                self._sftp_conn = None
                self._ftp_conn = None
                self._telnet_conn = None

            async def ssh(self):
                return self._ssh_conn

        host = UnixHost(..., _connection_factory=FakeConnections)
    """

    def __init__(
        self,
        ip: str,
        creds: dict[str, str],
        user: str | None,
        term: str,
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

        # Concurrent callers of ``ssh()``/``telnet()``/``ftp()``/``sftp()``
        # would otherwise all see ``_*_conn is None``, all open their own
        # real connection, and race to assign the cache slot — leaving the
        # losers orphaned (no ``close()`` ever called on their transports,
        # ``ResourceWarning`` on GC). Same double-checked-locking shape as
        # ``SessionManager._ensure_session``.
        self._ssh_lock = asyncio.Lock()
        self._sftp_lock = asyncio.Lock()
        self._ftp_lock = asyncio.Lock()
        self._telnet_lock = asyncio.Lock()

    @classmethod
    def create(cls, ctx: "TermContext") -> "ConnectionManager":
        """Build a connection backend from a :class:`TermContext`.

        The uniform construction seam (WS#4): a host calls
        ``build_term_backend(name).create(ctx)`` for built-in and custom
        backends alike. The built-in's ``create`` runs today's exact
        construction — internals untouched, only the call site moves here.
        """
        return cls(
            ip=ctx.ip,
            creds=ctx.creds,
            user=ctx.user,
            term=ctx.term,
            name=ctx.name,
            hop=ctx.hop,
            ssh_options=ctx.ssh_options,
            telnet_options=ctx.telnet_options,
            sftp_options=ctx.sftp_options,
            ftp_options=ctx.ftp_options,
        )

    @property
    def telnet_options(self) -> TelnetOptions:
        """Expose the stored ``TelnetOptions`` so callers that build their
        own ``TelnetClient`` (e.g. ``SessionManager.open_session``) can
        honor the same configuration.
        """
        return self._telnet_options

    @property
    def credentials(self) -> tuple[str, str]:
        """Return the active (username, password) pair.

        Returns ``('', '')`` when no credentials are configured — valid for a
        loginless shell (e.g. an RTOS telnet shell reached with
        ``TelnetOptions.login = False``), where the empty pair is never used.
        """
        if self._user is None:
            if not self._creds_dict:
                return ('', '')
            return next(iter(self._creds_dict.items()))
        return self._user, self._creds_dict[self._user]

    @property
    def ip(self) -> str:
        return self._ip

    @property
    def term(self) -> str:
        return self._term

    @term.setter
    def term(self, value: str) -> None:
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
        if self._ssh_conn is not None:
            return self._ssh_conn
        async with self._ssh_lock:
            if self._ssh_conn is not None:
                return self._ssh_conn
            user, password = self.credentials
            logger.debug(f"Connecting to {self._name} via SSH")
            tunnel = None
            if self._hop is not None:
                tunnel = await self._ensure_tunnel()
            conn = await ssh_connect(
                self._ip,
                username=user,
                password=password,
                tunnel=tunnel,
                **self._ssh_options._kwargs(),
            )
            await self._ssh_options._apply_post_connect(conn)
            self._ssh_conn = conn
            logger.debug(f"Connected to {self._name} via SSH")
            return conn

    async def sftp(self) -> SFTPClient:
        """Return the live SFTP client, opening it (and SSH if needed) first."""
        if self._sftp_conn is not None:
            return self._sftp_conn
        async with self._sftp_lock:
            if self._sftp_conn is not None:
                return self._sftp_conn
            conn = await self.ssh()
            logger.debug(f"Starting SFTP client for {self._name}")
            sftp = await conn.start_sftp_client(**self._sftp_options._kwargs())
            self._sftp_conn = sftp
            logger.debug(f"SFTP client connected for {self._name}")
            return sftp

    async def ftp(self) -> aioftp.Client:
        """Return the live FTP client, opening it if needed."""
        import aioftp

        if self._ftp_conn is not None:
            return self._ftp_conn
        async with self._ftp_lock:
            if self._ftp_conn is not None:
                return self._ftp_conn
            user, password = self.credentials
            ftp_port = self._ftp_options.port
            client_kwargs = self._ftp_options._client_kwargs()
            if self._hop is not None:
                local_port = await self._forward_port(ftp_port)
                client: aioftp.Client = _build_tunneled_ftp_client_cls()(
                    hop=self._hop, dest_host=self._ip,
                    **client_kwargs,
                )
                logger.debug(f"Connecting to {self._name} via FTP (tunneled)")
                await client.connect('localhost', local_port)
            else:
                client = aioftp.Client(**client_kwargs)
                logger.debug(f"Connecting to {self._name} via FTP")
                await client.connect(self._ip, ftp_port)
            await client.login(user, password)
            self._ftp_conn = client
            logger.debug(f"FTP connected to {self._name}")
            return client

    async def telnet(self) -> TelnetClient:
        """Return the live TelnetClient, opening it if needed.

        Telnet has no channel multiplexing — the underlying TCP connection
        and the TelnetClient are 1:1, so when a TelnetSession built on this
        client closes its writer (or the peer closes the connection), the
        cached client becomes stale. Rechecking ``alive`` here catches that
        case and reconnects, rather than handing back a dead client.
        """
        if self._telnet_conn is not None and self._telnet_conn.alive:
            return self._telnet_conn
        async with self._telnet_lock:
            if self._telnet_conn is not None and not self._telnet_conn.alive:
                # Best-effort cleanup of the stale client; close() is idempotent
                # and clears the writer/reader so a partial-close doesn't linger.
                try:
                    await self._telnet_conn.close()
                except Exception:
                    pass
                self._telnet_conn = None

            if self._telnet_conn is not None:
                return self._telnet_conn

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
            client = TelnetClient(
                connect_host,
                user=user,
                password=password,
                options=self._telnet_options,
                connect_port=connect_port,
            )
            # Don't publish the cached attribute until ``connect()`` succeeds.
            # ``connect()`` runs login (~1 s on real hardware), and a caller-
            # level ``wait_for`` cancellation lands somewhere in that handshake
            # — leaving the client at the login prompt with the writer still
            # open. ``alive`` only inspects the writer, so the next call would
            # reuse the half-built client and get the login banner echoed back
            # instead of a shell. Tear down on any exception (including
            # CancelledError) so the next call rebuilds cleanly.
            try:
                await client.connect()
            except BaseException:
                try:
                    await client.close()
                except Exception:
                    pass
                raise
            self._telnet_conn = client
            logger.debug(f"Connected to {self._name} via telnet")
            return client

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
            # asyncssh's ``wait_closed()`` returns when the SSH session
            # finishes — but in some teardown paths (notably hopped
            # connections where the parent tunnel survives the child) the
            # underlying asyncio ``_SelectorSocketTransport`` is left with
            # ``_closing=False`` even though the OS socket is gone (fd=-1).
            # That zombie transport sits in GC until later, when its
            # ``__del__`` fires ``ResourceWarning`` on a closed loop and
            # pytest's ``[unraisable]`` plugin escalates it into a flake on
            # whichever next test happens to be running. Grab the asyncio
            # transport before close and explicitly close() it after — this
            # sets ``_closing=True`` so ``__del__`` is a no-op.
            asyncio_transport = getattr(self._ssh_conn, '_transport', None)
            self._ssh_conn.close()
            await self._ssh_conn.wait_closed()
            if asyncio_transport is not None:
                asyncio_transport.close()
            self._ssh_conn = None

        if self._ftp_conn:
            await self._ftp_conn.quit()
            self._ftp_conn = None

        if self._telnet_conn:
            await self._telnet_conn.close()
            self._telnet_conn = None

        if self._hop is not None:
            await self._hop.close()

        # NOTE: the asyncssh zombie ``_SelectorSocketTransport`` is handled
        # precisely above by closing ``asyncio_transport`` explicitly, which
        # sets the *asyncio transport's* ``_closing=True`` so asyncio's own
        # ``_SelectorSocketTransport.__del__`` finalizer is a no-op. This is
        # unrelated to otto's host lifecycle and remains REQUIRED after the
        # removal of ``RemoteHost.__del__`` — do not delete it as "dead
        # ``__del__`` scaffolding". We deliberately
        # do *not* call ``gc.collect()`` here: a process-wide collection sweeps
        # up every leaked object in the interpreter — including sockets/loops
        # leaked by unrelated tests — firing their ``__del__`` and letting
        # pytest's ``[unraisable]`` plugin escalate those warnings into a flake
        # on whatever test happens to be calling ``close()``.


# Registry of term-protocol name -> ConnectionManager(-compatible) class, plus
# the host families each registered name applies to. Unlike the transfer
# registry — where each backend is a distinct class carrying its own
# ``host_families`` ClassVar — ssh and telnet share the single
# ``ConnectionManager`` class, so a term's applicable families cannot live on
# the class. They are stored per-registered-name in ``_TERM_FAMILIES`` instead.
# ``build_*`` returns the class so the host can call ``.create(ctx)`` on it.
_TERM_BACKENDS: dict[str, type[ConnectionManager]] = {}
_TERM_FAMILIES: dict[str, frozenset[str]] = {}


def register_term_backend(
    name: str, cls: type[ConnectionManager], *, host_families: frozenset[str]
) -> None:
    """Make a custom connection backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    Once registered, a host's ``term`` field can select it by name.

    *host_families* is the non-empty set of host families this term serves — a
    ``frozenset`` subset of ``{'unix', 'embedded'}``. Because ssh/telnet share
    one ``ConnectionManager`` class, the families are passed here rather than
    read from a class attribute (the transfer registry reads
    ``cls.host_families``). The host spec validator rejects a term applied to a
    family it does not serve (e.g. ``ssh`` on an embedded host); an empty
    *host_families* could never validate on any host, so it is rejected here.
    """
    if not host_families:
        raise ValueError(
            f"register_term_backend({name!r}): host_families is empty; "
            f"a term backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    _TERM_BACKENDS[name] = cls
    _TERM_FAMILIES[name] = host_families


def build_term_backend(name: str) -> type[ConnectionManager]:
    """Return the connection-backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _TERM_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_TERM_BACKENDS))
        raise ValueError(
            f"Unknown term backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_term_backend()."
        ) from None


def _register_builtin_term_backends() -> None:
    """Register otto's built-in term backends through the public path, so
    first-party and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``).
    """
    register_term_backend("ssh", ConnectionManager, host_families=frozenset({"unix"}))
    register_term_backend(
        "telnet", ConnectionManager, host_families=frozenset({"unix", "embedded"})
    )


_register_builtin_term_backends()
