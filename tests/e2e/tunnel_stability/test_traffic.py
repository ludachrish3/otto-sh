"""Survivor traffic soak (spec §3, test_traffic): one long-lived tunnel keeps
carrying sequence-numbered datagrams while neighbors churn. Delivery ratio
>= 0.95 (asserting 100% over UDP is flake-by-design); sent and received counts
are both recorded from what each side actually emitted."""

import asyncio
import shlex
import socket
import sys
import uuid

import pytest

from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, remove_tunnel
from tests._fixtures.tunnel_bed import (
    random_outfile,
    remove_remote_file,
    resolved_ip,
    send_udp,
    wait_for_udp_bound,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_SURVIVOR,
    PORTS_TRAFFIC_NEIGHBORS,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    soak_timeout,
    stream_listener_script,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=90.0, base=180.0)),
]

_DELIVERY_FLOOR = 0.95
_SEND_INTERVAL = 0.1
# The churn window alone can be seconds on a fast bed — tens of datagrams,
# where one drop moves the ratio by whole points and the 0.95 floor is
# nearly binary. The sender therefore keeps running AFTER churn until a
# real sample exists; the post-churn leg doubles as the survivor-liveness
# soak. (Review finding, 2026-07-17.)
_MIN_SENT = max(100, 50 * SOAK_CYCLES)


@pytest.mark.asyncio
async def test_survivor_traffic_during_neighbor_churn(tunnel_lab, reap_tunnels) -> None:
    tomato = tunnel_lab.hosts[EXIT]
    outfile = random_outfile()
    run_tag = uuid.uuid4().hex[:8]

    # Long-lived tunnel + long-lived listener on its far end.
    survivor = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORT_SURVIVOR, protocol="udp"
    )
    reap_tunnels.append(survivor.tunnel.id)
    listen_budget = soak_timeout(per_cycle=90.0, base=120.0)
    script = stream_listener_script(PORT_SURVIVOR, outfile, timeout=listen_budget)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    await tomato.exec(cmd, timeout=15, log=LogMode.QUIET)
    await wait_for_udp_bound(tomato, "127.0.0.1", PORT_SURVIVOR)

    sent: list[str] = []
    stop = asyncio.Event()
    enough_sent = asyncio.Event()  # set once len(sent) >= _MIN_SENT (ASYNC110: event, not poll)

    async def sender() -> None:
        # One persistent client socket for the whole streaming session (a real
        # long-lived UDP client keeps its local port fixed). tunnel_bed's
        # send_udp() intentionally opens a fresh ephemeral-port socket per
        # call — fine for a one-off probe, but in a tight loop it makes the
        # ingress socat's `fork` UDP listener treat EVERY datagram as a
        # distinct new peer and fork a child for it. At this sender's volume
        # (hundreds of datagrams) that forked-child population outpaces a
        # single reap pass in remove_tunnel's post-kill verify (found live,
        # 2026-07-17: reproducible `report.survivors` non-empty on the
        # survivor's own removal, self-heals only on a second reap attempt).
        # A single reused socket keeps this a genuine one-peer session, as
        # the module's own "one long-lived tunnel" traffic model intends.
        ip = resolved_ip("carrot")
        n = 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not stop.is_set():
                payload = f"{run_tag}-{n}"
                sock.sendto(payload.encode(), (ip, PORT_SURVIVOR))
                sent.append(payload)
                if len(sent) >= _MIN_SENT:
                    enough_sent.set()
                n += 1
                await asyncio.sleep(_SEND_INTERVAL)

    sender_task = asyncio.create_task(sender())
    try:
        # Neighbor churn around the survivor, status checkpoint each cycle.
        for cycle in range(SOAK_CYCLES):
            await add_remove_cycle(
                tunnel_lab,
                reap_tunnels,
                [(INGRESS, None), (EXIT, None)],
                port=PORTS_TRAFFIC_NEIGHBORS[0],
                procs=4,
                label=f"cycle {cycle}: ",
            )
            await add_remove_cycle(
                tunnel_lab,
                reap_tunnels,
                [(INGRESS, None), (EXIT, None), (RELAY, None)],
                port=PORTS_TRAFFIC_NEIGHBORS[1],
                procs=6,
                label=f"cycle {cycle}: ",
            )
            await assert_discovered(tunnel_lab, survivor.tunnel.id, procs=4)

        # Post-churn top-up: keep the survivor carrying traffic until the
        # sample is statistically meaningful (see _MIN_SENT). This leg IS
        # part of the soak — the survivor staying alive after churn ended.
        # Guarded against a dead sender: race the event against the sender
        # task itself so a sender that died (raised) surfaces its exception
        # here instead of hanging forever on an event nothing will ever set.
        wait_task = asyncio.ensure_future(enough_sent.wait())
        try:
            await asyncio.wait({sender_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
            if sender_task.done():
                await sender_task  # surfaces the sender's exception instead of hanging
        finally:
            wait_task.cancel()
    finally:
        stop.set()
        await sender_task

    try:
        # Post-churn liveness probe: a DISTINCT payload must still arrive.
        final_probe = f"{run_tag}-final"
        received_text = ""
        for _attempt in range(5):
            send_udp(resolved_ip("carrot"), PORT_SURVIVOR, final_probe.encode())
            await asyncio.sleep(1.0)
            result = await tomato.exec(
                f"cat {shlex.quote(outfile)} 2>/dev/null || true", timeout=15, log=LogMode.QUIET
            )
            received_text = result.value or ""
            if final_probe in received_text:
                break
        assert final_probe in received_text, "survivor tunnel dead after churn ended"

        for _ in range(5):  # STOP is UDP too; send it redundantly
            send_udp(resolved_ip("carrot"), PORT_SURVIVOR, b"STOP")
            await asyncio.sleep(0.2)

        received = {
            line for line in received_text.splitlines() if line.startswith(f"{run_tag}-")
        } - {final_probe}
        assert len(sent) >= _MIN_SENT, f"sender window too thin: {len(sent)} < {_MIN_SENT}"
        ratio = len(received & set(sent)) / len(sent)
        sys.stdout.write(
            f"traffic soak: sent={len(sent)} received={len(received & set(sent))} "
            f"ratio={ratio:.3f}\n"
        )
        assert ratio >= _DELIVERY_FLOOR, (
            f"delivery ratio {ratio:.3f} < {_DELIVERY_FLOOR} ({len(received)}/{len(sent)})"
        )
    finally:
        await remove_remote_file(tomato, outfile)

    report = await remove_tunnel(tunnel_lab, survivor.tunnel.id)
    assert report.survivors == []
    reap_tunnels.remove(survivor.tunnel.id)
