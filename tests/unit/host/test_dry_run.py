"""Tests for dry-run mode on hosts (via OttoContext)."""

from pathlib import Path

import pytest

from otto.host.host import is_dry_run
from otto.host.local_host import LocalHost
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.conftest import active_context


class TestGlobalDryRun:
    def test_global_flag_defaults_to_false(self):
        assert is_dry_run() is False

    # ── LocalHost ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_localhost_run_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("echo hello")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert result.command == "echo hello"
            assert "[DRY RUN]" in result.value

    @pytest.mark.asyncio
    async def test_localhost_run_does_not_spawn_subprocess(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("exit 1")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_run_list_returns_all_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.run(["cmd1", "cmd2", "cmd3"])

            assert len(result) == 3
            for r in result:
                assert r.status == Status.Skipped
            assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_localhost_oneshot_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.oneshot("echo hello")

            assert result.status == Status.Skipped
            assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_send_is_noop(self):
        with active_context(dry_run=True):
            host = LocalHost()
            await host.send("some text")

    @pytest.mark.asyncio
    async def test_localhost_expect_returns_empty(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.expect("some_pattern")
            assert result == ""

    @pytest.mark.asyncio
    async def test_localhost_put_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
            dest = Path("/tmp/dest")

            result = await host.put(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg

    @pytest.mark.asyncio
    async def test_localhost_get_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file.bin")]
            dest = Path("/tmp/dest")

            result = await host.get(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "GET" in result.msg

    # ── UnixHost ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_remotehost_run_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            result = (await host.run("ls -la")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert result.command == "ls -la"
            assert "[DRY RUN]" in result.value
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_oneshot_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            result = await host.oneshot("uname -a")

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_run_list_returns_all_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            result = await host.run(["cmd1", "cmd2", "cmd3"])

            assert len(result) == 3
            for r in result:
                assert r.status == Status.Skipped
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_send_is_noop(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            await host.send("some text")
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_expect_returns_empty(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            result = await host.expect("some_pattern")

            assert result == ""
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_put_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
            dest = Path("/remote/dest")

            result = await host.put(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_get_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(ip="10.0.0.1", element="box", creds={"user": "pass"}, log=LogMode.QUIET)
            files = [Path("/remote/file.bin")]
            dest = Path("/local/dest")

            result = await host.get(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "GET" in result.msg
