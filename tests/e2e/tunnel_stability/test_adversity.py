"""Adversity soaks (spec §3, test_adversity): control-plane correctness while
the data path is impaired, and the degrade->reap->re-add cycle under repetition.

The impaired path is the bed's REAL data plane: a dedicated eth2 NIC carrying
192.168.1.x on each peer (spec decision 6 — the mgmt netdev is refused by the
impair placement guard, so the data plane lives on its own device). Nothing is
created or deleted on the peers here; the fixture only asserts the bed
contract holds and fails loud with redeploy instructions if not."""

import asyncio
import contextlib
import shlex
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.host.daemon import kill_command
from otto.host.interface import Interface
from otto.link import ImpairmentParams, Link, LinkEndpoint, Selector, impair_link, repair_link
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import discover_observations
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    LISTEN_TIMEOUT,
    VEGGIES,
    assert_reachable,
    build_bed_host,
    cli_sut_dir,
    random_outfile,
    remove_remote_file,
    run_tunnel_cli,
    spawn_udp_listener,
    wait_for_listener_output,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_DEGRADE,
    PORT_IMPAIRED,
    SOAK_CYCLES,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=240.0)),
]

_DP_DEV = "eth2"
_DP_SUBNET = "192.168.1.0/24"
_CARROT_DP_IP = "192.168.1.11"
_TOMATO_DP_IP = "192.168.1.12"
_HOST_CMD_TIMEOUT = 30
_PROBE_COUNT = 30  # ≥1 of 30 must arrive; at 10% loss/direction P(all lost) ≈ 1e-30


async def _assert_dataplane_provisioned(host, ip: str) -> None:
    """Fail LOUD (with redeploy instructions) if the bed predates the eth2 NIC."""
    result = await host.exec(
        f"ip -o addr show dev {_DP_DEV} 2>/dev/null || true",
        timeout=_HOST_CMD_TIMEOUT,
        log=LogMode.QUIET,
    )
    assert ip in (result.value or ""), (
        f"{host.id}: {_DP_DEV} does not carry {ip} — the bed predates the dedicated "
        f"data-plane NIC; run 'vagrant reload test1 test2 test3' (halt+up, not a bare "
        f"provision) with the current Vagrantfile"
    )


@pytest_asyncio.fixture
async def dataplane_lab():
    """2-host lab whose carrot/tomato carry the declared eth2 data plane."""
    for ne in VEGGIES:
        await assert_reachable(ne, host_data(ne)["ip"])
    lab = Lab(name="tunnel_adversity")
    carrot = build_bed_host(
        "carrot", interfaces={_DP_DEV: Interface(ip=_CARROT_DP_IP, subnet=_DP_SUBNET)}
    )
    tomato = build_bed_host(
        "tomato", interfaces={_DP_DEV: Interface(ip=_TOMATO_DP_IP, subnet=_DP_SUBNET)}
    )
    lab.add_host(carrot)
    lab.add_host(tomato)
    await _assert_dataplane_provisioned(carrot, _CARROT_DP_IP)
    await _assert_dataplane_provisioned(tomato, _TOMATO_DP_IP)

    lab.links.append(
        Link(
            a=LinkEndpoint(host=INGRESS, interface=_DP_DEV, ip=_CARROT_DP_IP),
            b=LinkEndpoint(host=EXIT, interface=_DP_DEV, ip=_TOMATO_DP_IP),
            name="soak-seg",
        )
    )
    try:
        yield lab
    finally:
        with contextlib.suppress(Exception):
            await repair_link(lab, "soak-seg")
        # The netdev is permanent bed infrastructure — prove repair left it
        # pristine on BOTH peers, but never let a dirty qdisc leak the host
        # connections: collect first, close, then judge.
        leftovers: list[str] = []
        try:
            for host in (carrot, tomato):
                qdisc = await host.exec(
                    f"tc qdisc show dev {_DP_DEV}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
                )
                if "netem" in (qdisc.value or ""):
                    leftovers.append(f"{host.id}: {qdisc.value!r}")
        finally:
            await asyncio.gather(*(h.close() for h in (carrot, tomato)), return_exceptions=True)
        assert not leftovers, f"lingering netem on {_DP_DEV} after repair: {leftovers}"


async def _send_udp_from(host, ip: str, port: int, payloads: list[str]) -> None:
    """Fire datagrams from *host* (the dev VM has no data-plane address)."""
    lines = "\\n".join(payloads)
    script = (
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f'for line in "{lines}".split("\\n"):\n'
        f"    s.sendto(line.encode(), ({ip!r}, {port}))\n"
        "    time.sleep(0.05)\n"
    )
    await host.exec(
        f"python3 -c {shlex.quote(script)}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
    )


