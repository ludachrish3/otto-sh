"""Tests for the gcda fetcher."""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.remote import GcdaFetcher
from otto.result import CommandResult, Result
from otto.utils import Status


def _make_mock_host(host_id: str = "host1") -> MagicMock:
    host = MagicMock()
    host.id = host_id
    host.exec = AsyncMock()
    host.get = AsyncMock()
    return host


@pytest.fixture
def fake_config_module():
    """Install an OttoContext so all_hosts() returns test hosts.

    Yields a callable ``set_hosts(*hosts)`` that callers use to register
    the host list for the duration of the test.
    """
    lab = Lab(name="test_lab")
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)

    def set_hosts(*hosts: MagicMock) -> None:
        # Mutate the lab's REAL mapping in place. A `dict` subclass overriding
        # only `values()` used to stand in here, which worked exactly as long as
        # `all_hosts` touched nothing else; fleet scoping also iterates and
        # `.items()`s the mapping, and a half-implemented double answers those
        # with the empty dict underneath rather than failing.
        lab.hosts.clear()
        for h in hosts:
            lab.hosts[h.id] = h

    yield set_hosts
    reset_context(token)


class TestGcdaFetcher:
    @pytest.mark.asyncio
    async def test_fetch_all_happy_path(self, tmp_path, fake_config_module):
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
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
        host.exec.assert_called_once()
        host.get.assert_called_once()
        call_args = host.get.call_args
        gcda_paths = call_args[0][0]
        assert len(gcda_paths) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "family",
        ["UnixHost", "LocalHost", "EmbeddedHost", "DockerContainerHost"],
    )
    async def test_the_fetch_call_binds_on_every_family(self, tmp_path, fake_config_module, family):
        """The fetcher calls ``get`` through the ``Host`` protocol; every family's
        real ``get`` signature must bind that call.

        The double's ``get`` binds the fetcher's actual arguments against the
        family's REAL signature before answering — a ``TypeError`` here is the
        one production raised on containers (``show_progress`` missing from
        ``DockerContainerHost.get``, shipped in v0.10.0) with nothing but a
        mock in the way. A plain ``AsyncMock`` accepts any keyword and could
        never see it.
        """
        import importlib
        import inspect

        module = {
            "UnixHost": "otto.host.unix_host",
            "LocalHost": "otto.host.local_host",
            "EmbeddedHost": "otto.host.embedded_host",
            "DockerContainerHost": "otto.host.docker_host",
        }[family]
        cls = getattr(importlib.import_module(module), family)
        signature = inspect.signature(cls.get)
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )

        async def _get_like_the_family(*args, **kwargs):
            signature.bind(host, *args, **kwargs)  # raises exactly as the real method would
            return Result(Status.Success, value={})

        host.get = AsyncMock(side_effect=_get_like_the_family)
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all("/var/cov")

        assert "host1" in result, f"{family}.get did not bind the fetcher's call"

    @pytest.mark.asyncio
    async def test_fetch_all_no_gcda_files(self, tmp_path, fake_config_module):
        host = _make_mock_host()
        host.exec.return_value = CommandResult(
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
        unix.exec.return_value = CommandResult(
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
        host.exec.return_value = CommandResult(
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
        host.exec.return_value = CommandResult(
            Status.Success, value="", command="find ...", retcode=0
        )
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        await fetcher.clean_remote("/var/cov")
        host.exec.assert_called_once()
        assert "-delete" in host.exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_multiple_hosts(self, tmp_path, fake_config_module):
        host1 = _make_mock_host("host1")
        host2 = _make_mock_host("host2")
        for h in [host1, host2]:
            h.exec.return_value = CommandResult(
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
        host1 = _make_mock_host("test1")
        host2 = _make_mock_host("test2")
        for h in [host1, host2]:
            h.exec.return_value = CommandResult(
                Status.Success, value="/var/cov/file.gcda\n", command="find ...", retcode=0
            )
            h.get.return_value = Result(Status.Success, value={})
        fake_config_module(host1, host2)

        # `test1` alone selects nothing now — host ids are FULLMATCHED (D6).
        fetcher = GcdaFetcher(tmp_path / "staging", pattern=re.compile(r"test1.*"))
        result = await fetcher.fetch_all("/var/cov")

        assert set(result.keys()) == {"test1"}
        host2.exec.assert_not_called()
