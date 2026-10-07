"""The tunnel and check-echo sentinels against frozen ps samples (dump spec §13.5)."""

import subprocess
import sys

import pytest

from otto.result import CommandResult
from otto.tunnel import _tunnel_echoes as tunnel_echoes
from otto.tunnel import check_probes, model, sentinel
from otto.tunnel.check_probes import EchoTag, echo_sentinel
from otto.tunnel.discovery import parse_process_discovery
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from otto.utils import Status
from tests._fixtures.paths import TESTS_ROOT

TUNNEL = TESTS_ROOT / "_fixtures" / "formats" / "tunnel-sentinel"
ECHO = TESTS_ROOT / "_fixtures" / "formats" / "check-echo-sentinel"
SAMPLE_TUNNEL = Tunnel(
    protocol="udp",
    service_port=5000,
    path=(TunnelHop("rack1-a", "eth1"), TunnelHop("rack1-b")),
    dest="10.0.0.9",
)
SAMPLE_TAG = EchoTag(
    run="a1b2c3",
    tunnel_id="tun-000000000001-5000",
    protocol="tcp",
    role="fwd-echo",
    host_id="rack1-a",
)


class _PsHost:
    """A host whose every command prints one fixed ``ps`` listing."""

    id = "rack1-a"

    def __init__(self, listing: str) -> None:
        self.listing = listing

    async def exec(self, cmd, *, timeout, log):
        return CommandResult(status=Status.Success, value=self.listing, command=cmd, retcode=0)


def _tunnel_v1(observations) -> None:
    (seen,) = observations
    assert (seen.pid, seen.age_seconds) == (530366, 25891)
    assert seen.parsed.tunnel == SAMPLE_TUNNEL
    assert (
        seen.parsed.direction,
        seen.parsed.role,
        seen.parsed.hop_index,
        seen.parsed.carrier_port,
    ) == (Direction.FWD, Role.INGRESS, 0, 49152)


def _echo_v1(scan) -> None:
    assert scan.listed is True
    assert [(e.pid, e.tag, e.age_s) for e in scan.echoes] == [(777, SAMPLE_TAG, 65)]


TUNNEL_MEANING = {"v1": _tunnel_v1}
ECHO_MEANING = {"v1": _echo_v1}


@pytest.mark.parametrize("version", model.TUNNEL_SENTINEL_READ_VERSIONS)
def test_discovery_reads_each_declared_tunnel_sample(version):
    TUNNEL_MEANING[version](parse_process_discovery((TUNNEL / f"{version}.txt").read_text()))


@pytest.mark.asyncio
@pytest.mark.parametrize("version", model.CHECK_ECHO_READ_VERSIONS)
async def test_the_sweep_reads_each_declared_echo_sample(version):
    listing = (ECHO / f"{version}.txt").read_text()
    ECHO_MEANING[version](await tunnel_echoes.scan(_PsHost(listing)))


def test_the_decoders_are_exactly_the_declared_read_versions():
    assert sentinel.TUNNEL_SENTINEL_READ_VERSIONS is model.TUNNEL_SENTINEL_READ_VERSIONS
    assert check_probes.CHECK_ECHO_READ_VERSIONS is model.CHECK_ECHO_READ_VERSIONS
    assert sorted(sentinel._DECODERS) == sorted(model.TUNNEL_SENTINEL_READ_VERSIONS)
    assert sorted(check_probes._ECHO_DECODERS) == sorted(model.CHECK_ECHO_READ_VERSIONS)


def test_dispatch_follows_the_declared_read_versions(monkeypatch):
    monkeypatch.setattr(sentinel, "TUNNEL_SENTINEL_READ_VERSIONS", [])
    monkeypatch.setattr(check_probes, "CHECK_ECHO_READ_VERSIONS", [])
    assert parse_process_discovery((TUNNEL / "v1.txt").read_text()) == []
    assert check_probes.parse_echo_sentinel(echo_sentinel(SAMPLE_TAG)) is None


def test_every_declared_write_version_is_also_read():
    """A process launched under a version otto cannot read could never be found again."""
    assert set(model.TUNNEL_SENTINEL_WRITE_VERSIONS) <= set(model.TUNNEL_SENTINEL_READ_VERSIONS)
    assert set(model.CHECK_ECHO_WRITE_VERSIONS) <= set(model.CHECK_ECHO_READ_VERSIONS)


@pytest.mark.parametrize("version", model.TUNNEL_SENTINEL_WRITE_VERSIONS)
def test_the_tunnel_writer_emits_each_declared_version_byte_for_byte(version):
    token = encode_sentinel(
        SAMPLE_TUNNEL, direction=Direction.FWD, role=Role.INGRESS, hop_index=0, carrier_port=49152
    )
    assert token.startswith(f"otto-tunnel:{version}:")
    assert token in (TUNNEL / f"{version}.txt").read_text().split()


@pytest.mark.parametrize("version", model.CHECK_ECHO_WRITE_VERSIONS)
def test_the_echo_writer_emits_each_declared_version_byte_for_byte(version):
    token = echo_sentinel(SAMPLE_TAG)
    assert token.startswith(f"otto-check:{version}:")
    assert token in (ECHO / f"{version}.txt").read_text().split()


_WRITE_UNDER = {
    "TUNNEL_SENTINEL_WRITE_VERSIONS": (
        "from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop\n"
        "from otto.tunnel.sentinel import encode_sentinel\n"
        "path = (TunnelHop('rack1-a'), TunnelHop('rack1-b'))\n"
        "tunnel = Tunnel(protocol='tcp', service_port=5000, path=path)\n"
        "print(encode_sentinel(tunnel, direction=Direction.FWD, role=Role.INGRESS,"
        " hop_index=0, carrier_port=49152))\n"
    ),
    "CHECK_ECHO_WRITE_VERSIONS": (
        "from otto.tunnel.check_probes import EchoTag, echo_sentinel\n"
        "print(echo_sentinel(EchoTag(run='a1b2c3', tunnel_id='-', protocol='tcp',"
        " role='segment', host_id='rack1-a')))\n"
    ),
}
"""Each single-version writer, keyed by its write list, imported only after the list is set."""


def _write_under(name: str, versions: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one writer in a fresh interpreter whose declared write list is *versions*."""
    code = f"from otto.tunnel import model\nmodel.{name} = {versions!r}\n" + _WRITE_UNDER[name]
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("TUNNEL_SENTINEL_WRITE_VERSIONS", "otto-tunnel"),
        ("CHECK_ECHO_WRITE_VERSIONS", "otto-check"),
    ],
)
def test_each_single_version_writer_stamps_the_declared_write_version(name, prefix):
    """The writer takes its version from the list, so a changed list changes what it emits."""
    proc = _write_under(name, ["v9"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith(f"{prefix}:v9:")


@pytest.mark.parametrize("name", list(_WRITE_UNDER))
def test_a_second_write_version_fails_at_import_until_a_writer_chooses(name):
    proc = _write_under(name, ["v1", "v9"])
    assert proc.returncode != 0
    assert "too many values to unpack" in proc.stderr
