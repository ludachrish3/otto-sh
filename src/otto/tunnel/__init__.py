"""``otto.tunnel`` — host-resident bidirectional tunnels (#2b spec).

Library-first: the CLI is a thin consumer of this package's callable API.
Grown task-by-task; final re-export surface lands with the manage layer.
"""

from typing import TYPE_CHECKING

from .carrier import CARRIERS, DEFAULT_CARRIER, TunnelCarrier, build_carrier, register_carrier
from .discovery import (
    DiscoveredTunnel,
    TunnelDiscovery,
    TunnelNotMeasuredError,
    discover_tunnels,
)
from .manage import (
    AddedTunnel,
    DryRunPlan,
    RemovedReport,
    add_tunnel,
    remove_all_tunnels,
    remove_tunnel,
)
from .model import Direction, ProcKey, Role, Tunnel, TunnelHop, make_tunnel_id
from .sentinel import SENTINEL_PREFIX, ParsedSentinel, encode_sentinel, parse_sentinel

# .check pulls in otto.check's fingerprint/probe/render machinery; only
# `otto tunnel check` actually calls it. Every other importer of otto.tunnel
# never touches these names, so re-export them lazily to keep those surfaces
# out of check's import weight — same reasoning, and the same mechanism, as
# otto.link's `_CHECK_NAMES` block.
if TYPE_CHECKING:
    from .check import TunnelCheckReport, check_tunnel

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

_CHECK_NAMES = frozenset({"TunnelCheckReport", "check_tunnel"})


def __getattr__(name: str) -> object:
    """Lazily resolve the ``.check`` orchestration API on first access.

    Keeps ``otto.check``'s fingerprint/probe/render machinery off every
    otto.tunnel importer that only wants the carrier/discovery/manage layer —
    only ``otto tunnel check`` actually needs it.
    """
    if name in _CHECK_NAMES:
        from . import check

        return getattr(check, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
