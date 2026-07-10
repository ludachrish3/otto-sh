"""``otto.tunnel`` — host-resident bidirectional tunnels (#2b spec).

Library-first: the CLI is a thin consumer of this package's callable API.
Grown task-by-task; final re-export surface lands with the manage layer.
"""

from .discovery import DiscoveredTunnel, TunnelDiscovery, discover_tunnels
from .manage import AddedTunnel, RemovedReport, add_tunnel, remove_all_tunnels, remove_tunnel
from .model import Direction, ProcKey, Role, Tunnel, TunnelHop, make_tunnel_id
from .sentinel import SENTINEL_PREFIX, ParsedSentinel, encode_sentinel, parse_sentinel

__all__ = [
    "SENTINEL_PREFIX",
    "AddedTunnel",
    "Direction",
    "DiscoveredTunnel",
    "ParsedSentinel",
    "ProcKey",
    "RemovedReport",
    "Role",
    "Tunnel",
    "TunnelDiscovery",
    "TunnelHop",
    "add_tunnel",
    "discover_tunnels",
    "encode_sentinel",
    "make_tunnel_id",
    "parse_sentinel",
    "remove_all_tunnels",
    "remove_tunnel",
]
