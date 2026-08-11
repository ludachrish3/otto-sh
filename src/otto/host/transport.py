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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from ..errors import OttoError

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection
    from asyncssh.listener import SSHListener

logger = logging.getLogger(__name__)


class HopTransportTornDownError(OttoError, RuntimeError):
    """A ``close()`` overtook this operation while it was still building.

    Raised by the creation paths, never by ``close()`` itself: the work
    finished, but the transport it was building for has since been torn down,
    so the result was released rather than handed over — nothing would ever
    have closed it.

    This is NOT "the transport is closed". A closed transport is reusable and
    will build a fresh tunnel on the next call, which is what
    ``ConnectionManager`` relies on and what several suites use deliberately to
    force a reconnect. This error means only that one specific in-flight
    operation lost a race with teardown, which is what cancellation produces.

    ``RuntimeError`` for the same reason as the classes in ``errors.py`` — the
    ``except (ValueError, RuntimeError)`` clauses in ``otto.cli`` must keep
    catching it.
    """


class HopTransport(Protocol):
    """Minimal interface that ``ConnectionManager`` needs from a hop."""

    async def get_tunnel(self) -> "SSHClientConnection":
        """Return the underlying SSH connection to the hop host."""
        ...

    async def forward_port(self, dest_host: str, dest_port: int) -> int:
        """Forward a local ephemeral port to *dest_host:dest_port* through the tunnel.

        Returns the local port number to connect to.
        """
        ...

    def unforward_port(self, dest_host: str, dest_port: int) -> None:
        """Release the forward for *dest_host:dest_port*.  A no-op if absent."""
        ...

    async def close(self) -> None:
        """Close the transport and release all forwarded ports."""
        ...


