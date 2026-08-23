"""Tests for the embedded (Zephyr LLEXT) coverage collector / decoder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.coverage.fetcher.embedded import (
    EmbeddedGcdaCollector,
    _collect_one_embedded_host,
    collect_embedded_coverage,
    decode_cov_dump,
)
from otto.host.embedded_host import EmbeddedHost
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.paths import TESTS_ROOT

FIXTURES = TESTS_ROOT / "unit" / "fixtures" / "embedded_coverage"


def _mock_embedded_host(host_id: str, console_output: str) -> MagicMock:
    host = MagicMock(spec=EmbeddedHost)
    host.id = host_id
    host.exec = AsyncMock(
        return_value=CommandResult(Status.Success, value=console_output, command="dump", retcode=0),
    )
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

    assert result == {"cov_ext.c.gcda": expected}


@pytest.mark.asyncio
async def test_collect_one_host_lays_out_decoded_gcda_under_per_host_dir(tmp_path):
    """The collector drives `cov_dump` over the console and writes the decoded
    `.gcda` to `staging_root/<host.id>/`, the same layout GcdaFetcher produces.
    """
    console = (FIXTURES / "cov_dump_console.txt").read_text()
    expected = (FIXTURES / "cov_ext.c.gcda").read_bytes()
    host = _mock_embedded_host("zephyr37-llext", console)

    dest = await _collect_one_embedded_host(
        host,
        "llext call_fn cov cov_dump",
        tmp_path,
    )

    assert dest == tmp_path / "zephyr37-llext"
    assert (dest / "cov_ext.c.gcda").read_bytes() == expected
    host.exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_one_host_skips_non_embedded_hosts(tmp_path):
    """Unix/Docker hosts carry no console dumper — skip without touching them."""
    unix_host = MagicMock()  # not an EmbeddedHost
    unix_host.id = "test1"
    unix_host.exec = AsyncMock()

    dest = await _collect_one_embedded_host(
        unix_host,
        "llext call_fn cov cov_dump",
        tmp_path,
    )

    assert dest is None
    unix_host.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_all_stages_embedded_hosts_and_skips_others(
    tmp_path,
    fake_config_module,
):
    """collect_all dumps every embedded host into ``staging_root/<id>/`` and
    leaves non-embedded hosts untouched, returning ``{host_id: dir}`` like
    GcdaFetcher.fetch_all.
    """
    embedded = _mock_embedded_host(
        "zephyr37-llext",
        (FIXTURES / "cov_dump_console.txt").read_text(),
    )
    unix = MagicMock()  # not an EmbeddedHost
    unix.id = "test1"
    unix.exec = AsyncMock()
    fake_config_module(embedded, unix)

    collector = EmbeddedGcdaCollector(
        tmp_path / "staging",
        "llext call_fn cov cov_dump",
    )
    result = await collector.collect_all()

    assert set(result) == {"zephyr37-llext"}
    expected = (FIXTURES / "cov_ext.c.gcda").read_bytes()
    assert (result["zephyr37-llext"] / "cov_ext.c.gcda").read_bytes() == expected
    unix.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_embedded_coverage_drives_configured_extension(
    tmp_path,
    fake_config_module,
):
    """The [coverage.embedded].extension config drives `llext call_fn <ext> cov_dump`."""
    embedded = _mock_embedded_host(
        "zephyr37-llext",
        (FIXTURES / "cov_dump_console.txt").read_text(),
    )
    fake_config_module(embedded)
    cov_config = {"embedded": {"extension": "cov_ext"}}

    result = await collect_embedded_coverage(cov_config, tmp_path / "cov")

    assert set(result) == {"zephyr37-llext"}
    assert embedded.exec.await_args[0][0] == "llext call_fn cov_ext cov_dump"


@pytest.mark.asyncio
async def test_collect_embedded_coverage_noop_without_embedded_config(tmp_path):
    """No [coverage.embedded] section → nothing collected, no host touched."""
    result = await collect_embedded_coverage({"gcda_remote_dir": "/x"}, tmp_path / "cov")
    assert result == {}


@pytest.mark.asyncio
async def test_collect_embedded_coverage_scopes_hosts_by_pattern(
    tmp_path,
    fake_config_module,
):
    """A coverage host-id ``pattern`` selects which embedded hosts are dumped.

    The collect-from set is repo-declared (a ``[coverage].hosts`` regex), so a
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
    cov_config = {"embedded": {"extension": "cov_ext"}}

    result = await collect_embedded_coverage(
        cov_config,
        tmp_path / "cov",
        pattern=re.compile("zephyr37-llext"),
    )

    assert set(result) == {"zephyr37-llext"}
    other.exec.assert_not_awaited()
