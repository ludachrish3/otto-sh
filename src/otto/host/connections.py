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

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from typing_extensions import override

from ..registry import Registry, caller_module
from .login_proxy import Cred, LoginProxyError, resolve_chain
from .options import FtpOptions, SftpOptions, SshOptions, TelnetOptions
from .telnet import TelnetClient

if TYPE_CHECKING:
    import aioftp
    from asyncssh import SFTPClient, SSHClientConnection

    from .transport import HopTransport


@dataclass(frozen=True, slots=True)
class TelnetTarget:
    """Where a telnet client must dial to reach a device.

    Two fields that are easy to swap and impossible to tell apart once they
    are a bare pair — a hostname that is sometimes the device and sometimes
    ``"localhost"``, and a port that is sometimes the console's and sometimes
    an ephemeral local one. Naming them is what makes a caller's mistake
    visible at the call site rather than at connect time.
    """

    host: str
    """The address to dial -- the device's own ip, or ``"localhost"`` when tunnelled."""

    port: int
    """The port to dial -- ``telnet_options.port``, or the forwarded local port."""


@dataclass(frozen=True)
class TermContext:
    """Construction inputs a UnixHost provides to build its connection backend.

    The frozen public seam for custom term backends; carries only what the built-in already
    receives at its call site (no new coupling).
    """

    ip: str
    creds: list[Cred]
    user: str | None
    term: str
    name: str
    hop: "HopTransport | None" = None
    ssh_options: SshOptions | None = None
    telnet_options: TelnetOptions | None = None
    sftp_options: SftpOptions | None = None
    ftp_options: FtpOptions | None = None


logger = logging.getLogger(__name__)


@contextlib.contextmanager
def teardown_step(name: str, step: str) -> "Iterator[None]":
    """Guard one teardown step: log-and-continue, so cleanup can't mask real failures.

    Wraps a best-effort cleanup action — a close, a remote ``rm -rf`` — so its
    failure is a warning (``"{name}: {step} teardown failed: {e}"``), not the
    operation's outcome: if the body being cleaned up after already raised,
    the primary exception survives; if it succeeded, a failed cleanup doesn't
    fail it retroactively. This is the wrapper the
    ``no-awaited-exec-in-finally`` / ``no-awaited-close-in-finally``
    architecture gates point at; :func:`otto.lifecycle.compensate` is the
    alternative when the cleanup must also survive cancellation.

    Catches ``Exception`` only — ``CancelledError`` still propagates: an
    abandoned teardown (second Ctrl+C / deadline expiry) stops the chain
    loudly rather than pretending to finish it (chaos spec: teardown chain
    robustness).
    """
    try:
        yield
    except Exception as e:  # noqa: BLE001 — teardown chain must not let one step skip the rest
        logger.warning(f"{name}: {step} teardown failed: {e}")


_tunneled_ftp_client_cls: type | None = None


