"""
Tests for RemoteHost.

Integration tests require the Vagrant VMs to be running:
    vagrant up test1 test2

Run integration tests:
    pytest -m integration

Skip integration tests:
    pytest -m "not integration"
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from otto.host import HostSession, RemoteHost, RunResult
from otto.host.session import ShellSession
from otto.utils import CommandStatus, Status
from tests.unit.conftest import host_data, make_host
from tests.unit.host._transfer_retry import transfer_with_retry


@pytest.fixture
def host() -> RemoteHost:
    """Bare RemoteHost, no connections established."""
    return RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:

    def test_default_values(self, host: RemoteHost):
        assert host.ip == '10.0.0.1'
        assert host.ne == 'box'
        assert host.creds == {'user': 'pass'}
        assert host.term == 'ssh'
        assert host.transfer == 'scp'
        assert host.nc_options.exec_name == 'nc'
        assert host.nc_options.port == 9000
        assert host.is_virtual is False
        assert host.hop is None
        assert host.resources == set()
        assert host._connections._ssh_conn is None
        assert host._connections._sftp_conn is None
        assert host._connections._ftp_conn is None
        assert host._connections._telnet_conn is None



# ---------------------------------------------------------------------------
# ID and name generation
# ---------------------------------------------------------------------------

class TestIdAndNameGeneration:

    @pytest.mark.asyncio
    async def test_id_no_board(self):
        h = RemoteHost(ip='10.0.0.1', ne='Orange', creds={'u': 'p'}, log=False)
        assert h.id == 'orange'
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board(self):
        h = RemoteHost(ip='10.0.0.1', ne='Orange', board='Seed', creds={'u': 'p'}, log=False)
        assert h.id == 'orange_seed'
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board_and_slot(self):
        h = RemoteHost(ip='10.0.0.1', ne='Orange', board='Seed', slot=0, creds={'u': 'p'}, log=False)
        assert h.id == 'orange_seed0'
        await h.close()

    @pytest.mark.asyncio
    async def test_name_no_board(self):
        h = RemoteHost(ip='10.0.0.1', ne='orange', creds={'u': 'p'}, log=False)
        assert h.name == 'orange'
        await h.close()

    @pytest.mark.asyncio
    async def test_name_with_board(self):
        h = RemoteHost(ip='10.0.0.1', ne='orange', board='seed', creds={'u': 'p'}, log=False)
        assert h.name == 'orange seed'
        await h.close()

    @pytest.mark.asyncio
    async def test_name_override(self):
        h = RemoteHost(ip='10.0.0.1', ne='orange', creds={'u': 'p'}, name='custom', log=False)
        assert h.name == 'custom'
        await h.close()


# ---------------------------------------------------------------------------
# _creds
# ---------------------------------------------------------------------------

class TestCreds:

    def test_returns_first_pair(self, host: RemoteHost):
        user, password = host._creds
        assert user == 'user'
        assert password == 'pass'

    @pytest.mark.asyncio
    async def test_returns_first_pair_from_multiple_creds(self):
        h = RemoteHost(ip='10.0.0.1', ne='box',
                       creds={'vagrant': 'vagrant', 'test': 'Password1'},
                       log=False)
        user, password = h._creds
        assert user == 'vagrant'
        assert password == 'vagrant'
        await h.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose:

    @pytest.mark.asyncio
    async def test_close_when_not_connected_is_safe(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, log=False)
        await h.close()

    @pytest.mark.asyncio
    async def test_close_disconnects_ssh(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, log=False)
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        h._connections._ssh_conn = mock_conn
        await h.close()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_called_once()
        assert h._connections._ssh_conn is None


# ---------------------------------------------------------------------------
# run() — list form
# ---------------------------------------------------------------------------

class TestRunList:

    @pytest.mark.asyncio
    async def test_single_element_list(self, host: RemoteHost):
        ok = CommandStatus('echo hi', 'hi', Status.Success, 0)
        with patch.object(host, '_run_one', new_callable=AsyncMock, return_value=ok):
            result = await host.run(['echo hi'])
        assert len(result.statuses) == 1
        assert result.statuses[0] == ok
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_accepts_list_of_commands(self, host: RemoteHost):
        r1 = CommandStatus('ls', '', Status.Success, 0)
        r2 = CommandStatus('pwd', '/home', Status.Success, 0)
        with patch.object(host, '_run_one', new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(['ls', 'pwd'])
        assert len(result.statuses) == 2

    @pytest.mark.asyncio
    async def test_overall_success_when_all_pass(self, host: RemoteHost):
        r1 = CommandStatus('ls', '', Status.Success, 0)
        r2 = CommandStatus('pwd', '', Status.Success, 0)
        with patch.object(host, '_run_one', new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(['ls', 'pwd'])
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_overall_failed_when_any_fails(self, host: RemoteHost):
        r1 = CommandStatus('ls', '', Status.Success, 0)
        r2 = CommandStatus('badcmd', '', Status.Failed, 127)
        with patch.object(host, '_run_one', new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(['ls', 'badcmd'])
        assert result.status == Status.Failed


# ---------------------------------------------------------------------------
# Command execution (via session)
# ---------------------------------------------------------------------------

class TestCommandExecution:

    def _mock_session(self, result: CommandStatus) -> MagicMock:
        """Create a mock ShellSession that returns a fixed CommandStatus."""
        session = MagicMock(spec=ShellSession)
        session.alive = True
        session.run_cmd = AsyncMock(return_value=result)
        session.close = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_success(self, host: RemoteHost):
        ok = CommandStatus('echo hello', 'hello', Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run('echo hello')).only
        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.output == 'hello'

    @pytest.mark.asyncio
    async def test_failure(self, host: RemoteHost):
        fail = CommandStatus('badcmd', 'command not found', Status.Failed, 127)
        host._session_mgr._session = self._mock_session(fail)
        result = (await host.run('badcmd')).only
        assert result.status == Status.Failed
        assert result.retcode == 127

    @pytest.mark.asyncio
    async def test_connection_failure_propagates(self, host: RemoteHost):
        with patch.object(host._connections, 'ssh', new_callable=AsyncMock,
                          side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                await host.run('echo hi')

    @pytest.mark.asyncio
    async def test_command_recorded(self, host: RemoteHost):
        ok = CommandStatus('echo out', 'out', Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run('echo out')).only
        assert result.command == 'echo out'

    @pytest.mark.asyncio
    async def test_expects_forwarded_to_session(self, host: RemoteHost):
        ok = CommandStatus('sudo ls', '', Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        expects = [(r"Password:", "secret\n")]
        await host.run('sudo ls', expects=expects)
        host._session_mgr._session.run_cmd.assert_called_once_with(
            'sudo ls', expects=expects, timeout=None,
        )

    @pytest.mark.asyncio
    async def test_timeout_forwarded_to_session(self, host: RemoteHost):
        ok = CommandStatus('sleep 1', '', Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        await host.run('sleep 1', timeout=30.0)
        host._session_mgr._session.run_cmd.assert_called_once_with(
            'sleep 1', expects=None, timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_telnet_connection_failure_propagates(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       term='telnet', log=False)
        with patch.object(h._connections, 'telnet', new_callable=AsyncMock,
                          side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                await h.run('echo hi')
        await h.close()


# ---------------------------------------------------------------------------
# oneshot() — concurrent-safe command execution
# ---------------------------------------------------------------------------

class TestOneshot:

    def _mock_ssh_conn(self) -> MagicMock:
        conn = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn

    def _mock_ssh_process(self, lines: list[str], exit_status: int = 0) -> MagicMock:
        """Create a mock SSH process with async-iterable stdout."""
        process = MagicMock()

        class AsyncLineIter:
            def __init__(self, data: list[str]):
                self._data = iter(data)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._data)
                except StopIteration:
                    raise StopAsyncIteration

        process.stdout = AsyncLineIter(lines)
        mock_wait_result = MagicMock()
        mock_wait_result.exit_status = exit_status
        process.wait = AsyncMock(return_value=mock_wait_result)
        process.terminate = MagicMock()
        return process

    @pytest.mark.asyncio
    async def test_oneshot_ssh_success(self, host: RemoteHost):
        process = self._mock_ssh_process(['hello\n'])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.oneshot('echo hello')

        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.output == 'hello'

    @pytest.mark.asyncio
    async def test_oneshot_ssh_nonzero_exit(self, host: RemoteHost):
        process = self._mock_ssh_process(['not found\n'], exit_status=1)
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.oneshot('badcmd')

        assert result.status == Status.Failed
        assert result.retcode == 1

    @pytest.mark.asyncio
    async def test_oneshot_telnet_success(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       term='telnet', log=False)
        expected = CommandStatus('echo hello', 'hello', Status.Success, 0)

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.reader = MagicMock()
        mock_client.writer = MagicMock()
        mock_client.close = AsyncMock()

        mock_session = MagicMock()
        mock_session.run_cmd = AsyncMock(return_value=expected)
        mock_session.close = AsyncMock()

        with patch('otto.host.session.TelnetClient', return_value=mock_client):
            with patch('otto.host.session.TelnetSession', return_value=mock_session):
                result = await h.oneshot('echo hello')

        assert result.status == Status.Success
        assert result.output == 'hello'
        mock_client.connect.assert_called_once()
        mock_session.run_cmd.assert_called_once_with(
            'echo hello', expects=None, timeout=None,
        )
        await h.close()

    @pytest.mark.asyncio
    async def test_oneshot_telnet_concurrent_does_not_deadlock(self):
        """Regression: concurrent telnet ``oneshot()`` calls must not serialize.

        ``_put_files_nc`` launches ``nc -l <port>`` via ``oneshot(timeout=None)``
        to start a listener, then — when multiple files are transferred in
        parallel via ``asyncio.gather`` — other concurrent ``oneshot()`` calls
        run alongside it (port discovery for the next file, additional
        listeners, etc.).  The documented contract of ``oneshot()`` is that
        concurrent calls run independently.  When the telnet cache serializes
        all calls through a single session, the second call blocks waiting
        for the first to finish; the paired ``_connect_with_retry`` on the
        caller side then times out with "Remote nc listener on <ip>:<port>
        not ready".
        """
        h = RemoteHost(ip='10.0.0.1', ne='tomato_seed', creds={'u': 'p'},
                       term='telnet', log=False)

        listener_running = asyncio.Event()
        release_listener = asyncio.Event()

        async def _fake_run_cmd(cmd, expects=None, timeout=None):
            if 'nc -l' in cmd:
                listener_running.set()
                await release_listener.wait()
            return CommandStatus(cmd, '', Status.Success, 0)

        def _new_client(*args, **kwargs):
            c = MagicMock()
            c.connect = AsyncMock()
            c.reader = MagicMock()
            c.writer = MagicMock()
            c.close = AsyncMock()
            return c

        def _new_session(*args, **kwargs):
            s = MagicMock()
            s.run_cmd = AsyncMock(side_effect=_fake_run_cmd)
            s.close = AsyncMock()
            s.alive = True
            s._on_output = None
            return s

        with patch('otto.host.session.TelnetClient', side_effect=_new_client):
            with patch('otto.host.session.TelnetSession', side_effect=_new_session):
                listener_task = asyncio.create_task(
                    h.oneshot('nc -l 45681 < /dev/null > /tmp/x 2>/dev/null', timeout=None),
                )
                # Wait until the listener is actually running inside its
                # session, so we know it's holding whatever resource the
                # cache uses.
                await asyncio.wait_for(listener_running.wait(), timeout=1.0)

                # A concurrent oneshot() call must NOT block on the listener.
                # Under the bug this deadlocks and wait_for raises TimeoutError.
                try:
                    await asyncio.wait_for(h.oneshot('echo concurrent'), timeout=1.0)
                except asyncio.TimeoutError:
                    pytest.fail(
                        "h.oneshot() deadlocked waiting for a concurrent long-"
                        "running telnet oneshot — reproduces the "
                        "'Remote nc listener on <ip>:<port> not ready' "
                        "failure in _put_files_nc on telnet hosts",
                    )
                finally:
                    release_listener.set()
                    try:
                        await asyncio.wait_for(listener_task, timeout=1.0)
                    except Exception:
                        pass

        await h.close()

    @pytest.mark.asyncio
    async def test_oneshot_timeout_forwarded(self, host: RemoteHost):
        process = self._mock_ssh_process([])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        await host.oneshot('sleep 5', timeout=30.0)

        host._connections._ssh_conn.create_process.assert_called_once()


# ---------------------------------------------------------------------------
# File transfer: not-connected errors
# ---------------------------------------------------------------------------

class TestNotConnectedFileTransfer:

    @pytest.mark.asyncio
    async def test_scp_get_raises(self, host: RemoteHost):
        with patch.object(host._connections, 'ssh', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await host.get([Path('/remote/file.txt')], Path('/tmp'))

    @pytest.mark.asyncio
    async def test_scp_put_raises(self, host: RemoteHost):
        with patch.object(host._connections, 'ssh', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await host.put([Path('/tmp/file.txt')], Path('/tmp'))

    @pytest.mark.asyncio
    async def test_sftp_get_raises(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='sftp', log=False)
        with patch.object(h._connections, 'ssh', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await h.get([Path('/remote/file.txt')], Path('/tmp'))
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_raises(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='sftp', log=False)
        with patch.object(h._connections, 'ssh', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await h.put([Path('/tmp/file.txt')], Path('/tmp'))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_raises(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='ftp', log=False)
        with patch.object(h._connections, 'ftp', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await h.get([Path('/remote/file.txt')], Path('/tmp'))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_raises(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='ftp', log=False)
        with patch.object(h._connections, 'ftp', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with pytest.raises(RuntimeError, match="not connected"):
                await h.put([Path('/tmp/file.txt')], Path('/tmp'))
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_raises(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=False)
        stat_cs = CommandStatus('stat -c %s /remote/file.txt', '0', Status.Success, 0)
        mock_monitor = MagicMock()
        mock_monitor.run = AsyncMock(
            return_value=RunResult(status=Status.Success, statuses=[stat_cs])
        )
        mock_monitor.close = AsyncMock()

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_server.sockets = [MagicMock()]
            mock_server.sockets[0].getsockname.return_value = ('0.0.0.0', 9999)
            return mock_server

        with patch.object(h, 'open_session', AsyncMock(return_value=mock_monitor)):
            with patch.object(h, '_get_local_ip', return_value='127.0.0.1'):
                with patch.object(h, 'oneshot', new_callable=AsyncMock,
                                  side_effect=RuntimeError("not connected")):
                    with patch('otto.host.transfer.asyncio.start_server',
                               side_effect=fake_start_server):
                        status, _ = await h.get([Path('/remote/file.txt')], Path('/tmp'))
        assert status == Status.Error
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_raises(self, tmp_path: Path):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=False)
        src = tmp_path / 'file.txt'
        src.write_bytes(b'data')
        with patch.object(h, 'oneshot', new_callable=AsyncMock,
                          side_effect=RuntimeError("not connected")):
            with patch('otto.host.transfer._connect_with_retry',
                       AsyncMock(side_effect=ConnectionError("nc listener not ready"))):
                status, _ = await h.put([src], Path('/tmp'))
        assert status == Status.Error
        await h.close()


# ---------------------------------------------------------------------------
# File transfer: mocked success paths
# ---------------------------------------------------------------------------

class TestSshFileTransfer:

    def _mock_ssh_conn(self) -> MagicMock:
        conn = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn

    @pytest.mark.asyncio
    async def test_scp_get_success(self, host: RemoteHost):
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch('otto.host.transfer.asyncssh.scp', new_callable=AsyncMock) as mock_scp:
            status, msg = await host.get([Path('/etc/hostname')], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        assert msg == ''
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_scp_put_success(self, host: RemoteHost, tmp_path: Path):
        src = tmp_path / 'upload.txt'
        src.write_text('hello')
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch('otto.host.transfer.asyncssh.scp', new_callable=AsyncMock) as mock_scp:
            status, msg = await host.put([src], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_sftp_get_success(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='sftp', log=False)
        mock_sftp = MagicMock()
        mock_sftp.get = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, msg = await h.get([Path('/etc/hostname')], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        mock_sftp.get.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_success(self, tmp_path: Path):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='sftp', log=False)
        src = tmp_path / 'upload.txt'
        src.write_text('hello')
        mock_sftp = MagicMock()
        mock_sftp.put = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, msg = await h.put([src], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        mock_sftp.put.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_success(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='ftp', log=False)
        mock_ftp = MagicMock()
        mock_ftp.download = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, msg = await h.get([Path('/home/vagrant/test.txt')], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        mock_ftp.download.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_success(self, tmp_path: Path):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='ftp', log=False)
        src = tmp_path / 'upload.txt'
        src.write_text('hello')
        mock_ftp = MagicMock()
        mock_ftp.upload = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, msg = await h.put([src], Path('/tmp'), show_progress=False)
        assert status == Status.Success
        mock_ftp.upload.assert_called_once()
        await h.close()

# ---------------------------------------------------------------------------
# File transfer: netcat mocked unit tests
# ---------------------------------------------------------------------------

class TestNcFileTransfer:

    @pytest.mark.asyncio
    async def test_nc_get_success(self, tmp_path: Path):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=False)

        stat_cs = CommandStatus('stat -c %s /remote/file.txt', '1024', Status.Success, 0)
        send_cs = CommandStatus('nc ...', '', Status.Success, 0)

        mock_monitor = MagicMock()
        mock_monitor.run = AsyncMock(
            return_value=RunResult(status=Status.Success, statuses=[stat_cs])
        )
        mock_monitor.close = AsyncMock()

        dest = tmp_path / 'out'
        dest.mkdir()

        file_data = b'hello world'

        async def fake_start_server(cb, host, port):
            """Simulate asyncio.start_server: invoke the callback with a reader that yields file_data."""
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()

            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b''])
            writer = MagicMock()
            writer.close = MagicMock()

            # Fire the connection handler so it writes the file
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(cb(reader, writer))
            )
            return mock_server

        with patch.object(h, 'open_session', AsyncMock(return_value=mock_monitor)):
            with patch.object(h, 'oneshot', AsyncMock(return_value=send_cs)):
                with patch.object(h, '_get_local_ip', return_value='127.0.0.1'):
                    with patch('otto.host.transfer.asyncio.start_server',
                               side_effect=fake_start_server):
                        status, msg = await h.get(
                            [Path('/remote/file.txt')], dest, show_progress=False
                        )

        assert status == Status.Success
        assert msg == ''
        assert (dest / 'file.txt').read_bytes() == file_data
        mock_monitor.run.assert_called_once_with('stat -c %s /remote/file.txt')
        mock_monitor.close.assert_not_called()  # session persists for reuse; closed by host.close()
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_success(self, tmp_path: Path):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=False)

        src = tmp_path / 'upload.txt'
        src.write_bytes(b'test content')

        # Command-dispatched responses instead of a positional side_effect list:
        # _put_files_nc now also runs `_wait_for_remote_listener`, which
        # probes for `ss`/`netstat` and then polls the listener — several
        # extra oneshot calls whose order a positional list can't capture.
        async def mock_oneshot(cmd: str, **kw) -> CommandStatus:
            if 'nc -l' in cmd:
                return CommandStatus(cmd, '', Status.Success, 0)
            if cmd.startswith('type '):
                return CommandStatus(cmd, '', Status.Success, 0)
            if 'ss -tln' in cmd or 'netstat -tln' in cmd or '/proc/net/tcp' in cmd:
                return CommandStatus(cmd, '', Status.Success, 0)
            if cmd.startswith('stat -c %s '):
                return CommandStatus(cmd, str(src.stat().st_size), Status.Success, 0)
            # Port discovery (ss/netstat/python/proc) returns a port number.
            return CommandStatus(cmd, '44444', Status.Success, 0)

        sent_data = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=lambda d: sent_data.extend(d))
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        with patch.object(h, 'oneshot', AsyncMock(side_effect=mock_oneshot)):
            with patch('otto.host.transfer._connect_with_retry',
                       AsyncMock(return_value=(mock_reader, mock_writer))):
                status, msg = await h.put([src], Path('/tmp'), show_progress=False)

        assert status == Status.Success
        assert msg == ''
        assert sent_data == b'test content'
        mock_writer.drain.assert_called()
        mock_writer.close.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """During put, host.log must be False so per-host records are
        dropped by HostFilter; it must be restored to its prior value after
        the transfer completes."""
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=True)

        src = tmp_path / 'upload.txt'
        src.write_bytes(b'test content')

        log_states: list[bool] = []

        async def oneshot_capturing_log(cmd: str, **_kw) -> CommandStatus:
            log_states.append(h.log)
            # Compound strategy probe runs first (warm-up); return a valid
            # port+listener pair so the cascades don't fire.
            if cmd.startswith('port=proc; listener=proc'):
                return CommandStatus(cmd, 'python proc', Status.Success, 0)
            if 'nc -l' in cmd:
                return CommandStatus(cmd, '', Status.Success, 0)
            if 'ss -tln' in cmd or 'netstat -tln' in cmd or '/proc/net/tcp' in cmd:
                return CommandStatus(cmd, '', Status.Success, 0)
            if cmd.startswith('stat -c %s '):
                return CommandStatus(cmd, str(src.stat().st_size), Status.Success, 0)
            # Port discovery returns a port number.
            return CommandStatus(cmd, '44444', Status.Success, 0)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        assert h.log is True
        with patch.object(h, 'oneshot', AsyncMock(side_effect=oneshot_capturing_log)):
            with patch('otto.host.transfer._connect_with_retry',
                       AsyncMock(return_value=(mock_reader, mock_writer))):
                status, _ = await h.put([src], Path('/tmp'), show_progress=False)

        assert status == Status.Success
        assert log_states and all(state is False for state in log_states)
        assert h.log is True
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """Symmetric check for get — the monitor's stat call and the
        send oneshot must both run with host.log == False."""
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'},
                       transfer='nc', log=True)

        stat_cs = CommandStatus('stat -c %s /remote/file.txt', '11', Status.Success, 0)
        send_cs = CommandStatus('nc ...', '', Status.Success, 0)

        log_states: list[bool] = []

        async def monitor_run(*_a, **_kw) -> RunResult:
            log_states.append(h.log)
            return RunResult(status=Status.Success, statuses=[stat_cs])

        mock_monitor = MagicMock()
        mock_monitor.run = AsyncMock(side_effect=monitor_run)
        mock_monitor.close = AsyncMock()

        dest = tmp_path / 'out'
        dest.mkdir()

        file_data = b'hello world'

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b''])
            writer = MagicMock()
            writer.close = MagicMock()
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(cb(reader, writer))
            )
            return mock_server

        async def oneshot_capturing_log(*_a, **_kw) -> CommandStatus:
            log_states.append(h.log)
            return send_cs

        assert h.log is True
        with patch.object(h, 'open_session', AsyncMock(return_value=mock_monitor)):
            with patch.object(h, 'oneshot', AsyncMock(side_effect=oneshot_capturing_log)):
                with patch.object(h, '_get_local_ip', return_value='127.0.0.1'):
                    with patch('otto.host.transfer.asyncio.start_server',
                               side_effect=fake_start_server):
                        status, _ = await h.get(
                            [Path('/remote/file.txt')], dest, show_progress=False
                        )

        assert status == Status.Success
        assert log_states and all(state is False for state in log_states)
        assert h.log is True
        await h.close()


# ---------------------------------------------------------------------------
# Parameterized integration tests (SSH + Telnet)
# ---------------------------------------------------------------------------

_ALL_TERMS = pytest.mark.parametrize("host1", ["ssh", "telnet"], indirect=True)
_SSH_ONLY = pytest.mark.parametrize("host1", ["ssh"], indirect=True)


@_SSH_ONLY
class TestIntegration:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_connect_and_run_echo(self, host1: RemoteHost):
        result = (await host1.run('echo hello')).only
        assert result.status == Status.Success
        assert 'hello' in result.output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiple_commands_run_in_order(self, host1: RemoteHost):
        result = await host1.run(['echo first', 'echo second'])
        assert result.status == Status.Success
        assert len(result.statuses) == 2
        assert 'first' in result.statuses[0].output
        assert 'second' in result.statuses[1].output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_uname_returns_linux(self, host1: RemoteHost):
        result = (await host1.run('uname -s')).only
        assert result.status == Status.Success
        assert 'Linux' in result.output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_both_hosts_reachable(self, host1: RemoteHost):
        kwargs: dict[str, str] = {"term": host1.term}
        if host1.term == "telnet":
            kwargs["transfer"] = "ftp"
        host2 = make_host("tomato", **kwargs)
        try:
            for host in (host1, host2):
                result = (await host.run('echo ping')).only
                assert result.status == Status.Success
                assert 'ping' in result.output
        finally:
            await host2.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiline_output(self, host1: RemoteHost):
        result = (await host1.run("echo -e 'line1\\nline2\\nline3'")).only
        assert result.status == Status.Success
        lines = result.output.strip().splitlines()
        assert len(lines) == 3

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failing_command_returns_failed_status(self, host1: RemoteHost):
        result = (await host1.run('ls /nonexistent_dir_otto_test')).only
        assert result.status == Status.Failed
        assert result.retcode == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unexpected_eof_returns_error(self, host1: RemoteHost):
        result = (await host1.run('exit 42')).only
        assert result.status == Status.Error
        assert result.retcode == -1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_overall_status_reflects_failure(self, host1: RemoteHost):
        result = await host1.run(['echo ok', 'ls /nonexistent_dir_otto_test'])
        assert result.status == Status.Failed
        assert result.statuses[0].status == Status.Success
        assert result.statuses[1].status == Status.Failed

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_second_credential_works(self, host1: RemoteHost):
        """Verify the non-default (test) user can log in and run commands."""

        data = host_data("tomato")
        second_user, second_password = list(data["creds"].items())[1]
        host = RemoteHost(
            ip=data["ip"],
            user=second_user,
            ne=data["ne"],
            creds=data["creds"],
            board=data.get("board"),
        )
        try:
            result = (await host.run('whoami')).only
            assert result.status == Status.Success
            assert second_user in result.output
        finally:
            await host.close()


@_ALL_TERMS
class TestStatePersistence:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cd_persists_between_commands(self, host1: RemoteHost):
        try:
            await host1.run("cd /")
            await host1.run("cd tmp")
            result = (await host1.run("pwd")).only
            assert result.status == Status.Success
            assert result.output.strip() == "/tmp"
        finally:
            await host1.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_env_var_persists(self, host1: RemoteHost):
        try:
            await host1.run("export OTTO_TEST_VAR=hello123")
            result = (await host1.run("echo $OTTO_TEST_VAR")).only
            assert result.status == Status.Success
            assert "hello123" in result.output
        finally:
            await host1.close()


@_ALL_TERMS
class TestTimeout:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_timeout_returns_error(self, host1: RemoteHost):
        try:
            result = (await host1.run("sleep 999", timeout=0.5)).only
            assert result.status == Status.Error
            assert "timed out" in result.output
            if host1.term == 'ssh':
                assert result.retcode == -1
        finally:
            await host1.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_recovers_after_timeout(self, host1: RemoteHost):
        try:
            await host1.run("sleep 999", timeout=0.5)
            result = (await host1.run("echo recovered")).only
            assert result.status == Status.Success
            assert "recovered" in result.output
        finally:
            await host1.close()


@_ALL_TERMS
class TestSendExpect:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_python_repl(self, host1: RemoteHost):
        try:
            await host1.send("python3\n")
            await host1.expect(r">>> ", timeout=5.0)
            await host1.send("print('otto_test')\n")
            output = await host1.expect(r">>> ", timeout=5.0)
            assert "otto_test" in output
            await host1.send("exit()\n")
        finally:
            await host1.close()

# ---------------------------------------------------------------------------
# File transfer (SCP, SFTP, FTP)
# ---------------------------------------------------------------------------

_ALL_TRANSFERS = pytest.mark.parametrize(
    "transfer_host",
    [
        "scp", "sftp", "ftp", "nc",
        pytest.param(("nc", "telnet"), id="nc-telnet"),
    ],
    indirect=True,
)


# TODO: Test netcat with ssh and telnet as the underlying term types to ensure all permutations are tested
# NOTE: Transfers go through asyncssh/scp/sftp and have been observed to hang
# indefinitely when the remote SSH daemon stalls mid-protocol. get/put are
# wrapped in ``transfer_with_retry`` so an individual transfer is bounded
# and retried once, preventing the whole suite from blocking on a single flake.
@_ALL_TRANSFERS
class TestFileTransfer:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_file(self, transfer_host: RemoteHost, tmp_path: Path):
        """Download /etc/hostname and verify it matches the hostname command."""

        result = (await transfer_host.run('hostname')).only
        expected_hostname = result.output.strip()

        status, msg = await transfer_with_retry(
            lambda: transfer_host.get([Path('/etc/hostname')], tmp_path)
        )
        assert status == Status.Success, f"get failed: {msg}"

        local_hostname = (tmp_path / 'hostname').read_text().strip()
        assert local_hostname == expected_hostname

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_file(self, transfer_host: RemoteHost, tmp_path: Path):
        """Upload a file, verify it arrived, clean up."""

        content = 'file transfer test'
        src = tmp_path / f'otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt'
        src.write_text(content)
        remote_path = f'/tmp/otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt'

        status, msg = await transfer_with_retry(
            lambda: transfer_host.put([src], Path('/tmp'))
        )
        assert status == Status.Success, f"put failed: {msg}"

        result = (await transfer_host.run(f'cat {remote_path}')).only
        assert content in result.output

        await transfer_host.run(f'rm -f {remote_path}')


# ---------------------------------------------------------------------------
# open_session() — session creation (unit)
# ---------------------------------------------------------------------------

class TestOpenSession:
    """Unit tests for RemoteHost.open_session() — session creation and registration."""

    def _mock_shell_session(self, alive: bool = True) -> MagicMock:
        ok = CommandStatus('echo hi', 'hi', Status.Success, 0)
        session = MagicMock(spec=ShellSession)
        session.alive = alive
        session.run_cmd = AsyncMock(return_value=ok)
        session.send = AsyncMock()
        session.expect = AsyncMock(return_value='output')
        session.close = AsyncMock()
        return session

    def _mock_telnet_client(self) -> MagicMock:
        client = MagicMock()
        client.connect = AsyncMock()
        client.reader = MagicMock()
        client.writer = MagicMock()
        client.close = AsyncMock()
        return client

    # --- SSH ---

    @pytest.mark.asyncio
    async def test_ssh_returns_remote_session(self, host: RemoteHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', return_value=mock_shell):
            result = await host.open_session('monitor')
        assert isinstance(result, HostSession)
        assert result.alive is True

    @pytest.mark.asyncio
    async def test_ssh_session_registered_in_host(self, host: RemoteHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', return_value=mock_shell):
            result = await host.open_session('monitor')
        assert host._session_mgr._named_sessions['monitor'] is result

    @pytest.mark.asyncio
    async def test_ssh_session_uses_existing_conn(self, host: RemoteHost):
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        host._connections._ssh_conn = mock_conn
        mock_shell = self._mock_shell_session()
        with patch('otto.host.session.SshSession', return_value=mock_shell) as MockSshSession:
            await host.open_session('monitor')
        MockSshSession.assert_called_once_with(mock_conn)

    # --- Telnet ---

    @pytest.mark.asyncio
    async def test_telnet_returns_remote_session(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, term='telnet', log=False)
        mock_shell = self._mock_shell_session()
        with patch('otto.host.session.TelnetClient', return_value=self._mock_telnet_client()):
            with patch('otto.host.session.TelnetSession', return_value=mock_shell):
                result = await h.open_session('monitor')
        assert isinstance(result, HostSession)
        assert result.alive is True
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_connects_new_client(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, term='telnet', log=False)
        mock_client = self._mock_telnet_client()
        with patch('otto.host.session.TelnetClient', return_value=mock_client):
            with patch('otto.host.session.TelnetSession', return_value=self._mock_shell_session()):
                await h.open_session('monitor')
        mock_client.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_session_owns_its_client(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, term='telnet', log=False)
        mock_client = self._mock_telnet_client()
        with patch('otto.host.session.TelnetClient', return_value=mock_client):
            with patch('otto.host.session.TelnetSession', return_value=self._mock_shell_session()) as MockTelnetSession:
                await h.open_session('monitor')
        MockTelnetSession.assert_called_once_with(
            mock_client.reader,
            mock_client.writer,
            _owned_client=mock_client,
        )
        await h.close()

    # --- Multiple SSH sessions ---

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_are_distinct_objects(self, host: RemoteHost):
        shell_a = self._mock_shell_session()
        shell_b = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', side_effect=[shell_a, shell_b]):
            session_a = await host.open_session('alpha')
            session_b = await host.open_session('beta')
        assert session_a is not session_b
        assert host._session_mgr._named_sessions['alpha'] is session_a
        assert host._session_mgr._named_sessions['beta'] is session_b

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_both_alive(self, host: RemoteHost):
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', side_effect=[
            self._mock_shell_session(), self._mock_shell_session(),
        ]):
            s1 = await host.open_session('s1')
            s2 = await host.open_session('s2')
        assert s1.alive is True
        assert s2.alive is True

    # --- Multiple Telnet sessions ---

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_create_own_client(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, term='telnet', log=False)
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with patch('otto.host.session.TelnetClient', side_effect=[client_a, client_b]):
            with patch('otto.host.session.TelnetSession', side_effect=[
                self._mock_shell_session(), self._mock_shell_session(),
            ]):
                await h.open_session('alpha')
                await h.open_session('beta')
        client_a.connect.assert_called_once()
        client_b.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_own_separate_client(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, term='telnet', log=False)
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with patch('otto.host.session.TelnetClient', side_effect=[client_a, client_b]):
            with patch('otto.host.session.TelnetSession', side_effect=[
                self._mock_shell_session(), self._mock_shell_session(),
            ]) as MockTelnetSession:
                await h.open_session('alpha')
                await h.open_session('beta')
        calls = MockTelnetSession.call_args_list
        assert calls[0].kwargs['_owned_client'] is client_a
        assert calls[1].kwargs['_owned_client'] is client_b
        await h.close()

    # --- Mix of SSH host and Telnet host ---

    @pytest.mark.asyncio
    async def test_ssh_host_and_telnet_host_each_hold_own_sessions(self):
        """An SSH host and a Telnet host can hold independent named sessions simultaneously."""
        ssh_host = RemoteHost(ip='10.0.0.1', ne='ssh-box', creds={'u': 'p'}, term='ssh', log=False)
        telnet_host = RemoteHost(ip='10.0.0.2', ne='tel-box', creds={'u': 'p'}, term='telnet', log=False)

        ssh_shell = self._mock_shell_session()
        telnet_shell = self._mock_shell_session()
        mock_client = self._mock_telnet_client()

        ssh_host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', return_value=ssh_shell):
            ssh_session = await ssh_host.open_session('monitor')

        with patch('otto.host.session.TelnetClient', return_value=mock_client):
            with patch('otto.host.session.TelnetSession', return_value=telnet_shell):
                telnet_session = await telnet_host.open_session('monitor')

        assert isinstance(ssh_session, HostSession)
        assert isinstance(telnet_session, HostSession)
        assert ssh_session is not telnet_session
        assert ssh_host._session_mgr._named_sessions['monitor'] is ssh_session
        assert telnet_host._session_mgr._named_sessions['monitor'] is telnet_session
        await telnet_host.close()

    # --- Reuse and replacement ---

    @pytest.mark.asyncio
    async def test_reuse_live_session_returns_same_object(self, host: RemoteHost):
        mock_shell = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', return_value=mock_shell):
            first = await host.open_session('monitor')
            second = await host.open_session('monitor')
        assert first is second

    @pytest.mark.asyncio
    async def test_dead_session_is_replaced(self, host: RemoteHost):
        shell_old = self._mock_shell_session(alive=True)
        shell_new = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch('otto.host.session.SshSession', side_effect=[shell_old, shell_new]):
            first = await host.open_session('monitor')
            first._session.alive = False
            second = await host.open_session('monitor')
        assert first is not second
        assert host._session_mgr._named_sessions['monitor'] is second

    # --- Error cases ---

    @pytest.mark.asyncio
    async def test_unknown_term_raises_value_error(self):
        h = RemoteHost(ip='10.0.0.1', ne='box', creds={'u': 'p'}, log=False)
        h.term = 'foobar'  # type: ignore
        h._connections.term = 'foobar'  # type: ignore
        with pytest.raises(ValueError, match='foobar'):
            await h.open_session('monitor')


# ---------------------------------------------------------------------------
# HostSession proxy — delegation and lifecycle (unit)
# ---------------------------------------------------------------------------

class TestHostSessionProxy:
    """Unit tests for HostSession — argument forwarding, state, and cleanup."""

    def _make_remote_session(
        self,
        host: RemoteHost,
        name: str = 'monitor',
        alive: bool = True,
    ) -> tuple[HostSession, MagicMock]:
        ok = CommandStatus('echo hi', 'hi', Status.Success, 0)
        shell = MagicMock(spec=ShellSession)
        shell.alive = alive
        shell.run_cmd = AsyncMock(return_value=ok)
        shell.send = AsyncMock()
        shell.expect = AsyncMock(return_value='some output')
        shell.close = AsyncMock()
        remote = HostSession(
            name=name,
            session=shell,
            log_command=host._log_command,
            log_output=host._log_output,
            deregister=lambda n: host._session_mgr._named_sessions.pop(n, None),
        )
        host._session_mgr._named_sessions[name] = remote
        return remote, shell

    @pytest.mark.asyncio
    async def test_run_returns_command_status(self, host: RemoteHost):
        session, _ = self._make_remote_session(host)
        result = (await session.run('echo hi')).only
        assert isinstance(result, CommandStatus)
        assert result.status == Status.Success
        assert result.output == 'hi'

    @pytest.mark.asyncio
    async def test_run_delegates_cmd_to_shell_session(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        await session.run('ls /tmp')
        shell.run_cmd.assert_called_once_with('ls /tmp', expects=None, timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_forwards_expects(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        expects = [(r'Password:', 'secret\n')]
        await session.run('sudo ls', expects=expects)  # type: ignore[arg-type]
        shell.run_cmd.assert_called_once_with('sudo ls', expects=expects, timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_forwards_timeout(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        await session.run('sleep 5', timeout=60.0)
        shell.run_cmd.assert_called_once_with('sleep 5', expects=None, timeout=60.0)

    @pytest.mark.asyncio
    async def test_send_delegates(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        await session.send('hello\n')
        shell.send.assert_called_once_with('hello\n')

    @pytest.mark.asyncio
    async def test_expect_delegates_and_returns_output(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        result = await session.expect(r'\$')
        shell.expect.assert_called_once_with(r'\$', 10.0)
        assert result == 'some output'

    @pytest.mark.asyncio
    async def test_expect_forwards_timeout(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        await session.expect(r'\$', timeout=5.0)
        shell.expect.assert_called_once_with(r'\$', 5.0)

    def test_alive_true_when_session_alive(self, host: RemoteHost):
        session, _ = self._make_remote_session(host, alive=True)
        assert session.alive is True

    def test_alive_false_when_session_dead(self, host: RemoteHost):
        session, _ = self._make_remote_session(host, alive=False)
        assert session.alive is False

    @pytest.mark.asyncio
    async def test_close_calls_underlying_session_close(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        await session.close()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_removes_from_host_registry(self, host: RemoteHost):
        session, _ = self._make_remote_session(host, name='monitor')
        assert 'monitor' in host._session_mgr._named_sessions
        await session.close()
        assert 'monitor' not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exit(self, host: RemoteHost):
        session, shell = self._make_remote_session(host)
        async with session:
            shell.close.assert_not_called()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_removes_from_registry_on_exit(self, host: RemoteHost):
        session, _ = self._make_remote_session(host, name='monitor')
        async with session:
            assert 'monitor' in host._session_mgr._named_sessions
        assert 'monitor' not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_yields_self(self, host: RemoteHost):
        session, _ = self._make_remote_session(host)
        async with session as ctx:
            assert ctx is session


# ---------------------------------------------------------------------------
# Host-level lifecycle with named sessions (unit)
# ---------------------------------------------------------------------------

class TestOpenSessionCleanup:
    """Unit tests for host.close() and _connected interactions with named sessions."""

    def _add_mock_session(self, host: RemoteHost, name: str, alive: bool = True) -> MagicMock:
        """Register a HostSession backed by a mock ShellSession and return the shell mock."""
        shell = MagicMock(spec=ShellSession)
        shell.alive = alive
        shell.close = AsyncMock()
        host._session_mgr._named_sessions[name] = HostSession(
            name=name,
            session=shell,
            log_command=host._log_command,
            log_output=host._log_output,
            deregister=lambda n: host._session_mgr._named_sessions.pop(n, None),
        )
        return shell

    @pytest.mark.asyncio
    async def test_host_close_closes_all_named_sessions(self, host: RemoteHost):
        shell_a = self._add_mock_session(host, 'a')
        shell_b = self._add_mock_session(host, 'b')
        await host.close()
        shell_a.close.assert_called_once()
        shell_b.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_host_close_clears_registry(self, host: RemoteHost):
        self._add_mock_session(host, 'monitor')
        await host.close()
        assert host._session_mgr._named_sessions == {}

    def test_connected_true_with_live_named_session(self, host: RemoteHost):
        self._add_mock_session(host, 'monitor', alive=True)
        assert host._connected is True

    def test_connected_false_when_named_session_dead(self, host: RemoteHost):
        self._add_mock_session(host, 'monitor', alive=False)
        assert host._connected is False

    def test_connected_false_with_no_sessions(self, host: RemoteHost):
        assert host._connected is False

    def test_connected_true_with_multiple_sessions_one_alive(self, host: RemoteHost):
        self._add_mock_session(host, 'dead', alive=False)
        self._add_mock_session(host, 'live', alive=True)
        assert host._connected is True


# ---------------------------------------------------------------------------
# Named session integration tests (SSH + Telnet)
# ---------------------------------------------------------------------------

@_ALL_TERMS
class TestNamedSessionIntegration:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_named_session_runs_command(self, host1: RemoteHost):
        try:
            mon = await host1.open_session('monitor')
            result = (await mon.run('echo hello')).only
            assert result.status == Status.Success
            assert 'hello' in result.output
        finally:
            await host1.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_sessions_have_independent_state(self, host1: RemoteHost):
        """cd in one named session does not affect the other."""
        try:
            s1 = await host1.open_session('s1')
            s2 = await host1.open_session('s2')
            await s1.run('cd /tmp')
            await s2.run('cd /home')
            r1 = (await s1.run('pwd')).only
            r2 = (await s2.run('pwd')).only
            assert r1.output.strip() == '/tmp'
            assert '/home' in r2.output.strip()
        finally:
            await host1.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_context_manager_removes_session_from_registry(self, host1: RemoteHost):
        try:
            async with (await host1.open_session('monitor')) as mon:
                assert 'monitor' in host1._session_mgr._named_sessions
                result = (await mon.run('echo hi')).only
                assert result.status == Status.Success
            assert 'monitor' not in host1._session_mgr._named_sessions
        finally:
            await host1.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_host_close_closes_all_named_sessions(self, host1: RemoteHost):
        s1 = await host1.open_session('s1')
        s2 = await host1.open_session('s2')
        # Sessions initialize lazily on first I/O — run a command to make them alive
        await s1.run('echo init')
        await s2.run('echo init')
        assert s1.alive
        assert s2.alive
        await host1.close()
        assert not s1.alive
        assert not s2.alive
        assert host1._session_mgr._named_sessions == {}
