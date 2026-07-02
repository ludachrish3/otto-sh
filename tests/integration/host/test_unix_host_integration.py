"""
Unix-host integration tests (formerly tests/unit/host/test_host_integration.py).

These exercise behavior that is **specific to a POSIX shell** — bash builtins
(``echo``, ``cd``, ``export``), Linux commands (``uname -s``, ``ls``), and
transfer protocols (scp/sftp/ftp/nc). The contract these tests assert is
"this is what a Unix host does", not "this is what every otto host does";
the cross-OS contract lives in :mod:`test_host_contract` and is parametrized
over Unix and Zephyr backends both.

The file was renamed from ``test_host_integration`` and moved out of the
unit tree because it has no unit-test scope (every test carries
``@pytest.mark.integration`` and requires a Vagrant test VM). The
parametrized ``host1`` fixture lives in :mod:`tests.conftest` and resolves
``ssh`` / ``telnet`` / ``local`` to :class:`UnixHost` / :class:`LocalHost`.
"""

import logging
import time
from pathlib import Path

import pytest

from otto.host.host import Host
from otto.host.session import ShellSession
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests.conftest import host_data, make_host
from tests.integration.host._transfer_retry import transfer_with_retry

pytestmark = pytest.mark.timeout(30)

_ALL_HOSTS = pytest.mark.parametrize("host1", ["ssh", "telnet", "local"], indirect=True)
_REMOTE_ONLY = pytest.mark.parametrize("host1", ["ssh", "telnet"], indirect=True)
_ALL_TRANSFERS = pytest.mark.parametrize(
    "transfer_host",
    [
        "scp",
        "sftp",
        "ftp",
        "nc",
        pytest.param(("nc", "telnet"), id="nc-telnet"),
    ],
    indirect=True,
)


# ---------------------------------------------------------------------------
# Basic command execution
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestBasicCommands:
    @pytest.mark.asyncio
    async def test_echo(self, host1: Host):
        result = (await host1.run("echo hello")).only
        assert result.status == Status.Success
        assert "hello" in result.value

    @pytest.mark.asyncio
    async def test_multiple_commands_run_in_order(self, host1: Host):
        result = await host1.run(["echo first", "echo second"])
        assert result.status == Status.Success
        assert len(result) == 2
        assert "first" in result[0].value
        assert "second" in result[1].value

    @pytest.mark.asyncio
    async def test_uname_returns_linux(self, host1: Host):
        result = (await host1.run("uname -s")).only
        assert result.status == Status.Success
        assert "Linux" in result.value

    @pytest.mark.asyncio
    async def test_multiline_output(self, host1: Host):
        result = (await host1.run("echo -e 'line1\\nline2\\nline3'")).only
        assert result.status == Status.Success
        lines = result.value.strip().splitlines()
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_failing_command_returns_failed_status(self, host1: Host):
        result = (await host1.run("ls /nonexistent_dir_otto_test")).only
        assert result.status == Status.Failed
        # GNU `ls` returns 2 for a missing path on every backend (ssh/telnet/local).
        assert result.retcode == 2

    @pytest.mark.asyncio
    async def test_unexpected_eof_returns_error(self, host1: Host):
        result = (await host1.run("exit 42")).only
        assert result.status == Status.Error
        assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_overall_status_reflects_failure(self, host1: Host):
        result = await host1.run(["echo ok", "ls /nonexistent_dir_otto_test"])
        assert result.status == Status.Failed
        assert result[0].status == Status.Success
        assert result[1].status == Status.Failed


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_cd_persists_between_commands(self, host1: Host):
        await host1.run("cd /")
        await host1.run("cd tmp")
        result = (await host1.run("pwd")).only
        assert result.status == Status.Success
        assert result.value.strip() == "/tmp"

    @pytest.mark.asyncio
    async def test_env_var_persists(self, host1: Host):
        await host1.run("export OTTO_TEST_VAR=hello123")
        result = (await host1.run("echo $OTTO_TEST_VAR")).only
        assert result.status == Status.Success
        assert "hello123" in result.value


