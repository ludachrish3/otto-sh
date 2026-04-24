"""Tests for global dry-run mode on hosts."""

from datetime import timedelta
from pathlib import Path

import pytest

from otto.host.host import isDryRun, setDryRun
from otto.host.localHost import LocalHost
from otto.host.remoteHost import RemoteHost
from otto.utils import Status


class TestGlobalDryRun:

    @pytest.fixture(autouse=True)
    def reset_global_dry_run(self):
        """Ensure the global dry-run flag is reset after each test."""
        setDryRun(False)
        yield
        setDryRun(False)

    def test_global_flag_defaults_to_false(self):
        assert isDryRun() is False

    # ── LocalHost ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_localhost_run_returns_skipped(self):
        setDryRun(True)
        host = LocalHost()
        result = (await host.run("echo hello")).only

        assert result.status == Status.Skipped
        assert result.retcode == 0
        assert result.command == "echo hello"
        assert "[DRY RUN]" in result.output

    @pytest.mark.asyncio
    async def test_localhost_run_does_not_spawn_subprocess(self):
        setDryRun(True)
        host = LocalHost()
        result = (await host.run("exit 1")).only

        assert result.status == Status.Skipped
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_run_list_returns_all_skipped(self):
        setDryRun(True)
        host = LocalHost()
        result = await host.run(["cmd1", "cmd2", "cmd3"])

        assert len(result.statuses) == 3
        for r in result.statuses:
            assert r.status == Status.Skipped
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_localhost_oneshot_returns_skipped(self):
        setDryRun(True)
        host = LocalHost()
        result = await host.oneshot("echo hello")

        assert result.status == Status.Skipped
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_send_is_noop(self):
        setDryRun(True)
        host = LocalHost()
        await host.send("some text")

    @pytest.mark.asyncio
    async def test_localhost_expect_returns_empty(self):
        setDryRun(True)
        host = LocalHost()
        result = await host.expect("some_pattern")
        assert result == ""

    @pytest.mark.asyncio
    async def test_localhost_put_returns_skipped(self):
        setDryRun(True)
        host = LocalHost()
        files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
        dest = Path("/tmp/dest")

        status, msg = await host.put(files, dest)

        assert status == Status.Skipped
        assert "[DRY RUN]" in msg
        assert "PUT" in msg

    @pytest.mark.asyncio
    async def test_localhost_get_returns_skipped(self):
        setDryRun(True)
        host = LocalHost()
        files = [Path("/tmp/file.bin")]
        dest = Path("/tmp/dest")

        status, msg = await host.get(files, dest)

        assert status == Status.Skipped
        assert "[DRY RUN]" in msg
        assert "GET" in msg

    def test_localhost_start_repeat_is_noop(self):
        setDryRun(True)
        host = LocalHost()
        host.start_repeat(
            name="test", cmds=["uptime"], interval=timedelta(seconds=10),
        )

    # ── RemoteHost ──────────────────────────────────────────────���─────────

    @pytest.mark.asyncio
    async def test_remotehost_run_returns_skipped(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        result = (await host.run("ls -la")).only

        assert result.status == Status.Skipped
        assert result.retcode == 0
        assert result.command == "ls -la"
        assert "[DRY RUN]" in result.output
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_oneshot_returns_skipped(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        result = await host.oneshot("uname -a")

        assert result.status == Status.Skipped
        assert result.retcode == 0
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_run_list_returns_all_skipped(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        result = await host.run(["cmd1", "cmd2", "cmd3"])

        assert len(result.statuses) == 3
        for r in result.statuses:
            assert r.status == Status.Skipped
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_send_is_noop(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        await host.send("some text")
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_expect_returns_empty(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        result = await host.expect("some_pattern")

        assert result == ""
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_put_returns_skipped(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
        dest = Path("/remote/dest")

        status, msg = await host.put(files, dest)

        assert status == Status.Skipped
        assert "[DRY RUN]" in msg
        assert "PUT" in msg
        assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_get_returns_skipped(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        files = [Path("/remote/file.bin")]
        dest = Path("/local/dest")

        status, msg = await host.get(files, dest)

        assert status == Status.Skipped
        assert "[DRY RUN]" in msg
        assert "GET" in msg

    def test_remotehost_start_repeat_is_noop(self):
        setDryRun(True)
        host = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
        host.start_repeat(
            name="test", cmds=["uptime"], interval=timedelta(seconds=10),
        )
