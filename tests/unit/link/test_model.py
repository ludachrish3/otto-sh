"""Runtime Link model: identity, normalization, provenance."""

import dataclasses

import pytest

from otto.link import Link, LinkEndpoint, Provenance, make_link_id


def _ep(host: str, iface: str | None = "eth1") -> LinkEndpoint:
    return LinkEndpoint(host=host, interface=iface, ip="10.0.0.1")


class TestLinkId:
    def test_id_auto_computed(self):
        link = Link(a=_ep("carrot"), b=_ep("tomato"))
        assert link.id.startswith("lnk-")
        assert len(link.id) == 16

    def test_id_endpoint_order_invariant(self):
        assert (
            Link(a=_ep("carrot"), b=_ep("tomato")).id == Link(a=_ep("tomato"), b=_ep("carrot")).id
        )

    def test_id_ignores_ip_and_port(self):
        moved = LinkEndpoint(host="carrot", interface="eth1", ip="10.9.9.9", port=5000)
        assert Link(a=moved, b=_ep("tomato")).id == Link(a=_ep("carrot"), b=_ep("tomato")).id

    def test_id_distinguishes_protocol(self):
        a, b = _ep("carrot"), _ep("tomato")
        assert Link(a=a, b=b, protocol="udp").id != Link(a=a, b=b, protocol="tcp").id

    def test_id_distinguishes_interface(self):
        assert (
            Link(a=_ep("carrot", "eth1"), b=_ep("tomato")).id
            != Link(a=_ep("carrot", "eth2"), b=_ep("tomato")).id
        )

    def test_explicit_id_preserved(self):
        assert Link(a=_ep("a"), b=_ep("b"), id="lnk-abcdef123456").id == "lnk-abcdef123456"

    def test_make_link_id_matches_dataclass(self):
        a, b = _ep("carrot"), _ep("tomato")
        assert make_link_id(a, b, "tcp") == Link(a=a, b=b).id


class TestLinkDefaults:
    def test_protocol_defaults_tcp(self):
        assert Link(a=_ep("a"), b=_ep("b")).protocol == "tcp"

    def test_provenance_defaults_declared(self):
        assert Link(a=_ep("a"), b=_ep("b")).provenance is Provenance.DECLARED

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Link(a=_ep("a"), b=_ep("b")).protocol = "udp"  # type: ignore[misc]
