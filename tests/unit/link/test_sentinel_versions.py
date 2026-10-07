"""The otto-impair sentinel against frozen ps samples (dump spec §13.5)."""

import pytest

from otto.link import params, sentinel
from otto.link.params import Selector
from otto.link.sentinel import (
    ImpairTimer,
    encode_impair_sentinel,
    encode_impair_sentinel_v3,
    parse_impair_ps,
)
from tests._fixtures.paths import TESTS_ROOT

SAMPLES = TESTS_ROOT / "_fixtures" / "formats" / "link-impairment-sentinel"
RANGE = Selector(5000, "tcp", end=5010, side="dst")

MEANING = {
    "v1": [ImpairTimer(4242, "lnk-abc123", "eth1.100", None)],
    "v2": [ImpairTimer(4243, "lnk-abc123", "eth1.100", Selector(5201, "tcp"))],
    "v3": [ImpairTimer(4244, "lnk-abc123", "eth1.100", RANGE)],
}
"""What each declared read version's sample must decode to; a version with no entry fails."""

WRITERS = {
    "v1": lambda: encode_impair_sentinel("lnk-abc123", "eth1.100"),
    "v3": lambda: encode_impair_sentinel_v3("lnk-abc123", "eth1.100", RANGE),
}
"""Each write version's writer, called with the sample's own values."""


@pytest.mark.parametrize("version", params.IMPAIR_SENTINEL_READ_VERSIONS)
def test_the_ps_scan_reads_each_declared_sample(version):
    assert parse_impair_ps((SAMPLES / f"{version}.txt").read_text()) == MEANING[version]


def test_the_decoders_are_exactly_the_declared_read_versions():
    assert sentinel.IMPAIR_SENTINEL_READ_VERSIONS is params.IMPAIR_SENTINEL_READ_VERSIONS
    assert sorted(sentinel._DECODERS) == sorted(params.IMPAIR_SENTINEL_READ_VERSIONS)


def test_dispatch_follows_the_declared_read_versions(monkeypatch):
    monkeypatch.setattr(sentinel, "IMPAIR_SENTINEL_READ_VERSIONS", ["v1", "v3"])
    assert parse_impair_ps((SAMPLES / "v2.txt").read_text()) == []


def test_every_declared_write_version_is_also_read():
    """A timer launched under a version otto cannot read could never be found and cancelled."""
    assert set(params.IMPAIR_SENTINEL_WRITE_VERSIONS) <= set(params.IMPAIR_SENTINEL_READ_VERSIONS)


def test_the_named_writer_versions_are_the_declared_writes():
    named = [sentinel.IMPAIR_SENTINEL_VERSION, sentinel.IMPAIR_SENTINEL_VERSION_V3]
    assert sorted(named) == sorted(params.IMPAIR_SENTINEL_WRITE_VERSIONS) == sorted(WRITERS)


@pytest.mark.parametrize("version", params.IMPAIR_SENTINEL_WRITE_VERSIONS)
def test_each_declared_write_version_is_emitted_byte_for_byte(version):
    writer = WRITERS.get(version)
    assert writer is not None, f"declared write version {version} has no writer here"
    token = writer()
    assert token.startswith(f"otto-impair:{version}:")
    assert token in (SAMPLES / f"{version}.txt").read_text().split()
