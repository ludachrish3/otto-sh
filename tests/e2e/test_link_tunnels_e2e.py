"""Live-bed e2e tests for ``otto link`` dynamic tunnels (sub-project #2, Task 11).

Drives the real ``otto.link`` library API (``add_link`` / ``discover_dynamic_links``
/ ``remove_link``) against the three-VM veggies bed, mirroring
``tests/integration/host/test_hop_integration.py``'s host topology and
``tests/e2e/host/test_login_proxy_e2e.py``'s fail-loud-on-host-down probe.

Topology (see ``tests/_fixtures/lab_data/tech1/lab.json``)
------------------------------------------------------------
- carrot_seed (test1, 10.10.200.11, iface eth1=192.168.1.11) — tunnel ingress
- tomato_seed (test2, 10.10.200.12, iface eth1=192.168.1.12) — tunnel exit
- pepper_seed (test3, 10.10.200.13, no declared iface)       — relay dest

Each dynamic tunnel is a two-socat bridge: an ingress socat on the first
``--hosts`` entry accepts client traffic and ships it over a TCP carrier to an
egress socat on the last entry, which delivers to the destination (the exit
host itself, or a third ``--dest`` host for the relay case). Both socats bind
all interfaces, so a datagram sent to an ingress host's *management* ip
(``10.10.200.x``) is accepted even though the resolved ``LinkEndpoint`` ip
(used only for id/display and for where the egress side delivers) is the
host's single declared interface (``192.168.1.x``). See
``docs/superpowers/specs/2026-07-08-link-cli-tunnels-design.md`` §7.2-7.3.

Guaranteed teardown
--------------------
``reap_tunnels`` tracks every dynamic link id this module creates and reaps
each individually via ``remove_link`` in a fixture teardown block, which runs
even when the test body raises. It deliberately does NOT call the
owner-agnostic ``remove_all_links`` — the peer VMs are a bed SHARED with other
engineers' concurrent sessions, and a blind sweep would also reap any tunnel
someone else has live. ``remove_link`` on an id that is already gone (or was
never fully created) is a documented no-op (empty ``RemovedReport``), so
unconditional per-id cleanup is safe.

Known blocking issue for a live run (found while writing this module, NOT
introduced by it)
-------------------------------------------------------------------------
``otto.link.manage._alloc_carrier_port`` / ``_require_tools`` and
``otto.link.discovery.discover_observations`` all read ``result.output`` off
the value returned by ``UnixHost.oneshot(...)``. That call returns
``otto.result.CommandResult``, whose output field is ``.value``, not
``.output`` (``.output`` exists only on the unrelated ``ShellResult``) — see
``src/otto/result.py``. Every real ``UnixHost.oneshot`` path (confirmed for
the ssh branch in ``src/otto/host/session.py``) returns a bare
``CommandResult`` with no ``.output`` attribute, so ``add_link``,
``remove_link``, ``remove_all_links``, and ``discover_dynamic_links`` will all
raise ``AttributeError`` the first time they run against a real host. This
module is written correctly against the real ``CommandResult`` contract
(it reads ``.value``), but the production code it exercises has this
pre-existing bug and will need it fixed (``result.output`` -> ``result.value``
at ``src/otto/link/manage.py:75,138`` and ``src/otto/link/discovery.py:98``)
before any test below can pass live.

xdist / dev-VM rules
---------------------
Pinned to a single ``xdist_group`` so the fixed 3-host topology never runs
concurrently with itself, and intended as a single pass per the dev-VM load
rule (no heavy parallel load, never power VMs). Host-down is a loud
``RuntimeError`` naming the unreachable VM — never a ``pytest.skip``.
"""

import asyncio
import contextlib
import random
import shlex
import socket
import time
import uuid

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.link import add_link, discover_dynamic_links, remove_link
from otto.logger.mode import LogMode
from tests._fixtures.labdata import host_data

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
]

# Fixed 3-VM topology (see module docstring) -- host ids per the current
# element+board naming scheme used across every other e2e/integration test.
_INGRESS = "carrot_seed"  # test1, 10.10.200.11
_EXIT = "tomato_seed"  # test2, 10.10.200.12
_RELAY_DEST = "pepper_seed"  # test3, 10.10.200.13

_SSH_PORT = 22
_REACHABLE_TIMEOUT = 10
_LISTEN_TIMEOUT = 20.0
_POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Host / lab construction
# ---------------------------------------------------------------------------