# ---------------------------------------------------------------------------
# Timeout and recovery
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, host1: Host):
        result = (await host1.run("sleep 999", timeout=0.1)).only
        assert result.status == Status.Error, (
            f"expected Status.Error, got {result.status!r}; "
            f"retcode={result.retcode!r} output={result.value!r}"
        )
        assert "timed out" in result.value, (
            f"expected 'timed out' in output; "
            f"status={result.status!r} retcode={result.retcode!r} output={result.value!r}"
        )
        # The SSH process is killed on timeout, surfacing the sentinel retcode.
        # ``LocalHost`` carries no ``term``; the assertion is ssh-scoped.
        if getattr(host1, "term", None) == "ssh":
            assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_session_recovers_after_timeout(self, host1: Host):
        await host1.run("sleep 999", timeout=0.1)
        result = (await host1.run("echo recovered")).only
        assert result.status == Status.Success
        assert "recovered" in result.value


# ---------------------------------------------------------------------------
# Send / Expect
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestSendExpect:
    @pytest.mark.asyncio
    async def test_python_repl(self, host1: Host):
        # Use -i to force interactive mode (local sessions use PIPE, not PTY)
        await host1.send("python3 -i -c ''\n")
        await host1.expect(r">>> ", timeout=5.0)
        await host1.send("print('otto_test')\n")
        output = await host1.expect(r">>> ", timeout=5.0)
        assert "otto_test" in output
        await host1.send("exit()\n")


# ---------------------------------------------------------------------------
# Named sessions
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestNamedSessionIntegration:
    @pytest.mark.asyncio
    async def test_named_session_runs_command(self, host1: Host):
        mon = await host1.open_session("monitor")
        result = (await mon.run("echo hello")).only
        assert result.status == Status.Success
        assert "hello" in result.value
        await mon.close()

    @pytest.mark.asyncio
    async def test_two_sessions_have_independent_state(self, host1: Host):
        s1 = await host1.open_session("s1")
        s2 = await host1.open_session("s2")
        await s1.run("cd /tmp")
        await s2.run("cd /home")
        r1 = (await s1.run("pwd")).only
        r2 = (await s2.run("pwd")).only
        assert r1.value.strip() == "/tmp"
        assert "/home" in r2.value.strip()
        await s1.close()
        await s2.close()

    @pytest.mark.asyncio
    async def test_context_manager_removes_session_from_registry(self, host1: Host):
        async with await host1.open_session("monitor") as mon:
            assert "monitor" in host1._session_mgr._named_sessions
            result = (await mon.run("echo hi")).only
            assert result.status == Status.Success
        assert "monitor" not in host1._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_host_close_closes_all_named_sessions(self, host1: Host):
        s1 = await host1.open_session("s1")
        s2 = await host1.open_session("s2")
        # Sessions initialize lazily on first I/O — run a command to make them alive
        await s1.run("echo init")
        await s2.run("echo init")
        assert s1.alive
        assert s2.alive
        await host1.close()
        assert not s1.alive
        assert not s2.alive
        assert host1._session_mgr._named_sessions == {}


