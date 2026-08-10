"""
Unit tests for HopTransport / SshHopTransport.

Tests verify tunnel caching, port-forward delegation, and cascade cleanup
without touching real SSH connections.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh import SSHClientConnection

from otto.host.transport import SshHopTransport

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_conn() -> MagicMock:
    conn = MagicMock(spec=SSHClientConnection)
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    return conn


@pytest.fixture
def factory(mock_conn: MagicMock) -> AsyncMock:
    return AsyncMock(return_value=mock_conn)


@pytest.fixture
def transport(factory: AsyncMock) -> SshHopTransport:
    return SshHopTransport(factory)


# ---------------------------------------------------------------------------
# get_tunnel
# ---------------------------------------------------------------------------


class TestGetTunnel:
    @pytest.mark.asyncio
    async def test_calls_factory(
        self, transport: SshHopTransport, factory: AsyncMock, mock_conn: MagicMock
    ):
        result = await transport.get_tunnel()
        factory.assert_awaited_once()
        assert result is mock_conn

    @pytest.mark.asyncio
    async def test_caches_connection(self, transport: SshHopTransport, factory: AsyncMock):
        first = await transport.get_tunnel()
        second = await transport.get_tunnel()
        assert first is second
        factory.assert_awaited_once()


# ---------------------------------------------------------------------------
# forward_port
# ---------------------------------------------------------------------------


class TestForwardPort:
    @pytest.mark.asyncio
    async def test_delegates_to_connection(self, transport: SshHopTransport, mock_conn: MagicMock):
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=mock_listener)

        port = await transport.forward_port("10.0.0.5", 22)
        mock_conn.forward_local_port.assert_awaited_once_with("localhost", 0, "10.0.0.5", 22)
        assert port == 44444

    @pytest.mark.asyncio
    async def test_returns_local_port(self, transport: SshHopTransport, mock_conn: MagicMock):
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 55555
        mock_conn.forward_local_port = AsyncMock(return_value=mock_listener)

        assert await transport.forward_port("192.168.1.1", 8080) == 55555

    @pytest.mark.asyncio
    async def test_tracks_listeners(self, transport: SshHopTransport, mock_conn: MagicMock):
        """Distinct destinations each get their own listener, tracked for cleanup."""
        for expected_port in (11111, 22222):
            listener = MagicMock()
            listener.get_port.return_value = expected_port
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", expected_port)

        assert len(transport._port_forwards) == 2

    @pytest.mark.asyncio
    async def test_repeat_destination_reuses_one_listener(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """A destination that already has a forward does not get a second one.

        This is the whole fix: the netcat path forwards the same remote port
        once per file, and before caching each call built a listener that was
        held until ``close()`` — two descriptors per transfer, forever.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        ports = [await transport.forward_port("10.0.0.1", 9000) for _ in range(5)]

        assert ports == [44444] * 5
        mock_conn.forward_local_port.assert_awaited_once()
        assert len(transport._port_forwards) == 1

    @pytest.mark.asyncio
    async def test_same_port_on_a_different_host_is_a_different_forward(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """The key is the destination, not the port.

        No caller reaches this today — ``_build_hop_transport`` constructs a
        fresh transport per target host and ``ConnectionManager._forward_port``
        always passes its own ``_ip``, so no instance sees two destinations.
        Pinned anyway because ``forward_port`` takes ``dest_host`` as a
        parameter: the day anything shares a transport across hosts, keying on
        the port alone routes one host's traffic to another, silently.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        await transport.forward_port("10.0.0.1", 9000)
        await transport.forward_port("10.0.0.2", 9000)

        assert mock_conn.forward_local_port.await_count == 2
        assert len(transport._port_forwards) == 2

    @pytest.mark.asyncio
    async def test_unforward_closes_the_listener_and_frees_the_key(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Releasing must both close the socket and re-arm the cache.

        Closing without dropping the key hands the next caller a dead port;
        dropping without closing is the leak this whole change is about.
        """
        first, second = MagicMock(), MagicMock()
        first.get_port.return_value = 11111
        second.get_port.return_value = 22222

        mock_conn.forward_local_port = AsyncMock(return_value=first)
        assert await transport.forward_port("10.0.0.1", 9000) == 11111

        transport.unforward_port("10.0.0.1", 9000)
        first.close.assert_called_once()
        assert transport._port_forwards == {}

        mock_conn.forward_local_port = AsyncMock(return_value=second)
        assert await transport.forward_port("10.0.0.1", 9000) == 22222, (
            "forward_port handed back the released listener's port"
        )

    @pytest.mark.asyncio
    async def test_unforward_of_an_unknown_destination_is_a_no_op(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Callers release from a ``finally`` without knowing whether they took one.

        The netcat attempt can fail before ``forward_port``, and on a direct
        (untunneled) host it never forwards at all, so a release that raised
        would replace a real error with its own.
        """
        listener = MagicMock()
        listener.get_port.return_value = 11111
        mock_conn.forward_local_port = AsyncMock(return_value=listener)
        await transport.forward_port("10.0.0.1", 9000)

        transport.unforward_port("10.0.0.1", 9999)
        transport.unforward_port("10.0.0.2", 9000)
        transport.unforward_port("10.0.0.1", 9000)
        transport.unforward_port("10.0.0.1", 9000)

        listener.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_forward_binds_loopback_not_every_interface(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """The listener must not be reachable from off-box.

        Every caller connects to "localhost", so binding "" only ever bought
        off-box reachability nobody asked for. A forward has always lived
        until ``close()`` — this is not a new exposure the caching created,
        it is a standing one the caching made worth removing.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        await transport.forward_port("10.0.0.1", 9000)

        bind_host = mock_conn.forward_local_port.await_args.args[0]
        assert bind_host == "localhost", (
            f"port forward bound {bind_host!r}; a cached forward reachable from "
            "off-box is a standing route to the destination port"
        )

    @pytest.mark.asyncio
    async def test_concurrent_callers_build_one_forward(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Without the lock both callers miss the cache and one listener leaks.

        The loser's listener is never stored, so ``close()`` cannot reach it —
        exactly the orphaning ``_conn_lock`` exists to prevent for the tunnel.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444

        async def slow_forward(*_args: object) -> MagicMock:
            await asyncio.sleep(0.01)
            return listener

        mock_conn.forward_local_port = AsyncMock(side_effect=slow_forward)

        ports = await asyncio.gather(*(transport.forward_port("10.0.0.1", 9000) for _ in range(4)))

        assert ports == [44444] * 4
        mock_conn.forward_local_port.assert_awaited_once()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_connection(self, transport: SshHopTransport, mock_conn: MagicMock):
        await transport.get_tunnel()
        await transport.close()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closes_port_forwards(self, transport: SshHopTransport, mock_conn: MagicMock):
        listeners = []
        for port in (11111, 22222):
            listener = MagicMock()
            listener.get_port.return_value = port
            listener.close = MagicMock()
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", port)
            listeners.append(listener)

        await transport.close()
        for listener in listeners:
            listener.close.assert_called_once()
        assert transport._port_forwards == {}

    @pytest.mark.asyncio
    async def test_close_re_arms_the_forward_cache(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """``close()`` is the only thing that invalidates a cached forward.

        An emptied dict is not enough to assert: a mutant that moved entries
        to a side table, or that cleared only on the parent cascade, satisfies
        ``_port_forwards == {}`` and then hands out a closed listener's port
        forever. Assert the observable consequence instead.
        """
        first, second = MagicMock(), MagicMock()
        first.get_port.return_value = 11111
        second.get_port.return_value = 22222

        mock_conn.forward_local_port = AsyncMock(return_value=first)
        assert await transport.forward_port("10.0.0.1", 9000) == 11111
        await transport.close()

        mock_conn.forward_local_port = AsyncMock(return_value=second)
        assert await transport.forward_port("10.0.0.1", 9000) == 22222, (
            "forward_port returned the closed listener's port after close()"
        )

    @pytest.mark.asyncio
    async def test_one_bad_listener_does_not_abort_the_teardown(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """A raising ``listener.close()`` must not skip the rest of teardown.

        Unguarded, the first raise takes out every later listener, the tunnel
        teardown, and the parent cascade — the cleanup path of a leak fix
        becoming the largest leak in the file. Ordering makes this reachable:
        dict iteration is insertion-ordered, so the earliest forward decides
        whether the rest are closed.
        """
        listeners = []
        for port in (11111, 22222, 33333):
            listener = MagicMock()
            listener.get_port.return_value = port
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", port)
            listeners.append(listener)
        listeners[0].close.side_effect = OSError("listener already gone")

        await transport.close()

        for listener in listeners[1:]:
            listener.close.assert_called_once()
        assert transport._port_forwards == {}
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cascades_to_parent(self):
        parent_conn = MagicMock(spec=SSHClientConnection)
        parent_conn.close = MagicMock()
        parent_conn.wait_closed = AsyncMock()
        parent = SshHopTransport(AsyncMock(return_value=parent_conn))

        child_conn = MagicMock(spec=SSHClientConnection)
        child_conn.close = MagicMock()
        child_conn.wait_closed = AsyncMock()
        child = SshHopTransport(AsyncMock(return_value=child_conn), parent=parent)

        await child.get_tunnel()
        await parent.get_tunnel()
        await child.close()

        child_conn.close.assert_called_once()
        parent_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent(self, transport: SshHopTransport, mock_conn: MagicMock):
        await transport.get_tunnel()
        await transport.close()
        await transport.close()  # should not raise
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_without_connect(self, factory: AsyncMock):
        """Closing a transport that was never used is a no-op."""
        transport = SshHopTransport(factory)
        await transport.close()  # should not raise
        factory.assert_not_awaited()
