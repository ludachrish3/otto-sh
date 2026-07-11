"""``otto.tunnel`` — host-resident bidirectional tunnels (#2b spec).

Library-first: the CLI is a thin consumer of this package's callable API.
Grown task-by-task; final re-export surface lands with the manage layer.
"""

from .carrier import CARRIERS, DEFAULT_CARRIER, TunnelCarrier, build_carrier, register_carrier
from .discovery import DiscoveredTunnel, TunnelDiscovery, discover_tunnels
from .manage import AddedTunnel, RemovedReport, add_tunnel, remove_all_tunnels, remove_tunnel
from .model import Direction, ProcKey, Role, Tunnel, TunnelHop, make_tunnel_id
from .sentinel import SENTINEL_PREFIX, ParsedSentinel, encode_sentinel, parse_sentinel

__all__ = [
    "CARRIERS",
    "DEFAULT_CARRIER",
    "SENTINEL_PREFIX",
    "AddedTunnel",
    "Direction",
    "DiscoveredTunnel",
    "ParsedSentinel",
    "ProcKey",
    "RemovedReport",
    "Role",
    "Tunnel",
    "TunnelCarrier",
    "TunnelDiscovery",
    "TunnelHop",
    "add_tunnel",
    "build_carrier",
    "discover_tunnels",
    "encode_sentinel",
    "make_tunnel_id",
    "parse_sentinel",
    "register_carrier",
    "remove_all_tunnels",
    "remove_tunnel",
]
