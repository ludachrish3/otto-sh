"""Unit tests for the Tunnel model and id scheme (spec #2b §4)."""

import pytest

from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop, make_tunnel_id


def _path(*hosts: str) -> tuple[TunnelHop, ...]:
    return tuple(TunnelHop(host=h) for h in hosts)


class TestMakeTunnelId:
    def test_format_is_tun_12hex_port(self) -> None:
        tid = make_tunnel_id(_path("a", "b"), "udp", 5000)
        prefix, hexpart, port = tid.split("-")
        assert prefix == "tun"
        assert len(hexpart) == 12
        assert int(hexpart, 16) >= 0
        assert port == "5000"

    def test_deterministic(self) -> None:
        assert make_tunnel_id(_path("a", "b"), "udp", 5000) == make_tunnel_id(
            _path("a", "b"), "udp", 5000
        )

    def test_path_order_sensitive(self) -> None:
        assert make_tunnel_id(_path("a", "c", "b"), "udp", 5000) != make_tunnel_id(
            _path("b", "c", "a"), "udp", 5000
        )

    def test_interface_and_protocol_in_hash(self) -> None:
        with_if = (TunnelHop("a", "eth1"), TunnelHop("b"))
        assert make_tunnel_id(with_if, "udp", 5000) != make_tunnel_id(_path("a", "b"), "udp", 5000)
        assert make_tunnel_id(_path("a", "b"), "tcp", 5000) != make_tunnel_id(
            _path("a", "b"), "udp", 5000
        )

    def test_protocol_case_insensitive(self) -> None:
        assert make_tunnel_id(_path("a", "b"), "UDP", 5000) == make_tunnel_id(
            _path("a", "b"), "udp", 5000
        )

    def test_port_is_suffix_not_hashed(self) -> None:
        t1 = make_tunnel_id(_path("a", "b"), "udp", 5000)
        t2 = make_tunnel_id(_path("a", "b"), "udp", 6000)
        assert t1.rsplit("-", 1)[0] == t2.rsplit("-", 1)[0]


class TestTunnel:
    def test_id_autocomputed(self) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=_path("a", "b"))
        assert t.id == make_tunnel_id(_path("a", "b"), "udp", 5000)

    def test_explicit_id_kept(self) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=_path("a", "b"), id="tun-abc-5000")
        assert t.id == "tun-abc-5000"

    def test_path_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            Tunnel(protocol="udp", service_port=5000, path=_path("a"))

    def test_dest_outside_id(self) -> None:
        plain = Tunnel(protocol="udp", service_port=5000, path=_path("a", "b"))
        with_dest = Tunnel(protocol="udp", service_port=5000, path=_path("a", "b"), dest="c")
        assert plain.id == with_dest.id


class TestExpectedProcesses:
    def test_direct_pair_is_two_per_host(self) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=_path("a", "b"))
        assert t.expected_processes() == {
            ("a", Direction.FWD, Role.INGRESS),
            ("a", Direction.REV, Role.EGRESS),
            ("b", Direction.FWD, Role.EGRESS),
            ("b", Direction.REV, Role.INGRESS),
        }

    def test_three_hop_has_relays(self) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=_path("a", "c", "b"))
        expected = t.expected_processes()
        assert len(expected) == 6
        assert ("c", Direction.FWD, Role.RELAY) in expected
        assert ("c", Direction.REV, Role.RELAY) in expected
