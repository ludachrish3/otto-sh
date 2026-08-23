"""Placement resolution (endpoint + in-path) and the two mandatory refusals."""

import pytest

from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from otto.link.model import Link, LinkEndpoint
from otto.link.placement import (
    FlowDirection,
    Placement,
    endpoint_placements,
    ensure_not_hop_transit,
    ensure_not_local_link,
    ensure_not_mgmt,
    inpath_placements,
    parse_ip_addr,
)

BOTH = {FlowDirection.A_TO_B, FlowDirection.B_TO_A}

LINK = Link(
    a=LinkEndpoint(host="test1", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="test2", interface="eth1.200", ip="10.10.202.12"),
)

# real `ip -o addr show` shape (verified live on the bed)
TEST3_ADDRS = parse_ip_addr(
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever "
    "preferred_lft forever\n"
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\ "
    "      valid_lft forever\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global "
    "eth1.100\\  valid_lft forever\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global "
    "eth1.200\\  valid_lft forever\n"
)


class TestParseIpAddr:
    def test_netdevs_and_prefixes(self) -> None:
        assert set(TEST3_ADDRS) == {"lo", "eth1", "eth1.100", "eth1.200"}
        (eth1,) = TEST3_ADDRS["eth1"]
        assert str(eth1.ip) == "10.10.200.13"
        assert eth1.network.prefixlen == 24


class TestEndpointPlacements:
    def test_both_directions(self) -> None:
        assert endpoint_placements(LINK, BOTH) == [
            Placement("test1", "eth1.100", FlowDirection.A_TO_B),
            Placement("test2", "eth1.200", FlowDirection.B_TO_A),
        ]

    def test_single_direction(self) -> None:
        (p,) = endpoint_placements(LINK, {FlowDirection.B_TO_A})
        assert p == Placement("test2", "eth1.200", FlowDirection.B_TO_A)

    def test_unnamed_interface_not_impairable(self) -> None:
        bare = Link(a=LinkEndpoint(host="a_seed"), b=LINK.b)
        with pytest.raises(ValueError, match="no named interface"):
            endpoint_placements(bare, BOTH)


class TestInpathPlacements:
    def test_facing_resolution_by_subnet(self) -> None:
        # A→B egresses toward B: test3's eth1.200 faces test2; B→A faces test1
        assert inpath_placements(LINK, "test3", TEST3_ADDRS, BOTH) == [
            Placement("test3", "eth1.200", FlowDirection.A_TO_B),
            Placement("test3", "eth1.100", FlowDirection.B_TO_A),
        ]

    def test_not_in_path_fails_loud(self) -> None:
        off_path = Link(
            a=LinkEndpoint(host="x_seed", interface="eth9", ip="192.168.99.1"), b=LINK.b
        )
        with pytest.raises(ValueError, match=r"no interface on 'x_seed'.*192.168.99.1"):
            inpath_placements(off_path, "test3", TEST3_ADDRS, BOTH)

    def test_unresolved_endpoint_ip_rejected(self) -> None:
        no_ip = Link(a=LinkEndpoint(host="a_seed", interface="eth1"), b=LINK.b)
        with pytest.raises(ValueError, match="unresolved ip"):
            inpath_placements(no_ip, "test3", TEST3_ADDRS, BOTH)


class TestRefusals:
    def test_local_endpoint_link_refused(self) -> None:
        local = Link(a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0"), b=LINK.b)
        with pytest.raises(ValueError, match="local host as an endpoint"):
            ensure_not_local_link(local)

    def test_normal_link_passes(self) -> None:
        ensure_not_local_link(LINK)

    def test_local_impair_middlebox_refused(self) -> None:
        # A library-constructed link naming the local host as its in-path
        # middlebox would resolve placements on otto's own machine.
        local_mid = Link(a=LINK.a, b=LINK.b, impair=BUILTIN_LOCAL_HOST_ID)
        with pytest.raises(ValueError, match="local host as its in-path middlebox"):
            ensure_not_local_link(local_mid)

    def test_mgmt_netdev_refused(self) -> None:
        p = Placement("test3", "eth1", FlowDirection.A_TO_B)
        with pytest.raises(ValueError, match="management interface"):
            ensure_not_mgmt(p, TEST3_ADDRS, "10.10.200.13")

    def test_vlan_subinterface_passes_mgmt_check(self) -> None:
        # the e2e's whole premise: eth1.100 is NOT the mgmt netdev even though
        # it rides the same wire as eth1
        p = Placement("test3", "eth1.100", FlowDirection.A_TO_B)
        ensure_not_mgmt(p, TEST3_ADDRS, "10.10.200.13")

    def test_unknown_mgmt_ip_does_not_refuse(self) -> None:
        # mgmt address not visible in the table (e.g. NAT-fronted): no positive
        # match on the placement netdev → allow
        p = Placement("test3", "eth1.100", FlowDirection.A_TO_B)
        ensure_not_mgmt(p, TEST3_ADDRS, "203.0.113.7")


class TestHopTransitRefusal:
    def test_dependent_ip_on_placement_netdev_refused(self) -> None:
        # test2 reaches otto only via test3; its mgmt ip rides eth1.200's subnet.
        p = Placement("test3", "eth1.200", FlowDirection.A_TO_B)
        with pytest.raises(ValueError, match="hop transit"):
            ensure_not_hop_transit(p, TEST3_ADDRS, [("test2", "10.10.202.99")])

    def test_message_names_placement_and_dependent(self) -> None:
        p = Placement("test3", "eth1.200", FlowDirection.A_TO_B)
        with pytest.raises(ValueError, match=r"eth1.200.*test2"):
            ensure_not_hop_transit(p, TEST3_ADDRS, [("test2", "10.10.202.99")])

    def test_dependent_ip_on_other_netdev_allowed(self) -> None:
        # dependent's ip is in eth1.100's subnet, but this placement is eth1.200
        p = Placement("test3", "eth1.200", FlowDirection.A_TO_B)
        ensure_not_hop_transit(p, TEST3_ADDRS, [("test1", "10.10.201.99")])

    def test_no_dependents_allowed(self) -> None:
        p = Placement("test3", "eth1.200", FlowDirection.A_TO_B)
        ensure_not_hop_transit(p, TEST3_ADDRS, [])
