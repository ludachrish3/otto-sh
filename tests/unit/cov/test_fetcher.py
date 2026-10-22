"""Tests for the gcda fetcher."""

import io
import re
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.remote import GcdaFetcher, _fetch_one_product
from otto.host.product import Product
from otto.labs.loader import Lab
from otto.result import CommandResult, NotRunResult, Result
from otto.utils import Status


def _product(name: str, cov_dir: str | None = None, verdict: bool | None = True) -> MagicMock:
    p = MagicMock()
    p.name = name
    p.cov_dir = cov_dir
    p.instrumented = MagicMock(return_value=verdict)

    # The real defaults, bound to the mock: prepare touches nothing, reset
    # issues the find -delete the clean tests assert on. The side_effect must
    # itself be a coroutine function (not a lambda returning one) — AsyncMock
    # only awaits a side_effect it recognises via iscoroutinefunction; a
    # lambda wrapping a coroutine function hands back an un-awaited coroutine.
    async def _prepare_coverage(host: object) -> Result:
        return await Product.prepare_coverage(p, host)

    async def _reset_coverage(host: object) -> Result:
        return await Product.reset_coverage(p, host)

    p.prepare_coverage = AsyncMock(side_effect=_prepare_coverage)
    p.reset_coverage = AsyncMock(side_effect=_reset_coverage)
    return p


def _tar_get(files: dict[str, bytes] | None = None):
    """A ``host.get`` side effect that "downloads" a tar of *files* (cov_dir-relative).

    The fetcher asks for one remote archive; this writes a tar holding *files*
    at that archive's basename in the destination, the way a real transfer
    would, and answers success.
    """
    files = {"./foo.gcda": b"x"} if files is None else files
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    async def _get(paths, dest, **_kwargs):
        for remote in paths:
            (Path(dest) / Path(remote).name).write_bytes(buf.getvalue())
        return Result(Status.Success, value={})

    return _get


_OK = CommandResult(Status.Success, value="", command="sh", retcode=0)


def _mktemp_exec(host: MagicMock):
    """A ``host.exec`` side effect: the archive command answers the path its
    ``mktemp`` would make; a test that sets ``host.exec.return_value`` gets
    exactly that answer for every command instead."""

    async def _exec(cmd: str, **_kwargs: object) -> CommandResult:
        if host.exec.return_value is not _OK:
            return host.exec.return_value
        token = re.search(r"otto-fetch-[0-9a-f]+", cmd)
        if "tar -cf" in cmd and token is not None:
            path = f"/tmp/{token.group(0)}.Ab12Cd"
            return CommandResult(
                Status.Success, value=f"otto-archive:{path}\n", command="sh", retcode=0
            )
        return _OK

    return _exec


