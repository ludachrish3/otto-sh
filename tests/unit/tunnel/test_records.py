"""DiscoveredTunnel -> TunnelRecord adapter (spec 2026-07-16 §2)."""

from typing import ClassVar

import pytest

from otto.tunnel.discovery import DiscoveredTunnel
from otto.tunnel.model import Tunnel, TunnelHop
from otto.tunnel.records import TunnelScanFailedError, tunnel_record

TUNNEL = Tunnel(
    protocol="udp",
    service_port=15001,
    path=(TunnelHop(host="edge-gw"), TunnelHop(host="core-01"), TunnelHop(host="db-01")),
)


def _discovered(missing: frozenset = frozenset(), uncertain: bool = False) -> DiscoveredTunnel:
    expected = TUNNEL.expected_processes()
    return DiscoveredTunnel(
        tunnel=TUNNEL,
        present=expected - missing,
        missing=set(missing),
        age_seconds=120,
        uncertain=uncertain,
    )


def test_ok_tunnel_maps_ok_with_ordered_hops() -> None:
    rec = tunnel_record(_discovered())
    assert rec.status == "ok"
    assert rec.hops == ["edge-gw", "core-01", "db-01"]
    assert rec.carriers_present == 6
    assert rec.carriers_expected == 6
    assert rec.age_seconds == 120.0
    assert rec.id == TUNNEL.id
    assert rec.protocol == "udp"
    assert rec.service_port == 15001


def test_missing_carriers_map_degraded() -> None:
    some = frozenset(list(TUNNEL.expected_processes())[:2])
    rec = tunnel_record(_discovered(missing=some))
    assert rec.status == "degraded"
    assert rec.carriers_present == 4


def test_uncertain_wins_over_degraded() -> None:
    some = frozenset(list(TUNNEL.expected_processes())[:2])
    assert tunnel_record(_discovered(missing=some, uncertain=True)).status == "uncertain"


def test_all_unreachable_scan_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-unreachable is a FAILED scan, not an empty lab — it must raise so
    the collector keeps the last known set (guard what you emit)."""
    import asyncio

    from otto.tunnel import records as mod
    from otto.tunnel.discovery import TunnelDiscovery

    class _Host:
        has_bash: ClassVar = True
        id: ClassVar = "h1"

    class _Lab:
        hosts: ClassVar = {"h1": _Host()}

    async def fake_discover(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=["h1"])

    monkeypatch.setattr(mod, "discover_tunnels", fake_discover)
    with pytest.raises(TunnelScanFailedError):
        asyncio.run(mod.discover_tunnel_records(_Lab()))


def test_no_scannable_hosts_is_a_successful_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from otto.tunnel import records as mod
    from otto.tunnel.discovery import TunnelDiscovery

    class _Lab:
        hosts: ClassVar[dict] = {}

    async def fake_discover(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=[])

    monkeypatch.setattr(mod, "discover_tunnels", fake_discover)
    assert asyncio.run(mod.discover_tunnel_records(_Lab())) == []
