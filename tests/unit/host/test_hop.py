"""
Tests for multi-hop connectivity (SSH tunneling through intermediate hosts).

Unit tests use mock tunnel factories and the _connection_factory injection
pattern to verify hop wiring without real SSH connections.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncssh import SSHClientConnection

from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions, ScpOptions
from otto.host.remoteHost import RemoteHost
from otto.host.transport import SshHopTransport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def host() -> RemoteHost:
    """A simple host with no hop."""
    return RemoteHost(ip='10.0.0.1', ne='target', creds={'user': 'pass'}, log=False)


@pytest.fixture
def hop_host() -> RemoteHost:
    """A host configured with a hop."""
    return RemoteHost(
        ip='10.0.0.2', ne='target', creds={'user': 'pass'},
        hop='jumpbox', log=False,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class TestHopField:

    def test_default_hop_is_none(self, host: RemoteHost):
        assert host.hop is None

    def test_hop_field_set(self, hop_host: RemoteHost):
        assert hop_host.hop == 'jumpbox'

    def test_no_tunnel_when_no_hop(self, host: RemoteHost):
        assert not host._connections.has_tunnel

    def test_has_tunnel_when_hop_set(self, hop_host: RemoteHost):
        assert hop_host._connections.has_tunnel


# ---------------------------------------------------------------------------
# ConnectionManager tunnel wiring
# ---------------------------------------------------------------------------

class TestConnectionManagerTunnel:

    def test_no_tunnel_factory_by_default(self):
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test',
        )
        assert not cm.has_tunnel

    def test_tunnel_factory_stored(self):
        factory = AsyncMock(return_value=MagicMock(spec=SSHClientConnection))
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        assert cm.has_tunnel

    @pytest.mark.asyncio
    async def test_ensure_tunnel_calls_factory_once(self):
        mock_conn = MagicMock(spec=SSHClientConnection)
        factory = AsyncMock(return_value=mock_conn)
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        tunnel1 = await cm._ensure_tunnel()
        tunnel2 = await cm._ensure_tunnel()
        assert tunnel1 is tunnel2
        factory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ssh_uses_tunnel(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        factory = AsyncMock(return_value=mock_tunnel)
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        with patch('otto.host.connections.ssh_connect', new_callable=AsyncMock) as mock_connect:
            mock_ssh = MagicMock(spec=SSHClientConnection)
            mock_connect.return_value = mock_ssh
            result = await cm.ssh()
            mock_connect.assert_awaited_once_with(
                '10.0.0.1',
                username='user',
                password='pass',
                tunnel=mock_tunnel,
                port=22,
                known_hosts=None,
            )
            assert result is mock_ssh

    @pytest.mark.asyncio
    async def test_ssh_no_tunnel_when_not_configured(self):
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test',
        )
        with patch('otto.host.connections.ssh_connect', new_callable=AsyncMock) as mock_connect:
            mock_ssh = MagicMock(spec=SSHClientConnection)
            mock_connect.return_value = mock_ssh
            result = await cm.ssh()
            mock_connect.assert_awaited_once_with(
                '10.0.0.1',
                username='user',
                password='pass',
                tunnel=None,
                port=22,
                known_hosts=None,
            )

    @pytest.mark.asyncio
    async def test_telnet_uses_port_forward_when_tunneled(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 54321
        mock_tunnel.forward_local_port = AsyncMock(return_value=mock_listener)
        factory = AsyncMock(return_value=mock_tunnel)

        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='telnet', name='test', hop=SshHopTransport(factory),
        )
        with patch('otto.host.connections.TelnetClient') as MockTelnet:
            mock_tc = MagicMock()
            mock_tc.connect = AsyncMock()
            MockTelnet.return_value = mock_tc
            result = await cm.telnet()
            MockTelnet.assert_called_once()
            call_args = MockTelnet.call_args
            assert call_args.args == ('localhost',)
            assert call_args.kwargs['user'] == 'user'
            assert call_args.kwargs['password'] == 'pass'
            assert call_args.kwargs['connect_port'] == 54321
            mock_tunnel.forward_local_port.assert_awaited_once_with('', 0, '10.0.0.1', 23)

    @pytest.mark.asyncio
    async def test_ftp_uses_tunneled_client_when_hop_present(self):
        from otto.host.connections import TunneledFtpClient

        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 54322
        mock_tunnel.forward_local_port = AsyncMock(return_value=mock_listener)
        factory = AsyncMock(return_value=mock_tunnel)

        hop = SshHopTransport(factory)
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=hop,
        )
        with patch.object(TunneledFtpClient, 'connect', new_callable=AsyncMock) as mock_connect, \
             patch.object(TunneledFtpClient, 'login', new_callable=AsyncMock) as mock_login:
            result = await cm.ftp()
            assert isinstance(result, TunneledFtpClient)
            mock_connect.assert_awaited_once_with('localhost', 54322)
            mock_tunnel.forward_local_port.assert_awaited_once_with('', 0, '10.0.0.1', 21)

    @pytest.mark.asyncio
    async def test_forward_port_public_api(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 55555
        mock_tunnel.forward_local_port = AsyncMock(return_value=mock_listener)
        factory = AsyncMock(return_value=mock_tunnel)

        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        port = await cm.forward_port(8080)
        assert port == 55555
        mock_tunnel.forward_local_port.assert_awaited_once_with('', 0, '10.0.0.1', 8080)

    @pytest.mark.asyncio
    async def test_forward_port_raises_without_tunnel(self):
        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test',
        )
        with pytest.raises(RuntimeError, match="requires a tunnel"):
            await cm.forward_port(8080)


# ---------------------------------------------------------------------------
# TunneledFtpClient data port forwarding
# ---------------------------------------------------------------------------

class TestTunneledFtpClient:

    @pytest.mark.asyncio
    async def test_open_connection_forwards_data_port(self):
        """_open_connection creates an SSH forward for the PASV data port."""
        from otto.host.connections import TunneledFtpClient

        mock_hop = MagicMock()
        mock_hop.forward_port = AsyncMock(return_value=44444)

        client = TunneledFtpClient(hop=mock_hop, dest_host='10.0.0.1')
        client._tunnel_data = True  # Simulate post-connect state

        with patch('aioftp.Client._open_connection', new_callable=AsyncMock) as mock_super:
            mock_super.return_value = (MagicMock(), MagicMock())
            await client._open_connection('10.0.0.1', 7725)

            # Should forward the PASV data port through the hop
            mock_hop.forward_port.assert_awaited_once_with('10.0.0.1', 7725)
            # Should connect to localhost via the forwarded port
            mock_super.assert_awaited_once_with('localhost', 44444)

    @pytest.mark.asyncio
    async def test_open_connection_uses_dest_host_not_pasv_host(self):
        """Even if PASV returns a different IP (e.g. 0.0.0.0), we forward to dest_host."""
        from otto.host.connections import TunneledFtpClient

        mock_hop = MagicMock()
        mock_hop.forward_port = AsyncMock(return_value=55555)

        client = TunneledFtpClient(hop=mock_hop, dest_host='10.0.0.1')
        client._tunnel_data = True  # Simulate post-connect state

        with patch('aioftp.Client._open_connection', new_callable=AsyncMock) as mock_super:
            mock_super.return_value = (MagicMock(), MagicMock())
            # PASV response might return 0.0.0.0 — we ignore that and use dest_host
            await client._open_connection('0.0.0.0', 9999)

            mock_hop.forward_port.assert_awaited_once_with('10.0.0.1', 9999)
            mock_super.assert_awaited_once_with('localhost', 55555)

    @pytest.mark.asyncio
    async def test_open_connection_passthrough_before_connect(self):
        """Before connect(), _open_connection passes through to super (control connection)."""
        from otto.host.connections import TunneledFtpClient

        mock_hop = MagicMock()
        mock_hop.forward_port = AsyncMock()

        client = TunneledFtpClient(hop=mock_hop, dest_host='10.0.0.1')
        # _tunnel_data is False by default (pre-connect)

        with patch('aioftp.Client._open_connection', new_callable=AsyncMock) as mock_super:
            mock_super.return_value = (MagicMock(), MagicMock())
            await client._open_connection('localhost', 54321)

            # Should NOT forward — just pass through to super
            mock_hop.forward_port.assert_not_awaited()
            mock_super.assert_awaited_once_with('localhost', 54321)


# ---------------------------------------------------------------------------
# Close / cleanup
# ---------------------------------------------------------------------------

class TestTunnelCleanup:

    @pytest.mark.asyncio
    async def test_close_cleans_up_tunnel(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_tunnel.close = MagicMock()
        mock_tunnel.wait_closed = AsyncMock()
        factory = AsyncMock(return_value=mock_tunnel)

        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        # Establish the tunnel
        await cm._ensure_tunnel()
        await cm.close()
        mock_tunnel.close.assert_called_once()
        mock_tunnel.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_cleans_up_port_forwards(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 54321
        mock_listener.close = MagicMock()
        mock_tunnel.forward_local_port = AsyncMock(return_value=mock_listener)
        mock_tunnel.close = MagicMock()
        mock_tunnel.wait_closed = AsyncMock()
        factory = AsyncMock(return_value=mock_tunnel)

        cm = ConnectionManager(
            ip='10.0.0.1', creds={'user': 'pass'}, user=None,
            term='ssh', name='test', hop=SshHopTransport(factory),
        )
        await cm.forward_port(8080)
        await cm.close()
        mock_listener.close.assert_called_once()


# ---------------------------------------------------------------------------
# Rebuild connections (CLI --hop support)
# ---------------------------------------------------------------------------

class TestRebuildConnections:

    def test_rebuild_adds_tunnel(self):
        host = RemoteHost(ip='10.0.0.1', ne='target', creds={'user': 'pass'}, log=False)
        assert not host._connections.has_tunnel

        host.hop = 'some_hop'
        host.rebuild_connections()
        assert host._connections.has_tunnel

    def test_rebuild_removes_tunnel(self):
        host = RemoteHost(
            ip='10.0.0.1', ne='target', creds={'user': 'pass'},
            hop='some_hop', log=False,
        )
        assert host._connections.has_tunnel

        host.hop = None
        host.rebuild_connections()
        assert not host._connections.has_tunnel


# ---------------------------------------------------------------------------
# Netcat hop guards and support
# ---------------------------------------------------------------------------

class TestNetcatGetThroughHop:

    @pytest.mark.asyncio
    async def test_nc_get_uses_forward_port_when_tunneled(self, tmp_path):
        """Netcat GET through a hop uses SSH port forwarding (reversed-listener approach)."""
        from pathlib import Path

        from otto.host.transfer import FileTransfer
        from otto.utils import CommandStatus, Status

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = True
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'ssh'
        mock_connections._name = 'test'
        mock_connections.forward_port = AsyncMock(return_value=44444)

        # Monitor session returns file size via run.
        mock_session = AsyncMock()
        from otto.host import RunResult
        mock_session.run = AsyncMock(return_value=RunResult(
            status=Status.Success,
            statuses=[CommandStatus(
                command='stat -c %s /remote/a.txt', output='9\n',
                status=Status.Success, retcode=0,
            )],
        ))

        mock_exec = AsyncMock(side_effect=[
            # _find_free_port
            CommandStatus(command='python3 ...', output='55555\n', status=Status.Success, retcode=0),
            # nc -l listen command (sends file data)
            CommandStatus(command='nc -l ...', output='', status=Status.Success, retcode=0),
        ])

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='python',
                port_cmd=None,
                listener_check='ss',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(return_value=mock_session),
            exec_cmd=mock_exec,
        )

        with (
            patch.object(ft, '_wait_for_remote_listener', new_callable=AsyncMock) as mock_wait,
            patch('otto.host.transfer._connect_with_retry', new_callable=AsyncMock) as mock_connect,
        ):
            # Mock reader that returns file data then EOF.
            mock_reader = AsyncMock()
            mock_reader.read = AsyncMock(side_effect=[b'test data', b''])
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_connect.return_value = (mock_reader, mock_writer)

            dst = tmp_path
            status, err = await ft.get_files([Path('/remote/a.txt')], dst, show_progress=False)

            assert status == Status.Success, f"Unexpected error: {err}"
            # Verify port forwarding was used.
            mock_connections.forward_port.assert_awaited_once_with(55555)
            # Verify connection went through the forwarded port.
            mock_connect.assert_awaited_once()
            call_args = mock_connect.call_args
            assert call_args[0][0] == 'localhost'
            assert call_args[0][1] == 44444
            # Verify the downloaded file was written.
            assert (dst / 'a.txt').read_bytes() == b'test data'

    @pytest.mark.asyncio
    async def test_nc_get_without_tunnel_uses_start_server(self, tmp_path):
        """Without a hop, netcat GET uses asyncio.start_server (remote connects back)."""
        from pathlib import Path

        from otto.host.transfer import FileTransfer
        from otto.utils import CommandStatus, Status

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'ssh'
        mock_connections._name = 'test'

        # Monitor session returns file size.
        mock_session = AsyncMock()
        from otto.host import RunResult
        mock_session.run = AsyncMock(return_value=RunResult(
            status=Status.Success,
            statuses=[CommandStatus(
                command='stat -c %s /remote/a.txt', output='9\n',
                status=Status.Success, retcode=0,
            )],
        ))

        # exec_cmd handles the nc -N send command.
        mock_exec = AsyncMock(return_value=CommandStatus(
            command='nc -N ...', output='', status=Status.Success, retcode=0,
        ))

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='python',
                port_cmd=None,
                listener_check='ss',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(return_value=mock_session),
            exec_cmd=mock_exec,
        )

        with patch('asyncio.start_server', new_callable=AsyncMock) as mock_start_server:
            # Simulate the server accepting a connection that sends data.
            mock_socket = MagicMock()
            mock_socket.getsockname.return_value = ('0.0.0.0', 12345)
            mock_server = AsyncMock()
            mock_server.sockets = [mock_socket]
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            # The exec_cmd for nc -N triggers the _on_connect callback.
            # We need to capture the callback and invoke it manually.
            async def fake_start_server(callback, host, port):
                # Simulate a connection by calling the callback.
                mock_reader = AsyncMock()
                mock_reader.read = AsyncMock(side_effect=[b'test data', b''])
                mock_writer = MagicMock()
                mock_writer.close = MagicMock()
                # Schedule the callback to run after start_server returns.
                async def _run_callback():
                    await callback(mock_reader, mock_writer)
                asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(_run_callback()))
                return mock_server

            mock_start_server.side_effect = fake_start_server

            status, err = await ft.get_files([Path('/remote/a.txt')], tmp_path, show_progress=False)

            assert status == Status.Success, f"Unexpected error: {err}"
            # Verify start_server was called (direct path), not forward_port.
            mock_start_server.assert_awaited_once()
            mock_connections.forward_port.assert_not_awaited()


class TestNetcatPutThroughHop:

    @pytest.mark.asyncio
    async def test_nc_put_uses_forward_port_when_tunneled(self):
        """Netcat PUT through a hop uses SSH port forwarding to reach the remote listener."""
        from otto.host.transfer import FileTransfer
        from pathlib import Path
        import tempfile

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = True
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'ssh'
        mock_connections._name = 'test'
        mock_connections.forward_port = AsyncMock(return_value=44444)

        # Simulate the remote nc command succeeding
        from otto.utils import CommandStatus, Status
        mock_exec = AsyncMock(side_effect=[
            # _find_free_port
            CommandStatus(command='python3 ...', output='55555\n', status=Status.Success, retcode=0),
            # nc -l listen command
            CommandStatus(command='nc -l ...', output='', status=Status.Success, retcode=0),
        ])

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='python',
                port_cmd=None,
                listener_check='ss',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(),
            exec_cmd=mock_exec,
        )

        # Create a small temp file to transfer
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmp:
            tmp.write(b'test data')
            tmp_path = Path(tmp.name)

        try:
            with (
                patch.object(ft, '_wait_for_remote_listener', new_callable=AsyncMock),
                patch('otto.host.transfer._connect_with_retry', new_callable=AsyncMock) as mock_connect,
            ):
                mock_writer = MagicMock()
                mock_writer.write = MagicMock()
                mock_writer.drain = AsyncMock()
                mock_writer.close = MagicMock()
                mock_writer.wait_closed = AsyncMock()
                mock_connect.return_value = (MagicMock(), mock_writer)

                status, err = await ft.put_files([tmp_path], Path('/tmp'), show_progress=False)

                # Verify port forwarding was used
                mock_connections.forward_port.assert_awaited_once_with(55555)
                # Verify connection went to localhost via the forwarded port
                mock_connect.assert_awaited_once()
                call_args = mock_connect.call_args
                assert call_args[0][0] == 'localhost'
                assert call_args[0][1] == 44444
        finally:
            tmp_path.unlink()

    @pytest.mark.asyncio
    async def test_nc_put_without_tunnel_connects_directly(self):
        """Without a hop, netcat PUT connects directly to the target IP."""
        from otto.host.transfer import FileTransfer
        from pathlib import Path
        import tempfile

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = '10.0.0.1'
        mock_connections.term = 'ssh'
        mock_connections._name = 'test'

        from otto.utils import CommandStatus, Status
        mock_exec = AsyncMock(side_effect=[
            CommandStatus(command='python3 ...', output='55555\n', status=Status.Success, retcode=0),
            CommandStatus(command='nc -l ...', output='', status=Status.Success, retcode=0),
        ])

        ft = FileTransfer(
            connections=mock_connections,
            name='test',
            transfer='nc',
            nc_options=NcOptions(
                exec_name='nc',
                port=9000,
                port_strategy='python',
                port_cmd=None,
                listener_check='ss',
                listener_cmd=None,
            ),
            scp_options=ScpOptions(),
            get_local_ip=lambda: '127.0.0.1',
            open_session=AsyncMock(),
            exec_cmd=mock_exec,
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmp:
            tmp.write(b'test data')
            tmp_path = Path(tmp.name)

        try:
            with patch('otto.host.transfer._connect_with_retry', new_callable=AsyncMock) as mock_connect:
                mock_writer = MagicMock()
                mock_writer.write = MagicMock()
                mock_writer.drain = AsyncMock()
                mock_writer.close = MagicMock()
                mock_writer.wait_closed = AsyncMock()
                mock_connect.return_value = (MagicMock(), mock_writer)

                status, err = await ft.put_files([tmp_path], Path('/tmp'), show_progress=False)

                # Verify direct connection (no forward_port called)
                mock_connections.forward_port.assert_not_awaited()
                call_args = mock_connect.call_args
                assert call_args[0][0] == '10.0.0.1'
                assert call_args[0][1] == 55555
        finally:
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:

    @pytest.mark.asyncio
    async def test_cycle_detection_in_tunnel_factory(self):
        """Verify that circular hop references are detected."""
        host_a = RemoteHost(ip='10.0.0.1', ne='hostA', creds={'user': 'pass'}, hop='hostb', log=False)
        host_b = RemoteHost(ip='10.0.0.2', ne='hostB', creds={'user': 'pass'}, hop='hosta', log=False)

        # Patch before _build_hop_transport so the deferred import picks up the mock
        with patch('otto.configmodule.get_host') as mock_get_host:
            def _get(host_id):
                return {'hosta': host_a, 'hostb': host_b}[host_id]
            mock_get_host.side_effect = _get

            transport = host_a._build_hop_transport()
            with pytest.raises(ValueError, match="Circular hop detected"):
                await transport.get_tunnel()
