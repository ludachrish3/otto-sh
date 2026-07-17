"""Baseline add/remove churn (spec §3, test_churn): the tunnel lifecycle under
repetition. Traffic is probed on the FIRST and LAST cycle only; process-level
verification runs every cycle (spec's wall-clock/signal trade)."""

import uuid

import pytest

from otto.tunnel import add_tunnel, discover_tunnels, remove_all_tunnels, remove_tunnel
from tests._fixtures.tunnel_bed import (
    LISTEN_TIMEOUT,
    random_outfile,
    remove_remote_file,
    resolved_ip,
    send_udp,
    spawn_udp_listener,
    wait_for_listener_output,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_CHURN_ALTERNATING,
    PORT_CHURN_DIRECT,
    PORT_CHURN_MULTIHOP,
    PORTS_REMOVE_ALL,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    assert_gone,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=60.0)),
]


async def _probe_traffic(lab, *, ingress_ne: str, listen_host_id: str, port: int) -> None:
    """One end-to-end datagram through the tunnel (the tunnel_bed pattern)."""
    listener_host = lab.hosts[listen_host_id]
    outfile = random_outfile()
    payload = f"otto-soak-{uuid.uuid4().hex}".encode()
    try:
        await spawn_udp_listener(listener_host, port, outfile, timeout=LISTEN_TIMEOUT)
        send_udp(resolved_ip(ingress_ne), port, payload)
        received = await wait_for_listener_output(listener_host, outfile)
        _src, _, recv_payload = received.partition(" ")
        assert recv_payload == payload.decode(), (
            f"expected {payload.decode()!r} through the tunnel, got {received!r}"
        )
    finally:
        await remove_remote_file(listener_host, outfile)


@pytest.mark.asyncio
async def test_direct_churn(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x (add 2-hop -> ok/4 procs -> remove clean -> gone); traffic on
    the first and last cycle."""
    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_CHURN_DIRECT, protocol="udp")
        reap_tunnels.append(added.tunnel.id)
        await assert_discovered(tunnel_lab, added.tunnel.id, procs=4, label=f"cycle {cycle}: ")
        if cycle in (0, SOAK_CYCLES - 1):
            await _probe_traffic(
                tunnel_lab, ingress_ne="carrot", listen_host_id=EXIT, port=PORT_CHURN_DIRECT
            )
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
        await assert_gone(tunnel_lab, added.tunnel.id, label=f"cycle {cycle}: ")


@pytest.mark.asyncio
async def test_multihop_churn(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x the 3-hop lifecycle: ok/6 procs, relay pair on the middle host."""
    chain = [(INGRESS, None), (EXIT, None), (RELAY, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_CHURN_MULTIHOP, protocol="udp")
        reap_tunnels.append(added.tunnel.id)
        discovery = await discover_tunnels(tunnel_lab)
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"cycle {cycle}: tunnel not discovered"
        assert found.status == "ok", f"cycle {cycle}: {found.status!r}"
        assert len(found.present) == 6, f"cycle {cycle}: {len(found.present)} procs"
        relay_procs = [key for key in found.present if key[0] == EXIT]
        assert len(relay_procs) == 2, f"cycle {cycle}: relay procs {relay_procs!r}"
        if cycle in (0, SOAK_CYCLES - 1):
            await _probe_traffic(
                tunnel_lab, ingress_ne="carrot", listen_host_id=RELAY, port=PORT_CHURN_MULTIHOP
            )
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
        await assert_gone(tunnel_lab, added.tunnel.id, label=f"cycle {cycle}: ")


@pytest.mark.asyncio
async def test_alternating_shape_frees_the_port(tunnel_lab, reap_tunnels) -> None:
    """2-hop and 3-hop alternately on the SAME service port: each remove must
    genuinely free the port for a differently-shaped successor (spec §3 —
    catches lingering binds a same-shape re-add could mask)."""
    two_hop = [(INGRESS, None), (EXIT, None)]
    three_hop = [(INGRESS, None), (EXIT, None), (RELAY, None)]
    for cycle in range(SOAK_CYCLES):
        chain, procs = (two_hop, 4) if cycle % 2 == 0 else (three_hop, 6)
        await add_remove_cycle(
            tunnel_lab,
            reap_tunnels,
            chain,
            port=PORT_CHURN_ALTERNATING,
            procs=procs,
            label=f"cycle {cycle}: ",
        )


@pytest.mark.asyncio
async def test_remove_all_sweep_cycling(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x (build 3 mixed-shape tunnels -> remove_all_tunnels -> clean):
    the owner-agnostic reap path under repetition."""
    shapes = [
        ([(INGRESS, None), (EXIT, None)], PORTS_REMOVE_ALL[0]),
        ([(EXIT, None), (RELAY, None)], PORTS_REMOVE_ALL[1]),
        ([(INGRESS, None), (EXIT, None), (RELAY, None)], PORTS_REMOVE_ALL[2]),
    ]
    for cycle in range(SOAK_CYCLES):
        ids = []
        for chain, port in shapes:
            added = await add_tunnel(tunnel_lab, chain, port=port, protocol="udp")
            reap_tunnels.append(added.tunnel.id)
            ids.append(added.tunnel.id)
        report = await remove_all_tunnels(tunnel_lab)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        assert set(ids) <= set(report.removed_ids), (
            f"cycle {cycle}: removed {report.removed_ids!r}, expected ⊇ {ids!r}"
        )
        for tunnel_id in ids:
            reap_tunnels.remove(tunnel_id)
        post = await discover_tunnels(tunnel_lab)
        assert post.tunnels == [], f"cycle {cycle}: discovery not empty: {post.tunnels!r}"
