"""Tests for the gcda fetcher."""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.remote import GcdaFetcher, _clean_one_host, _fetch_one_product
from otto.result import CommandResult, Result
from otto.utils import Status


def _product(name: str, cov_dir: str | None = None, verdict: bool | None = True) -> MagicMock:
    p = MagicMock()
    p.name = name
    p.cov_dir = cov_dir
    p.instrumented = MagicMock(return_value=verdict)
    return p


def _make_mock_host(host_id: str = "host1", products=None) -> MagicMock:
    host = MagicMock()
    host.id = host_id
    host.exec = AsyncMock()
    host.get = AsyncMock()
    host.products = list(products) if products is not None else [_product("app", "/var/cov")]
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
        result = await fetcher.fetch_all()

        assert ("host1", "app") in result
        host.exec.assert_called_once()
        host.get.assert_called_once()
        call_args = host.get.call_args
        gcda_paths = call_args[0][0]
        assert len(gcda_paths) == 2

    @pytest.mark.asyncio
    async def test_fetch_all_stages_per_host_per_product(self, tmp_path, fake_config_module):
        """Each instrumented product gets its own `find` at its own cov_dir and its
        own staging leaf; a product that names no cov_dir falls back to /tmp/<name>."""
        host = _make_mock_host("host1", [_product("app", "/var/cov/app"), _product("agent")])
        host.exec.return_value = CommandResult(
            Status.Success, value="/x/foo.gcda\n/x/bar.gcda\n", command="find ...", retcode=0
        )
        host.get.return_value = Result(Status.Success, value={})
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert set(result) == {("host1", "app"), ("host1", "agent")}
        assert result[("host1", "app")] == tmp_path / "staging" / "host1" / "app"
        assert result[("host1", "agent")] == tmp_path / "staging" / "host1" / "agent"
        finds = [c.args[0] for c in host.exec.call_args_list]
        assert finds == [
            "find /var/cov/app -name '*.gcda' -type f",
            "find /tmp/agent -name '*.gcda' -type f",  # the default cov_dir
        ]
        assert host.get.call_count == 2

    @pytest.mark.asyncio
    async def test_uninstrumented_and_unknown_products_are_not_searched(
        self, tmp_path, fake_config_module
    ):
        host = _make_mock_host("host1", [_product("a", verdict=False), _product("b", verdict=None)])
        fake_config_module(host)
        assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        host.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_container_hosts_are_fetched(self, tmp_path, fake_config_module):
        """A container's `exec`/`get` are `docker exec`/`docker cp` — the same fetch
        path serves it, so a product living in a container is not skipped."""
        from otto.host.docker_host import DockerContainerHost

        host = MagicMock(spec=DockerContainerHost)  # isinstance-true double
        host.id = "p.repo.api"
        host.products = [_product("app")]
        host.exec = AsyncMock()
        host.get = AsyncMock()
        host.exec.return_value = CommandResult(
            Status.Success, value="/tmp/app/a.gcda\n", command="find", retcode=0
        )
        host.get.return_value = Result(Status.Success, value={})
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert ("p.repo.api", "app") in result

    @pytest.mark.asyncio
    async def test_a_failed_product_does_not_stop_the_others(self, tmp_path, fake_config_module):
        """One product's failed transfer is logged and skipped; its siblings still land."""
        host = _make_mock_host(
            "host1", [_product("bad", "/var/bad"), _product("good", "/var/good")]
        )
        host.exec.return_value = CommandResult(
            Status.Success, value="/x/foo.gcda\n", command="find ...", retcode=0
        )
        host.get.side_effect = [
            Result(Status.Error, value={}, msg="connection refused"),
            Result(Status.Success, value={}),
        ]
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert set(result) == {("host1", "good")}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("family", "expected_fetched"),
        [
            ("UnixHost", True),
            ("LocalHost", False),
            ("EmbeddedHost", False),
            ("DockerContainerHost", True),
        ],
    )
    async def test_the_fetch_call_binds_on_every_family(
        self, tmp_path, fake_config_module, family, expected_fetched
    ):
        """The fetcher calls ``get`` through the ``Host`` protocol; every family's
        real ``get`` signature must bind that call.

        The double's ``get`` binds the fetcher's actual arguments against the
        family's REAL signature before answering — a ``TypeError`` here is the
        one production raised on containers (``show_progress`` missing from
        ``DockerContainerHost.get``, shipped in v0.10.0) with nothing but a
        mock in the way. A plain ``AsyncMock`` accepts any keyword and could
        never see it.

        The double is ``spec``'d to the family so the fetcher's own
        ``isinstance`` skip (local and embedded hosts have no fetchable
        counters) is exercised by the same parametrization.
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
        host = MagicMock(spec=cls)
        host.id = "host1"
        host.products = [_product("app", "/var/cov")]
        host.exec = AsyncMock(
            return_value=CommandResult(
                Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
            )
        )

        async def _get_like_the_family(*args, **kwargs):
            signature.bind(host, *args, **kwargs)  # raises exactly as the real method would
            return Result(Status.Success, value={})

        host.get = AsyncMock(side_effect=_get_like_the_family)
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        if expected_fetched:
            assert ("host1", "app") in result, f"{family}.get did not bind the fetcher's call"
        else:
            assert result == {}, f"{family} must be skipped by the remote fetcher"

    @pytest.mark.asyncio
    async def test_fetch_all_no_gcda_files(self, tmp_path, fake_config_module):
        """Zero .gcda for a product is not an error — it just yields no entry."""
        host = _make_mock_host()
        host.exec.return_value = CommandResult(
            Status.Success, value="", command="find ...", retcode=0
        )
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all()
        assert len(result) == 0
        assert not (tmp_path / "staging" / "host1").exists()

    @pytest.mark.asyncio
    async def test_a_failed_find_warns_where_an_empty_one_stays_quiet(
        self, tmp_path, fake_config_module, caplog
    ):
        """The two are different events. A host that could not be searched is a
        WARNING; an instrumented product that a run never exercised producing no
        counters is ordinary, so it must not cry wolf on every collection."""
        import logging

        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Error, value="find: '/var/cov': No such file", command="find", retcode=1
        )
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        assert "find failed for app on host1 at /var/cov" in caplog.text
        assert "No such file" in caplog.text

        caplog.clear()
        host.exec.return_value = CommandResult(Status.Success, value="", command="find", retcode=0)
        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        assert caplog.text == ""

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
        result = await fetcher.fetch_all()

        assert ("host1", "app") in result
        assert not any(host_id == "local" for host_id, _ in result)
        assert not (tmp_path / "staging" / "local").exists()

    @pytest.mark.asyncio
    async def test_fetch_all_transfer_failure(self, tmp_path, fake_config_module):
        """A failed transfer leaves NO product leaf behind: the report walk
        iterates `<cov_dir>/<host>/<product>` and an empty one would be read as
        a product that genuinely produced nothing."""
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )
        host.get.return_value = Result(Status.Error, value={}, msg="connection refused")
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all()
        assert len(result) == 0
        assert not (tmp_path / "staging" / "host1" / "app").exists()

    @pytest.mark.asyncio
    async def test_a_partly_transferred_product_leaf_is_removed_too(
        self, tmp_path, fake_config_module
    ):
        """The failure cleanup is an rmtree, not an rmdir — a transfer that got
        some files down before failing must not leave a half-populated leaf."""
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )

        async def _get_half_way(_files, dest, **_kwargs):
            (dest / "foo.gcda").write_bytes(b"partial")
            return Result(Status.Error, value={}, msg="connection reset")

        host.get = AsyncMock(side_effect=_get_half_way)
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert result == {}
        assert not (tmp_path / "staging" / "host1" / "app").exists()

    @pytest.mark.asyncio
    async def test_a_raising_get_leaves_no_leaf_behind_either(self, tmp_path):
        """The cleanup must not be reachable only through the RETURNED failure.

        A transport that raises — or a cancellation at a bed teardown — drops
        the same half-populated leaf as a failure result does, and a later merge
        cannot tell a partial product from a complete one. Driven at
        ``_fetch_one_product``, the frame that owns the leaf: the host-level
        gather above it turns any exception into a logged per-host miss, which
        would hide both halves of this. The exception is re-raised untouched —
        the cleanup must not swallow a transport failure into a quiet "no data".
        """
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Success, value="/var/cov/foo.gcda\n", command="find ...", retcode=0
        )

        async def _get_then_raise(_files, dest, **_kwargs):
            (dest / "foo.gcda").write_bytes(b"partial")
            raise ConnectionResetError("transport went away mid-transfer")

        host.get = AsyncMock(side_effect=_get_then_raise)

        with pytest.raises(ConnectionResetError, match="went away"):
            await _fetch_one_product(host, host.products[0], tmp_path / "staging")

        assert not (tmp_path / "staging" / "host1" / "app").exists()

    @pytest.mark.asyncio
    async def test_a_cov_dir_with_a_space_is_shell_quoted(self, tmp_path, fake_config_module):
        """An unquoted `/opt/My App/cov` would reach `find` as two start points —
        and on the clean path, `-delete` would then run against the wrong one."""
        host = _make_mock_host("host1", [_product("app", "/opt/My App/cov")])
        host.exec.return_value = CommandResult(Status.Success, value="", command="find", retcode=0)
        fake_config_module(host)

        await GcdaFetcher(tmp_path / "staging").fetch_all()
        assert host.exec.call_args_list[0].args[0] == (
            "find '/opt/My App/cov' -name '*.gcda' -type f"
        )

        host.exec.reset_mock()
        await GcdaFetcher(tmp_path / "staging").clean_remote()
        assert host.exec.call_args_list[0].args[0] == (
            "find '/opt/My App/cov' -name '*.gcda' -type f -delete"
        )

    @pytest.mark.asyncio
    async def test_a_malformed_product_name_never_reaches_the_host(
        self, tmp_path, fake_config_module
    ):
        """A name that is not a single safe path segment is rejected BEFORE any
        host command is issued — `../x` must not be able to steer a `find`, least
        of all the one that appends `-delete`."""
        from otto.coverage.fetcher.remote import _fetch_one_product

        host = _make_mock_host("host1", [_product("../x", "/var/cov")])
        fake_config_module(host)

        # The raise itself, at the unit that performs the check.
        with pytest.raises(ValueError, match=r"product name"):
            await _fetch_one_product(host, host.products[0], tmp_path / "staging")
        host.exec.assert_not_called()

        # And through the fleet walk, where `do_for_all_hosts` captures each
        # host's exception as a value: the walk yields nothing and, crucially,
        # still never ran a command.
        assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        host.exec.assert_not_called()

        with pytest.raises(ValueError, match=r"product name"):
            await _clean_one_host(host)
        host.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_clean_remote(self, tmp_path, fake_config_module):
        host = _make_mock_host()
        host.exec.return_value = CommandResult(
            Status.Success, value="", command="find ...", retcode=0
        )
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        await fetcher.clean_remote()
        host.exec.assert_called_once()
        assert "-delete" in host.exec.call_args[0][0]

    @pytest.mark.asyncio
    async def test_clean_remote_deletes_per_instrumented_product(
        self, tmp_path, fake_config_module
    ):
        host = _make_mock_host(
            "host1", [_product("app", "/var/cov/app"), _product("skip", verdict=False)]
        )
        host.exec.return_value = CommandResult(Status.Success, value="", command="find", retcode=0)
        fake_config_module(host)

        await GcdaFetcher(tmp_path).clean_remote()

        assert [c.args[0] for c in host.exec.call_args_list] == [
            "find /var/cov/app -name '*.gcda' -type f -delete"
        ]

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
        result = await fetcher.fetch_all()
        assert set(result) == {("host1", "app"), ("host2", "app")}

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
        result = await fetcher.fetch_all()

        assert set(result) == {("test1", "app")}
        host2.exec.assert_not_called()
