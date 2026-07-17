"""Soak knobs, port map, and churn helpers for the tunnel stability suite.

Spec: docs/superpowers/specs/2026-07-16-tunnel-stability-suite-design.md.
Every knob is read HERE and nowhere else.
"""

import asyncio
import contextlib
import os
import uuid

from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel

SOAK_CYCLES = int(os.environ.get("OTTO_TUNNEL_SOAK_CYCLES", "5"))
"""Internal loop depth per soak test. `make stability-tunnel CYCLES=N` sets it."""

ARM_SECONDS = 180  # auto-CONT well past the wedged phase's worst case

INGRESS = "carrot_seed"  # test1, 10.10.200.11
EXIT = "tomato_seed"  # test2, 10.10.200.12
RELAY = "pepper_seed"  # test3, 10.10.200.13

# Port map (spec §1): the 15100-15199 block, disjoint from test_tunnel_e2e's
# 15000-15004. One module per row; never borrow across rows.
PORT_CHURN_DIRECT = 15100
PORT_CHURN_MULTIHOP = 15101
PORT_CHURN_ALTERNATING = 15102
PORTS_REMOVE_ALL = (15103, 15104, 15105)
PORTS_POPULATION = tuple(range(15110, 15120))
PORT_RACING = 15120
PORT_DISCOVERY_CHURN = 15121
PORT_SURVIVOR = 15130
PORTS_TRAFFIC_NEIGHBORS = (15131, 15132)
PORT_IMPAIRED = 15140
PORT_DEGRADE = 15141
PORT_PHANTOM_REAL = 15150
PORT_PHANTOM_CHAIN = 15151
PORT_SIGSTOP = 15152
PORTS_MONITOR_CHURN = (15160, 15161)


def soak_timeout(per_cycle: float, base: float = 120.0) -> float:
    """Per-test ceiling scaled to the knob. Generous by design — the live bed
    is never killed at a tight timeout (dev-VM rule); a genuine wedge still
    fails, just with slack for slow SSH days."""
    return base + per_cycle * SOAK_CYCLES


async def assert_discovered(lab, tunnel_id: str, *, procs: int, label: str = "") -> None:
    """The tunnel is discovered, status ``ok``, with exactly *procs* processes.

    *label* (e.g. ``"cycle 3: "``) prefixes every assertion so a soak loop's
    failure names its iteration."""
    discovery = await discover_tunnels(lab)
    found = next((d for d in discovery.tunnels if d.tunnel.id == tunnel_id), None)
    assert found is not None, f"{label}tunnel {tunnel_id!r} not in discover_tunnels"
    assert found.status == "ok", f"{label}expected status 'ok', got {found.status!r}"
    assert len(found.present) == procs, (
        f"{label}expected {procs} processes, got {len(found.present)}"
    )


async def assert_gone(lab, tunnel_id: str, label: str = "") -> None:
    """*label* (e.g. ``"cycle 3: "``) prefixes the assertion so a soak loop's
    failure names its iteration."""
    discovery = await discover_tunnels(lab)
    assert not any(d.tunnel.id == tunnel_id for d in discovery.tunnels), (
        f"{label}{tunnel_id!r} still discoverable after remove"
    )


async def add_remove_cycle(
    lab, reap, chain, *, port: int, procs: int, protocol: str = "udp", label: str = ""
) -> str:
    """One full verified lifecycle: add → discovered ok → remove clean → gone.

    *label* (e.g. ``"cycle 3: "``) prefixes every assertion so a soak loop's
    failure names its iteration.

    Returns the cycled tunnel id (deterministic per (chain, port) — spec §4 of
    the tunnel design: ``tun-<12hex>-<port>``)."""
    added = await add_tunnel(lab, chain, port=port, protocol=protocol)
    reap.append(added.tunnel.id)
    await assert_discovered(lab, added.tunnel.id, procs=procs, label=label)
    report = await remove_tunnel(lab, added.tunnel.id)
    assert added.tunnel.id in report.removed_ids, (
        f"{label}remove did not report {added.tunnel.id!r} as removed"
    )
    assert report.survivors == [], f"{label}survivors after remove: {report.survivors!r}"
    reap.remove(added.tunnel.id)
    await assert_gone(lab, added.tunnel.id, label=label)
    return added.tunnel.id


