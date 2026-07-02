"""Tests for the gcda fetcher."""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.configmodule.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.remote import GcdaFetcher
from otto.result import CommandResult, Result
from otto.utils import Status


def _make_mock_host(host_id: str = "host1") -> MagicMock:
    host = MagicMock()
    host.id = host_id
    host.oneshot = AsyncMock()
    host.get = AsyncMock()
    return host


@pytest.fixture
def fake_config_module():
    """Install an OttoContext so all_hosts() returns test hosts.

    Yields a callable ``set_hosts(*hosts)`` that callers use to register
    the host list for the duration of the test.
    """
    current: dict[str, MagicMock] = {}

    class _FakeHostsDict(dict):
        """Dict that always reflects the latest `current` mapping."""

        def values(self):
            return list(current.values())

    lab = Lab(name="test_lab")
    lab.hosts = _FakeHostsDict()  # type: ignore[assignment]
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)

    def set_hosts(*hosts: MagicMock) -> None:
        current.clear()
        for h in hosts:
            current[h.id] = h

    yield set_hosts
    reset_context(token)


class TestGcdaFetcher:
    @pytest.mark.asyncio
    async def test_fetch_all_happy_path(self, tmp_path, fake_config_module):
        host = _make_mock_host("host1")
        host.oneshot.return_value = CommandResult(
            Status.Success,
            value="/var/cov/foo.gcda\n/var/cov/bar.gcda\n",
            command="find ...",
            retcode=0,
        )
        host.get.return_value = Result(Status.Success, value={})
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all("/var/cov")

        assert "host1" in result
        host.oneshot.assert_called_once()
        host.get.assert_called_once()
        call_args = host.get.call_args
        gcda_paths = call_args[0][0]
        assert len(gcda_paths) == 2

    @pytest.mark.asyncio
    async def test_fetch_all_no_gcda_files(self, tmp_path, fake_config_module):
        host = _make_mock_host()
        host.oneshot.return_value = CommandResult(
            Status.Success, value="", command="find ...", retcode=0
        )
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all("/var/cov")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_fetch_all_skips_builtin_local(self, tmp_path, fake_config_module):
        """The built-in `local` host (injected into every lab) has no remote .gcda —
        the fetcher must skip it (no find, no empty staging dir), not query it."""
        from otto.host.local_host import LocalHost

        local = LocalHost()
        unix = _make_mock_host("host1")
        unix.oneshot.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )
        unix.get.return_value = Result(Status.Success, value={})
        fake_config_module(local, unix)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all("/var/cov")

        assert "host1" in result
        assert "local" not in result  # LocalHost skipped before any remote query
        assert not (tmp_path / "staging" / "local").exists()

    @pytest.mark.asyncio
    async def test_fetch_all_transfer_failure(self, tmp_path, fake_config_module):
        host = _make_mock_host("host1")
        host.oneshot.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )
        host.get.return_value = Result(Status.Error, value={}, msg="connection refused")
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all("/var/cov")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_clean_remote(self, tmp_path, fake_config_module):
        host = _make_mock_host()
        host.oneshot.return_value = CommandResult(
            Status.Success, value="", command="find ...", retcode=0
        )
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        await fetcher.clean_remote("/var/cov")
        host.oneshot.assert_called_once()
        assert "-delete" in host.oneshot.call_args[0][0]

    @pytest.mark.asyncio
    async def test_multiple_hosts(self, tmp_path, fake_config_module):
        host1 = _make_mock_host("host1")
        host2 = _make_mock_host("host2")
        for h in [host1, host2]:
            h.oneshot.return_value = CommandResult(
                Status.Success, value="/var/cov/file.gcda\n", command="find ...", retcode=0
            )
            h.get.return_value = Result(Status.Success, value={})
        fake_config_module(host1, host2)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all("/var/cov")
        assert len(result) == 2
        assert "host1" in result
        assert "host2" in result

    @pytest.mark.asyncio
    async def test_pattern_filters_hosts(self, tmp_path, fake_config_module):
        """A regex pattern scopes the fetcher to matching hosts only."""
        host1 = _make_mock_host("carrot_seed")
        host2 = _make_mock_host("tomato_seed")
        for h in [host1, host2]:
            h.oneshot.return_value = CommandResult(
                Status.Success, value="/var/cov/file.gcda\n", command="find ...", retcode=0
            )
            h.get.return_value = Result(Status.Success, value={})
        fake_config_module(host1, host2)

        fetcher = GcdaFetcher(tmp_path / "staging", pattern=re.compile(r"carrot"))
        result = await fetcher.fetch_all("/var/cov")

        assert set(result.keys()) == {"carrot_seed"}
        host2.oneshot.assert_not_called()
