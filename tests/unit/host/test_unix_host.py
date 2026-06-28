"""
Unit tests for UnixHost — pure, no-VM coverage of the class internals
(initialization, run/oneshot dispatch, mocked file-transfer paths, session
creation and the HostSession proxy/lifecycle).

The behavior that needs a live Vagrant bed lives in
:mod:`tests.integration.host.test_unix_host_integration` (parametrized over
ssh / telnet / local).
"""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import HostSession, UnixHost
from otto.host.session import ShellSession
from otto.utils import CommandStatus, Status


@pytest.fixture
def host() -> UnixHost:
    """Bare UnixHost, no connections established."""
    return UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=False)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_values(self, host: UnixHost):
        assert host.ip == "10.0.0.1"
        assert host.element == "box"
        assert host.creds == {"user": "pass"}
        assert host.term == "ssh"
        assert host.transfer == "scp"
        assert host.nc_options.exec_name == "nc"
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
        h = UnixHost(ip="10.0.0.1", element="Orange", creds={"u": "p"}, log=False)
        assert h.id == "orange"
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board(self):
        h = UnixHost(ip="10.0.0.1", element="Orange", board="Seed", creds={"u": "p"}, log=False)
        assert h.id == "orange_seed"
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board_and_slot(self):
        h = UnixHost(
            ip="10.0.0.1", element="Orange", board="Seed", slot=0, creds={"u": "p"}, log=False
        )
        assert h.id == "orange_seed0"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_no_board(self):
        h = UnixHost(ip="10.0.0.1", element="orange", creds={"u": "p"}, log=False)
        assert h.name == "orange"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_with_board(self):
        h = UnixHost(ip="10.0.0.1", element="orange", board="seed", creds={"u": "p"}, log=False)
        assert h.name == "orange seed"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_override(self):
        h = UnixHost(ip="10.0.0.1", element="orange", creds={"u": "p"}, name="custom", log=False)
        assert h.name == "custom"
        await h.close()


# ---------------------------------------------------------------------------
# _creds
# ---------------------------------------------------------------------------


class TestCreds:
    def test_returns_first_pair(self, host: UnixHost):
        user, password = host._creds
        assert user == "user"
        assert password == "pass"

    @pytest.mark.asyncio
    async def test_returns_first_pair_from_multiple_creds(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds={"vagrant": "vagrant", "test": "Password1"},
            log=False,
        )
        user, password = h._creds
        assert user == "vagrant"
        assert password == "vagrant"
        await h.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_when_not_connected_is_safe(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=False)
        await h.close()

    @pytest.mark.asyncio
    async def test_close_disconnects_ssh(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=False)
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        h._connections._ssh_conn = mock_conn
        await h.close()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_called_once()
        assert h._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_close_does_not_run_process_wide_gc(self):
        """close() must not trigger a process-wide gc.collect().

        A process-wide collection sweeps up objects leaked by *other* tests
        and fires their ``__del__``; pytest's ``[unraisable]`` plugin then
        escalates those ResourceWarnings into a flake on whichever test
        happened to call ``close()``.
        """
        import gc

        collected: list[bool] = []

        class _LeakSentinel:
            def __del__(self):
                collected.append(True)

        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=False)

        # Disable automatic generational gc *before* building the cycle, so
        # only an explicit gc.collect() — not incidental allocation pressure —
        # can reclaim the sentinel.
        gc.disable()
        try:
            # Unreachable reference cycle holding the sentinel; reclaimable
            # only by gc.collect(), not by refcounting.
            cycle: dict = {}
            cycle["self"] = cycle
            cycle["sentinel"] = _LeakSentinel()
            del cycle

            await h.close()
            assert not collected, "close() ran a process-wide gc.collect()"
        finally:
            gc.enable()
            gc.collect()  # clean up our own cycle


# ---------------------------------------------------------------------------
# run() — list form
# ---------------------------------------------------------------------------