def _make_mock_host(host_id: str = "host1", products=None) -> MagicMock:
    host = MagicMock()
    host.id = host_id
    host.exec = AsyncMock(return_value=_OK, side_effect=_mktemp_exec(host))
    host.get = AsyncMock(side_effect=_tar_get())
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
        """The counters come down as ONE archive and land at their cov_dir-relative
        paths; the archive itself is removed on both ends."""
        host = _make_mock_host("host1")
        host.get.side_effect = _tar_get({"./foo.gcda": b"1", "./sub/bar.gcda": b"2"})
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all()

        dest = result[("host1", "app")]
        assert (dest / "foo.gcda").read_bytes() == b"1"
        assert (dest / "sub" / "bar.gcda").read_bytes() == b"2"
        assert not list(dest.glob("*.tar"))
        host.get.assert_called_once()
        (remote_tar,) = host.get.call_args[0][0]
        cmds = [c.args[0] for c in host.exec.call_args_list]
        token = remote_tar.name.split(".")[0]
        assert remote_tar.as_posix() == f"/tmp/{token}.Ab12Cd"
        assert cmds[0] == (
            "! test -d /var/cov && echo otto-no-cov-dir || "
            f'(t=$(mktemp "${{TMPDIR:-/tmp}}/{token}.XXXXXX") && echo "otto-archive:$t" '
            '&& cd /var/cov && tar -cf "$t" .)'
        )
        assert cmds[1:] == [f'rm -f "${{TMPDIR:-/tmp}}"/{token}.*']

    @pytest.mark.asyncio
    async def test_fetch_all_stages_per_host_per_product(self, tmp_path, fake_config_module):
        """Each instrumented product gets its own archive of its own cov_dir and its
        own staging leaf; a product that names no cov_dir falls back to /tmp/<name>."""
        host = _make_mock_host("host1", [_product("app", "/var/cov/app"), _product("agent")])
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert set(result) == {("host1", "app"), ("host1", "agent")}
        assert result[("host1", "app")] == tmp_path / "staging" / "host1" / "app"
        assert result[("host1", "agent")] == tmp_path / "staging" / "host1" / "agent"
        archives = [c.args[0] for c in host.exec.call_args_list if "tar -cf" in c.args[0]]
        assert len(archives) == 2
        assert archives[0].startswith("! test -d /var/cov/app ")
        assert '&& cd /var/cov/app && tar -cf "$t" .)' in archives[0]
        # the default cov_dir
        assert '&& cd /tmp/agent && tar -cf "$t" .)' in archives[1]
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
        host.id = "p.proj.api"
        host.products = [_product("app")]
        host.exec = AsyncMock(return_value=_OK, side_effect=_mktemp_exec(host))
        host.get = AsyncMock(side_effect=_tar_get())
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert ("p.proj.api", "app") in result

    @pytest.mark.asyncio
    async def test_a_failed_product_does_not_stop_the_others(self, tmp_path, fake_config_module):
        """One product's failed transfer is logged and skipped; its siblings still land."""
        host = _make_mock_host(
            "host1", [_product("bad", "/var/bad"), _product("good", "/var/good")]
        )
        good_get = _tar_get()

        async def _bad_then_good(paths, dest, **kwargs):
            if host.get.await_count == 1:
                return Result(Status.Error, value={}, msg="connection refused")
            return await good_get(paths, dest, **kwargs)

        host.get.side_effect = _bad_then_good
        fake_config_module(host)

        result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert set(result) == {("host1", "good")}

    @pytest.mark.asyncio
    async def test_a_corrupt_archive_fails_only_its_own_product(
        self, tmp_path, fake_config_module, caplog
    ):
        """An archive ``safe_extract`` refuses is that product's failed fetch —
        a WARNING naming it — never an exception that loses its siblings."""
        import logging

        host = _make_mock_host(
            "host1", [_product("bad", "/var/bad"), _product("good", "/var/good")]
        )
        good_get = _tar_get()

        async def _corrupt_then_good(paths, dest, **kwargs):
            if host.get.await_count == 1:
                for remote in paths:
                    (Path(dest) / Path(remote).name).write_bytes(b"not a tar at all" * 64)
                return Result(Status.Success, value={})
            return await good_get(paths, dest, **kwargs)

        host.get.side_effect = _corrupt_then_good
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert set(result) == {("host1", "good")}
        assert not (tmp_path / "staging" / "host1" / "bad").exists()
        assert "Failed to fetch .gcda for bad from host1" in caplog.text

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
        host.exec = AsyncMock(return_value=_OK, side_effect=_mktemp_exec(host))
        serve = _tar_get()

        async def _get_like_the_family(*args, **kwargs):
            signature.bind(host, *args, **kwargs)  # raises exactly as the real method would
            return await serve(*args, **kwargs)

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
        host.get.side_effect = _tar_get({})
        fake_config_module(host)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all()
        assert len(result) == 0
        assert not (tmp_path / "staging" / "host1").exists()

    @pytest.mark.asyncio
    async def test_a_failed_archive_warns_where_an_empty_one_stays_quiet(
        self, tmp_path, fake_config_module, caplog
    ):
        """The two are different events. A cov_dir tar could not archive whole is
        a WARNING (and no fallback, no transfer); an instrumented product that a
        run never exercised producing no counters is ordinary, so it must not cry
        wolf on every collection."""
        import logging

        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Error, value="tar: ./z: Cannot open: Permission denied", command="sh", retcode=2
        )
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        assert "Failed to archive .gcda for app on host1 at /var/cov" in caplog.text
        assert "Permission denied" in caplog.text
        host.get.assert_not_called()

        caplog.clear()
        host.exec.return_value = _OK
        host.get.side_effect = _tar_get({})
        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_a_failed_archive_relays_the_devices_output_verbatim(
        self, tmp_path, fake_config_module, caplog
    ):
        """The device's tar error reaches every sink as the device printed it (#697).

        The record does not opt in to Rich markup, so the console and both
        log files show its message as written.
        """
        import logging

        from tests._fixtures.log_text import shown

        output = "tar: ./[x:y].gcda: Cannot open [/x]"
        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(Status.Error, value=output, command="sh", retcode=2)
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        rendered = [shown(r) for r in caplog.records]
        assert any(line.endswith(f": {output}") for line in rendered), rendered

    @pytest.mark.asyncio
    async def test_a_missing_cov_dir_is_quiet_and_fetches_nothing(
        self, tmp_path, fake_config_module, caplog
    ):
        """No cov_dir on the host means the product never wrote counters — the
        same ordinary event as an empty one: no WARNING, no transfer, no leaf."""
        import logging

        from otto.coverage.fetcher.remote import _NO_COV_DIR

        host = _make_mock_host("host1")
        host.exec.return_value = CommandResult(
            Status.Success, value=f"{_NO_COV_DIR}\n", command="sh", retcode=0
        )
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            assert await GcdaFetcher(tmp_path / "staging").fetch_all() == {}
        assert caplog.text == ""
        host.get.assert_not_called()
        assert not (tmp_path / "staging" / "host1").exists()

    @pytest.mark.asyncio
    async def test_fetch_all_skips_builtin_local(self, tmp_path, fake_config_module):
        """The built-in `local` host (injected into every lab) has no remote .gcda —
        the fetcher must skip it (no find, no empty staging dir), not query it."""
        from otto.host.local_host import LocalHost

        local = LocalHost()
        unix = _make_mock_host("host1")
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
        host.get.side_effect = None
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

        async def _get_then_raise(_files, dest, **_kwargs):
            (dest / "foo.gcda").write_bytes(b"partial")
            raise ConnectionResetError("transport went away mid-transfer")

        host.get = AsyncMock(side_effect=_get_then_raise)

        with pytest.raises(ConnectionResetError, match="went away"):
            await _fetch_one_product(host, host.products[0], tmp_path / "staging")

        assert not (tmp_path / "staging" / "host1" / "app").exists()
        # The host-side archive is removed on the raising path too.
        assert (
            host.exec.call_args_list[-1].args[0].startswith('rm -f "${TMPDIR:-/tmp}"/otto-fetch-')
        )

    @pytest.mark.asyncio
    async def test_a_cov_dir_with_a_space_is_shell_quoted(self, tmp_path, fake_config_module):
        """An unquoted `/opt/My App/cov` would reach `test`/`cd` as two words."""
        host = _make_mock_host("host1", [_product("app", "/opt/My App/cov")])
        fake_config_module(host)

        await GcdaFetcher(tmp_path / "staging").fetch_all()
        archive = host.exec.call_args_list[0].args[0]
        assert archive.startswith("! test -d '/opt/My App/cov' ")
        assert "&& cd '/opt/My App/cov' && tar -cf \"$t\" .)" in archive
        # The clean path's own quoting (a `find … -delete`) is
        # ``Product.reset_coverage``'s default, pinned directly in
        # tests/unit/host/test_product.py.

    @pytest.mark.asyncio
    async def test_a_malformed_product_name_never_reaches_the_host(
        self, tmp_path, fake_config_module
    ):
        """A name that is not a single safe path segment is rejected BEFORE any
        host command is issued — `../x` must not be able to steer a `find`."""
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
        # The clean path's own name check (before `reset_coverage` is called)
        # lives in ``otto.coverage.collect._reset_products`` now — pinned in
        # tests/unit/cov/test_clean.py.

    @pytest.mark.asyncio
    async def test_prepare_coverage_runs_before_the_archive(self, tmp_path, fake_config_module):
        host = _make_mock_host("host1")
        order: list[str] = []
        host.products[0].prepare_coverage = AsyncMock(
            side_effect=lambda h: (order.append("prepare"), Result(Status.Success))[1]
        )
        host.exec.side_effect = lambda cmd, **k: (
            order.append("archive" if "tar -cf" in cmd else "rm"),
            _OK,
        )[1]
        fake_config_module(host)
        await GcdaFetcher(tmp_path / "staging").fetch_all()
        assert order == ["prepare", "archive", "rm"]

    @pytest.mark.asyncio
    async def test_a_failed_prepare_skips_the_product_and_leaves_no_leaf(
        self, tmp_path, fake_config_module, caplog
    ):
        host = _make_mock_host("host1")
        host.products[0].prepare_coverage = AsyncMock(
            return_value=Result(Status.Error, msg="no /sys/module/app/kmodcov/dump")
        )
        fake_config_module(host)
        result = await GcdaFetcher(tmp_path / "staging").fetch_all()
        assert result == {}
        host.exec.assert_not_called()
        host.get.assert_not_called()
        assert not (tmp_path / "staging" / "host1").exists()
        assert "host1:app: prepare_coverage failed: no /sys/module/app/kmodcov/dump" in caplog.text

    @pytest.mark.asyncio
    async def test_a_declined_prepare_is_a_dry_run_not_a_failure(
        self, tmp_path, fake_config_module, caplog
    ):
        """A hook that runs a host command is declined under a dry run; that is not
        a failure to warn about, and the archive that follows is not attempted."""
        import logging

        host = _make_mock_host("host1")
        host.products[0].prepare_coverage = AsyncMock(
            return_value=NotRunResult(
                status=Status.NotRun, command="cat dump", retcode=-1, host_name="host1"
            )
        )
        fake_config_module(host)
        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            result = await GcdaFetcher(tmp_path / "staging").fetch_all()
        assert result == {}
        host.exec.assert_not_called()
        assert not (tmp_path / "staging" / "host1").exists()
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_fetch_dry_run_stages_nothing_and_does_not_warn(
        self, tmp_path, fake_config_module, caplog
    ):
        """Same decline, on the fetch path's archive: nothing to stage, and the
        archive warning must not fire on a value that was never measured."""
        import logging

        host = _make_mock_host("host1")
        host.exec = AsyncMock(
            return_value=NotRunResult(
                status=Status.NotRun, command="sh", retcode=-1, host_name="host1"
            )
        )
        fake_config_module(host)

        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            result = await GcdaFetcher(tmp_path / "staging").fetch_all()

        assert result == {}
        assert not (tmp_path / "staging" / "host1").exists()
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_multiple_hosts(self, tmp_path, fake_config_module):
        host1 = _make_mock_host("host1")
        host2 = _make_mock_host("host2")
        fake_config_module(host1, host2)

        fetcher = GcdaFetcher(tmp_path / "staging")
        result = await fetcher.fetch_all()
        assert set(result) == {("host1", "app"), ("host2", "app")}

    @pytest.mark.asyncio
    async def test_pattern_filters_hosts(self, tmp_path, fake_config_module):
        """A regex pattern scopes the fetcher to matching hosts only."""
        host1 = _make_mock_host("test1")
        host2 = _make_mock_host("test2")
        fake_config_module(host1, host2)

        # `test1` alone selects nothing now — host ids are FULLMATCHED (D6).
        fetcher = GcdaFetcher(tmp_path / "staging", pattern=re.compile(r"test1.*"))
        result = await fetcher.fetch_all()

        assert set(result) == {("test1", "app")}
        host2.exec.assert_not_called()
