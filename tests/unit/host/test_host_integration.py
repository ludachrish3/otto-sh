"""
Shared integration tests that run against all host backends (SSH, Telnet, Local).

These test session-level behavior that is common across all host types.
Remote-only tests (file transfer protocols, multi-host, credentials) stay
in test_remoteHost.py.
"""

import logging
import time

import pytest

from otto.host.host import Host
from otto.utils import Status

pytestmark = pytest.mark.timeout(30)

_ALL_HOSTS = pytest.mark.parametrize("host1", ["ssh", "telnet", "local"], indirect=True)
_REMOTE_ONLY = pytest.mark.parametrize("host1", ["ssh", "telnet"], indirect=True)


# ---------------------------------------------------------------------------
# Basic command execution
# ---------------------------------------------------------------------------

@_ALL_HOSTS
class TestBasicCommands:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_echo(self, host1: Host):
        result = (await host1.run('echo hello')).only
        assert result.status == Status.Success
        assert 'hello' in result.output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiple_commands_run_in_order(self, host1: Host):
        result = await host1.run(['echo first', 'echo second'])
        assert result.status == Status.Success
        assert len(result.statuses) == 2
        assert 'first' in result.statuses[0].output
        assert 'second' in result.statuses[1].output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_uname_returns_linux(self, host1: Host):
        result = (await host1.run('uname -s')).only
        assert result.status == Status.Success
        assert 'Linux' in result.output

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiline_output(self, host1: Host):
        result = (await host1.run("echo -e 'line1\\nline2\\nline3'")).only
        assert result.status == Status.Success
        lines = result.output.strip().splitlines()
        assert len(lines) == 3

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failing_command_returns_failed_status(self, host1: Host):
        result = (await host1.run('ls /nonexistent_dir_otto_test')).only
        assert result.status == Status.Failed


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

@_ALL_HOSTS
class TestStatePersistence:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cd_persists_between_commands(self, host1: Host):
        await host1.run("cd /")
        await host1.run("cd tmp")
        result = (await host1.run("pwd")).only
        assert result.status == Status.Success
        assert result.output.strip() == "/tmp"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_env_var_persists(self, host1: Host):
        await host1.run("export OTTO_TEST_VAR=hello123")
        result = (await host1.run("echo $OTTO_TEST_VAR")).only
        assert result.status == Status.Success
        assert "hello123" in result.output


# ---------------------------------------------------------------------------
# Timeout and recovery
# ---------------------------------------------------------------------------

@_ALL_HOSTS
class TestTimeout:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_timeout_returns_error(self, host1: Host):
        result = (await host1.run("sleep 999", timeout=0.5)).only
        assert result.status == Status.Error, (
            f"expected Status.Error, got {result.status!r}; "
            f"retcode={result.retcode!r} output={result.output!r}"
        )
        assert "timed out" in result.output, (
            f"expected 'timed out' in output; "
            f"status={result.status!r} retcode={result.retcode!r} output={result.output!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_recovers_after_timeout(self, host1: Host):
        await host1.run("sleep 999", timeout=0.5)
        result = (await host1.run("echo recovered")).only
        assert result.status == Status.Success
        assert "recovered" in result.output


# ---------------------------------------------------------------------------
# Send / Expect
# ---------------------------------------------------------------------------

@_ALL_HOSTS
class TestSendExpect:

    @pytest.mark.asyncio
    @pytest.mark.integration
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
    @pytest.mark.integration
    async def test_named_session_runs_command(self, host1: Host):
        mon = await host1.open_session('monitor')
        result = (await mon.run('echo hello')).only
        assert result.status == Status.Success
        assert 'hello' in result.output
        await mon.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_sessions_have_independent_state(self, host1: Host):
        s1 = await host1.open_session('s1')
        s2 = await host1.open_session('s2')
        await s1.run('cd /tmp')
        await s2.run('cd /home')
        r1 = (await s1.run('pwd')).only
        r2 = (await s2.run('pwd')).only
        assert r1.output.strip() == '/tmp'
        assert '/home' in r2.output.strip()
        await s1.close()
        await s2.close()


# ---------------------------------------------------------------------------
# Incremental output logging
# ---------------------------------------------------------------------------

@_ALL_HOSTS
class TestIncrementalLogging:
    """Verify that command output is logged line-by-line as it arrives."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiline_output_logged_incrementally(self, host1: Host, caplog):
        """Each output line appears as a separate log record with distinct timestamps."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (await host1.run(
                "for i in 1 2 3; do echo line_$i; sleep 0.05; done",
                timeout=10.0,
            )).only

        assert result.status == Status.Success
        # Each line should have been logged individually
        output_records = [
            r for r in caplog.records
            if hasattr(r, 'host') and 'line_' in r.getMessage()
        ]
        assert len(output_records) >= 3
        # Timestamps should be spread out (not all batched at the end)
        if len(output_records) >= 2:
            span = output_records[-1].created - output_records[0].created
            assert span > 0.05, "Log records should arrive incrementally, not all at once"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_long_running_command_logs_before_completion(self, host1: Host, caplog):
        """Output produced early is logged before the command finishes."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            start = time.time()
            result = (await host1.run(
                "echo early_output; sleep 0.1; echo late_output",
                timeout=10.0,
            )).only
            end = time.time()

        assert result.status == Status.Success
        early_records = [
            r for r in caplog.records
            if hasattr(r, 'host') and 'early_output' in r.getMessage()
            and '> |' in r.getMessage()
        ]
        assert len(early_records) >= 1, "'early_output' should have been logged"
        # early_output should have been logged well before the command finished
        assert early_records[0].created - start < end - start, (
            "early_output should be logged before command completion"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_sentinels_in_logs(self, host1: Host, caplog):
        """Sentinel markers should never appear in log output."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (await host1.run("echo sentinel_test", timeout=10.0)).only

        assert result.status == Status.Success
        for record in caplog.records:
            msg = record.getMessage()
            assert "__OTTO_" not in msg, f"Sentinel found in log: {msg}"