class TestRunList:
    @pytest.mark.asyncio
    async def test_single_element_list(self, host: UnixHost):
        ok = CommandStatus("echo hi", "hi", Status.Success, 0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok):
            result = await host.run(["echo hi"])
        assert len(result.statuses) == 1
        assert result.statuses[0] == ok
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_accepts_list_of_commands(self, host: UnixHost):
        r1 = CommandStatus("ls", "", Status.Success, 0)
        r2 = CommandStatus("pwd", "/home", Status.Success, 0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "pwd"])
        assert len(result.statuses) == 2

    @pytest.mark.asyncio
    async def test_overall_success_when_all_pass(self, host: UnixHost):
        r1 = CommandStatus("ls", "", Status.Success, 0)
        r2 = CommandStatus("pwd", "", Status.Success, 0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "pwd"])
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_overall_failed_when_any_fails(self, host: UnixHost):
        r1 = CommandStatus("ls", "", Status.Success, 0)
        r2 = CommandStatus("badcmd", "", Status.Failed, 127)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "badcmd"])
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
    async def test_success(self, host: UnixHost):
        ok = CommandStatus("echo hello", "hello", Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run("echo hello")).only
        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_failure(self, host: UnixHost):
        fail = CommandStatus("badcmd", "command not found", Status.Failed, 127)
        host._session_mgr._session = self._mock_session(fail)
        result = (await host.run("badcmd")).only
        assert result.status == Status.Failed
        assert result.retcode == 127

    @pytest.mark.asyncio
    async def test_connection_failure_propagates(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=ConnectionError("refused"),
            ),
            pytest.raises(ConnectionError),
        ):
            await host.run("echo hi")

    @pytest.mark.asyncio
    async def test_command_recorded(self, host: UnixHost):
        ok = CommandStatus("echo out", "out", Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run("echo out")).only
        assert result.command == "echo out"

    @pytest.mark.asyncio
    async def test_expects_forwarded_to_session(self, host: UnixHost):
        ok = CommandStatus("sudo ls", "", Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        expects = [(r"Password:", "secret\n")]
        await host.run("sudo ls", expects=expects)
        host._session_mgr._session.run_cmd.assert_called_once_with(
            "sudo ls",
            expects=expects,
            timeout=None,
            on_output=None,
            write_progress=None,
        )

    @pytest.mark.asyncio
    async def test_timeout_forwarded_to_session(self, host: UnixHost):
        ok = CommandStatus("sleep 1", "", Status.Success, 0)
        host._session_mgr._session = self._mock_session(ok)
        await host.run("sleep 1", timeout=30.0)
        host._session_mgr._session.run_cmd.assert_called_once_with(
            "sleep 1",
            expects=None,
            timeout=30.0,
            on_output=None,
            write_progress=None,
        )

    @pytest.mark.asyncio
    async def test_telnet_connection_failure_propagates(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        with (
            patch.object(
                h._connections,
                "telnet",
                new_callable=AsyncMock,
                side_effect=ConnectionError("refused"),
            ),
            pytest.raises(ConnectionError),
        ):
            await h.run("echo hi")
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
                    raise StopAsyncIteration from None

        process.stdout = AsyncLineIter(lines)
        mock_wait_result = MagicMock()
        mock_wait_result.exit_status = exit_status
        process.wait = AsyncMock(return_value=mock_wait_result)
        process.terminate = MagicMock()
        return process

    @pytest.mark.asyncio
    async def test_oneshot_ssh_success(self, host: UnixHost):
        process = self._mock_ssh_process(["hello\n"])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.oneshot("echo hello")

        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_oneshot_ssh_nonzero_exit(self, host: UnixHost):
        process = self._mock_ssh_process(["not found\n"], exit_status=1)
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.oneshot("badcmd")

        assert result.status == Status.Failed
        assert result.retcode == 1

    @pytest.mark.asyncio
    async def test_oneshot_telnet_success(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        expected = CommandStatus("echo hello", "hello", Status.Success, 0)

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.reader = MagicMock()
        mock_client.writer = MagicMock()
        mock_client.close = AsyncMock()

        mock_session = MagicMock()
        mock_session.run_cmd = AsyncMock(return_value=expected)
        mock_session.close = AsyncMock()
        mock_session._ensure_initialized = AsyncMock()

        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=mock_session),
        ):
            result = await h.oneshot("echo hello")

        assert result.status == Status.Success
        assert result.output == "hello"
        mock_client.connect.assert_called_once()
        mock_session.run_cmd.assert_called_once_with(
            "echo hello",
            expects=None,
            timeout=None,
            on_output=None,
        )
        await h.close()

    @pytest.mark.concurrency
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
        h = UnixHost(
            ip="10.0.0.1", element="tomato_seed", creds={"u": "p"}, term="telnet", log=False
        )

        listener_running = asyncio.Event()
        release_listener = asyncio.Event()

        async def _fake_run_cmd(cmd, expects=None, timeout=None, on_output=None):
            if "nc -l" in cmd:
                listener_running.set()
                await release_listener.wait()
            return CommandStatus(cmd, "", Status.Success, 0)

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
            s._ensure_initialized = AsyncMock()
            s.alive = True
            s._on_output = None
            return s

        with (
            patch("otto.host.session.TelnetClient", side_effect=_new_client),
            patch("otto.host.session.TelnetSession", side_effect=_new_session),
        ):
            listener_task = asyncio.create_task(
                h.oneshot("nc -l 45681 < /dev/null > /tmp/x 2>/dev/null", timeout=None),
            )
            # Wait until the listener is actually running inside its
            # session, so we know it's holding whatever resource the
            # cache uses.
            await asyncio.wait_for(listener_running.wait(), timeout=1.0)

            # A concurrent oneshot() call must NOT block on the listener.
            # Under the bug this deadlocks and wait_for raises TimeoutError.
            try:
                await asyncio.wait_for(h.oneshot("echo concurrent"), timeout=1.0)
            except asyncio.TimeoutError:
                pytest.fail(
                    "h.oneshot() deadlocked waiting for a concurrent long-"
                    "running telnet oneshot — reproduces the "
                    "'Remote nc listener on <ip>:<port> not ready' "
                    "failure in _put_files_nc on telnet hosts",
                )
            finally:
                release_listener.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(listener_task, timeout=1.0)

        await h.close()

    @pytest.mark.asyncio
    async def test_oneshot_timeout_forwarded(self, host: UnixHost):
        process = self._mock_ssh_process([])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        await host.oneshot("sleep 5", timeout=30.0)

        host._connections._ssh_conn.create_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_oneshot_forwards_log_false(self):
        from unittest.mock import AsyncMock

        from otto.host.unix_host import UnixHost
        from otto.utils import CommandStatus, Status

        h = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=False)
        h._session_mgr = AsyncMock()
        h._session_mgr.oneshot.return_value = CommandStatus(
            command="c",
            output="",
            status=Status.Success,
            retcode=0,
        )
        await h.oneshot("base64 /bin/ls", log=False)
        h._session_mgr.oneshot.assert_awaited_once_with(
            "base64 /bin/ls",
            timeout=None,
            log=False,
        )


