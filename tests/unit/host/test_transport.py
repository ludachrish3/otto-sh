"""
Unit tests for HopTransport / SshHopTransport.

Tests verify tunnel caching, port-forward delegation, and cascade cleanup
without touching real SSH connections.
"""

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
    async def test_calls_factory(self, transport: SshHopTransport, factory: AsyncMock, mock_conn: MagicMock):
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

        port = await transport.forward_port('10.0.0.5', 22)
        mock_conn.forward_local_port.assert_awaited_once_with('', 0, '10.0.0.5', 22)
        assert port == 44444

    @pytest.mark.asyncio
    async def test_returns_local_port(self, transport: SshHopTransport, mock_conn: MagicMock):
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 55555
        mock_conn.forward_local_port = AsyncMock(return_value=mock_listener)

        assert await transport.forward_port('192.168.1.1', 8080) == 55555

    @pytest.mark.asyncio
    async def test_tracks_listeners(self, transport: SshHopTransport, mock_conn: MagicMock):
        """Multiple forward_port calls accumulate listeners for cleanup."""
        for expected_port in (11111, 22222):
            listener = MagicMock()
            listener.get_port.return_value = expected_port
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port('10.0.0.1', expected_port)

        assert len(transport._port_forwards) == 2


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
            await transport.forward_port('10.0.0.1', port)
            listeners.append(listener)

        await transport.close()
        for listener in listeners:
            listener.close.assert_called_once()
        assert transport._port_forwards == []

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
