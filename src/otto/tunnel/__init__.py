"""``otto.tunnel`` — host-resident bidirectional tunnels (#2b spec).

Library-first: the CLI is a thin consumer of this package's callable API.
Grown task-by-task; final re-export surface lands with the manage layer.

Every name is exported lazily (PEP 562), the shape every otto package shares.
The socat carrier is registered by reference in ``.carrier``, so no import
here exists for its side effect. The resolver does not write a resolved name
back into the module dict; see ``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .carrier import CARRIERS as CARRIERS
    from .carrier import DEFAULT_CARRIER as DEFAULT_CARRIER
    from .carrier import TunnelCarrier as TunnelCarrier
    from .carrier import build_carrier as build_carrier
    from .carrier import register_carrier as register_carrier
    from .check import TunnelCheckReport as TunnelCheckReport
    from .check import check_tunnel as check_tunnel
    from .discovery import DiscoveredTunnel as DiscoveredTunnel
    from .discovery import TunnelDiscovery as TunnelDiscovery
    from .discovery import TunnelNotMeasuredError as TunnelNotMeasuredError
    from .discovery import discover_tunnels as discover_tunnels
    from .manage import AddedTunnel as AddedTunnel
    from .manage import DryRunPlan as DryRunPlan
    from .manage import RemovedReport as RemovedReport
    from .manage import add_tunnel as add_tunnel
    from .manage import remove_all_tunnels as remove_all_tunnels
    from .manage import remove_tunnel as remove_tunnel
    from .model import Direction as Direction
    from .model import ProcKey as ProcKey
    from .model import Role as Role
    from .model import Tunnel as Tunnel
    from .model import TunnelHop as TunnelHop
    from .model import make_tunnel_id as make_tunnel_id
    from .sentinel import SENTINEL_PREFIX as SENTINEL_PREFIX
    from .sentinel import ParsedSentinel as ParsedSentinel
    from .sentinel import encode_sentinel as encode_sentinel
    from .sentinel import parse_sentinel as parse_sentinel

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CARRIERS": "otto.tunnel.carrier",
    "DEFAULT_CARRIER": "otto.tunnel.carrier",
    "TunnelCarrier": "otto.tunnel.carrier",
    "build_carrier": "otto.tunnel.carrier",
    "register_carrier": "otto.tunnel.carrier",
    "TunnelCheckReport": "otto.tunnel.check",
    "check_tunnel": "otto.tunnel.check",
    "DiscoveredTunnel": "otto.tunnel.discovery",
    "TunnelDiscovery": "otto.tunnel.discovery",
    "TunnelNotMeasuredError": "otto.tunnel.discovery",
    "discover_tunnels": "otto.tunnel.discovery",
    "AddedTunnel": "otto.tunnel.manage",
    "DryRunPlan": "otto.tunnel.manage",
    "RemovedReport": "otto.tunnel.manage",
    "add_tunnel": "otto.tunnel.manage",
    "remove_all_tunnels": "otto.tunnel.manage",
    "remove_tunnel": "otto.tunnel.manage",
    "Direction": "otto.tunnel.model",
    "ProcKey": "otto.tunnel.model",
    "Role": "otto.tunnel.model",
    "Tunnel": "otto.tunnel.model",
    "TunnelHop": "otto.tunnel.model",
    "make_tunnel_id": "otto.tunnel.model",
    "ParsedSentinel": "otto.tunnel.sentinel",
    "SENTINEL_PREFIX": "otto.tunnel.sentinel",
    "encode_sentinel": "otto.tunnel.sentinel",
    "parse_sentinel": "otto.tunnel.sentinel",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.tunnel's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CARRIERS",
    "DEFAULT_CARRIER",
    "SENTINEL_PREFIX",
    "AddedTunnel",
    "Direction",
    "DiscoveredTunnel",
    "DryRunPlan",
    "ParsedSentinel",
    "ProcKey",
    "RemovedReport",
    "Role",
    "Tunnel",
    "TunnelCarrier",
    "TunnelCheckReport",
    "TunnelDiscovery",
    "TunnelHop",
    "TunnelNotMeasuredError",
    "add_tunnel",
    "build_carrier",
    "check_tunnel",
    "discover_tunnels",
    "encode_sentinel",
    "make_tunnel_id",
    "parse_sentinel",
    "register_carrier",
    "remove_all_tunnels",
    "remove_tunnel",
]
