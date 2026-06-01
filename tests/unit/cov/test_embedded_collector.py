"""Tests for the embedded (Zephyr LLEXT) coverage collector / decoder."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.coverage.fetcher.embedded import (
    _collect_one_embedded_host,
    decode_cov_dump,
)
from otto.host.embeddedHost import EmbeddedHost
from otto.utils import CommandStatus, Status

FIXTURES = Path(__file__).parent.parent / "fixtures" / "embedded_coverage"


def _mock_embedded_host(host_id: str, console_output: str) -> MagicMock:
    host = MagicMock(spec=EmbeddedHost)
    host.id = host_id
    host.oneshot = AsyncMock(
        return_value=CommandStatus("dump", console_output, Status.Success, 0),
    )
    return host


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
    host = _mock_embedded_host("sprout_cov", console)

    dest = await _collect_one_embedded_host(
        host, "llext call_fn cov cov_dump", tmp_path,
    )

    assert dest == tmp_path / "sprout_cov"
    assert (dest / "cov_ext.c.gcda").read_bytes() == expected
    host.oneshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_one_host_skips_non_embedded_hosts(tmp_path):
    """Unix/Docker hosts carry no console dumper — skip without touching them."""
    unix_host = MagicMock()  # not an EmbeddedHost
    unix_host.id = "carrot"
    unix_host.oneshot = AsyncMock()

    dest = await _collect_one_embedded_host(
        unix_host, "llext call_fn cov cov_dump", tmp_path,
    )

    assert dest is None
    unix_host.oneshot.assert_not_awaited()
