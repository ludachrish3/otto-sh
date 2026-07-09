"""Runtime Link model: identity, normalization, provenance."""

import dataclasses

import pytest

from otto.link import Link, LinkEndpoint, Provenance, make_link_id
from otto.link.model import make_static_link_id


def _ep(host: str, iface: str | None = "eth1") -> LinkEndpoint:
    return LinkEndpoint(host=host, interface=iface, ip="10.0.0.1")


class TestLinkId:
    def test_id_auto_computed(self):
        """Default provenance is DECLARED (static), so the id is the readable
        ``a--b`` handle, not a ``lnk-<hex>`` route hash (that form is reserved
        for DYNAMIC links; see ``test_link_id.py``)."""
        link = Link(a=_ep("carrot"), b=_ep("tomato"))
        assert link.id == "carrot--tomato"

    def test_id_endpoint_order_invariant(self):
        assert (
            Link(a=_ep("carrot"), b=_ep("tomato")).id == Link(a=_ep("tomato"), b=_ep("carrot")).id
        )

    def test_id_ignores_ip_and_port(self):
        moved = LinkEndpoint(host="carrot", interface="eth1", ip="10.9.9.9", port=5000)
        assert Link(a=moved, b=_ep("tomato")).id == Link(a=_ep("carrot"), b=_ep("tomato")).id

    def test_id_distinguishes_protocol(self):
        """Route-hash property of ``make_link_id`` itself (frozen contract).

        Exercised directly rather than via ``Link(...).id``: default provenance
        is DECLARED (static), whose id is the protocol-agnostic ``a--b``
        handle — this property now only holds for the DYNAMIC id builder.
        """
        a, b = _ep("carrot"), _ep("tomato")
        assert make_link_id(a, b, "udp") != make_link_id(a, b, "tcp")

    def test_id_protocol_case_insensitive(self):
        """Protocol is lowercased in the id, so a future ``--protocol UDP`` mints
        the same route id as the declared ``"udp"`` (stability contract)."""
        a, b = _ep("carrot"), _ep("tomato")
        assert make_link_id(a, b, "UDP") == make_link_id(a, b, "udp")
        assert make_link_id(a, b, "TCP") == make_link_id(a, b, "tcp")

    def test_id_distinguishes_interface(self):
        """Same rationale as ``test_id_distinguishes_protocol`` above: exercise
        ``make_link_id`` directly since the static (default-provenance) ``Link``
        id no longer varies by interface."""
        assert make_link_id(_ep("carrot", "eth1"), _ep("tomato"), "tcp") != make_link_id(
            _ep("carrot", "eth2"), _ep("tomato"), "tcp"
        )

    def test_explicit_id_preserved(self):
        assert Link(a=_ep("a"), b=_ep("b"), id="lnk-abcdef123456").id == "lnk-abcdef123456"

    def test_make_static_link_id_matches_dataclass(self):
        """Static-provenance counterpart of ``test_dynamic_link_computes_suffixed_id``
        in ``test_link_id.py``: for the default (DECLARED) provenance, the
        dataclass's auto-computed id matches ``make_static_link_id`` directly —
        ``make_link_id`` (the route hash) is no longer involved for static links.
        """
        a, b = _ep("carrot"), _ep("tomato")
        assert make_static_link_id(a, b, None) == Link(a=a, b=b).id


class TestLinkDefaults:
    def test_protocol_defaults_tcp(self):
        assert Link(a=_ep("a"), b=_ep("b")).protocol == "tcp"

    def test_provenance_defaults_declared(self):
        assert Link(a=_ep("a"), b=_ep("b")).provenance is Provenance.DECLARED

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Link(a=_ep("a"), b=_ep("b")).protocol = "udp"  # type: ignore[misc]