# ---------------------------------------------------------------------------
# File transfer: not-connected errors
# ---------------------------------------------------------------------------


class TestNotConnectedFileTransfer:
    @pytest.mark.asyncio
    async def test_scp_get_raises(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await host.get([Path("/remote/file.txt")], Path("/tmp"))

    @pytest.mark.asyncio
    async def test_scp_put_raises(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await host.put([Path("/tmp/file.txt")], Path("/tmp"))

    @pytest.mark.asyncio
    async def test_sftp_get_raises(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="sftp", log=False)
        with (
            patch.object(
                h._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.get([Path("/remote/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_raises(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="sftp", log=False)
        with (
            patch.object(
                h._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.put([Path("/tmp/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_raises(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="ftp", log=False)
        with (
            patch.object(
                h._connections,
                "ftp",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.get([Path("/remote/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_raises(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="ftp", log=False)
        with (
            patch.object(
                h._connections,
                "ftp",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.put([Path("/tmp/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_raises(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=False)

        # The file-size stat succeeds; the nc send oneshot fails (not
        # connected) — get must surface that as an error, not raise.
        async def mock_oneshot(cmd: str, **kw) -> CommandStatus:
            if cmd.startswith("stat -c %s"):
                return CommandStatus(cmd, "0", Status.Success, 0)
            raise RuntimeError("not connected")

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_server.sockets = [MagicMock()]
            mock_server.sockets[0].getsockname.return_value = ("0.0.0.0", 9999)
            return mock_server

        with (
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch.object(h, "oneshot", new_callable=AsyncMock, side_effect=mock_oneshot),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, _ = await h.get([Path("/remote/file.txt")], Path("/tmp"))
        assert status == Status.Error
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_raises(self, tmp_path: Path):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=False)
        src = tmp_path / "file.txt"
        src.write_bytes(b"data")
        with (
            patch.object(
                h, "oneshot", new_callable=AsyncMock, side_effect=RuntimeError("not connected")
            ),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(side_effect=ConnectionError("nc listener not ready")),
            ),
        ):
            status, _ = await h.put([src], Path("/tmp"))
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
    async def test_scp_get_success(self, host: UnixHost):
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch("asyncssh.scp", new_callable=AsyncMock) as mock_scp:
            status, msg = await host.get([Path("/etc/hostname")], Path("/tmp"), show_progress=False)
        assert status == Status.Success
        assert msg == ""
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_scp_put_success(self, host: UnixHost, tmp_path: Path):
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch("asyncssh.scp", new_callable=AsyncMock) as mock_scp:
            status, _msg = await host.put([src], Path("/tmp"), show_progress=False)
        assert status == Status.Success
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_sftp_get_success(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="sftp", log=False)
        mock_sftp = MagicMock()
        mock_sftp.get = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, _msg = await h.get([Path("/etc/hostname")], Path("/tmp"), show_progress=False)
        assert status == Status.Success
        mock_sftp.get.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_success(self, tmp_path: Path):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="sftp", log=False)
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        mock_sftp = MagicMock()
        mock_sftp.put = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, _msg = await h.put([src], Path("/tmp"), show_progress=False)
        assert status == Status.Success
        mock_sftp.put.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_success(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="ftp", log=False)
        mock_ftp = MagicMock()
        mock_ftp.download = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, _msg = await h.get(
            [Path("/home/vagrant/test.txt")], Path("/tmp"), show_progress=False
        )
        assert status == Status.Success
        mock_ftp.download.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_success(self, tmp_path: Path):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="ftp", log=False)
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        mock_ftp = MagicMock()
        mock_ftp.upload = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, _msg = await h.put([src], Path("/tmp"), show_progress=False)
        assert status == Status.Success
        mock_ftp.upload.assert_called_once()
        await h.close()


# ---------------------------------------------------------------------------
# File transfer: netcat mocked unit tests
# ---------------------------------------------------------------------------


class TestNcFileTransfer:
    @pytest.mark.asyncio
    async def test_nc_get_success(self, tmp_path: Path):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=False)

        send_cs = CommandStatus("nc ...", "", Status.Success, 0)

        # Control-plane ops (the file-size stat) and the nc send all route
        # through `oneshot` now — no dedicated monitor session.
        async def mock_oneshot(cmd: str, **kw) -> CommandStatus:
            if cmd.startswith("stat -c %s"):
                return CommandStatus(cmd, "1024", Status.Success, 0)
            return send_cs

        dest = tmp_path / "out"
        dest.mkdir()

        file_data = b"hello world"

        async def fake_start_server(cb, host, port):
            """Simulate asyncio.start_server: invoke the callback with a reader that yields file_data."""
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()

            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b""])
            writer = MagicMock()
            writer.close = MagicMock()

            # Fire the connection handler so it writes the file
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(cb(reader, writer)))
            return mock_server

        with (
            patch.object(h, "oneshot", AsyncMock(side_effect=mock_oneshot)) as mock_os,
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, msg = await h.get([Path("/remote/file.txt")], dest, show_progress=False)

        assert status == Status.Success
        assert msg == ""
        assert (dest / "file.txt").read_bytes() == file_data
        # The file-size stat ran as a control-plane oneshot.
        assert any(
            c.args and c.args[0] == "stat -c %s /remote/file.txt" for c in mock_os.await_args_list
        )
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_success(self, tmp_path: Path):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=False)

        src = tmp_path / "upload.txt"
        src.write_bytes(b"test content")

        # Command-dispatched responses instead of a positional side_effect list:
        # _put_files_nc now also runs `_wait_for_remote_listener`, which
        # probes for `ss`/`netstat` and then polls the listener — several
        # extra oneshot calls whose order a positional list can't capture.
        async def mock_oneshot(cmd: str, **kw) -> CommandStatus:
            if "nc -l" in cmd:
                return CommandStatus(cmd, "", Status.Success, 0)
            if cmd.startswith("type "):
                return CommandStatus(cmd, "", Status.Success, 0)
            if "ss -tln" in cmd or "netstat -tln" in cmd or "/proc/net/tcp" in cmd:
                return CommandStatus(cmd, "", Status.Success, 0)
            if cmd.startswith("stat -c %s "):
                return CommandStatus(cmd, str(src.stat().st_size), Status.Success, 0)
            # Port discovery (ss/netstat/python/proc) returns a port number.
            return CommandStatus(cmd, "44444", Status.Success, 0)

        sent_data = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=lambda d: sent_data.extend(d))
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        with (
            patch.object(h, "oneshot", AsyncMock(side_effect=mock_oneshot)),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
        ):
            status, msg = await h.put([src], Path("/tmp"), show_progress=False)

        assert status == Status.Success
        assert msg == ""
        assert sent_data == b"test content"
        mock_writer.drain.assert_called()
        mock_writer.close.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """During put, host.log must be False so per-host records are
        dropped by HostFilter; it must be restored to its prior value after
        the transfer completes."""
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=True)

        src = tmp_path / "upload.txt"
        src.write_bytes(b"test content")

        log_states: list[bool] = []

        async def oneshot_capturing_log(cmd: str, **_kw) -> CommandStatus:
            log_states.append(h.log)
            # Compound strategy probe runs first (warm-up); return a valid
            # port+listener pair so the cascades don't fire.
            if cmd.startswith("port=proc; listener=proc"):
                return CommandStatus(cmd, "python proc", Status.Success, 0)
            if "nc -l" in cmd:
                return CommandStatus(cmd, "", Status.Success, 0)
            if "ss -tln" in cmd or "netstat -tln" in cmd or "/proc/net/tcp" in cmd:
                return CommandStatus(cmd, "", Status.Success, 0)
            if cmd.startswith("stat -c %s "):
                return CommandStatus(cmd, str(src.stat().st_size), Status.Success, 0)
            # Port discovery returns a port number.
            return CommandStatus(cmd, "44444", Status.Success, 0)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        assert h.log is True
        with (
            patch.object(h, "oneshot", AsyncMock(side_effect=oneshot_capturing_log)),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
        ):
            status, _ = await h.put([src], Path("/tmp"), show_progress=False)

        assert status == Status.Success
        assert log_states and all(state is False for state in log_states)
        assert h.log is True
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """Symmetric check for get — the file-size stat and the send oneshot
        must both run with host.log == False."""
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, transfer="nc", log=True)

        send_cs = CommandStatus("nc ...", "", Status.Success, 0)

        log_states: list[bool] = []

        dest = tmp_path / "out"
        dest.mkdir()

        file_data = b"hello world"

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b""])
            writer = MagicMock()
            writer.close = MagicMock()
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(cb(reader, writer)))
            return mock_server

        async def oneshot_capturing_log(cmd: str, *_a, **_kw) -> CommandStatus:
            log_states.append(h.log)
            if cmd.startswith("stat -c %s"):
                return CommandStatus(cmd, "11", Status.Success, 0)
            return send_cs

        assert h.log is True
        with (
            patch.object(h, "oneshot", AsyncMock(side_effect=oneshot_capturing_log)),
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, _ = await h.get([Path("/remote/file.txt")], dest, show_progress=False)

        assert status == Status.Success
        assert log_states and all(state is False for state in log_states)
        assert h.log is True
        await h.close()


# ---------------------------------------------------------------------------
# open_session() — session creation (unit)
# ---------------------------------------------------------------------------


class TestOpenSession:
    """Unit tests for UnixHost.open_session() — session creation and registration."""

    def _mock_shell_session(self, alive: bool = True) -> MagicMock:
        ok = CommandStatus("echo hi", "hi", Status.Success, 0)
        session = MagicMock(spec=ShellSession)
        session.alive = alive
        session.run_cmd = AsyncMock(return_value=ok)
        session.send = AsyncMock()
        session.expect = AsyncMock(return_value="output")
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
    async def test_ssh_returns_remote_session(self, host: UnixHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            result = await host.open_session("monitor")
        assert isinstance(result, HostSession)
        assert result.alive is True

    @pytest.mark.asyncio
    async def test_ssh_session_registered_in_host(self, host: UnixHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            result = await host.open_session("monitor")
        assert host._session_mgr._named_sessions["monitor"] is result

    @pytest.mark.asyncio
    async def test_ssh_session_uses_existing_conn(self, host: UnixHost):
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        host._connections._ssh_conn = mock_conn
        mock_shell = self._mock_shell_session()
        with patch("otto.host.session.SshSession", return_value=mock_shell) as MockSshSession:  # noqa: N806 — CapWords for a class mock
            await host.open_session("monitor")
        MockSshSession.assert_called_once_with(
            mock_conn,
            command_frame=None,
            init_timeout=None,
        )

    # --- Telnet ---

    @pytest.mark.asyncio
    async def test_telnet_returns_remote_session(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        mock_shell = self._mock_shell_session()
        with (
            patch("otto.host.session.TelnetClient", return_value=self._mock_telnet_client()),
            patch("otto.host.session.TelnetSession", return_value=mock_shell),
        ):
            result = await h.open_session("monitor")
        assert isinstance(result, HostSession)
        assert result.alive is True
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_connects_new_client(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        mock_client = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=self._mock_shell_session()),
        ):
            await h.open_session("monitor")
        mock_client.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_session_owns_its_client(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        mock_client = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch(
                "otto.host.session.TelnetSession", return_value=self._mock_shell_session()
            ) as MockTelnetSession,  # noqa: N806 — CapWords for a class mock
        ):
            await h.open_session("monitor")
        MockTelnetSession.assert_called_once_with(
            mock_client.reader,
            mock_client.writer,
            _owned_client=mock_client,
            command_frame=None,
            init_timeout=None,
            write_chunk_size=mock_client.options.write_chunk_size,
            write_chunk_delay=mock_client.options.write_chunk_delay,
        )
        await h.close()

    # --- Multiple SSH sessions ---

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_are_distinct_objects(self, host: UnixHost):
        shell_a = self._mock_shell_session()
        shell_b = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", side_effect=[shell_a, shell_b]):
            session_a = await host.open_session("alpha")
            session_b = await host.open_session("beta")
        assert session_a is not session_b
        assert host._session_mgr._named_sessions["alpha"] is session_a
        assert host._session_mgr._named_sessions["beta"] is session_b

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_both_alive(self, host: UnixHost):
        host._connections._ssh_conn = MagicMock()
        with patch(
            "otto.host.session.SshSession",
            side_effect=[
                self._mock_shell_session(),
                self._mock_shell_session(),
            ],
        ):
            s1 = await host.open_session("s1")
            s2 = await host.open_session("s2")
        assert s1.alive is True
        assert s2.alive is True

    # --- Multiple Telnet sessions ---

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_create_own_client(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", side_effect=[client_a, client_b]),
            patch(
                "otto.host.session.TelnetSession",
                side_effect=[
                    self._mock_shell_session(),
                    self._mock_shell_session(),
                ],
            ),
        ):
            await h.open_session("alpha")
            await h.open_session("beta")
        client_a.connect.assert_called_once()
        client_b.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_own_separate_client(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, term="telnet", log=False)
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", side_effect=[client_a, client_b]),
            patch(
                "otto.host.session.TelnetSession",
                side_effect=[
                    self._mock_shell_session(),
                    self._mock_shell_session(),
                ],
            ) as MockTelnetSession,  # noqa: N806 — CapWords for a class mock
        ):
            await h.open_session("alpha")
            await h.open_session("beta")
        calls = MockTelnetSession.call_args_list
        assert calls[0].kwargs["_owned_client"] is client_a
        assert calls[1].kwargs["_owned_client"] is client_b
        await h.close()

    # --- Mix of SSH host and Telnet host ---

    @pytest.mark.asyncio
    async def test_ssh_host_and_telnet_host_each_hold_own_sessions(self):
        """An SSH host and a Telnet host can hold independent named sessions simultaneously."""
        ssh_host = UnixHost(
            ip="10.0.0.1", element="ssh-box", creds={"u": "p"}, term="ssh", log=False
        )
        telnet_host = UnixHost(
            ip="10.0.0.2", element="tel-box", creds={"u": "p"}, term="telnet", log=False
        )

        ssh_shell = self._mock_shell_session()
        telnet_shell = self._mock_shell_session()
        mock_client = self._mock_telnet_client()

        ssh_host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=ssh_shell):
            ssh_session = await ssh_host.open_session("monitor")

        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=telnet_shell),
        ):
            telnet_session = await telnet_host.open_session("monitor")

        assert isinstance(ssh_session, HostSession)
        assert isinstance(telnet_session, HostSession)
        assert ssh_session is not telnet_session
        assert ssh_host._session_mgr._named_sessions["monitor"] is ssh_session
        assert telnet_host._session_mgr._named_sessions["monitor"] is telnet_session
        await telnet_host.close()

    # --- Reuse and replacement ---

    @pytest.mark.asyncio
    async def test_reuse_live_session_returns_same_object(self, host: UnixHost):
        mock_shell = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            first = await host.open_session("monitor")
            second = await host.open_session("monitor")
        assert first is second

    @pytest.mark.asyncio
    async def test_dead_session_is_replaced(self, host: UnixHost):
        shell_old = self._mock_shell_session(alive=True)
        shell_new = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", side_effect=[shell_old, shell_new]):
            first = await host.open_session("monitor")
            first._session.alive = False
            second = await host.open_session("monitor")
        assert first is not second
        assert host._session_mgr._named_sessions["monitor"] is second

    # --- Error cases ---

    @pytest.mark.asyncio
    async def test_unknown_term_raises_value_error(self):
        h = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=False)
        h.term = "foobar"  # type: ignore
        h._connections.term = "foobar"  # type: ignore
        with pytest.raises(ValueError, match="foobar"):
            await h.open_session("monitor")


# ---------------------------------------------------------------------------
# HostSession proxy — delegation and lifecycle (unit)
# ---------------------------------------------------------------------------


class TestHostSessionProxy:
    """Unit tests for HostSession — argument forwarding, state, and cleanup."""

    def _make_remote_session(
        self,
        host: UnixHost,
        name: str = "monitor",
        alive: bool = True,
    ) -> tuple[HostSession, MagicMock]:
        ok = CommandStatus("echo hi", "hi", Status.Success, 0)
        shell = MagicMock(spec=ShellSession)
        shell.alive = alive
        shell.run_cmd = AsyncMock(return_value=ok)
        shell.send = AsyncMock()
        shell.expect = AsyncMock(return_value="some output")
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
    async def test_run_returns_command_status(self, host: UnixHost):
        session, _ = self._make_remote_session(host)
        result = (await session.run("echo hi")).only
        assert isinstance(result, CommandStatus)
        assert result.status == Status.Success
        assert result.output == "hi"

    @pytest.mark.asyncio
    async def test_run_delegates_cmd_to_shell_session(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.run("ls /tmp")
        shell.run_cmd.assert_called_once_with("ls /tmp", expects=None, timeout=10.0, on_output=None)

    @pytest.mark.asyncio
    async def test_run_forwards_expects(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        expects = [(r"Password:", "secret\n")]
        await session.run("sudo ls", expects=expects)  # type: ignore[arg-type]
        shell.run_cmd.assert_called_once_with(
            "sudo ls", expects=expects, timeout=10.0, on_output=None
        )

    @pytest.mark.asyncio
    async def test_run_forwards_timeout(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.run("sleep 5", timeout=60.0)
        shell.run_cmd.assert_called_once_with("sleep 5", expects=None, timeout=60.0, on_output=None)

    @pytest.mark.asyncio
    async def test_send_delegates(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.send("hello\n")
        shell.send.assert_called_once_with("hello\n")

    @pytest.mark.asyncio
    async def test_expect_delegates_and_returns_output(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        result = await session.expect(r"\$")
        shell.expect.assert_called_once_with(r"\$", 10.0)
        assert result == "some output"

    @pytest.mark.asyncio
    async def test_expect_forwards_timeout(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.expect(r"\$", timeout=5.0)
        shell.expect.assert_called_once_with(r"\$", 5.0)

    def test_alive_true_when_session_alive(self, host: UnixHost):
        session, _ = self._make_remote_session(host, alive=True)
        assert session.alive is True

    def test_alive_false_when_session_dead(self, host: UnixHost):
        session, _ = self._make_remote_session(host, alive=False)
        assert session.alive is False

    @pytest.mark.asyncio
    async def test_close_calls_underlying_session_close(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.close()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_removes_from_host_registry(self, host: UnixHost):
        session, _ = self._make_remote_session(host, name="monitor")
        assert "monitor" in host._session_mgr._named_sessions
        await session.close()
        assert "monitor" not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exit(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        async with session:
            shell.close.assert_not_called()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_removes_from_registry_on_exit(self, host: UnixHost):
        session, _ = self._make_remote_session(host, name="monitor")
        async with session:
            assert "monitor" in host._session_mgr._named_sessions
        assert "monitor" not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_yields_self(self, host: UnixHost):
        session, _ = self._make_remote_session(host)
        async with session as ctx:
            assert ctx is session


# ---------------------------------------------------------------------------
# Host-level lifecycle with named sessions (unit)
# ---------------------------------------------------------------------------


class TestOpenSessionCleanup:
    """Unit tests for host.close() and _connected interactions with named sessions."""

    def _add_mock_session(self, host: UnixHost, name: str, alive: bool = True) -> MagicMock:
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
    async def test_host_close_closes_all_named_sessions(self, host: UnixHost):
        shell_a = self._add_mock_session(host, "a")
        shell_b = self._add_mock_session(host, "b")
        await host.close()
        shell_a.close.assert_called_once()
        shell_b.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_host_close_clears_registry(self, host: UnixHost):
        self._add_mock_session(host, "monitor")
        await host.close()
        assert host._session_mgr._named_sessions == {}

    def test_connected_true_with_live_named_session(self, host: UnixHost):
        self._add_mock_session(host, "monitor", alive=True)
        assert host._connected is True

    def test_connected_false_when_named_session_dead(self, host: UnixHost):
        self._add_mock_session(host, "monitor", alive=False)
        assert host._connected is False

    def test_connected_false_with_no_sessions(self, host: UnixHost):
        assert host._connected is False

    def test_connected_true_with_multiple_sessions_one_alive(self, host: UnixHost):
        self._add_mock_session(host, "dead", alive=False)
        self._add_mock_session(host, "live", alive=True)
        assert host._connected is True


@pytest.mark.asyncio
async def test_host_current_user_reads_default_session():
    from unittest.mock import MagicMock

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds={"admin": "secret"}, user="admin", log=False
    )
    transport = MagicMock(spec=ShellSession)
    transport.current_user = "admin"
    host._session_mgr._session = transport
    assert host.current_user == "admin"


@pytest.mark.asyncio
async def test_unix_switch_user_updates_host_current_user():
    from unittest.mock import AsyncMock, MagicMock

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=False,
    )
    transport = MagicMock(spec=ShellSession)
    transport.alive = True
    transport.send = AsyncMock()
    transport.expect = AsyncMock(return_value="Password:")
    transport.current_user = "admin"
    host._session_mgr._session = transport
    await host.switch_user("root")
    assert host.current_user == "root"


# ---------------------------------------------------------------------------
# Kernel modules
# ---------------------------------------------------------------------------


def _unix_host():
    from otto.host.unix_host import UnixHost

    return UnixHost(
        ip="10.0.0.1", element="box", creds={"admin": "secret"}, user="admin", log=False
    )


@pytest.mark.asyncio
async def test_loaded_modules_parses_proc_modules_column_one():
    from unittest.mock import AsyncMock

    from otto.utils import CommandStatus, Status

    host = _unix_host()
    proc = "ext4 737280 2 - Live 0x0\nnvme 49152 3 nvme_core, Live 0x0\n"
    host.oneshot = AsyncMock(
        return_value=CommandStatus("cat /proc/modules", proc, Status.Success, 0)
    )
    assert await host._loaded_modules() == ["ext4", "nvme"]


@pytest.mark.asyncio
async def test_loaded_modules_empty_when_read_fails():
    from unittest.mock import AsyncMock

    from otto.utils import CommandStatus, Status

    host = _unix_host()
    host.oneshot = AsyncMock(return_value=CommandStatus("cat /proc/modules", "", Status.Error, 1))
    assert await host._loaded_modules() == []


@pytest.mark.asyncio
async def test_lsmod_returns_loaded_module_names():
    from unittest.mock import AsyncMock

    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=["ext4", "nvme"])
    assert await host.lsmod() == ["ext4", "nvme"]


def _run_result(cmd, output, status, retcode):
    from otto.host.host import RunResult
    from otto.utils import CommandStatus

    return RunResult(status=status, statuses=[CommandStatus(cmd, output, status, retcode)])


@pytest.mark.asyncio
async def test_load_stages_then_insmod_sudo_for_nonroot(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"  # non-root
    ko = tmp_path / "my-mod.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/my-mod.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    status, msg = await host.load(ko)
    assert status is Status.Success and msg == ""
    host.put.assert_awaited_once()
    assert host.run.await_args.args[0] == "insmod /tmp/my-mod.ko"
    assert host.run.await_args.kwargs["sudo"] is True
    host.rm.assert_awaited_once()  # staged file cleaned up


@pytest.mark.asyncio
async def test_load_no_sudo_when_current_user_root(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "root"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/m.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    await host.load(ko)
    assert host.run.await_args.kwargs["sudo"] is False


@pytest.mark.asyncio
async def test_load_put_failure_short_circuits(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Error, "scp failed"))
    host.run = AsyncMock()
    status, msg = await host.load(ko)
    assert status is Status.Error and "staging" in msg
    host.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_error_message_uses_normalized_name(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "foo-bar.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(
        return_value=_run_result("insmod ...", "Invalid module format", Status.Error, 1)
    )
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    status, msg = await host.load(ko)
    assert status is Status.Error
    assert "foo_bar" in msg and "Invalid module format" in msg


@pytest.mark.asyncio
async def test_unload_idempotent_when_not_resident():
    from unittest.mock import AsyncMock

    from otto.utils import Status

    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=["ext4"])
    host.run = AsyncMock()
    status, msg = await host.unload("my_mod")
    assert status is Status.Success and msg == ""
    host.run.assert_not_awaited()  # not resident → no rmmod


@pytest.mark.asyncio
async def test_unload_rmmod_with_sudo_when_resident():
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=["my_mod"])
    host.run = AsyncMock(return_value=_run_result("rmmod my_mod", "", Status.Success, 0))
    status, _msg = await host.unload("my-mod")  # dash normalized to my_mod
    assert status is Status.Success
    assert host.run.await_args.args[0] == "rmmod my_mod"
    assert host.run.await_args.kwargs["sudo"] is True


@pytest.mark.asyncio
async def test_unload_error_maps_rmmod_failure():
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=["my_mod"])
    host.run = AsyncMock(
        return_value=_run_result("rmmod my_mod", "Module my_mod is in use", Status.Error, 1)
    )
    status, msg = await host.unload("my_mod")
    assert status is Status.Error and "in use" in msg


@pytest.mark.asyncio
async def test_lsmod_dry_run_returns_empty():
    """Dry-run yields no module list (not the dry-run banner parsed as a name)."""
    from tests.conftest import active_context

    host = _unix_host()
    with active_context(dry_run=True):
        assert await host.lsmod() == []


@pytest.mark.asyncio
async def test_unload_dry_run_issues_rmmod_without_idempotency_check():
    """Under dry-run the idempotency check is skipped, so the would-be ``rmmod``
    is still issued (symmetric with load's dry-run insmod)."""
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status
    from tests.conftest import active_context

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=[])  # would short-circuit if consulted
    host.run = AsyncMock(return_value=_run_result("rmmod foo", "[DRY RUN]", Status.Skipped, 0))
    with active_context(dry_run=True):
        status, _ = await host.unload("foo")
    host.run.assert_awaited_once()
    assert host.run.await_args.args[0] == "rmmod foo"
    host._loaded_modules.assert_not_awaited()  # idempotency check skipped in dry-run
    assert status is Status.Success