def _build_host(ne: str) -> UnixHost:
    """Build a real ``UnixHost`` from the veggies lab data (mirrors
    ``tests/integration/host/test_hop_integration.py::_build_host``)."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        term="ssh",
        transfer="scp",
        log=LogMode.QUIET,
    )


async def _assert_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) on a down VM -- never skip (dev-VM rule).

    Mirrors ``tests/e2e/host/test_login_proxy_e2e.py::_assert_sshd_reachable``:
    a bounded raw TCP connect to :22 isolates "bed down" from any later
    otto-level failure, so a down VM never masquerades as a link/discovery bug.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, _SSH_PORT), timeout=_REACHABLE_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element}_seed ({ip}) unreachable on :{_SSH_PORT} -- bed down? "
            f"(otto link e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


@pytest_asyncio.fixture
async def link_lab():
    """Real ``Lab`` over the 3-VM veggies bed (carrot/tomato/pepper = test1/2/3).

    Every ``otto.link`` function takes ``lab`` explicitly and never reads
    ambient ``OttoContext``, so (unlike ``test_hop_integration.py``'s
    module-scoped context fixture) this owns a private ``Lab`` per test.
    """
    for ne in ("carrot", "tomato", "pepper"):
        await _assert_reachable(ne, host_data(ne)["ip"])

    lab = Lab(name="link_tunnels_e2e")
    for ne in ("carrot", "tomato", "pepper"):
        lab.add_host(_build_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


@pytest_asyncio.fixture
async def reap_tunnels(link_lab):
    """Guaranteed teardown: reap every tunnel this test created, even on failure.

    See the module docstring for why this reaps by tracked id via
    ``remove_link`` rather than sweeping with ``remove_all_links``.
    """
    created: list[str] = []
    yield created
    for link_id in created:
        with contextlib.suppress(Exception):
            await remove_link(link_lab, link_id)


# ---------------------------------------------------------------------------
# UDP listener / sender helpers
# ---------------------------------------------------------------------------


def _random_service_port() -> int:
    """A wide, randomized high port for the tunnel's service port.

    Collisions are not pre-checked (unlike the internal carrier port, which
    ``add_link`` allocates itself) -- acceptable for a single-pass e2e run.
    """
    return random.randint(30000, 40000)  # noqa: S311 -- test port pick, not security-sensitive


def _listener_script(port: int, outfile: str, timeout: float) -> str:
    """Python source (run remotely) that waits for one UDP datagram and
    records ``"<source-ip> <payload>"`` to *outfile*."""
    return (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"s.bind(('0.0.0.0', {port}))\n"
        f"s.settimeout({timeout})\n"
        "data, addr = s.recvfrom(65535)\n"
        f"open({outfile!r}, 'w').write(addr[0] + ' ' + data.decode('utf-8', 'replace'))\n"
    )


async def _spawn_udp_listener(host: UnixHost, port: int, outfile: str, timeout: float) -> None:
    """Start a detached UDP listener on *host* that records one datagram to *outfile*.

    Mirrors ``otto.link.socat.launch_command``'s own detachment idiom
    (``setsid`` + stdio-to-``/dev/null`` + trailing ``&``) so the listener
    outlives this ``oneshot`` call.
    """
    script = _listener_script(port, outfile, timeout)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    await host.oneshot(cmd, timeout=15, log=LogMode.QUIET)


async def _wait_for_listener_output(
    host: UnixHost,
    outfile: str,
    timeout: float = _LISTEN_TIMEOUT,
    interval: float = _POLL_INTERVAL,
) -> str:
    """Poll *outfile* on *host* until it holds ``"<source-ip> <payload>"``."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = await host.oneshot(
            f"cat {shlex.quote(outfile)} 2>/dev/null || true", timeout=15, log=LogMode.QUIET
        )
        last = (result.value or "").strip()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(
        f"host {host.id!r}: timed out after {timeout}s waiting for a datagram in "
        f"{outfile!r}; last read: {last!r}"
    )


async def _rm(host: UnixHost, path: str) -> None:
    """Best-effort remote cleanup of a scratch file."""
    await host.oneshot(f"rm -f {shlex.quote(path)}", timeout=15, log=LogMode.QUIET)