@pytest.mark.asyncio
async def test_churn_under_port_scoped_impairment(dataplane_lab) -> None:
    """delay+loss on ONLY the tunnel's UDP service port, on the eth2 data
    plane (ssh untouched twice over — different netdev AND port-scoped):
    add/verify/remove stays fully correct every cycle; a lossy traffic probe
    (30 datagrams) still delivers at least one."""
    # NOTE: the shared reap_tunnels fixture reaps against tunnel_lab, which
    # this test does not use — requesting it would build three extra host
    # connections for nothing. Track and reap against dataplane_lab manually,
    # with the same guarantees (finally below + module-final sweep).
    created: list[str] = []
    tomato = dataplane_lab.hosts[EXIT]
    chain = [(INGRESS, _DP_DEV), (EXIT, _DP_DEV)]
    sel = Selector(PORT_IMPAIRED, "udp")
    await impair_link(
        dataplane_lab, "soak-seg", ImpairmentParams(delay_ms=80.0, loss_pct=10.0), selector=sel
    )
    try:
        for cycle in range(SOAK_CYCLES):
            added = await add_tunnel(dataplane_lab, chain, port=PORT_IMPAIRED, protocol="udp")
            created.append(added.tunnel.id)
            discovery = await discover_tunnels(dataplane_lab)
            found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
            assert found is not None, (
                f"cycle {cycle}: control plane wrong under impairment: not discovered"
            )
            assert found.status == "ok", (
                f"cycle {cycle}: control plane wrong under impairment: {found.status!r}"
            )
            if cycle in (0, SOAK_CYCLES - 1):
                outfile = random_outfile()
                tag = uuid.uuid4().hex[:8]
                payloads = [f"{tag}-{i}" for i in range(_PROBE_COUNT)]
                try:
                    await spawn_udp_listener(tomato, PORT_IMPAIRED, outfile, timeout=LISTEN_TIMEOUT)
                    # Sender runs ON tomato (the dev VM has no data-plane
                    # address): datagrams enter carrot's eth2 ingress, ride
                    # the tunnel back, and land on tomato's loopback listener.
                    await _send_udp_from(tomato, _CARROT_DP_IP, PORT_IMPAIRED, payloads)
                    received = await wait_for_listener_output(tomato, outfile)
                    assert received.split(" ", 1)[-1].startswith(tag), (
                        f"cycle {cycle}: no probe datagram delivered under 10% loss "
                        f"({_PROBE_COUNT} sent): {received!r}"
                    )
                finally:
                    await remove_remote_file(tomato, outfile)
            report = await remove_tunnel(dataplane_lab, added.tunnel.id)
            assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
            created.remove(added.tunnel.id)
    finally:
        for tunnel_id in created:
            with contextlib.suppress(Exception):
                await remove_tunnel(dataplane_lab, tunnel_id)
        with contextlib.suppress(Exception):
            await repair_link(dataplane_lab, "soak-seg")


@pytest.mark.asyncio
async def test_repeated_degrade_recover(tunnel_lab, reap_tunnels, tmp_path: Path) -> None:
    """CYCLES x (add -> out-of-band kill one hop's pids -> 'degraded (...)' ->
    remove reaps the remainder -> the SAME spec re-adds cleanly). The re-add is
    the loop's next iteration: degradation must leave no residue on the port
    or id.

    Cycle 0 also proves the degradation is PLAINLY VISIBLE to the user through
    the real CLI (recovery contract, spec decision 7) — not just library
    state: the CLI discovers by scanning the same bed hosts, so a
    library-added tunnel is visible to it, and that cross-layer agreement is
    exactly what this asserts.
    """
    carrot = tunnel_lab.hosts[INGRESS]
    chain = [(INGRESS, None), (EXIT, None)]
    cli_sut = cli_sut_dir(tmp_path)
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_DEGRADE, protocol="udp")
        reap_tunnels.append(added.tunnel.id)

        observations, _unreachable = await discover_observations(tunnel_lab)
        pids = [
            obs.pid
            for origin, obs in observations
            if obs.parsed.tunnel.id == added.tunnel.id and origin == INGRESS
        ]
        assert pids, f"cycle {cycle}: no tagged pids on {INGRESS!r} before kill"
        result = await carrot.exec(kill_command(pids), timeout=15, log=LogMode.QUIET)
        assert result.is_ok, f"cycle {cycle}: out-of-band kill failed: {result.value!r}"

        degraded = await discover_tunnels(tunnel_lab)
        found = next((d for d in degraded.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"cycle {cycle}: tunnel vanished after partial kill"
        assert found.status.startswith("degraded ("), (
            f"cycle {cycle}: expected degraded, got {found.status!r}"
        )
        assert found.health == "degraded", f"cycle {cycle}: health {found.health!r}"

        if cycle == 0:
            # Recovery contract (spec decision 7): the degradation must be
            # PLAINLY VISIBLE to the user — assert the real CLI's rendering,
            # not just library state.
            stdout = run_tunnel_cli(cli_sut, "list")
            assert added.tunnel.id in stdout, f"CLI list does not show {added.tunnel.id!r}"
            assert "degraded (" in stdout, f"CLI list does not render degradation: {stdout!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
