"""
Hop transport abstractions for multi-hop connectivity.

A ``HopTransport`` decouples the transport mechanism (SSH tunnel, future
telnet relay, etc.) from ``ConnectionManager``.  The concrete
``SshHopTransport`` wraps an ``SSHClientConnection`` and provides tunnel
access and local port forwarding — the same operations that Phase 1
performed inline inside ``ConnectionManager``.

For multi-hop chains each transport may hold a reference to a *parent*
transport.  Closing a transport cascades to its parent, tearing down the
entire chain from the outermost hop inward.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from asyncssh import SSHClientConnection
from asyncssh.listener import SSHListener

from ..logger import get_otto_logger

logger = get_otto_logger()


class HopTransport(Protocol):
    """Minimal interface that ``ConnectionManager`` needs from a hop."""

    async def get_tunnel(self) -> SSHClientConnection: ...
    async def forward_port(self, dest_host: str, dest_port: int) -> int: ...
    async def close(self) -> None: ...


class SshHopTransport:
    """Concrete ``HopTransport`` backed by an SSH connection.

    Parameters
    ----------
    factory:
        Async callable that returns an ``SSHClientConnection`` to the hop
        host.  Called at most once (lazily, on first ``get_tunnel``).
    parent:
        Optional parent transport whose tunnel this transport's connection
        rides over.  Closed automatically when *this* transport is closed.
    """

    def __init__(
        self,
        factory: Callable[..., Awaitable[SSHClientConnection]],
        parent: SshHopTransport | None = None,
    ) -> None:
        self._factory = factory
        self._parent = parent
        self._conn: SSHClientConnection | None = None
        self._port_forwards: list[SSHListener] = []
        # Serialize tunnel creation: without this, concurrent callers that
        # find ``_conn is None`` each open their own SSH connection to the
        # hop and race to assign the slot. The losers are orphaned (no
        # ``close()`` ever called on their transports → ``ResourceWarning``
        # on GC). Double-checked locking matches the pattern in
        # ``ConnectionManager.ssh`` and ``SessionManager._ensure_session``.
        self._conn_lock = asyncio.Lock()

    async def get_tunnel(self, _visited: set[str] | None = None) -> SSHClientConnection:
        """Return the hop SSH connection, creating it via the factory if needed.

        ``_visited`` threads the cycle-detection set used by
        :meth:`RemoteHost._build_hop_transport`'s factory through the
        parent chain.  External callers don't need to pass it.
        """
        if self._conn is not None:
            return self._conn
        async with self._conn_lock:
            if self._conn is not None:
                return self._conn
            if _visited is None:
                self._conn = await self._factory()
            else:
                self._conn = await self._factory(_visited=_visited)
            return self._conn

    async def forward_port(self, dest_host: str, dest_port: int) -> int:
        """Forward a local ephemeral port to *dest_host:dest_port* through the tunnel.

        Returns the local port number to connect to.
        """
        conn = await self.get_tunnel()
        listener = await conn.forward_local_port('', 0, dest_host, dest_port)
        self._port_forwards.append(listener)
        return listener.get_port()

    async def close(self) -> None:
        """Close port forwards, the tunnel connection, and the parent transport."""
        for listener in self._port_forwards:
            listener.close()
        self._port_forwards.clear()

        if self._conn is not None:
            # See ``ConnectionManager.close`` for the full story: asyncssh's
            # ``wait_closed()`` returns before the underlying asyncio
            # transport's ``connection_lost`` callback fires, which leaves
            # ``transport._closing=False`` even though the OS socket is
            # already torn down. The zombie transport then triggers
            # ``ResourceWarning`` from ``__del__`` on a closed loop, which
            # pytest's ``[unraisable]`` plugin escalates into a flake on
            # the *next* test. Capture the asyncio transport before close
            # and explicitly ``close()`` it after.
            asyncio_transport = getattr(self._conn, '_transport', None)
            self._conn.close()
            await self._conn.wait_closed()
            if asyncio_transport is not None:
                asyncio_transport.close()
            self._conn = None

        if self._parent is not None:
            await self._parent.close()
