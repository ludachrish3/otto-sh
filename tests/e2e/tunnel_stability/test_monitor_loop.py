"""Live Collector tunnel loop over the bed (spec §5): convergence after each
churn settle (one deterministic _tunnel_pass = one tick), CLI/monitor seam
parity, and last-known-state under a SIGSTOP wedge. Docker-free by design."""

import asyncio
import contextlib
import time

import pytest

from otto.config.lab import Lab
from otto.host.options import SshOptions
from otto.logger.mode import LogMode
from otto.monitor.collector import MetricCollector
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import _TUNNEL_HOST_TIMEOUT
from otto.tunnel.records import discover_tunnel_records
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import build_bed_host
from tests.e2e.tunnel_stability._harness import (
    ARM_SECONDS,
    EXIT,
    INGRESS,
    PORTS_MONITOR_CHURN,
    SOAK_CYCLES,
    arm_auto_cont,
    assert_sshd_responsive,
    cancel_auto_cont,
    soak_timeout,
    sshd_listener_pid,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=300.0)),
]

_TICK_BUDGET = _TUNNEL_HOST_TIMEOUT + 15.0


def _spy_collector(lab) -> tuple[MetricCollector, list[dict]]:
    """Composition-site wiring (cli/monitor.py:225) with spy sinks: real
    discovery over the real bed, no web server."""
    published: list[dict] = []
    c = MetricCollector(hosts=[], tunnel_source=lambda: discover_tunnel_records(lab))
    c.session_id = "tunnel-soak"
    c._publish = published.append  # type: ignore[method-assign]
    return c, published


async def _timed_pass(collector: MetricCollector) -> float:
    started = time.monotonic()
    await collector._tunnel_pass()
    return time.monotonic() - started


@pytest.mark.asyncio
async def test_loop_converges_with_churn_and_seam_parity(tunnel_lab, reap_tunnels) -> None:
    """After each churn settle, ONE pass converges the record set to the live
    set; each record's status equals the DiscoveredTunnel.health the CLI
    reads for the same bed state (seam parity, asserted not assumed)."""
    collector, published = _spy_collector(tunnel_lab)
    elapsed = await _timed_pass(collector)
    assert elapsed < _TICK_BUDGET
    assert collector.get_tunnel_records() == [], "bed not clean at start"

    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORTS_MONITOR_CHURN[0], protocol="udp")
        reap_tunnels.append(added.tunnel.id)
        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"cycle {cycle}: tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id], f"cycle {cycle}: {records!r}"

        discovery = await discover_tunnels(tunnel_lab)
        found = next(d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id)
        assert records[0].status == found.health, (
            f"cycle {cycle}: wire status {records[0].status!r} != CLI health {found.health!r}"
        )
        assert records[0].hops == [INGRESS, EXIT], f"cycle {cycle}: hops {records[0].hops!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == []
        reap_tunnels.remove(added.tunnel.id)
        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"cycle {cycle}: settle-to-empty tick took {elapsed:.1f}s"
        assert collector.get_tunnel_records() == [], f"cycle {cycle}: not converged to empty"
    assert published, "collector never published a fragment"


@pytest.mark.asyncio
async def test_loop_holds_last_known_under_wedge_then_reconverges(tunnel_lab, reap_tunnels) -> None:
    """SIGSTOP tomato's sshd listener mid-monitoring: ticks keep completing
    within budget, the tunnel is HELD (as 'uncertain', never blanked), and the
    set reconverges to 'ok' after CONT."""
    control = tunnel_lab.hosts[EXIT]
    tomato_ip = host_data("tomato")["ip"]

    # The collector scans its OWN lab (fresh host objects) so that closing a
    # host forces the next scan through a brand-new connection — a pooled
    # pre-wedge connection would never feel the wedge.
    monitor_lab = Lab(name="monitor_wedge")
    for ne in ("carrot", "tomato"):
        monitor_lab.add_host(build_bed_host(ne, ssh_options=SshOptions(connect_timeout=5)))
    collector, _published = _spy_collector(monitor_lab)

    added = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORTS_MONITOR_CHURN[1], protocol="udp"
    )
    reap_tunnels.append(added.tunnel.id)
    pid = await sshd_listener_pid(control)
    stopped = False
    succeeded = False
    try:
        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"initial tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id]
        assert records[0].status == "ok"

        # Arm auto-recovery BEFORE stopping: a failed teardown cannot wedge the bed.
        arm_tag = await arm_auto_cont(control, pid)
        stop_result = await control.exec(f"sudo -n kill -STOP {pid}", timeout=15, log=LogMode.QUIET)
        assert stop_result.is_ok
        stopped = True
        await monitor_lab.hosts[EXIT].close()  # next scan opens a fresh, wedged connection

        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"wedged tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id], "wedge blanked the set"
        assert records[0].status == "uncertain", f"held status {records[0].status!r}"

        cont = await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        assert cont.is_ok
        stopped = False
        await assert_sshd_responsive(tomato_ip)
        await monitor_lab.hosts[EXIT].close()  # reconnect cleanly post-recovery

        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"reconverge tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert records, f"did not reconverge: {records!r}"
        assert records[0].status == "ok", f"did not reconverge: {records!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == []
        reap_tunnels.remove(added.tunnel.id)
        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"settle-to-empty tick took {elapsed:.1f}s"
        assert collector.get_tunnel_records() == []
        succeeded = True
    finally:
        if stopped:  # test body failed mid-wedge: recover NOW, loudly if we can't
            with contextlib.suppress(Exception):
                await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        await asyncio.gather(
            *(h.close() for h in monitor_lab.hosts.values()), return_exceptions=True
        )
        try:
            await assert_sshd_responsive(tomato_ip)
        except Exception as exc:
            raise AssertionError(
                f"tomato sshd NOT responsive after wedge test — auto-CONT fires within "
                f"{ARM_SECONDS}s; else 'sudo kill -CONT {pid}' on test2: {exc!r}"
            ) from exc
        if succeeded:
            # Recovery is fully proven (both the explicit CONT above AND this
            # probe succeeded, AND every assertion in the wedge/recovery body
            # passed) — the armed safety net has no job left to do. Cancel it
            # so a later `make stability-tunnel COUNT=N` iteration's own STOP
            # window can never be un-stuck by THIS run's orphaned sleeper.
            # On any failure path (probe failed, CONT failed, or an earlier
            # assertion failed) `succeeded` stays False and the timer is left
            # armed — it IS the safety net for exactly that case.
            await cancel_auto_cont(control, arm_tag)
