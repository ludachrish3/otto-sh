"""Tests for the embedded (Zephyr LLEXT) coverage collector / decoder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.embedded import (
    EmbeddedGcdaCollector,
    _collect_one_embedded_host,
    collect_embedded_coverage,
    decode_cov_dump,
)
from otto.host.embedded_host import EmbeddedHost
from otto.labs.loader import Lab
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.paths import TESTS_ROOT

FIXTURES = TESTS_ROOT / "unit" / "fixtures" / "embedded_coverage"
#: The full path the fixture capture dumps, leading ``/`` stripped: the key the
#: decoder files it under and the path it is staged at inside a product dir.
DUMPED = "home/vagrant/build/cov_ext_app/CMakeFiles/cov_ext_llext_lib.dir/src/cov_ext.c.gcda"


def _llext_product(name: str = "cov_ext", dump_fn: str = "cov_dump", verdict: bool = True):
    p = MagicMock()
    p.name = name
    p.dump_fn = dump_fn
    p.instrumented = MagicMock(return_value=verdict)
    return p


def _mock_embedded_host(host_id: str, console_output: str, *, products=None) -> MagicMock:
    from otto.host.binary_loader import LlextHexLoader

    host = MagicMock(spec=EmbeddedHost)
    host.id = host_id
    host.loader = LlextHexLoader()
    host.exec = AsyncMock(
        return_value=CommandResult(Status.Success, value=console_output, command="dump", retcode=0),
    )
    host.products = [_llext_product()] if products is None else products
    return host


@pytest.fixture
def fake_config_module():
    """Install an OttoContext so ``all_hosts()`` returns test hosts.

    Yields ``set_hosts(*hosts)`` to register the host list for the test.
    """
    lab = Lab(name="test_lab")
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)

    def set_hosts(*hosts: MagicMock) -> None:
        # The lab's REAL mapping, mutated in place — see the twin fixture in
        # ``tests/unit/cov/test_fetcher.py``: fleet scoping iterates and
        # ``.items()``s ``lab.hosts``, which a ``values()``-only double answers
        # from the empty dict underneath instead of failing.
        lab.hosts.clear()
        for h in hosts:
            lab.hosts[h.id] = h

    yield set_hosts
    reset_context(token)


def test_decode_cov_dump_reconstructs_gcda_from_console_capture():
    """A real `cov_dump` console capture decodes to the exact `.gcda` bytes.

    Fixtures are a live capture from the mps2_an385 feasibility gate: the
    serial hexdump (`Emitting N bytes for <path> / hexdump / <path> / Gcov End`)
    and the known-good `.gcda` that `arm-zephyr-eabi-gcov` accepted.
    """
    console = (FIXTURES / "cov_dump_console.txt").read_text()
    expected = (FIXTURES / "cov_ext.c.gcda").read_bytes()

    result = decode_cov_dump(console)

    assert result == {DUMPED: expected}


@pytest.mark.asyncio
async def test_collect_one_host_lays_out_decoded_gcda_under_per_host_dir(tmp_path):
    """The collector drives the product's dump over the console via the board's
    loader and writes the decoded `.gcda` to `<staging_root>/<host.id>/<product>/`, the
    same layout GcdaFetcher produces."""
    console = (FIXTURES / "cov_dump_console.txt").read_text()
    expected = (FIXTURES / "cov_ext.c.gcda").read_bytes()
    host = _mock_embedded_host("zephyr37-llext", console)

    dest = await _collect_one_embedded_host(host, tmp_path)

    assert dest == {"cov_ext": tmp_path / "zephyr37-llext" / "cov_ext"}
    assert (dest["cov_ext"] / DUMPED).read_bytes() == expected
    host.exec.assert_awaited_once()
    assert host.exec.await_args[0][0] == "llext call_fn cov_ext cov_dump"


def test_decode_cov_dump_keeps_the_full_dumped_path():
    """Two units with one basename stay two entries, each under its own path."""
    block = "Emitting 1 bytes for {p}\n00000000: {b}\n{p}\nGcov End\n"
    first = block.format(p="/b/a/main.c.gcda", b="01")
    second = block.format(p="/b/c/main.c.gcda", b="02")

    expected = {"b/a/main.c.gcda": b"\x01", "b/c/main.c.gcda": b"\x02"}
    assert decode_cov_dump(first + second) == expected


@pytest.mark.asyncio
async def test_a_dumped_path_that_escapes_the_product_dir_is_not_staged(tmp_path, caplog):
    """A ``..`` in a console-dumped path must not write outside the product dir."""
    import logging

    evil = "/x/../../../../evil.gcda"
    console = f"Emitting 1 bytes for {evil}\n00000000: 01\n{evil}\nGcov End\n"
    host = _mock_embedded_host("b", console)

    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.embedded"):
        assert await _collect_one_embedded_host(host, tmp_path / "s") is None

    assert not (tmp_path / "evil.gcda").exists()
    assert not (tmp_path / "s" / "b" / "cov_ext").exists()
    assert "evil.gcda" in caplog.text


@pytest.mark.asyncio
async def test_a_collection_never_reports_stale_counters_as_fetched(tmp_path):
    """The product leaf is reset before the decoded blocks are staged: a ``.gcda``
    left by an earlier collection into the same output does not survive."""
    stale = tmp_path / "zephyr37-llext" / "cov_ext" / "old" / "gone.c.gcda"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    host = _mock_embedded_host("zephyr37-llext", (FIXTURES / "cov_dump_console.txt").read_text())

    dest = await _collect_one_embedded_host(host, tmp_path)

    leaf = dest["cov_ext"]
    assert [p.relative_to(leaf).as_posix() for p in leaf.rglob("*.gcda")] == [DUMPED]


@pytest.mark.asyncio
async def test_collect_one_host_skips_non_embedded_hosts(tmp_path):
    """Unix/Docker hosts carry no console dumper — skip without touching them."""
    unix_host = MagicMock()  # not an EmbeddedHost
    unix_host.id = "test1"
    unix_host.exec = AsyncMock()

    dest = await _collect_one_embedded_host(unix_host, tmp_path)

    assert dest is None
    unix_host.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_board_with_no_instrumented_product_is_skipped(tmp_path):
    """An uninstrumented product is never dumped — the console is not touched."""
    host = _mock_embedded_host("b", "", products=[_llext_product("x", verdict=False)])

    assert await _collect_one_embedded_host(host, tmp_path) is None

    host.exec.assert_not_called()


@pytest.mark.asyncio
async def test_a_product_without_a_dump_fn_is_skipped_with_a_warning(tmp_path, caplog):
    """Only an llext product exports a dump function. One that does not is named
    in a WARNING and skipped — never dumped through a guessed function name."""
    import logging

    # spec'd to exactly what it has: `getattr(p, "dump_fn", None)` must miss.
    bare = MagicMock(spec=["name", "instrumented"])
    bare.name = "daemon"
    bare.instrumented = MagicMock(return_value=True)
    host = _mock_embedded_host("zephyr37-llext", "", products=[bare])

    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.embedded"):
        assert await _collect_one_embedded_host(host, tmp_path) is None

    host.exec.assert_not_called()
    assert "daemon on zephyr37-llext has no dump_fn" in caplog.text


@pytest.mark.asyncio
async def test_each_instrumented_product_is_dumped_by_its_own_dump_fn(tmp_path):
    """Two instrumented products on one board are dumped separately, each through
    its own exported dump function."""
    console = (FIXTURES / "cov_dump_console.txt").read_text()
    host = _mock_embedded_host(
        "zephyr37-llext",
        console,
        products=[_llext_product("cov_ext"), _llext_product("other", dump_fn="gcov_dump")],
    )

    dest = await _collect_one_embedded_host(host, tmp_path)

    assert set(dest) == {"cov_ext", "other"}
    assert [c.args[0] for c in host.exec.call_args_list] == [
        "llext call_fn cov_ext cov_dump",
        "llext call_fn other gcov_dump",
    ]


@pytest.mark.asyncio
async def test_collect_all_stages_embedded_hosts_and_skips_others(
    tmp_path,
    fake_config_module,
):
    """collect_all dumps every embedded host into ``<staging_root>/<id>/<product>/`` and
    leaves non-embedded hosts untouched, returning ``{(host_id, product): dir}``
    like GcdaFetcher.fetch_all.
    """
    embedded = _mock_embedded_host(
        "zephyr37-llext",
        (FIXTURES / "cov_dump_console.txt").read_text(),
    )
    unix = MagicMock()  # not an EmbeddedHost
    unix.id = "test1"
    unix.exec = AsyncMock()
    fake_config_module(embedded, unix)

    collector = EmbeddedGcdaCollector(tmp_path / "staging")
    result = await collector.collect_all()

    assert set(result) == {("zephyr37-llext", "cov_ext")}
    expected = (FIXTURES / "cov_ext.c.gcda").read_bytes()
    assert (result[("zephyr37-llext", "cov_ext")] / DUMPED).read_bytes() == expected
    unix.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_embedded_coverage_keys_by_host_and_product(tmp_path, fake_config_module):
    console = (FIXTURES / "cov_dump_console.txt").read_text()
    fake_config_module(_mock_embedded_host("zephyr37-llext", console))

    result = await collect_embedded_coverage(tmp_path)

    assert set(result) == {("zephyr37-llext", "cov_ext")}


@pytest.mark.asyncio
async def test_collect_embedded_coverage_noop_without_embedded_hosts(
    tmp_path,
    fake_config_module,
):
    """No embedded board in the lab → nothing collected."""
    unix = MagicMock()  # not an EmbeddedHost
    unix.id = "test1"
    unix.exec = AsyncMock()
    fake_config_module(unix)

    assert await collect_embedded_coverage(tmp_path / "cov") == {}
    unix.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_embedded_coverage_scopes_hosts_by_pattern(
    tmp_path,
    fake_config_module,
):
    """A coverage host-id ``pattern`` selects which embedded hosts are dumped.

    The collect-from set is project-declared (a ``[coverage].hosts`` regex), so a
    host that does not match the pattern is never touched.
    """
    import re

    target = _mock_embedded_host(
        "zephyr37-llext",
        (FIXTURES / "cov_dump_console.txt").read_text(),
    )
    other = _mock_embedded_host(
        "zephyr37-fat",
        (FIXTURES / "cov_dump_console.txt").read_text(),
    )
    fake_config_module(target, other)

    result = await collect_embedded_coverage(
        tmp_path / "cov",
        pattern=re.compile("zephyr37-llext"),
    )

    assert set(result) == {("zephyr37-llext", "cov_ext")}
    other.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_container_only_pattern_collects_nothing_without_refusing(
    tmp_path,
    fake_config_module,
):
    """A pattern naming only containers walks them, skips them, and collects nothing.

    A container is not embedded, so nothing is dumped from it. The walk must
    still reach it rather than refuse the selection: a container-only
    ``[coverage].hosts`` is legal, and its counters are the remote fetcher's.
    """
    import re

    from otto.host.docker_host import DockerContainerHost

    container = MagicMock(spec=DockerContainerHost)
    container.id = "test1.r1.api"
    container.exec = AsyncMock()
    fake_config_module(container)

    got = await collect_embedded_coverage(tmp_path / "stage", pattern=re.compile(r".*\.r1\.api"))

    assert got == {}
    container.exec.assert_not_awaited()