def _build_tunneled_ftp_client_cls() -> type:
    """Build (once) the ``aioftp.Client`` subclass that routes FTP data connections through a hop.

    Defined lazily — and cached — so merely importing this module does not pull
    in the heavy ``aioftp`` package. ``aioftp`` is only needed when an FTP
    connection is actually opened (or the class is introspected by a test). See
    ``tests/unit/host/test_lazy_network_imports.py``. The cached class is also
    surfaced as the module attribute ``TunneledFtpClient`` via ``__getattr__``,
    so ``from otto.host.connections import TunneledFtpClient`` and
    ``isinstance(...)`` checks remain stable.
    """
    global _tunneled_ftp_client_cls  # noqa: PLW0603 — module-level singleton/cache
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

        def __init__(self, hop: "HopTransport", dest_host: str, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
            super().__init__(**kwargs)
            self._hop = hop
            self._dest_host = dest_host
            self._tunnel_data = False

        @override
        async def connect(self, host: str, port: int = aioftp.DEFAULT_PORT) -> list[str]:  # type: ignore[override]
            # Control connection is already forwarded — connect normally.
            info = await super().connect(host, port)
            # Enable tunnel override for subsequent PASV data connections.
            self._tunnel_data = True
            return info

        @override
        async def _open_connection(  # type: ignore[no-untyped-def, override]
            self, host: str, port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
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
            return await conn.open_connection(self._dest_host, port)  # ty: ignore[invalid-return-type]

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
                self._user_ssh_conns = {}
                self._user_sftp_conns = {}
                self._user_locks = {}

            async def ssh(self):
                return self._ssh_conn


        host = UnixHost(..., _connection_factory=FakeConnections)
    """

    def __init__(
        self,
        ip: str,
        creds: list[Cred],
        user: str | None,
        term: str,
        name: str,
        hop: "HopTransport | None" = None,
        ssh_options: SshOptions | None = None,
        telnet_options: TelnetOptions | None = None,
        sftp_options: SftpOptions | None = None,
        ftp_options: FtpOptions | None = None,
    ) -> None:
        self._ip = ip
        self._creds = creds
        self._user = user
        self._term = term
        self._name = name
        self._hop = hop
        self._ssh_options = ssh_options or SshOptions()
        self._telnet_options = telnet_options or TelnetOptions()
        self._sftp_options = sftp_options or SftpOptions()
        self._ftp_options = ftp_options or FtpOptions()

        self._ssh_conn: "SSHClientConnection | None" = None
        self._sftp_conn: "SFTPClient | None" = None
        self._ftp_conn: "aioftp.Client | None" = None
        self._telnet_conn: TelnetClient | None = None

        # Per-user connections (spec 2026-09-01 §3): ``ssh_as``/``sftp_as``
        # authenticate the transport AS a given login, keyed by that login,
        # separate from the primary ``login_target`` slots above.
        self._user_ssh_conns: "dict[str, SSHClientConnection]" = {}
        self._user_sftp_conns: "dict[str, SFTPClient]" = {}
        self._user_locks: "dict[str, asyncio.Lock]" = {}

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
        """Expose the stored ``TelnetOptions`` so custom callers honor the same configuration."""
        return self._telnet_options

    @property
    def login_target(self) -> str:
        """The login the session should end up as (host.user or first entry)."""
        if self._user is not None:
            return self._user
        return self._creds[0].login if self._creds else ""

    @property
    # DEBT(no-tuple-return): a (user, password) pair; wants a frozen dataclass.
    # ast-grep-ignore: no-tuple-return
    def credentials(self) -> tuple[str, str | None]:
        """(username, password) for TRANSPORT auth — the resolved direct cred.

        For a proxied ``login_target`` this is the via-chain's directly
        loginable end; the hops are applied post-handshake (see
        ``proxy_hops``). ``('', '')`` when no creds are configured.
        """
        if not self._creds:
            return ("", "")
        direct, _ = resolve_chain(self._creds, self.login_target)
        return direct.login, direct.password

    @property
    def proxy_hops(self) -> list[Cred]:
        """Proxied creds to apply after the marker handshake, outermost first."""
        if not self._creds:
            return []
        _, hops = resolve_chain(self._creds, self.login_target)
        return hops

    @property
    def ip(self) -> str:
        """IP address this connection manager dials."""
        return self._ip

    @property
    def term(self) -> str:
        """Active terminal type (``'ssh'`` or ``'telnet'``)."""
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
            or self._user_ssh_conns
            or self._user_sftp_conns
        )

    @property
    def has_tunnel(self) -> bool:
        """Whether this connection manager is configured to use a tunnel."""
        return self._hop is not None

    async def _ensure_tunnel(self) -> "SSHClientConnection":
        """Return the tunnel SSH connection, creating it via the hop transport if needed."""
        assert self._hop is not None  # noqa: S101 — internal invariant: callers must check has_tunnel before calling _ensure_tunnel()
        logger.debug(f"Establishing SSH tunnel for {self._name}")
        tunnel = await self._hop.get_tunnel()
        logger.debug(f"SSH tunnel established for {self._name}")
        return tunnel

    async def _forward_port(self, dest_port: int) -> int:
        """Forward a local ephemeral port to ``self._ip:dest_port`` through the tunnel.

        Returns the local port number to connect to.
        """
        assert self._hop is not None  # noqa: S101 — internal invariant: callers must check has_tunnel before calling _forward_port()
        local_port = await self._hop.forward_port(self._ip, dest_port)
        logger.debug(
            f"Forwarding localhost:{local_port} -> {self._ip}:{dest_port} for {self._name}"
        )
        return local_port

    async def ssh(self) -> "SSHClientConnection":
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
                **self._ssh_options._kwargs(),  # noqa: SLF001 — intra-package access to SshOptions._kwargs
            )
            await self._ssh_options._apply_post_connect(conn)  # noqa: SLF001 — intra-package access to SshOptions._apply_post_connect
            self._ssh_conn = conn
            logger.debug(f"Connected to {self._name} via SSH")
            return conn

    async def sftp(self) -> "SFTPClient":
        """Return the live SFTP client, opening it (and SSH if needed) first."""
        if self._sftp_conn is not None:
            return self._sftp_conn
        async with self._sftp_lock:
            if self._sftp_conn is not None:
                return self._sftp_conn
            conn = await self.ssh()
            logger.debug(f"Starting SFTP client for {self._name}")
            sftp = await conn.start_sftp_client(**self._sftp_options._kwargs())  # noqa: SLF001 — intra-package access to SftpOptions._kwargs
            self._sftp_conn = sftp
            logger.debug(f"SFTP client connected for {self._name}")
            return sftp

    def _direct_cred_for(self, user: str) -> Cred:
        """Resolve the cred to authenticate a transport as *user* — zero hops or refuse.

        ``resolve_chain`` answers both halves: an unknown login raises its own
        loud error; a known login reachable only through proxy hops refuses
        here, because connection-level auth cannot replay interactive hops
        (spec 2026-09-01 §2.4) — ``login(user=...)``/``as_user`` CAN, and the
        message says so.
        """
        direct, hops = resolve_chain(self._creds, user)
        if hops or direct.login != user:
            raise LoginProxyError(
                f"{self._name}: user {user!r} has no directly-loginable cred "
                f"(resolves via {direct.login!r} + {len(hops)} hop(s)); "
                f"stateless per-user auth cannot replay proxy hops — use "
                f"login(user=...) or as_user() instead"
            )
        return direct

    async def ssh_as(self, user: str) -> "SSHClientConnection":
        """Return a cached SSH connection AUTHENTICATED AS *user* (spec 2026-09-01 §3).

        Separate from :meth:`ssh` (the login-target connection) and keyed by
        login. Same ip/tunnel path; same double-checked-locking shape as the
        primary slots. Closed by :meth:`close` with everything else.
        """
        conn = self._user_ssh_conns.get(user)
        if conn is not None:
            return conn
        lock = self._user_locks.setdefault(user, asyncio.Lock())
        async with lock:
            conn = self._user_ssh_conns.get(user)
            if conn is not None:
                return conn
            direct = self._direct_cred_for(user)
            logger.debug(f"Connecting to {self._name} via SSH as {user!r}")
            tunnel = None
            if self._hop is not None:
                tunnel = await self._ensure_tunnel()
            conn = await ssh_connect(
                self._ip,
                username=direct.login,
                password=direct.password,
                tunnel=tunnel,
                **self._ssh_options._kwargs(),  # noqa: SLF001 — intra-package access to SshOptions._kwargs
            )
            await self._ssh_options._apply_post_connect(conn)  # noqa: SLF001 — intra-package access to SshOptions._apply_post_connect
            self._user_ssh_conns[user] = conn
            return conn

    async def sftp_as(self, user: str) -> "SFTPClient":
        """Return a cached SFTP client on :meth:`ssh_as`'s connection for *user*."""
        sftp = self._user_sftp_conns.get(user)
        if sftp is not None:
            return sftp
        lock = self._user_locks.setdefault(f"{user}\x00sftp", asyncio.Lock())
        async with lock:
            sftp = self._user_sftp_conns.get(user)
            if sftp is not None:
                return sftp
            conn = await self.ssh_as(user)
            sftp = await conn.start_sftp_client(**self._sftp_options._kwargs())  # noqa: SLF001 — intra-package access to SftpOptions._kwargs
            self._user_sftp_conns[user] = sftp
            return sftp

    async def ftp(self) -> "aioftp.Client":
        """Return the live FTP client, opening it if needed."""
        import aioftp

        if self._ftp_conn is not None:
            return self._ftp_conn
        async with self._ftp_lock:
            if self._ftp_conn is not None:
                return self._ftp_conn
            user, password = self.credentials
            ftp_port = self._ftp_options.port
            client_kwargs = self._ftp_options._client_kwargs()  # noqa: SLF001 — intra-package access to FtpOptions._client_kwargs
            if self._hop is not None:
                local_port = await self._forward_port(ftp_port)
                client: aioftp.Client = _build_tunneled_ftp_client_cls()(
                    hop=self._hop,
                    dest_host=self._ip,
                    **client_kwargs,
                )
                logger.debug(f"Connecting to {self._name} via FTP (tunneled)")
                await client.connect("localhost", local_port)
            else:
                client = aioftp.Client(**client_kwargs)
                logger.debug(f"Connecting to {self._name} via FTP")
                await client.connect(self._ip, ftp_port)
            await client.login(user, password or "")
            self._ftp_conn = client
            logger.debug(f"FTP connected to {self._name}")
            return client

    async def telnet_target(self) -> TelnetTarget:
        """Where a NEW telnet client must dial to reach this device.

        The device's own ip and ``telnet_options.port`` when it is directly
        reachable; ``"localhost"`` and a forwarded local port when it sits
        behind a hop, whose console is only reachable through the tunnel.

        This exists as a method — rather than as the six inline lines it
        replaces — because it had TWO callers and only one of them made the
        decision. :meth:`telnet` (the default session's transport) forwarded;
        ``SessionManager.open_session`` (every NAMED session, and therefore the
        whole telnet exec pool that backs ``UnixHost.exec`` and the nc
        transfers) hand-rolled ``TelnetClient(connections.ip, ...)`` with no
        forward and no ``connect_port``. For a hop-fronted telnet host that
        dials the address literally: a BusyBox bed guest's ``ip`` is an address
        on a /30 that exists ONLY on test1, so dialling it from here reaches
        nothing at all, while ``run`` — which forwards — works perfectly.
        Measured 2026-08-21 against bb1350, when the guests were still reached
        through a QEMU hostfwd and their ``ip`` was ``127.0.0.1``: ``exec``
        raised ``ConnectionRefusedError [Errno 111] Connect call failed
        ('127.0.0.1', 2335)`` against the DEV VM's own loopback. The addressing
        has since moved onto real TAP NICs and the defect's shape is unchanged
        — a literal dial is a dial at the wrong machine either way.

        The defect was invisible until this bed existed. otto's other
        hop-fronted telnet devices are the Zephyr consoles, which are
        single-client and only ever use the default session; the BusyBox guests
        are the first that both sit behind a hop and open named sessions.

        Forwards are cached per destination by
        :meth:`~otto.host.transport.SshHopTransport.forward_port`, so a pool of
        exec sessions shares one listener rather than taking one each.
        """
        remote_port = self._telnet_options.port
        if self._hop is not None:
            return TelnetTarget("localhost", await self._forward_port(remote_port))
        return TelnetTarget(self._ip, remote_port)

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
                with contextlib.suppress(Exception):
                    await self._telnet_conn.close()
                self._telnet_conn = None

            if self._telnet_conn is not None:
                return self._telnet_conn

            user, password = self.credentials
            target = await self.telnet_target()
            logger.debug(f"Connecting to {self._name} via telnet")
            client = TelnetClient(
                target.host,
                user=user,
                password=password or "",
                options=self._telnet_options,
                connect_port=target.port,
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
                with contextlib.suppress(Exception):
                    await client.close()
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

    def unforward_port(self, dest_port: int) -> None:
        """Release a forward taken out by :meth:`forward_port`.

        The counterpart a per-transfer caller needs: without it a forward
        lives until the host closes, so a bulk netcat put strands one
        listening socket per file for the rest of the session.  Tolerant by
        design — no tunnel, or a destination that was never forwarded, is a
        no-op, so a caller can release unconditionally from its cleanup path
        without first working out whether it took one.

        Only for forwards a caller owns end to end.  The ftp and telnet
        forwards are held by their own cached clients and must not be
        released per-operation.
        """
        if self._hop is None:
            return
        self._hop.unforward_port(self._ip, dest_port)
        logger.debug(f"Released forward to {self._ip}:{dest_port} for {self._name}")

    async def close(self) -> None:
        """Close all open connections, port forwards, and the tunnel.

        Every step is individually guarded (log-and-continue) so one raising
        step — e.g. ``ftp.quit()`` on a dead socket — cannot skip the steps
        behind it, in particular the SSH-hop teardown (chaos spec: teardown
        chain robustness). Cached slots are cleared take-then-clear BEFORE
        each close attempt so a failing close can't leave a half-dead
        connection cached for reuse. ``CancelledError`` is not guarded: a
        force-abandoned teardown stops the chain, loudly.
        """
        sftp, self._sftp_conn = self._sftp_conn, None
        ssh, self._ssh_conn = self._ssh_conn, None
        ftp, self._ftp_conn = self._ftp_conn, None
        telnet, self._telnet_conn = self._telnet_conn, None
        user_sftp, self._user_sftp_conns = self._user_sftp_conns, {}
        user_ssh, self._user_ssh_conns = self._user_ssh_conns, {}
        self._user_locks = {}

        for login, client in user_sftp.items():
            with teardown_step(self._name, f"sftp-as-{login}"):
                client.exit()

        for login, conn in user_ssh.items():
            with teardown_step(self._name, f"ssh-as-{login}"):
                # Mirror the primary ssh slot's zombie-transport mitigation
                # below: without it a hopped per-user connection can leave an
                # asyncio transport with ``_closing=False`` after the OS
                # socket is gone, firing a ``ResourceWarning`` on GC that
                # pytest escalates against a later, unrelated test.
                asyncio_transport = getattr(conn, "_transport", None)
                try:
                    conn.close()
                    await conn.wait_closed()
                finally:
                    if asyncio_transport is not None:
                        asyncio_transport.close()

        if sftp:
            with teardown_step(self._name, "sftp"):
                sftp.exit()

        if ssh:
            with teardown_step(self._name, "ssh"):
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
                # sets ``_closing=True`` so ``__del__`` is a no-op. In a
                # ``finally`` so a raising/cancelled ``wait_closed`` still gets
                # the mitigation.
                asyncio_transport = getattr(ssh, "_transport", None)
                try:
                    ssh.close()
                    await ssh.wait_closed()
                finally:
                    if asyncio_transport is not None:
                        asyncio_transport.close()

        if ftp:
            with teardown_step(self._name, "ftp"):
                await ftp.quit()

        if telnet:
            with teardown_step(self._name, "telnet"):
                await telnet.close()

        if self._hop is not None:
            with teardown_step(self._name, "hop"):
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


class _UserConnections:
    """A per-user view over a :class:`ConnectionManager` for transfer backends.

    Delegates everything to the primary manager except the two auth-carrying
    accessors: ``ssh()``/``sftp()`` return the *user*'s own connection, so an
    unchanged scp/sftp/nc backend runs over it and files land owned by the
    user (spec 2026-09-01 §4). ``ftp()`` refuses — that client authenticates
    separately with its own creds. Not a subclass: delegation via
    ``__getattr__`` keeps this immune to manager fields it never heard of.
    """

    def __init__(self, primary: "ConnectionManager", user: str) -> None:
        self._primary = primary
        self._user = user

    async def ssh(self) -> "SSHClientConnection":
        return await self._primary.ssh_as(self._user)

    async def sftp(self) -> "SFTPClient":
        return await self._primary.sftp_as(self._user)

    async def ftp(self) -> "NoReturn":
        raise NotImplementedError(
            f"{self._primary._name}: put/get(user=...) over ftp — "  # noqa: SLF001 — sibling class in the same module
            f"the ftp backend authenticates separately with its own creds"
        ) from None

    def __getattr__(self, name: str) -> "Any":
        return getattr(self._primary, name)


@dataclass(frozen=True)
class TermBackend:
    """A registered term backend: the manager class + the host families it serves."""

    cls: type[ConnectionManager]
    host_families: frozenset[str]


TERM_BACKENDS: Registry[TermBackend] = Registry(
    "term backend", register_hint="otto.host.connections.register_term_backend()"
)


def register_term_backend(
    name: str,
    cls: type[ConnectionManager],
    *,
    host_families: frozenset[str],
    overwrite: bool = False,
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

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    if not host_families:
        raise ValueError(
            f"register_term_backend({name!r}): host_families is empty; "
            f"a term backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    TERM_BACKENDS.register(
        name,
        TermBackend(cls=cls, host_families=host_families),
        overwrite=overwrite,
        origin=caller_module(),
    )


def build_term_backend(name: str) -> type[ConnectionManager]:
    """Return the connection-backend class registered under *name*.

    Raises:
        ValueError: If *name* is not registered; the message lists registered
            names and suggests near-misses.
    """
    return TERM_BACKENDS.get(name).cls


def _register_builtin_term_backends() -> None:
    """Register otto's built-in term backends through the public path.

    Ensures first-party and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``).
    """
    register_term_backend("ssh", ConnectionManager, host_families=frozenset({"unix"}))
    register_term_backend(
        "telnet", ConnectionManager, host_families=frozenset({"unix", "embedded"})
    )


_register_builtin_term_backends()
