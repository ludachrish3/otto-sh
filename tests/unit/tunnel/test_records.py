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


def test_a_dry_run_of_a_lab_with_no_scannable_host_still_returns_empty() -> None:
    """The vacuous case, driven through the REAL discovery rather than a stub.

    `discover_tunnel_records` raises on a dry run because nothing was scanned —
    except here, where there was nothing to scan. Discovery only ever visits
    `has_bash` hosts, so "no otto tunnels" follows from lab data and the
    monitor gets the same complete `[]` a real pass produces, rather than a
    keep-the-last-set raise it would have to log a warning for.
    """
    import asyncio

    from otto.tunnel import records as mod
    from tests.conftest import active_context

    class _Host:
        has_bash: ClassVar = False
        id: ClassVar = "h1"

    class _Lab:
        hosts: ClassVar = {"h1": _Host()}

    with active_context(dry_run=True):
        assert asyncio.run(mod.discover_tunnel_records(_Lab())) == []


def test_a_dry_run_raises_rather_than_blanking_the_monitor_s_tunnel_layer() -> None:
    """The monitor's contract survives the third discovery state.

    ``discover_tunnel_records`` "raises rather than returning ``[]``" so the
    collector's tunnel loop keeps its last known set
    (``docs/architecture/subsystems/network.md``). A ``not_measured``
    discovery is empty with an EMPTY ``unreachable`` list, so the
    all-unreachable count above can never fire for it — without its own arm it
    would return ``[]`` and blank the topology overlay.
    """
    import asyncio

    from otto.tunnel import records as mod
    from otto.tunnel.discovery import TunnelDiscovery, TunnelNotMeasuredError

    class _Host:
        has_bash: ClassVar = True
        id: ClassVar = "h1"

    class _Lab:
        hosts: ClassVar = {"h1": _Host()}

    async def fake_discover(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=[], not_measured=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "discover_tunnels", fake_discover)
        with pytest.raises(TunnelNotMeasuredError, match="was not issued"):
            asyncio.run(mod.discover_tunnel_records(_Lab()))

    # POSITIVE CONTROL: the same lab shape, measured and genuinely empty, is
    # still the successful `[]` this must not be confused with.
    async def measured_empty(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=[])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "discover_tunnels", measured_empty)
        assert asyncio.run(mod.discover_tunnel_records(_Lab())) == []


def test_the_collector_keeps_its_last_set_when_the_scan_was_never_issued() -> None:
    """The raise has to be one the collector's tunnel loop actually catches."""
    import asyncio

    from otto.models.monitor import TunnelRecord
    from otto.monitor.collector import MetricCollector
    from otto.tunnel.discovery import TunnelNotMeasuredError

    known = [TunnelRecord.model_construct(id="tun-x", hops=["a"], status="ok")]

    async def refuse() -> list[TunnelRecord]:
        raise TunnelNotMeasuredError("this is a dry run")

    collector = MetricCollector(hosts=[], tunnel_source=refuse)
    collector._tunnels = known
    asyncio.run(collector._tunnel_pass())
    assert collector._tunnels == known