class SshHopTransport:
    """Concrete ``HopTransport`` backed by an SSH connection.

    Parameters
    ----------
    factory:
        Async callable that returns an ``SSHClientConnection`` to the hop
        host.  Called lazily, on the first ``get_tunnel`` of each connection
        generation — so once for a transport that is used and closed, and
        again after a ``close()`` if the transport is used again, which is
        supported and relied on.  Concurrent first callers coalesce onto one
        call via ``_conn_lock``.
    parent:
        Optional parent transport whose tunnel this transport's connection
        rides over.  Closed automatically when *this* transport is closed,
        including when a handshake is abandoned mid-flight.
    """

    def __init__(
        self,
        factory: "Callable[..., Awaitable[SSHClientConnection]]",
        parent: "SshHopTransport | None" = None,
    ) -> None:
        self._factory = factory
        self._parent = parent
        self._conn: "SSHClientConnection | None" = None
        # Keyed by destination, because a forward is a *route*, not a
        # session: asyncssh opens a fresh channel to ``dest_host:dest_port``
        # for each connection the local listener accepts, so one listener
        # serves every transfer that ever targets that destination. Without
        # this a telnet reconnect took out a second forward for port 23 and
        # held it until the host closed, once per reconnect.
        #
        # Entries are dropped by ``unforward_port`` and by ``close()``. Beyond
        # that the lifetime rule is ``_conn``'s: within one generation this
        # class never rebuilds a tunnel it has handed out, so a connection that
        # dies under us breaks a cached forward and a fresh one alike. A
        # ``close()`` starts a new generation, and the next caller gets a new
        # tunnel and an empty cache.
        self._port_forwards: "dict[tuple[str, int], SSHListener]" = {}
        # Serialize tunnel creation: without this, concurrent callers that
        # find ``_conn is None`` each open their own SSH connection to the
        # hop and race to assign the slot. The losers are orphaned (no
        # ``close()`` ever called on their transports → ``ResourceWarning``
        # on GC). Double-checked locking matches the pattern in
        # ``ConnectionManager.ssh`` and ``SessionManager._ensure_session``.
        self._conn_lock = asyncio.Lock()
        # Guards creation of a forward, so two callers racing on the same
        # destination produce one listener rather than two — the second of
        # which would never be stored and so never closed. Held only across
        # local socket work; see ``forward_port`` for why the tunnel is
        # resolved outside it.
        self._forward_lock = asyncio.Lock()
        # Bumped by ``close()`` before it awaits anything. A GENERATION rather
        # than a closed flag, because a closed transport stays usable: the next
        # ``get_tunnel`` builds a fresh tunnel, which is what
        # ``ConnectionManager`` relies on and what suites like
        # tunnel_stability's monitor-loop test use deliberately to force a
        # reconnect through a wedged sshd. What must not survive teardown is a
        # resource whose OWNER is gone, so each creation path records the
        # generation it started in and compares after its await; see ``close()``
        # for why a lock cannot do this job.
        self._generation = 0

    async def get_tunnel(self, _visited: set[str] | None = None) -> "SSHClientConnection":
        """Return the hop SSH connection, creating it via the factory if needed.

        ``_visited`` threads the cycle-detection set used by
        ``RemoteHost._build_hop_transport``'s factory through the
        parent chain.  External callers don't need to pass it.
        """
        if self._conn is not None:
            return self._conn
        # Recorded BEFORE the lock, not inside it. An uncontended
        # ``Lock.acquire()`` returns without yielding, but a contended one does
        # — so a caller queued behind another's handshake would otherwise
        # resume, read the generation a ``close()`` had already bumped, and
        # find nothing wrong with adopting a connection whose owner is gone.
        # ``forward_port`` records it at entry for the same reason.
        generation = self._generation
        async with self._conn_lock:
            if self._conn is not None:
                return self._conn
            if _visited is None:
                conn = await self._factory()
            else:
                conn = await self._factory(_visited=_visited)
            if generation != self._generation:
                # ``close()`` ran while the handshake was in flight. It could
                # not have torn this connection down — it did not exist yet —
                # so adopting it now would leave a live SSH session to the hop
                # with no owner. That is the zombie-transport class the teardown
                # below is all about. A LATER caller may legitimately build a
                # tunnel again; it is this one, whose owner is gone, that must
                # not be handed over.
                #
                # The parent goes with it. On a multi-hop chain the factory
                # ASSIGNS ``self._parent`` and opens its tunnel inside this
                # same await (``RemoteHost._build_hop_transport``), so
                # ``close()`` saw ``_parent is None`` and skipped its cascade —
                # tearing down only ``conn`` would move the zombie one hop up
                # rather than remove it.
                #
                # The reference is KEPT, exactly as ``close()`` keeps it. An
                # earlier cut cleared it here "so a later close() does not
                # cascade into it twice", which was both unnecessary — a second
                # cascade sweeps an empty dict and a ``None`` conn, which is
                # what makes ``close()`` idempotent — and harmful: a transport
                # handed its parent through the constructor documents that the
                # parent is "closed automatically when THIS transport is
                # closed", and clearing the slot silently ends that.
                await self._teardown_connection(conn)
                if self._parent is not None:
                    await self._parent.close()
                raise HopTransportTornDownError(
                    "hop transport was closed while its tunnel was being established"
                )
            self._conn = conn
            return conn

    async def forward_port(self, dest_host: str, dest_port: int) -> int:
        """Forward a local ephemeral port to *dest_host:dest_port* through the tunnel.

        Repeat calls for the same destination reuse one listener and return
        the same local port.  A forward carries no per-transfer state — the
        channel to *dest_host* is opened when a local connection is accepted,
        so a listener created while one remote ``nc`` was up reaches the next
        one just as well, and a rebuilt telnet client is carried by the
        forward the dead one used.

        The listener lives until :meth:`unforward_port` or :meth:`close`.  A
        caller that owns its destination end to end should release it; see
        :meth:`unforward_port` for why caching alone is not enough.

        Returns the local port number to connect to.
        """
        # Recorded before anything can yield, so it covers every await between
        # here and the store: the tunnel resolve, the wait for
        # ``_forward_lock`` (which DOES yield when contended), and the bind.
        generation = self._generation
        key = (dest_host, dest_port)
        cached = self._port_forwards.get(key)
        if cached is not None:
            return cached.get_port()
        # Resolve the tunnel BEFORE taking the forward lock. A cold
        # ``get_tunnel`` is a full SSH handshake to the hop, and holding this
        # lock across it would queue every other destination behind one
        # connect — each of them under a caller-side deadline
        # (``_NC_FORWARD_SETUP_TIMEOUT``). ``get_tunnel`` has its own
        # double-checked lock, so concurrent callers coalesce there exactly as
        # they did before this cache existed. What is left inside the lock is
        # local socket work: getaddrinfo, bind, create_server.
        conn = await self.get_tunnel()
        async with self._forward_lock:
            cached = self._port_forwards.get(key)
            if cached is not None:
                return cached.get_port()
            # Bind the loopback name rather than every interface. Every
            # caller connects to "localhost", so binding "" only ever bought
            # off-box reachability nobody asked for — and since a forward
            # outlives the operation that opened it, that was a route into
            # the destination's port standing there for anyone on the network
            # to use. Independent of the caching above; it is here because
            # both are about what a forward's lifetime costs.
            listener = await conn.forward_local_port("localhost", 0, dest_host, dest_port)
            if generation != self._generation:
                # Same window, one await later: the teardown loop cannot close
                # a listener that did not exist when it ran, and storing this
                # one now puts it in a dict that has already been swept.
                # Guarded like every other close in this file, so a listener
                # that raises on close reports the torn-down condition rather
                # than replacing it with its own failure.
                from .connections import teardown_step

                with teardown_step(f"{dest_host}:{dest_port}", "orphaned port forward"):
                    listener.close()
                raise HopTransportTornDownError(
                    f"hop transport was closed while forwarding to {dest_host}:{dest_port}"
                )
            self._port_forwards[key] = listener
            return listener.get_port()

    def unforward_port(self, dest_host: str, dest_port: int) -> None:
        """Release the forward for *dest_host:dest_port*.  A no-op if absent.

        Caching alone bounds the leak only where the destination repeats, and
        the netcat path is where it does not: ``_put_files_nc`` gathers every
        file concurrently and each in-flight transfer reserves its own remote
        port, so a bulk put opens one forward per file with no reuse
        available.  Measured on an 8-file put through a hop: 6 descriptors
        stranded with caching alone, 0 once the attempt releases its own.  The
        same holds sequentially on any target whose port strategy resolves to
        ``python`` or ``custom``, which return a fresh ephemeral port every
        call rather than rescanning from the base.

        Deliberately synchronous, and it does not ``wait_closed()``.  Callers
        release from a ``finally`` that may be running under cancellation,
        where an ``await`` can raise before the rest of the block — the same
        reason the netcat reap is shaped as an exit rather than a list of
        branches.  ``listener.close()`` closes the listening sockets on the
        spot, which is what the descriptor count is about; awaiting the
        servers' shutdown would additionally block on connections still being
        forwarded through them.

        No lock: ``dict.pop`` cannot interleave with anything, and the only
        racing writer would be a ``forward_port`` for this same destination,
        which cannot happen — remote ports are reserved for the life of an
        attempt.  Anything this does miss is still caught by ``close()``.
        """
        listener = self._port_forwards.pop((dest_host, dest_port), None)
        if listener is not None:
            listener.close()

    async def close(self) -> None:
        """Release the port forwards, the tunnel connection, and the parent transport.

        Does NOT make the transport unusable: a later call builds a fresh
        tunnel, which ``ConnectionManager`` relies on and which several suites
        use deliberately to force a reconnect. What close ends is the current
        GENERATION of resources, so anything still being built for the old one
        is released by its own creation path instead of being handed over.
        """
        # The generation ends FIRST, before anything below can yield. This is
        # the whole mechanism: each creation path compares the generation it
        # started in against this one after its await, so a resource that
        # finishes building during this teardown releases itself rather than
        # registering with an owner that is gone. Pinned by
        # ``test_the_generation_ends_before_close_can_yield`` — the mocked
        # ``wait_closed`` in the other window tests never yields, so they cannot
        # see this ordering and a bump moved to the end of the method passes
        # every one of them.
        self._generation += 1
        # Take-then-clear, as in ``ConnectionManager.close``: the resource stops
        # being reachable for reuse BEFORE the close attempt that might fail,
        # and the loop iterates a snapshot rather than a live dict.
        forwards, self._port_forwards = self._port_forwards, {}
        conn, self._conn = self._conn, None

        # Each listener in its own guard. Unguarded, one raising ``close()``
        # would skip every remaining listener AND the tunnel teardown below
        # AND the parent cascade — the cleanup path of a leak fix becoming the
        # largest leak in the file. Same reasoning as ``teardown_step`` in
        # ``ConnectionManager.close``, which exists for this; imported inside
        # the method because ``connections`` imports this module for typing,
        # and a module-level import back would be a cycle waiting to become
        # real. ``host.py`` reaches for it the same way. Below the bump on
        # purpose: an ImportError here must not leave the generation unended.
        from .connections import teardown_step

        for (dest_host, dest_port), listener in forwards.items():
            with teardown_step(f"{dest_host}:{dest_port}", "port forward"):
                listener.close()

        if conn is not None:
            await self._teardown_connection(conn)

        if self._parent is not None:
            await self._parent.close()

    @staticmethod
    async def _teardown_connection(conn: "SSHClientConnection") -> None:
        """Close *conn* and defuse asyncio's zombie transport.

        See ``ConnectionManager.close`` for the full story: asyncssh's
        ``wait_closed()`` returns before the underlying asyncio transport's
        ``connection_lost`` callback fires, which leaves ``_closing=False``
        even though the OS socket is already torn down. The zombie transport
        then triggers ``ResourceWarning`` from ``__del__`` on a closed loop,
        which pytest's ``[unraisable]`` plugin escalates into a flake on the
        *next* test. Capture the asyncio transport before close and explicitly
        ``close()`` it after.

        Shared with ``get_tunnel``'s post-close path rather than inlined
        twice: a connection abandoned there is abandoned for exactly this
        reason, so a bare ``conn.close()`` would leave behind the very warning
        the caller is trying to prevent. In a ``finally`` so a raising or
        cancelled ``wait_closed`` still gets the mitigation, matching
        ``ConnectionManager.close``.
        """
        asyncio_transport = getattr(conn, "_transport", None)
        try:
            conn.close()
            await conn.wait_closed()
        finally:
            if asyncio_transport is not None:
                asyncio_transport.close()