def _send_udp(ip: str, port: int, payload: bytes) -> None:
    """Send one UDP datagram from this (dev-VM) process to *ip*:*port*.

    Sent to the ingress host's management ip -- the ingress socat's
    ``UDP4-LISTEN``/``TCP4-LISTEN`` binds all interfaces (see module
    docstring), so the 10.10.200.x management address is reachable even
    though the resolved ``LinkEndpoint`` ip is the declared 192.168.1.x iface.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(payload, (ip, port))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udp_tunnel_delivers_and_lists_and_removes(link_lab, reap_tunnels) -> None:
    """``add_link`` test1->test2 (UDP) delivers, is discoverable, and
    ``remove_link`` reaps it."""
    exit_host = link_lab.hosts[_EXIT]
    port = _random_service_port()
    outfile = f"/tmp/otto_link_e2e_{uuid.uuid4().hex}.out"
    payload = f"otto-link-e2e-{uuid.uuid4().hex}".encode()

    try:
        # Listener must be up before the datagram is sent -- UDP delivery to a
        # not-yet-bound port is simply dropped, never queued.
        await _spawn_udp_listener(exit_host, port, outfile, timeout=_LISTEN_TIMEOUT)

        tunnel = await add_link(
            link_lab, [(_INGRESS, None), (_EXIT, None)], port=port, protocol="udp"
        )
        reap_tunnels.append(tunnel.link.id)

        # --- delivery: a datagram to test1's ingress port arrives on test2 ---
        _send_udp(host_data("carrot")["ip"], port, payload)
        received = await _wait_for_listener_output(exit_host, outfile)
        _src_ip, _, recv_payload = received.partition(" ")
        assert recv_payload == payload.decode(), (
            f"expected payload {payload.decode()!r} delivered to {exit_host.id!r}, got {received!r}"
        )

        # --- list: the tunnel is discoverable by its id ---
        discovered_ids = {link.id for link in await discover_dynamic_links(link_lab)}
        assert tunnel.link.id in discovered_ids, (
            f"expected {tunnel.link.id!r} in discover_dynamic_links, got {discovered_ids!r}"
        )

        # --- remove: reap by id, then confirm it's gone ---
        report = await remove_link(link_lab, tunnel.link.id)
        assert tunnel.link.id in report.removed_ids, (
            f"remove_link did not report {tunnel.link.id!r} as removed: {report!r}"
        )
        remaining_ids = {link.id for link in await discover_dynamic_links(link_lab)}
        assert tunnel.link.id not in remaining_ids, (
            f"{tunnel.link.id!r} still discoverable after remove_link: {remaining_ids!r}"
        )
    finally:
        await _rm(exit_host, outfile)


@pytest.mark.asyncio
async def test_relay_dest_appears_sourced_from_exit(link_lab, reap_tunnels) -> None:
    """``add_link`` test1->test2 ``--dest`` test3: test3 sees the datagram
    sourced from test2's ip, not a loopback/relay artifact (spec §7.3)."""
    relay_host = link_lab.hosts[_RELAY_DEST]
    port = _random_service_port()
    outfile = f"/tmp/otto_link_e2e_{uuid.uuid4().hex}.out"
    payload = f"otto-link-e2e-relay-{uuid.uuid4().hex}".encode()

    try:
        await _spawn_udp_listener(relay_host, port, outfile, timeout=_LISTEN_TIMEOUT)

        tunnel = await add_link(
            link_lab,
            [(_INGRESS, None), (_EXIT, None)],
            port=port,
            protocol="udp",
            dest=(_RELAY_DEST, None),
        )
        reap_tunnels.append(tunnel.link.id)

        _send_udp(host_data("carrot")["ip"], port, payload)
        received = await _wait_for_listener_output(relay_host, outfile)
        src_ip, _, recv_payload = received.partition(" ")

        assert recv_payload == payload.decode(), (
            f"expected payload {payload.decode()!r} relayed to {relay_host.id!r}, got {received!r}"
        )
        exit_ip = host_data("tomato")["ip"]
        assert src_ip == exit_ip, (
            f"expected the relayed datagram on {relay_host.id!r} to appear sourced "
            f"from {_EXIT!r} ({exit_ip}), got source {src_ip!r}"
        )
    finally:
        await _rm(relay_host, outfile)


@pytest.mark.asyncio
async def test_non_otto_socat_is_excluded(link_lab) -> None:
    """A plain, untagged socat process must never appear in ``discover_dynamic_links``.

    No ``reap_tunnels`` here: the rogue process is never otto-tagged, so
    ``remove_link`` could never reap it anyway -- its cleanup is the ``pkill``
    in the ``finally`` block below.
    """
    exit_host = link_lab.hosts[_EXIT]
    port = _random_service_port()

    before_ids = {link.id for link in await discover_dynamic_links(link_lab)}

    # A bare socat listener with NO otto sentinel (no `exec -a` argv[0] tag).
    cmd = f"setsid socat TCP4-LISTEN:{port},fork,reuseaddr - </dev/null >/dev/null 2>&1 &"
    await exit_host.oneshot(cmd, timeout=15, log=LogMode.QUIET)
    try:
        links = await discover_dynamic_links(link_lab)
        after_ids = {link.id for link in links}
        assert after_ids == before_ids, (
            f"a plain untagged socat must not surface as a discovered tunnel; "
            f"new entries: {after_ids - before_ids!r}"
        )
        assert not any(port in (link.a.port, link.b.port) for link in links), (
            f"discover_dynamic_links must not reference the rogue socat's port {port}"
        )
    finally:
        await exit_host.oneshot(
            f"pkill -f {shlex.quote(f'TCP4-LISTEN:{port},')} || true", timeout=15, log=LogMode.QUIET
        )
