"""Sentinel wire-format codec: encode <-> parse round-trips and discovery parsing."""

import getpass

from otto.link import Link, LinkEndpoint, Provenance
from otto.link.sentinel import encode_sentinel, parse_discovery, parse_sentinel


def _dynamic_link(a_port=5000, b_port=5001, a_iface="eth1", proto="udp") -> Link:
    return Link(
        a=LinkEndpoint(host="carrot_seed", interface=a_iface, port=a_port),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", port=b_port),
        protocol=proto,
        provenance=Provenance.DYNAMIC,
    )


class TestRoundTrip:
    def test_encode_parse_round_trip(self):
        link = _dynamic_link()
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None
        assert parsed.id == link.id
        assert parsed.protocol == "udp"
        assert parsed.provenance is Provenance.DYNAMIC
        assert {(e.host, e.interface, e.port) for e in (parsed.a, parsed.b)} == {
            ("carrot_seed", "eth1", 5000),
            ("tomato_seed", "eth1", 5001),
        }
        # No lab context at parse time, so endpoint ips stay empty (resolved later).
        assert parsed.a.ip == ""
        assert parsed.b.ip == ""

    def test_colon_in_interface_name_survives(self):
        link = _dynamic_link(a_iface="eth0:1")
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None
        assert "eth0:1" in {parsed.a.interface, parsed.b.interface}

    def test_none_iface_and_port_round_trip(self):
        link = Link(
            a=LinkEndpoint(host="a"), b=LinkEndpoint(host="b"), provenance=Provenance.DYNAMIC
        )
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None
        assert parsed.a.interface is None
        assert parsed.a.port is None

    def test_no_username_in_wire_format(self):
        """Owner-agnostic frame: exactly the 10 documented colon segments and
        none carries the username. A substring check (``getuser() not in token``)
        would false-fail for a user whose name is a substring of a fixed segment
        (e.g. a user named ``seed`` or ``udp``); a structural check cannot.
        """
        link = _dynamic_link()
        segments = encode_sentinel(link).split(":")
        assert segments == [
            "otto-link",
            "v1",
            link.id,
            "udp",
            "carrot_seed",
            "eth1",
            "5000",
            "tomato_seed",
            "eth1",
            "5001",
        ]
        assert getpass.getuser() not in segments


class TestParseRejections:
    def test_non_otto_token_none(self):
        assert parse_sentinel("socat:UDP4-LISTEN:5000") is None

    def test_future_version_none(self):
        good = encode_sentinel(_dynamic_link())
        assert parse_sentinel(good.replace(":v1:", ":v2:", 1)) is None

    def test_malformed_none(self):
        assert parse_sentinel("otto-link:v1:only-three") is None
        assert parse_sentinel("") is None


class TestParseDiscovery:
    def test_groups_processes_by_id(self):
        link = _dynamic_link()
        token = encode_sentinel(link)
        ps = (
            f"1201 {token}\n"
            f"1202 {token}\n"  # second process, same link
            "1300 socat UDP4-LISTEN:9999,fork TCP4:10.0.0.1:9999\n"  # non-otto: excluded
            "1400 /usr/sbin/sshd -D\n"
        )
        links = parse_discovery(ps)
        assert len(links) == 1
        assert links[0].id == link.id

    def test_distinct_links_kept_separate(self):
        one, two = _dynamic_link(), _dynamic_link(proto="tcp")
        ps = f"1 {encode_sentinel(one)}\n2 {encode_sentinel(two)}\n"
        assert {ln.id for ln in parse_discovery(ps)} == {one.id, two.id}

    def test_same_id_processes_backfill_ports(self):
        """Two tagged processes of one tunnel share an id (the dynamic id
        encodes only ``a.port``), so ``parse_discovery`` groups them AND
        backfills a port one process could not report: first non-``None`` per
        end wins. Here ``one`` omits ``b.port`` and ``two`` supplies it, so the
        merged link recovers both ends' ports.
        """
        one = _dynamic_link(b_port=None)  # a=5000, b unknown from this process
        two = _dynamic_link()  # a=5000, b=5001 -> SAME id as `one`
        assert one.id == two.id  # id encodes only a.port, so these collide
        ps = f"1 {encode_sentinel(one)}\n2 {encode_sentinel(two)}\n"
        (merged,) = parse_discovery(ps)
        assert merged.id == two.id
        assert {merged.a.port, merged.b.port} == {5000, 5001}  # b backfilled

    def test_empty_and_garbage_input(self):
        assert parse_discovery("") == []
        assert parse_discovery("not a ps line at all\n\n") == []
