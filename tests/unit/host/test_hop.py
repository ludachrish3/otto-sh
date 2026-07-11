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
from otto.host.login_proxy import Cred
from otto.host.options import NcOptions
from otto.host.transport import SshHopTransport
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


def _cs(
    *, command: str = "", output: str = "", status: Status = Status.Success, retcode: int = 0
) -> CommandResult:
    """Build a :class:`~otto.result.CommandResult` for the netcat-over-hop fakes.

    The nc backend reads command output from ``.value``; this keeps the old
    ``command=/output=/status=/retcode=`` keyword call shape, mapping ``output``
    onto ``value``.
    """
    return CommandResult(command=command, value=output, status=status, retcode=retcode)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def host() -> UnixHost:
    """A simple host with no hop."""
    return UnixHost(
        ip="10.0.0.1",
        element="target",
        creds=[Cred(login="user", password="pass")],
        log=LogMode.QUIET,
    )


@pytest.fixture
def hop_host() -> UnixHost:
    """A host configured with a hop."""
    return UnixHost(
        ip="10.0.0.2",
        element="target",
        creds=[Cred(login="user", password="pass")],
        hop="jumpbox",
        log=LogMode.QUIET,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class TestHopField:
    def test_default_hop_is_none(self, host: UnixHost):
        assert host.hop is None

    def test_hop_field_set(self, hop_host: UnixHost):
        assert hop_host.hop == "jumpbox"

    def test_no_tunnel_when_no_hop(self, host: UnixHost):
        assert not host._connections.has_tunnel

    def test_has_tunnel_when_hop_set(self, hop_host: UnixHost):
        assert hop_host._connections.has_tunnel


# ---------------------------------------------------------------------------
# ConnectionManager tunnel wiring
# ---------------------------------------------------------------------------


class TestConnectionManagerTunnel:
    def test_no_tunnel_factory_by_default(self):
        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
        )
        assert not cm.has_tunnel

    def test_tunnel_factory_stored(self):
        factory = AsyncMock(return_value=MagicMock(spec=SSHClientConnection))
        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
        )
        assert cm.has_tunnel

    @pytest.mark.asyncio
    async def test_ensure_tunnel_calls_factory_once(self):
        mock_conn = MagicMock(spec=SSHClientConnection)
        factory = AsyncMock(return_value=mock_conn)
        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
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
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
        )
        with patch("otto.host.connections.ssh_connect", new_callable=AsyncMock) as mock_connect:
            mock_ssh = MagicMock(spec=SSHClientConnection)
            mock_connect.return_value = mock_ssh
            result = await cm.ssh()
            mock_connect.assert_awaited_once_with(
                "10.0.0.1",
                username="user",
                password="pass",
                tunnel=mock_tunnel,
                port=22,
                known_hosts=None,
            )
            assert result is mock_ssh

    @pytest.mark.asyncio
    async def test_ssh_no_tunnel_when_not_configured(self):
        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
        )
        with patch("otto.host.connections.ssh_connect", new_callable=AsyncMock) as mock_connect:
            mock_ssh = MagicMock(spec=SSHClientConnection)
            mock_connect.return_value = mock_ssh
            await cm.ssh()
            mock_connect.assert_awaited_once_with(
                "10.0.0.1",
                username="user",
                password="pass",
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
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="telnet",
            name="test",
            hop=SshHopTransport(factory),
        )
        with patch("otto.host.connections.TelnetClient") as MockTelnet:  # noqa: N806 — CapWords for a class mock
            mock_tc = MagicMock()
            mock_tc.connect = AsyncMock()
            MockTelnet.return_value = mock_tc
            await cm.telnet()
            MockTelnet.assert_called_once()
            call_args = MockTelnet.call_args
            assert call_args.args == ("localhost",)
            assert call_args.kwargs["user"] == "user"
            assert call_args.kwargs["password"] == "pass"
            assert call_args.kwargs["connect_port"] == 54321
            mock_tunnel.forward_local_port.assert_awaited_once_with("", 0, "10.0.0.1", 23)

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
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=hop,
        )
        with (
            patch.object(TunneledFtpClient, "connect", new_callable=AsyncMock) as mock_connect,
            patch.object(TunneledFtpClient, "login", new_callable=AsyncMock),
        ):
            result = await cm.ftp()
            assert isinstance(result, TunneledFtpClient)
            mock_connect.assert_awaited_once_with("localhost", 54322)
            mock_tunnel.forward_local_port.assert_awaited_once_with("", 0, "10.0.0.1", 21)

    @pytest.mark.asyncio
    async def test_forward_port_public_api(self):
        mock_tunnel = MagicMock(spec=SSHClientConnection)
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 55555
        mock_tunnel.forward_local_port = AsyncMock(return_value=mock_listener)
        factory = AsyncMock(return_value=mock_tunnel)

        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
        )
        port = await cm.forward_port(8080)
        assert port == 55555
        mock_tunnel.forward_local_port.assert_awaited_once_with("", 0, "10.0.0.1", 8080)

    @pytest.mark.asyncio
    async def test_forward_port_raises_without_tunnel(self):
        cm = ConnectionManager(
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
        )
        with pytest.raises(RuntimeError, match="requires a tunnel"):
            await cm.forward_port(8080)


