"""Concurrency soaks (spec §3, test_concurrency): standing population,
racing conflicting adds, discovery under churn."""

import asyncio
import time
from collections import deque

import pytest

from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.manage import AddedTunnel
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_DISCOVERY_CHURN,
    PORT_RACING,
    PORTS_POPULATION,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=90.0)),
]


def _status_is_wellformed(status: str) -> bool:
    """A discovery status is 'ok'/'degraded (…)', optionally '?'-suffixed
    (uncertain). Anything else is an impossible state."""
    bare = status.rstrip("?")
    return bare == "ok" or bare.startswith("degraded (")


@pytest.mark.asyncio
async def test_concurrent_population(tunnel_lab, reap_tunnels) -> None:
    """A standing population of 3 tunnels; each cycle retires the oldest and
    adds a fresh one. After every mutation, discovery reports EXACTLY the live
    set — strict equality, so a dirty bed fails loud rather than hiding."""
    ports = deque(PORTS_POPULATION)
    shapes = [
        [(INGRESS, None), (EXIT, None)],
        [(EXIT, None), (RELAY, None)],
        [(INGRESS, None), (EXIT, None), (RELAY, None)],
    ]
    live: list[tuple[str, int]] = []  # (tunnel_id, port), oldest first

    async def _assert_exact_live_set() -> None:
        discovery = await discover_tunnels(tunnel_lab)
        assert {d.tunnel.id for d in discovery.tunnels} == {tid for tid, _ in live}, (
            f"discovery {sorted(d.tunnel.id for d in discovery.tunnels)!r} != "
            f"live {sorted(tid for tid, _ in live)!r}"
        )

    for i in range(3):
        added = await add_tunnel(
            tunnel_lab, shapes[i % len(shapes)], port=ports.popleft(), protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)
        live.append((added.tunnel.id, added.tunnel.service_port))
        await _assert_exact_live_set()

    for cycle in range(SOAK_CYCLES):
        oldest_id, oldest_port = live.pop(0)
        report = await remove_tunnel(tunnel_lab, oldest_id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(oldest_id)
        ports.append(oldest_port)
        await _assert_exact_live_set()
        added = await add_tunnel(
            tunnel_lab, shapes[cycle % len(shapes)], port=ports.popleft(), protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)
        live.append((added.tunnel.id, added.tunnel.service_port))
        await _assert_exact_live_set()

    for tunnel_id, _port in live:
        report = await remove_tunnel(tunnel_lab, tunnel_id)
        assert report.survivors == []
        reap_tunnels.remove(tunnel_id)


@pytest.mark.asyncio
async def test_racing_conflicting_adds(tunnel_lab, reap_tunnels) -> None:
    """Two simultaneous add_tunnel calls for the SAME port+endpoints: exactly
    one succeeds, the loser raises, zero tagged residue after the winner is
    removed. Spec §3/§8: this asserts the INTENDED contract; the current
    _check_conflicts scan-before-launch TOCTOU may violate it — if so, Task 6
    lands the product fix rather than bending this test."""
    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        results = await asyncio.gather(
            add_tunnel(tunnel_lab, chain, port=PORT_RACING, protocol="udp"),
            add_tunnel(tunnel_lab, chain, port=PORT_RACING, protocol="udp"),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, AddedTunnel)]
        losers = [r for r in results if isinstance(r, BaseException)]
        for winner in winners:  # track before asserting, so reap covers a double-win
            reap_tunnels.append(winner.tunnel.id)
        assert len(winners) == 1, f"cycle {cycle}: exactly-one-wins violated: {results!r}"
        assert len(losers) == 1, f"cycle {cycle}: exactly-one-wins violated: {results!r}"
        await assert_discovered(tunnel_lab, winners[0].tunnel.id, procs=4)
        report = await remove_tunnel(tunnel_lab, winners[0].tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(winners[0].tunnel.id)


@pytest.mark.asyncio
async def test_discovery_under_churn(tunnel_lab, reap_tunnels) -> None:
    """A poller hammers discover_tunnels while add/remove cycles run. Every
    snapshot is internally consistent; a tunnel whose remove returned before
    the snapshot STARTED never reappears."""
    chain = [(INGRESS, None), (EXIT, None)]
    stop = asyncio.Event()
    snapshots: list[tuple[float, set[str]]] = []  # (scan start monotonic, ids)
    removed_at: dict[str, float] = {}  # tunnel id -> monotonic when remove returned

    async def poller() -> None:
        while not stop.is_set():
            started = time.monotonic()
            discovery = await discover_tunnels(tunnel_lab)
            for d in discovery.tunnels:
                assert _status_is_wellformed(d.status), f"impossible status {d.status!r}"
            snapshots.append((started, {d.tunnel.id for d in discovery.tunnels}))

    async def churner() -> None:
        for i in range(SOAK_CYCLES):
            tunnel_id = await add_remove_cycle(
                tunnel_lab,
                reap_tunnels,
                chain,
                port=PORT_DISCOVERY_CHURN,
                procs=4,
                label=f"cycle {i}: ",
            )
            removed_at[tunnel_id] = time.monotonic()

    poll_task = asyncio.create_task(poller())
    try:
        await churner()
    finally:
        stop.set()
        await poll_task

    assert snapshots, "poller never completed a scan"
    # The cycled id is deterministic per (chain, port): every cycle reuses it,
    # so 'gone forever' only holds after the LAST removal.
    final_removals = dict(removed_at)
    for tid, removed_ts in final_removals.items():
        ghosts = [started for started, ids in snapshots if started > removed_ts and tid in ids]
        assert not ghosts, f"{tid!r} reappeared in {len(ghosts)} post-remove snapshot(s)"
