"""Impairment placements: where one direction's qdisc lands (spec §4/§9).

A placement is ``(host, netdev, direction)``. Two resolvers map a link to
placements — endpoint mode and in-path (middlebox) mode — plus the two
mandatory refusals: never a management interface, never a link with the local
host as an endpoint.
"""

import enum
from collections.abc import Collection
from dataclasses import dataclass
from ipaddress import IPv4Interface, ip_address, ip_interface

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from .model import Link, LinkEndpoint

# `ip -o addr show` field count for an addressed line: index, netdev, "inet", cidr, ...
_MIN_ADDR_FIELDS = 4


class FlowDirection(enum.Enum):
    """One direction of a link's traffic, in endpoint order."""

    A_TO_B = "a->b"
    B_TO_A = "b->a"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one direction's impairment lands: a netdev on a host."""

    host_id: str
    netdev: str
    direction: FlowDirection


def parse_ip_addr(output: str) -> dict[str, list[IPv4Interface]]:
    """Parse ``ip -o addr show`` output into netdev -> addressed interfaces."""
    table: dict[str, list[IPv4Interface]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= _MIN_ADDR_FIELDS and fields[2] == "inet":
            table.setdefault(fields[1], []).append(
                ip_interface(fields[3])  # ty: ignore[invalid-argument-type]
            )
    return table


def ensure_not_local_link(link: Link) -> None:
    """Refuse any link with the local host as an endpoint OR its middlebox (spec §9).

    The local host's connectivity to the bed IS otto's management path, in
    EVERY mode — as an endpoint, or (``link.impair``) as the in-path middlebox
    that would service the impairment on otto's own machine.
    """
    for end in (link.a, link.b):
        if end.host == BUILTIN_LOCAL_HOST_ID:
            raise ValueError(
                f"link {link.id!r} has the local host as an endpoint — otto's own "
                "path to the bed; refusing to impair it in any placement mode"
            )
    if link.impair == BUILTIN_LOCAL_HOST_ID:
        raise ValueError(
            f"link {link.id!r} has the local host as its in-path middlebox (impair) — "
            "otto's own path to the bed; refusing to impair it in any placement mode"
        )


def ensure_not_mgmt(
    placement: Placement, addr_table: dict[str, list[IPv4Interface]], mgmt_ip: str
) -> None:
    """Refuse a placement on the netdev carrying the host's management ip (§9).

    Only a POSITIVE match refuses: a mgmt ip invisible in the table (e.g. a
    NAT-fronted host) cannot be on the placement netdev.
    """
    for netdev, addrs in addr_table.items():
        if any(str(a.ip) == mgmt_ip for a in addrs) and netdev == placement.netdev:
            raise ValueError(
                f"refusing to impair {placement.netdev!r} on {placement.host_id!r} — "
                "it is the management interface otto reaches the host through "
                "(self-lockout)"
            )


def ensure_not_hop_transit(
    placement: Placement,
    addr_table: dict[str, list[IPv4Interface]],
    dependent_mgmt_ips: Collection[tuple[str, str]],
) -> None:
    """Refuse a placement whose netdev carries another host's hop/management transit (§9).

    Where :func:`ensure_not_mgmt` protects the placement host's OWN management
    interface, this protects a MIDDLEMAN's forwarding interface. If some lab
    host reaches otto only by hopping THROUGH the placement host, degrading the
    netdev that carries that host's management subnet severs otto → that host —
    a self-lockout one indirection out. *dependent_mgmt_ips* are the
    ``(host_id, mgmt_ip)`` pairs whose hop chain includes this placement's host;
    a positive subnet match on the placement netdev refuses.
    """
    subnets = addr_table.get(placement.netdev, [])
    for dependent_id, mgmt_ip in dependent_mgmt_ips:
        if not mgmt_ip:
            continue
        addr = ip_address(mgmt_ip)
        if any(addr in a.network for a in subnets):
            raise ValueError(
                f"refusing to impair {placement.netdev!r} on {placement.host_id!r} — "
                f"it carries the management path to {dependent_id!r} "
                "(hop transit; self-lockout)"
            )


def endpoint_placements(link: Link, directions: Collection[FlowDirection]) -> list[Placement]:
    """Endpoint mode: each direction lands on its ORIGIN endpoint's interface."""
    out: list[Placement] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        if direction not in directions:
            continue
        end = link.a if direction is FlowDirection.A_TO_B else link.b
        if end.interface is None:
            raise ValueError(
                f"link {link.id!r}: endpoint {end.host!r} has no named interface — "
                "not impairable (spec §4)"
            )
        out.append(Placement(end.host, end.interface, direction))
    return out


def inpath_placements(
    link: Link,
    impair_host_id: str,
    addr_table: dict[str, list[IPv4Interface]],
    directions: Collection[FlowDirection],
) -> list[Placement]:
    """In-path mode: each direction lands on the middlebox netdev FACING it.

    The facing netdev is the one toward the direction's target endpoint,
    auto-resolved by subnet match (spec §4).
    """
    out: list[Placement] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        if direction not in directions:
            continue
        toward = link.b if direction is FlowDirection.A_TO_B else link.a
        netdev = _facing_netdev(addr_table, toward)
        if netdev is None:
            raise ValueError(
                f"{impair_host_id!r} has no interface on {toward.host!r}'s subnet "
                f"({toward.ip}) — it is not in this link's path"
            )
        out.append(Placement(impair_host_id, netdev, direction))
    return out


def _facing_netdev(
    addr_table: dict[str, list[IPv4Interface]], endpoint: LinkEndpoint
) -> str | None:
    if not endpoint.ip:
        raise ValueError(
            f"endpoint {endpoint.host!r} has an unresolved ip — cannot resolve the "
            "middlebox's facing interface"
        )
    target = ip_address(endpoint.ip)
    for netdev, addrs in addr_table.items():
        if any(target in a.network for a in addrs):
            return netdev
    return None