# --- SIGSTOP safety-net orchestration (shared by test_health.py's and
# test_monitor_loop.py's SIGSTOP-wedge tests) -------------------------------

_SSHD_PID_CMD = (
    "systemctl show ssh -p MainPID --value 2>/dev/null || systemctl show sshd -p MainPID --value"
)


async def sshd_listener_pid(control) -> int:
    """The sshd LISTENER pid (systemd MainPID) — per-connection children keep
    serving while it is stopped, which is exactly the point: our control
    channel survives, NEW connections hang at the banner. Asserts the MainPID
    is NOT the pid serving this very session (spec §8): stopping that one
    would freeze the control channel we recover through."""
    result = await control.exec(_SSHD_PID_CMD, timeout=15, log=LogMode.QUIET)
    pid = int((result.value or "0").strip().splitlines()[-1])
    assert pid > 0, f"could not resolve sshd MainPID: {result.value!r}"
    session_parent = await control.exec("sh -c 'echo $PPID'", timeout=15, log=LogMode.QUIET)
    assert str(pid) != (session_parent.value or "").strip(), (
        f"MainPID {pid} IS our session's server process — refusing to stop it"
    )
    return pid


async def assert_sshd_responsive(ip: str) -> None:
    """A FRESH connect must produce an SSH banner — connect alone is not
    enough (the kernel backlog completes handshakes for a stopped listener)."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 22), timeout=10)
    try:
        banner = await asyncio.wait_for(reader.readline(), timeout=10)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    assert banner.startswith(b"SSH-"), f"no ssh banner from {ip}: {banner!r}"


async def arm_auto_cont(control, pid: int, *, arm_seconds: int = ARM_SECONDS) -> str:
    """Arm a detached auto-recovery sleeper on *control* that CONTs *pid*
    after *arm_seconds*. Call this BEFORE issuing the STOP: arming first
    means a failed teardown can never wedge the bed.

    Returns a unique per-invocation marker riding along in the armed
    sleeper's own command line (a shell comment — inert to `sh`, but
    `pgrep -f`/`pkill -f` match the full argv). A tunnel/test id is NOT
    unique enough for this: it can be deterministic per invocation, so
    repeated `make stability-tunnel COUNT=N` invocations would tag their
    sleeper identically and a later run could cancel an EARLIER run's
    still-legitimately-armed safety net.
    """
    arm_tag = f"sigstop-arm-{uuid.uuid4().hex}"
    await control.exec(
        f"sudo -n setsid sh -c 'sleep {arm_seconds}; kill -CONT {pid} # {arm_tag}' "
        f"</dev/null >/dev/null 2>&1 &",
        timeout=15,
        log=LogMode.QUIET,
    )
    return arm_tag


async def cancel_auto_cont(control, arm_tag: str) -> None:
    """Best-effort cancel of the sleeper armed by :func:`arm_auto_cont`.

    The CALLER decides *when*: only call this once recovery is fully proven
    (the explicit CONT succeeded AND a fresh responsiveness probe succeeded
    AND every assertion in the wedge/recovery body passed) — the sleeper IS
    the safety net for every other path, so cancelling early would remove
    the net a still-in-flight failure needs. Suppresses all exceptions: a
    failure here must never fail the test.
    """
    with contextlib.suppress(Exception):
        await control.exec(f"sudo -n pkill -f {arm_tag} || true", timeout=15, log=LogMode.QUIET)


def stream_listener_script(port: int, outfile: str, timeout: float) -> str:
    """Python source (run remotely, detached) that appends every datagram's
    payload to *outfile*, one per line, until 'STOP' arrives or *timeout*
    passes. Binds 127.0.0.1 for the same overlap reason as
    ``tunnel_bed.listener_script``."""
    return (
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.settimeout(1.0)\n"
        f"deadline = time.monotonic() + {timeout}\n"
        f"out = open({outfile!r}, 'a', buffering=1)\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        data, _addr = s.recvfrom(65535)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    text = data.decode('utf-8', 'replace')\n"
        "    out.write(text + '\\n')\n"
        "    if text == 'STOP':\n"
        "        break\n"
    )