# ---------------------------------------------------------------------------
# TunneledFtpClient data port forwarding
# ---------------------------------------------------------------------------


class TestTunneledFtpClient:
    @pytest.mark.asyncio
    async def test_open_connection_forwards_data_port(self):
        """_open_connection opens a direct SSH channel to the PASV data port."""
        from otto.host.connections import TunneledFtpClient

        mock_conn = MagicMock()
        mock_conn.open_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_hop = MagicMock()
        mock_hop.get_tunnel = AsyncMock(return_value=mock_conn)

        client = TunneledFtpClient(hop=mock_hop, dest_host="10.0.0.1")
        client._tunnel_data = True  # Simulate post-connect state

        await client._open_connection("10.0.0.1", 7725)

        mock_hop.get_tunnel.assert_awaited_once()
        # Should open a direct channel to the PASV data port via the tunnel —
        # no local listener, no proxied socket pair.
        mock_conn.open_connection.assert_awaited_once_with("10.0.0.1", 7725)

    @pytest.mark.asyncio
    async def test_open_connection_uses_dest_host_not_pasv_host(self):
        """Even if PASV returns a different IP (e.g. 0.0.0.0), we connect to dest_host."""
        from otto.host.connections import TunneledFtpClient

        mock_conn = MagicMock()
        mock_conn.open_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_hop = MagicMock()
        mock_hop.get_tunnel = AsyncMock(return_value=mock_conn)

        client = TunneledFtpClient(hop=mock_hop, dest_host="10.0.0.1")
        client._tunnel_data = True  # Simulate post-connect state

        # PASV response might return 0.0.0.0 — we ignore that and use dest_host
        await client._open_connection("0.0.0.0", 9999)

        mock_conn.open_connection.assert_awaited_once_with("10.0.0.1", 9999)

    @pytest.mark.asyncio
    async def test_open_connection_passthrough_before_connect(self):
        """Before connect(), _open_connection passes through to super (control connection)."""
        from otto.host.connections import TunneledFtpClient

        mock_hop = MagicMock()
        mock_hop.forward_port = AsyncMock()

        client = TunneledFtpClient(hop=mock_hop, dest_host="10.0.0.1")
        # _tunnel_data is False by default (pre-connect)

        with patch("aioftp.Client._open_connection", new_callable=AsyncMock) as mock_super:
            mock_super.return_value = (MagicMock(), MagicMock())
            await client._open_connection("localhost", 54321)

            # Should NOT forward — just pass through to super
            mock_hop.forward_port.assert_not_awaited()
            mock_super.assert_awaited_once_with("localhost", 54321)


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
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
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
            ip="10.0.0.1",
            creds=[Cred(login="user", password="pass")],
            user=None,
            term="ssh",
            name="test",
            hop=SshHopTransport(factory),
        )
        await cm.forward_port(8080)
        await cm.close()
        mock_listener.close.assert_called_once()


# ---------------------------------------------------------------------------
# Rebuild connections (CLI --hop support)
# ---------------------------------------------------------------------------