# ---------------------------------------------------------------------------
# Incremental output logging
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestIncrementalLogging:
    """Verify that command output is logged line-by-line as it arrives."""

    @pytest.mark.asyncio
    async def test_multiline_output_logged_incrementally(self, host1: Host, caplog):
        """Each output line appears as a separate log record with distinct timestamps."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (
                await host1.run(
                    "for i in 1 2 3; do echo line_$i; sleep 0.05; done",
                    timeout=10.0,
                )
            ).only

        assert result.status == Status.Success
        # Each line should have been logged individually
        output_records = [
            r for r in caplog.records if hasattr(r, "host") and "line_" in r.getMessage()
        ]
        assert len(output_records) >= 3
        # Timestamps should be spread out (not all batched at the end)
        if len(output_records) >= 2:
            span = output_records[-1].created - output_records[0].created
            assert span > 0.05, "Log records should arrive incrementally, not all at once"

    @pytest.mark.asyncio
    async def test_long_running_command_logs_before_completion(self, host1: Host, caplog):
        """Output produced early is logged before the command finishes."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            start = time.time()
            result = (
                await host1.run(
                    "echo early_output; sleep 0.1; echo late_output",
                    timeout=10.0,
                )
            ).only
            end = time.time()

        assert result.status == Status.Success
        early_records = [
            r
            for r in caplog.records
            if hasattr(r, "host") and "early_output" in r.getMessage() and "> |" in r.getMessage()
        ]
        assert len(early_records) >= 1, "'early_output' should have been logged"
        # early_output should have been logged well before the command finished
        assert early_records[0].created - start < end - start, (
            "early_output should be logged before command completion"
        )

    @pytest.mark.asyncio
    async def test_no_sentinels_in_logs(self, host1: Host, caplog):
        """Sentinel markers should never appear in log output."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (await host1.run("echo sentinel_test", timeout=10.0)).only

        assert result.status == Status.Success
        for record in caplog.records:
            msg = record.getMessage()
            assert "__OTTO_" not in msg, f"Sentinel found in log: {msg}"


# ---------------------------------------------------------------------------
# Multi-host reachability
# ---------------------------------------------------------------------------


@_REMOTE_ONLY
class TestReachability:
    """A second host on the bed is independently reachable alongside host1.

    Remote-only: the test builds a second :class:`UnixHost` matching host1's
    term, which has no ``local`` analogue (``LocalHost`` carries no ``term``).
    """

    @pytest.mark.asyncio
    async def test_both_hosts_reachable(self, host1: Host):
        kwargs: dict[str, str] = {"term": host1.term}
        if host1.term == "telnet":
            kwargs["transfer"] = "ftp"
        host2 = make_host("tomato", **kwargs)
        try:
            for host in (host1, host2):
                result = (await host.run("echo ping")).only
                assert result.status == Status.Success
                assert "ping" in result.value
        finally:
            await host2.close()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    @pytest.mark.asyncio
    async def test_second_credential_works(self):
        """Verify the non-default (test) user can log in and run commands."""
        data = host_data("tomato")
        second_user, _second_password = list(data["creds"].items())[1]
        host = UnixHost(
            ip=data["ip"],
            user=second_user,
            element=data["element"],
            creds=data["creds"],
            board=data.get("board"),
        )
        try:
            result = (await host.run("whoami")).only
            assert result.status == Status.Success
            assert second_user in result.value
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_telnet_bad_credentials_fails_fast(self, monkeypatch):
        """A telnet login with a wrong password must raise a clear error
        promptly — not hang.

        Telnet ``login()`` no longer drains to silence; the bounded marker
        handshake in ``_ensure_initialized`` is what catches a failed login
        (the device stays in its login-prompt loop, so the READY marker can
        never appear). The timeout is shrunk here so the test stays fast.
        """
        monkeypatch.setattr(ShellSession, "_init_timeout", 3.0)

        data = host_data("carrot")
        user = next(iter(data["creds"].keys()))
        host = UnixHost(
            ip=data["ip"],
            user=user,
            element=data["element"],
            creds={user: "definitely-the-wrong-password"},
            board=data.get("board"),
            term="telnet",
            transfer="ftp",
        )
        try:
            start = time.monotonic()
            with pytest.raises(ConnectionError):
                await host.run("echo hello")
            elapsed = time.monotonic() - start
            # Bounded by the shrunk handshake timeout — proves it did not hang.
            assert elapsed < 15
        finally:
            await host.close()


# ---------------------------------------------------------------------------
# File transfer (SCP, SFTP, FTP, nc)
# ---------------------------------------------------------------------------


# NOTE: Transfers go through asyncssh/scp/sftp and have been observed to hang
# indefinitely when the remote SSH daemon stalls mid-protocol. get/put are
# wrapped in ``transfer_with_retry`` so an individual transfer is bounded
# and retried once, preventing the whole suite from blocking on a single flake.
@_ALL_TRANSFERS
class TestFileTransfer:
    @pytest.mark.asyncio
    async def test_get_file(self, transfer_host: UnixHost, tmp_path: Path):
        """Download /etc/hostname and verify it matches the hostname command."""
        result = (await transfer_host.run("hostname")).only
        expected_hostname = result.value.strip()

        res = await transfer_with_retry(
            lambda: transfer_host.get([Path("/etc/hostname")], tmp_path)
        )
        assert res.status == Status.Success, f"get failed: {res.msg}"

        local_hostname = (tmp_path / "hostname").read_text().strip()
        assert local_hostname == expected_hostname

    @pytest.mark.asyncio
    async def test_put_file(self, transfer_host: UnixHost, tmp_path: Path):
        """Upload a file, verify it arrived, clean up."""
        content = "file transfer test"
        src = tmp_path / f"otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt"
        src.write_text(content)
        remote_path = f"/tmp/otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt"

        res = await transfer_with_retry(lambda: transfer_host.put([src], Path("/tmp")))
        assert res.status == Status.Success, f"put failed: {res.msg}"

        result = (await transfer_host.run(f"cat {remote_path}")).only
        assert content in result.value

        await transfer_host.run(f"rm -f {remote_path}")
