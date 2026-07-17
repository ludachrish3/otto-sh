"""Adapter: tunnel discovery -> monitor ``TunnelRecord`` rows.

Lives tunnel-side so the monitor package never imports ``otto.tunnel`` —
the collector consumes these through an injected callable composed in
``otto.cli.monitor`` (spec 2026-07-16 §2).
"""

from typing import TYPE_CHECKING

from ..models.monitor import TunnelRecord
from .discovery import DiscoveredTunnel, discover_tunnels

if TYPE_CHECKING:
    from ..config.lab import Lab


class TunnelScanFailedError(RuntimeError):
    """A discovery pass that reached no host at all.

    Raised instead of returning ``[]`` so a dead scan can never masquerade as
    an empty lab and blank the topology's tunnel layer.
    """


def tunnel_record(discovered: DiscoveredTunnel) -> TunnelRecord:
    """Map one discovery result to its wire record.

    Status reads :attr:`DiscoveredTunnel.health`, never parsed from the
    human ``status`` string.
    """
    tunnel = discovered.tunnel
    return TunnelRecord(
        id=tunnel.id,
        protocol=tunnel.protocol,
        service_port=tunnel.service_port,
        hops=[hop.host for hop in tunnel.path],
        status=discovered.health,
        carriers_present=len(discovered.present),
        carriers_expected=len(tunnel.expected_processes()),
        age_seconds=float(discovered.age_seconds),
    )


async def discover_tunnel_records(lab: "Lab") -> list[TunnelRecord]:
    """One full-lab scan as sorted wire records; raises on a dead scan."""
    discovery = await discover_tunnels(lab)
    scannable = [h for h in lab.hosts.values() if getattr(h, "has_bash", False)]
    if scannable and len(discovery.unreachable) == len(scannable):
        raise TunnelScanFailedError(
            f"tunnel scan reached none of the lab's {len(scannable)} scannable hosts"
        )
    return sorted((tunnel_record(d) for d in discovery.tunnels), key=lambda r: r.id)