class TestRebuildConnections:
    def test_rebuild_adds_tunnel(self):
        host = UnixHost(
            ip="10.0.0.1",
            element="target",
            creds=[Cred(login="user", password="pass")],
            log=LogMode.QUIET,
        )
        assert not host._connections.has_tunnel

        host.hop = "some_hop"
        host.rebuild_connections()
        assert host._connections.has_tunnel

    def test_rebuild_removes_tunnel(self):
        host = UnixHost(
            ip="10.0.0.1",
            element="target",
            creds=[Cred(login="user", password="pass")],
            hop="some_hop",
            log=LogMode.QUIET,
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

        from otto.host.transfer import NcFileTransfer

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = True
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "ssh"
        mock_connections._name = "test"
        mock_connections.forward_port = AsyncMock(return_value=44444)

        # exec_cmd handles every control + transfer command: file-size stat,
        # port-find, and the nc listener.
        async def mock_exec(cmd: str, *a, **kw) -> CommandResult:
            if cmd.startswith("stat -c %s"):
                output = "9\n"
            elif "nc " in cmd:
                output = ""
            else:  # port-find
                output = "55555\n"
            return _cs(command=cmd, output=output, status=Status.Success, retcode=0)

        mock_exec = AsyncMock(side_effect=mock_exec)

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="python",
                port_cmd=None,
                listener_check="ss",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=mock_exec,
        )

        with (
            patch.object(ft, "_wait_for_remote_listener", new_callable=AsyncMock),
            patch(
                "otto.host.transfer.nc._connect_with_retry", new_callable=AsyncMock
            ) as mock_connect,
        ):
            # Mock reader that returns file data then EOF.
            mock_reader = AsyncMock()
            mock_reader.read = AsyncMock(side_effect=[b"test data", b""])
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_connect.return_value = (mock_reader, mock_writer)

            dst = tmp_path
            res = await ft.get_files([Path("/remote/a.txt")], dst, show_progress=False)
            status, err = res.status, res.msg

            assert status == Status.Success, f"Unexpected error: {err}"
            # Verify port forwarding was used.
            mock_connections.forward_port.assert_awaited_once_with(55555)
            # Verify connection went through the forwarded port.
            mock_connect.assert_awaited_once()
            call_args = mock_connect.call_args
            assert call_args[0][0] == "localhost"
            assert call_args[0][1] == 44444
            # Verify the downloaded file was written.
            assert (dst / "a.txt").read_bytes() == b"test data"

    @pytest.mark.asyncio
    async def test_nc_get_without_tunnel_uses_start_server(self, tmp_path):
        """Without a hop, netcat GET uses asyncio.start_server (remote connects back)."""
        from pathlib import Path

        from otto.host.transfer import NcFileTransfer

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "ssh"
        mock_connections._name = "test"

        # exec_cmd handles the file-size stat and the nc -N send command.
        async def mock_exec(cmd: str, *a, **kw) -> CommandResult:
            output = "9\n" if cmd.startswith("stat -c %s") else ""
            return _cs(command=cmd, output=output, status=Status.Success, retcode=0)

        mock_exec = AsyncMock(side_effect=mock_exec)

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="python",
                port_cmd=None,
                listener_check="ss",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=mock_exec,
        )

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start_server:
            # Simulate the server accepting a connection that sends data.
            mock_socket = MagicMock()
            mock_socket.getsockname.return_value = ("0.0.0.0", 12345)
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
                mock_reader.read = AsyncMock(side_effect=[b"test data", b""])
                mock_writer = MagicMock()
                mock_writer.close = MagicMock()

                # Schedule the callback to run after start_server returns.
                async def _run_callback():
                    await callback(mock_reader, mock_writer)

                asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(_run_callback()))
                return mock_server

            mock_start_server.side_effect = fake_start_server

            res = await ft.get_files([Path("/remote/a.txt")], tmp_path, show_progress=False)
            status, err = res.status, res.msg

            assert status == Status.Success, f"Unexpected error: {err}"
            # Verify start_server was called (direct path), not forward_port.
            mock_start_server.assert_awaited_once()
            mock_connections.forward_port.assert_not_awaited()


