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

from collections.abc import Awaitable, Callable
from typing import Protocol

from asyncssh import SSHClientConnection
from asyncssh.listener import SSHListener

from ..logger import getOttoLogger

logger = getOttoLogger()


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
        factory: Callable[[], Awaitable[SSHClientConnection]],
        parent: SshHopTransport | None = None,
    ) -> None:
        self._factory = factory
        self._parent = parent
        self._conn: SSHClientConnection | None = None
        self._port_forwards: list[SSHListener] = []

    async def get_tunnel(self) -> SSHClientConnection:
        """Return the hop SSH connection, creating it via the factory if needed."""
        if self._conn is None:
            self._conn = await self._factory()
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
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

        if self._parent is not None:
            await self._parent.close()
