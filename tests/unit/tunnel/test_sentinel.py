"""Sentinel v2 wire-format round-trips and rejection paths (spec #2b §5)."""

import pytest

from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel, parse_sentinel

TUNNEL = Tunnel(
    protocol="udp",
    service_port=5000,
    path=(TunnelHop("carrot_seed", "eth1"), TunnelHop("tomato_soil"), TunnelHop("pepper_pot")),
    dest="beet_row",
)


def _token(**overrides: object) -> str:
    kwargs: dict = {
        "direction": Direction.FWD,
        "role": Role.RELAY,
        "hop_index": 1,
        "carrier_port": 50001,
    }
    kwargs.update(overrides)
    return encode_sentinel(TUNNEL, **kwargs)


class TestRoundTrip:
    def test_full_round_trip(self) -> None:
        parsed = parse_sentinel(_token())
        assert parsed is not None
        assert parsed.tunnel == TUNNEL
        assert parsed.direction is Direction.FWD
        assert parsed.role is Role.RELAY
        assert parsed.hop_index == 1
        assert parsed.carrier_port == 50001

    def test_wire_id_passes_through_verbatim(self) -> None:
        t = Tunnel(
            protocol="udp",
            service_port=5000,
            path=(TunnelHop("a"), TunnelHop("b")),
            id="tun-feedface0000-5000",
        )
        parsed = parse_sentinel(
            encode_sentinel(
                t, direction=Direction.REV, role=Role.INGRESS, hop_index=1, carrier_port=50002
            )
        )
        assert parsed is not None
        assert parsed.tunnel.id == "tun-feedface0000-5000"

    def test_no_dest_encodes_empty(self) -> None:
        t = Tunnel(protocol="tcp", service_port=80, path=(TunnelHop("a"), TunnelHop("b")))
        parsed = parse_sentinel(
            encode_sentinel(
                t, direction=Direction.FWD, role=Role.INGRESS, hop_index=0, carrier_port=49500
            )
        )
        assert parsed is not None
        assert parsed.tunnel.dest is None

    @pytest.mark.parametrize("iface", ["eth0:1", "weird,name", "pct%20"])
    def test_hostile_interface_chars_survive(self, iface: str) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=(TunnelHop("a", iface), TunnelHop("b")))
        parsed = parse_sentinel(
            encode_sentinel(
                t, direction=Direction.FWD, role=Role.INGRESS, hop_index=0, carrier_port=49500
            )
        )
        assert parsed is not None
        assert parsed.tunnel.path[0].interface == iface

    def test_segment_count_is_eleven(self) -> None:
        assert len(_token().split(":")) == 11


class TestRejection:
    @pytest.mark.parametrize(
        "token",
        [
            "",
            "socat",
            "otto-link:v1:lnk-abc-5000:udp:a:eth0:5000:b:eth0:5000",  # v1 era: gone
            "otto-tunnel:v2:x:udp:5000:50001:fwd:relay:1::a%40,b",  # unknown version
            "otto-tunnel:v1:only:three",  # wrong segment count
        ],
    )
    def test_foreign_or_malformed_is_none(self, token: str) -> None:
        assert parse_sentinel(token) is None

    def test_bad_int_fields_are_none(self) -> None:
        good = _token()
        parts = good.split(":")
        parts[4] = "notaport"  # svc-port
        assert parse_sentinel(":".join(parts)) is None

    def test_bad_direction_or_role_is_none(self) -> None:
        parts = _token().split(":")
        parts[6] = "sideways"
        assert parse_sentinel(":".join(parts)) is None

    def test_single_host_path_is_none(self) -> None:
        parts = _token().split(":")
        parts[10] = "onlyone"
        assert parse_sentinel(":".join(parts)) is None


class TestWireGolden:
    def test_encode_produces_the_exact_v1_bytes(self):
        # STABILITY CONTRACT (spec §5): these bytes are what live processes
        # carry in argv[0]. If this test fails, the refactor broke the wire.
        tunnel = Tunnel(
            protocol="tcp",
            service_port=8080,
            path=(TunnelHop(host="h1", interface="eth0"), TunnelHop(host="h2")),
            dest=None,
            id="tun-abc-8080",
        )
        token = encode_sentinel(
            tunnel, direction=Direction.FWD, role=Role.INGRESS, hop_index=0, carrier_port=50000
        )
        assert token == (
            "otto-tunnel:v1:tun-abc-8080:tcp:8080:50000:fwd:ingress:0::h1%2540eth0%2Ch2"
        )
        parsed = parse_sentinel(token)
        assert parsed is not None
        assert parsed.tunnel == tunnel