class TestNetcatPutThroughHop:
    @pytest.mark.asyncio
    async def test_nc_put_uses_forward_port_when_tunneled(self):
        """Netcat PUT through a hop uses SSH port forwarding to reach the remote listener."""
        import tempfile
        from pathlib import Path

        from otto.host.transfer import NcFileTransfer

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = True
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "ssh"
        mock_connections._name = "test"
        mock_connections.forward_port = AsyncMock(return_value=44444)

        # Simulate the remote nc command succeeding
        mock_exec = AsyncMock(
            side_effect=[
                # _find_free_port
                _cs(command="python3 ...", output="55555\n", status=Status.Success, retcode=0),
                # nc -l listen command
                _cs(command="nc -l ...", output="", status=Status.Success, retcode=0),
            ]
        )

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="python",
                port_cmd=None,
                listener_check="ss",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=mock_exec,
        )

        # Create a small temp file to transfer
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp.write(b"test data")
            tmp_path = Path(tmp.name)

        try:
            with (
                patch.object(ft, "_wait_for_remote_listener", new_callable=AsyncMock),
                patch(
                    "otto.host.transfer.nc._connect_with_retry", new_callable=AsyncMock
                ) as mock_connect,
            ):
                mock_writer = MagicMock()
                mock_writer.write = MagicMock()
                mock_writer.drain = AsyncMock()
                mock_writer.close = MagicMock()
                mock_writer.wait_closed = AsyncMock()
                mock_connect.return_value = (MagicMock(), mock_writer)

                await ft.put_files([tmp_path], Path("/tmp"), show_progress=False)

                # Verify port forwarding was used
                mock_connections.forward_port.assert_awaited_once_with(55555)
                # Verify connection went to localhost via the forwarded port
                mock_connect.assert_awaited_once()
                call_args = mock_connect.call_args
                assert call_args[0][0] == "localhost"
                assert call_args[0][1] == 44444
        finally:
            tmp_path.unlink()

    @pytest.mark.asyncio
    async def test_nc_put_without_tunnel_connects_directly(self):
        """Without a hop, netcat PUT connects directly to the target IP."""
        import tempfile
        from pathlib import Path

        from otto.host.transfer import NcFileTransfer

        mock_connections = MagicMock(spec=ConnectionManager)
        mock_connections.has_tunnel = False
        mock_connections.ip = "10.0.0.1"
        mock_connections.term = "ssh"
        mock_connections._name = "test"

        mock_exec = AsyncMock(
            side_effect=[
                _cs(command="python3 ...", output="55555\n", status=Status.Success, retcode=0),
                _cs(command="nc -l ...", output="", status=Status.Success, retcode=0),
            ]
        )

        ft = NcFileTransfer(
            connections=mock_connections,
            name="test",
            transfer="nc",
            nc_options=NcOptions(
                exec_name="nc",
                port=9000,
                port_strategy="python",
                port_cmd=None,
                listener_check="ss",
                listener_cmd=None,
            ),
            get_local_ip=lambda: "127.0.0.1",
            exec_cmd=mock_exec,
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp.write(b"test data")
            tmp_path = Path(tmp.name)

        try:
            with patch(
                "otto.host.transfer.nc._connect_with_retry", new_callable=AsyncMock
            ) as mock_connect:
                mock_writer = MagicMock()
                mock_writer.write = MagicMock()
                mock_writer.drain = AsyncMock()
                mock_writer.close = MagicMock()
                mock_writer.wait_closed = AsyncMock()
                mock_connect.return_value = (MagicMock(), mock_writer)

                await ft.put_files([tmp_path], Path("/tmp"), show_progress=False)

                # Verify direct connection (no forward_port called)
                mock_connections.forward_port.assert_not_awaited()
                call_args = mock_connect.call_args
                assert call_args[0][0] == "10.0.0.1"
                assert call_args[0][1] == 55555
        finally:
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_cycle_detection_in_tunnel_factory(self):
        """Verify that circular hop references are detected.

        Uses the transport built during __post_init__ (when _lab is None),
        then wires into a Lab — this is the production ordering and confirms
        _create_tunnel reads self._lab lazily rather than at build time.
        """
        from otto.config.lab import Lab

        host_a = UnixHost(
            ip="10.0.0.1",
            element="hostA",
            creds=[Cred(login="user", password="pass")],
            hop="hostb",
            log=LogMode.QUIET,
        )
        host_b = UnixHost(
            ip="10.0.0.2",
            element="hostB",
            creds=[Cred(login="user", password="pass")],
            hop="hosta",
            log=LogMode.QUIET,
        )

        # Capture the transport built at construction time (with _lab=None).
        transport_a = host_a._connections._hop

        # Wire both hosts into a lab AFTER construction (production ordering).
        lab = Lab(name="cycle_test")
        lab.add_host(host_a)
        lab.add_host(host_b)

        # The transport was built before wiring; it must still detect the cycle.
        with pytest.raises(ValueError, match="Circular hop detected"):
            await transport_a.get_tunnel()

    @pytest.mark.asyncio
    async def test_hop_transport_reads_lab_lazily_after_addhost(self):
        """The transport built during __post_init__ (when _lab is None) must still
        resolve the hop once the host is added to a Lab — i.e. _create_tunnel reads
        self._lab lazily, not at build time.

        With the eager bug (lab = self._lab at build time), the closure holds
        None forever and get_tunnel() raises "no lab back-reference".
        With the lazy fix (lab = self._lab inside _create_tunnel), it resolves
        correctly after add_host wires self._lab.
        """
        from otto.config.lab import Lab

        mock_ssh_conn = MagicMock(spec=SSHClientConnection)

        # Patch asyncssh.connect BEFORE construction so the closure captures the mock.
        # _build_hop_transport does `from asyncssh import connect as _ssh_connect`
        # at call time — patching asyncssh.connect makes the import pick up the mock.
        with patch("asyncssh.connect", AsyncMock(return_value=mock_ssh_conn)):
            jumpbox = UnixHost(
                ip="10.10.0.1",
                element="jumpbox",
                creds=[Cred(login="admin", password="secret")],
                log=LogMode.QUIET,
            )
            target = UnixHost(
                ip="10.10.0.2",
                element="target",
                creds=[Cred(login="user", password="pass")],
                hop="jumpbox",
                log=LogMode.QUIET,
            )

            # Capture the transport built at __post_init__ time — _lab was None then.
            transport = target._connections._hop
            assert transport is not None, "hop transport should be wired at construction"

            # Wire both hosts into a lab AFTER construction (production ordering).
            lab = Lab(name="lazy_test")
            lab.add_host(jumpbox)
            lab.add_host(target)

            # Drive the transport — must NOT raise "no lab back-reference" if lazy.
            result = await transport.get_tunnel()

        # If lazy read works, we resolved the hop and got back the mock SSH connection.
        assert result is mock_ssh_conn


# ---------------------------------------------------------------------------
# Standalone host: hop resolution via active OttoContext (FD-model)
# ---------------------------------------------------------------------------


class TestStandaloneHostHopResolution:
    @pytest.mark.asyncio
    async def test_standalone_host_resolves_hop_from_active_context_lab(self):
        """A host constructed standalone (not add_host'd) with a hop must resolve the
        hop target from the active OttoContext's lab (FD-model), not raise.

        Regression test for: _create_tunnel raising "no lab back-reference" when
        self._lab is None because the host was never added to a Lab.
        """
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        mock_ssh_conn = MagicMock(spec=SSHClientConnection)

        with patch("asyncssh.connect", AsyncMock(return_value=mock_ssh_conn)):
            # Build the hop TARGET and add it to a Lab.
            jumpbox = UnixHost(
                ip="10.20.0.1",
                element="jumpbox",
                creds=[Cred(login="admin", password="secret")],
                log=LogMode.QUIET,
            )
            lab = Lab(name="fd_model_test")
            lab.add_host(jumpbox)

            # Install the lab in an active OttoContext so try_get_context() returns it.
            ctx = OttoContext(lab=lab)
            token = set_context(ctx)
            try:
                # Build the host-under-test STANDALONE — do NOT add_host it.
                # Its _lab remains None; the hop target must come from the active context.
                standalone = UnixHost(
                    ip="10.20.0.2",
                    element="target",
                    creds=[Cred(login="user", password="pass")],
                    hop="jumpbox",
                    log=LogMode.QUIET,
                )
                assert standalone._lab is None, "standalone host must have no lab back-reference"

                transport = standalone._connections._hop
                assert transport is not None, "hop transport should be wired at construction"

                # Drive the tunnel — must NOT raise "no lab back-reference" or
                # "cannot resolve hop"; it should reach the mocked asyncssh.connect.
                result = await transport.get_tunnel()
            finally:
                reset_context(token)

        assert result is mock_ssh_conn, (
            "Standalone host should have resolved jumpbox from the active context's lab"
        )
