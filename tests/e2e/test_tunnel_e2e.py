"""Live-bed e2e tests for ``otto tunnel`` (sub-project #2b, Task 12).

Drives the real ``otto.tunnel`` library API (``add_tunnel`` / ``discover_tunnels``
/ ``remove_tunnel``) against the three-VM veggies bed, mirroring the retired
``tests/e2e/test_link_tunnels_e2e.py`` (see ``git show
main:tests/e2e/test_link_tunnels_e2e.py``): host construction via
``host_data``/``make_host``, fail-loud-on-host-down, reap-by-tracked-id
teardown, and a single ``xdist_group`` so this fixed 3-host topology never
runs concurrently with itself.

Topology (see ``tests/_fixtures/lab_data/tech1/lab.json``)
------------------------------------------------------------
- carrot_seed (test1, 10.10.200.11)
- tomato_seed (test2, 10.10.200.12)
- pepper_seed (test3, 10.10.200.13)

``lab.json`` declares an ``eth2``/``192.168.1.x`` data-plane interface for
carrot/tomato/pepper, and the bed provisions those addresses for real
(Vagrantfile: a dedicated second NIC on the ``otto-dataplane`` internal
network — originally stacked onto ``eth1`` 2026-07-16, moved to its own
``eth2`` netdev the same day so the data plane is impairable; the mgmt
netdev refusal in ``otto.link`` is per-device). The library-API tests below
still run over the management ips:
``make_host``/``_build_host`` never wires the lab-data ``interfaces`` dict
onto the constructed ``UnixHost`` (its ``interfaces`` field defaults to
empty), so ``otto.tunnel.manage._resolve_one`` falls back to the host's own
management ip (``10.10.200.x``), which the dev VM shares a subnet with --
datagrams are sent directly from this process, exactly like the retired
link e2e's ``_send_udp``. The CLI cycle test (test 5) is the one that loads
the lab data verbatim and exercises the declared data plane.

Bind semantics changed from the retired ``otto.link`` era
-----------------------------------------------------------
Unlike the old two-socat link bridge (which bound the ingress socat to
``0.0.0.0``), ``otto.tunnel``'s ingress/egress socats bind the endpoint's
*specific* resolved data-plane ip (spec §6.3 loop-hazard fix; see
``src/otto/tunnel/socat.py::ingress_socat_args``) rather than the wildcard
address. A datagram aimed at that exact ip (the management ip here) is still
delivered normally.

Guaranteed teardown
--------------------
Each test tracks every tunnel id it creates in ``reap_tunnels`` and reaps it
individually via ``remove_tunnel`` in a fixture teardown block that runs even
when the test body raises. A module-scoped, autouse, *synchronous* fixture
(``_final_leftover_sweep``, using its own ``asyncio.run`` since it fires after
every per-test event loop has already closed) does a final ``ps`` sweep for
``otto-tunnel:`` tagged processes on all three peers after every test in this
module has run, and FAILS (never skips) if anything survived.

xdist / dev-VM rules
---------------------
Pinned to a single ``xdist_group`` (never runs concurrently with itself) and
intended as a single pass per the dev-VM load rule (no heavy parallel load,
never power VMs, never SIGTERM a wedged live-bed run at a tight timeout).
Host-down is a loud ``RuntimeError`` naming the unreachable VM -- never a
``pytest.skip``.
"""

import asyncio
import contextlib
import json
import random
import re
import shlex
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.config.repo import DockerCompose, DockerImage, DockerSettings, Repo
from otto.docker.compose import compose_down, compose_up
from otto.host.daemon import kill_command
from otto.host.docker_host import DockerContainerHost
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import (
    DISCOVERY_PS_COMMAND,
    discover_observations,
    parse_process_discovery,
)
from tests._fixtures.labdata import host_data, make_host
from tests.e2e._otto_subprocess import REPO1, run_otto

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
]

_INGRESS = "carrot_seed"  # test1, 10.10.200.11
_EXIT = "tomato_seed"  # test2, 10.10.200.12
_RELAY_DEST = "pepper_seed"  # test3, 10.10.200.13

_SSH_PORT = 22
_REACHABLE_TIMEOUT = 10
_LISTEN_TIMEOUT = 20.0
_POLL_INTERVAL = 1.0

_PORT_DIRECT = 15000
_PORT_MULTIHOP = 15001
_PORT_CONTAINER = 15002
_PORT_DEGRADE = 15003
_PORT_CLI_CYCLE = 15004
_PORT_FOREIGN = 45003

REPO2_DIR = Path(__file__).resolve().parents[1] / "repo2"
OLDOS_DOCKER_DIR = REPO2_DIR / "docker" / "oldos"
OLDOS_COMPOSE_PROJECT = "otto-tunnel-e2e-oldos"


# ---------------------------------------------------------------------------
# Host / lab construction
# ---------------------------------------------------------------------------


def _build_host(ne: str) -> UnixHost:
    """Build a real, docker-capable ``UnixHost`` from the veggies lab data."""
    return make_host(ne, term="ssh", transfer="scp", log=LogMode.QUIET, docker_capable=True)


def _resolved_ip(ne: str) -> str:
    """The ip ``otto.tunnel.manage._resolve_one`` picks for these test-built hosts.

    These ``UnixHost`` objects never carry a populated ``interfaces`` dict (see
    the module docstring), so ``_resolve_one`` always falls back to the host's
    own management ip -- regardless of what ``lab.json`` declares.
    """
    return host_data(ne)["ip"]


async def _assert_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) on a down VM -- never skip (dev-VM rule)."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, _SSH_PORT), timeout=_REACHABLE_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element}_seed ({ip}) unreachable on :{_SSH_PORT} -- bed down? "
            f"(otto tunnel e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


@pytest_asyncio.fixture
async def tunnel_lab():
    """Real ``Lab`` over the 3-VM veggies bed (carrot/tomato/pepper = test1/2/3)."""
    for ne in ("carrot", "tomato", "pepper"):
        await _assert_reachable(ne, host_data(ne)["ip"])

    lab = Lab(name="tunnel_e2e")
    for ne in ("carrot", "tomato", "pepper"):
        lab.add_host(_build_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


@pytest_asyncio.fixture
async def reap_tunnels(tunnel_lab):
    """Guaranteed teardown: reap every tunnel this test created, even on failure."""
    created: list[str] = []
    yield created
    for tunnel_id in created:
        with contextlib.suppress(Exception):
            await remove_tunnel(tunnel_lab, tunnel_id)


async def _assert_no_leftover_tunnel_processes() -> None:
    r"""Scan all three peers for ``otto-tunnel:`` tagged processes; raise if any remain.

    Must decode each line through :func:`parse_process_discovery` (the same
    strict sentinel parser production discovery uses) rather than treat any
    non-empty ``DISCOVERY_PS_COMMAND`` output as a leak: that command's own
    ``ps | \grep -a ' otto-tunnel:'`` pipeline always shows up in its own `ps`
    snapshot (the grep argv literally contains the search string), which is a
    self-match, not a real tagged tunnel process. ``parse_process_discovery``
    requires the full 11-segment sentinel and correctly ignores that noise.
    """
    hosts = [_build_host(ne) for ne in ("carrot", "tomato", "pepper")]
    try:
        leaks: list[str] = []
        for host in hosts:
            result = await host.exec(DISCOVERY_PS_COMMAND, timeout=15, log=LogMode.QUIET)
            observed = parse_process_discovery(result.value or "")
            if observed:
                detail = ", ".join(f"pid={o.pid} tunnel={o.parsed.tunnel.id}" for o in observed)
                leaks.append(f"{host.id}: {detail}")
    finally:
        await asyncio.gather(*(h.close() for h in hosts), return_exceptions=True)
    assert not leaks, "otto-tunnel processes leaked past test module teardown:\n" + "\n".join(leaks)


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep():
    """Module-final bed hygiene: FAIL (never skip) if any tagged process survived.

    Plain sync fixture (not ``pytest_asyncio``) running its own throwaway
    ``asyncio.run`` -- it fires strictly after every per-test event loop in
    this module has already closed, so it needs no ``loop_scope`` coordination.
    """
    yield
    asyncio.run(_assert_no_leftover_tunnel_processes())


# ---------------------------------------------------------------------------
# UDP send / receive helpers
# ---------------------------------------------------------------------------


def _listener_script(port: int, outfile: str, timeout: float) -> str:
    """Python source (run remotely) that waits for one UDP datagram and
    records ``"<source-ip> <payload>"`` to *outfile*.

    Binds ``127.0.0.1`` specifically, never the wildcard ``0.0.0.0``: every
    delivery target in this module is exactly ``127.0.0.1`` (the egress
    processes' own hardcoded loopback delivery), and the SAME host always
    also runs the opposite direction's ingress, which binds *its own*
    resolved management ip specifically on the very same port. A wildcard
    bind here would collide with that already-bound specific address.
    """
    return (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        f"s.settimeout({timeout})\n"
        "data, addr = s.recvfrom(65535)\n"
        f"open({outfile!r}, 'w').write(addr[0] + ' ' + data.decode('utf-8', 'replace'))\n"
    )


_BIND_CONFIRM_TIMEOUT = 5.0


async def _wait_for_udp_bound(
    host: UnixHost, ip: str, port: int, timeout: float = _BIND_CONFIRM_TIMEOUT
) -> None:
    """Poll until a UDP socket is bound to *ip*:*port* on *host*.

    Closes a real launch race (found live): ``host.exec`` returns as soon
    as the remote shell has *accepted* the backgrounded ``setsid python3 ...
    &`` command, which is well before the python3 interpreter has actually
    started and called ``bind()``. A UDP datagram sent in that gap arrives at
    a not-yet-bound port and is simply dropped (never queued) -- so every
    listener spawn must be followed by this confirmation, not just trusted to
    already be up by the time a sender fires.
    """
    deadline = time.monotonic() + timeout
    needle = f"{ip}:{port}"
    while time.monotonic() < deadline:
        result = await host.exec(
            "ss -H -u -a -n 2>/dev/null || true", timeout=15, log=LogMode.QUIET
        )
        if needle in (result.value or ""):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"host {host.id!r}: no UDP listener bound to {needle} within {timeout}s")


async def _spawn_udp_listener(host: UnixHost, port: int, outfile: str, timeout: float) -> None:
    """Start a detached UDP listener on *host* and confirm it is bound before returning.

    See :func:`_wait_for_udp_bound` for why the confirmation is required.
    """
    script = _listener_script(port, outfile, timeout)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    await host.exec(cmd, timeout=15, log=LogMode.QUIET)
    await _wait_for_udp_bound(host, "127.0.0.1", port)


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
        result = await host.exec(
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
    await host.exec(f"rm -f {shlex.quote(path)}", timeout=15, log=LogMode.QUIET)


def _send_udp(ip: str, port: int, payload: bytes) -> None:
    """Send one UDP datagram from this (dev-VM) process to *ip*:*port*.

    Valid because the resolved ingress ip is each host's real, dev-VM-reachable
    management ip for every host built in this module (see the module
    docstring) -- the ingress socat's specific bind still accepts a datagram
    addressed to exactly that ip.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(payload, (ip, port))


def _random_outfile() -> str:
    return f"/tmp/otto_tunnel_e2e_{uuid.uuid4().hex}.out"


def _foreign_socat_port() -> int:
    return _PORT_FOREIGN + random.randint(0, 999)  # noqa: S311 -- test port pick, not security-sensitive


# ---------------------------------------------------------------------------
# Test 1: direct a<->b UDP bidirectional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_tunnel_bidirectional(tunnel_lab, reap_tunnels) -> None:
    """``add_tunnel`` carrot<->tomato (UDP): both directions deliver, ``list``-level
    discovery shows ``ok`` with all 4 processes, and ``remove`` reaps cleanly."""
    carrot = tunnel_lab.hosts[_INGRESS]
    tomato = tunnel_lab.hosts[_EXIT]
    port = _PORT_DIRECT
    fwd_outfile = _random_outfile()
    rev_outfile = _random_outfile()
    fwd_payload = f"otto-tunnel-e2e-fwd-{uuid.uuid4().hex}".encode()
    rev_payload = f"otto-tunnel-e2e-rev-{uuid.uuid4().hex}".encode()

    try:
        added = await add_tunnel(
            tunnel_lab, [(_INGRESS, None), (_EXIT, None)], port=port, protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)

        # --- FWD: sender near carrot -> listener on tomato loopback ---
        await _spawn_udp_listener(tomato, port, fwd_outfile, timeout=_LISTEN_TIMEOUT)
        _send_udp(_resolved_ip("carrot"), port, fwd_payload)
        fwd_received = await _wait_for_listener_output(tomato, fwd_outfile)
        _src_ip, _, fwd_recv_payload = fwd_received.partition(" ")
        assert fwd_recv_payload == fwd_payload.decode(), (
            f"expected FWD payload {fwd_payload.decode()!r} on tomato, got {fwd_received!r}"
        )

        # --- REV: b-side sender on tomato -> listener on carrot loopback ---
        await _spawn_udp_listener(carrot, port, rev_outfile, timeout=_LISTEN_TIMEOUT)
        _send_udp(_resolved_ip("tomato"), port, rev_payload)
        rev_received = await _wait_for_listener_output(carrot, rev_outfile)
        _src_ip, _, rev_recv_payload = rev_received.partition(" ")
        assert rev_recv_payload == rev_payload.decode(), (
            f"expected REV payload {rev_payload.decode()!r} on carrot, got {rev_received!r}"
        )

        # --- list-level discovery: ok, 4 processes ---
        discovery = await discover_tunnels(tunnel_lab)
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"tunnel {added.tunnel.id!r} not in discover_tunnels"
        assert found.status == "ok", f"expected status 'ok', got {found.status!r}"
        assert len(found.present) == 4, f"expected 4 processes, got {len(found.present)}"

        # --- remove: reap, verify clean ---
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert added.tunnel.id in report.removed_ids
        assert report.survivors == [], f"survivors after remove: {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)

        post = await discover_tunnels(tunnel_lab)
        assert not any(d.tunnel.id == added.tunnel.id for d in post.tunnels), (
            f"{added.tunnel.id!r} still discoverable after remove_tunnel"
        )
    finally:
        await _rm(tomato, fwd_outfile)
        await _rm(carrot, rev_outfile)


# ---------------------------------------------------------------------------
# Test 2: multi-hop a->c->b relay + VIA rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multihop_relay_and_via(tunnel_lab, reap_tunnels) -> None:
    """``add_tunnel`` carrot,tomato,pepper (UDP): relay processes on tomato,
    discovery status ``ok`` with 6 processes, and ``_fmt_via`` names tomato."""
    from otto.cli.tunnel import _fmt_via

    pepper = tunnel_lab.hosts[_RELAY_DEST]
    port = _PORT_MULTIHOP
    outfile = _random_outfile()
    payload = f"otto-tunnel-e2e-multihop-{uuid.uuid4().hex}".encode()

    try:
        added = await add_tunnel(
            tunnel_lab,
            [(_INGRESS, None), (_EXIT, None), (_RELAY_DEST, None)],
            port=port,
            protocol="udp",
        )
        reap_tunnels.append(added.tunnel.id)

        await _spawn_udp_listener(pepper, port, outfile, timeout=_LISTEN_TIMEOUT)
        _send_udp(_resolved_ip("carrot"), port, payload)
        received = await _wait_for_listener_output(pepper, outfile)
        _src_ip, _, recv_payload = received.partition(" ")
        assert recv_payload == payload.decode(), (
            f"expected payload {payload.decode()!r} relayed to pepper, got {received!r}"
        )

        # --- relay processes present on tomato: 2 tagged (FWD relay + REV relay) ---
        discovery = await discover_tunnels(tunnel_lab)
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"tunnel {added.tunnel.id!r} not in discover_tunnels"
        assert found.status == "ok", f"expected status 'ok', got {found.status!r}"
        assert len(found.present) == 6, f"expected 6 processes, got {len(found.present)}"
        tomato_procs = [key for key in found.present if key[0] == _EXIT]
        assert len(tomato_procs) == 2, f"expected 2 relay processes on tomato, got {tomato_procs!r}"

        # --- VIA rendering names the relay host ---
        assert _EXIT in _fmt_via(added.tunnel), (
            f"expected {_EXIT!r} in _fmt_via output: {_fmt_via(added.tunnel)!r}"
        )

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"survivors after remove: {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
    finally:
        await _rm(pepper, outfile)


# ---------------------------------------------------------------------------
# Test 3: container endpoint (centos:7 oldos, no systemd -> setsid fallback)
# ---------------------------------------------------------------------------


def _oldos_repo() -> Repo:
    """A ``Repo`` over ``tests/repo2`` with docker settings replaced wholesale
    to point at the oldos fixture -- the existing repo2 compose entry (used by
    ``tests/e2e/docker/``) is never touched or referenced."""
    repo = Repo(sut_dir=REPO2_DIR)
    repo.docker_settings = DockerSettings(
        registry_url=repo.docker_settings.registry_url,
        images=(
            DockerImage(
                name="oldos",
                dockerfile=OLDOS_DOCKER_DIR / "Dockerfile",
                context=OLDOS_DOCKER_DIR,
            ),
        ),
        composes=(
            DockerCompose(
                path=OLDOS_DOCKER_DIR / "compose.yml",
                default_host=None,
                services=("oldos",),
            ),
        ),
    )
    return repo


async def _spawn_container_listener(
    container: DockerContainerHost, port: int, outfile: str
) -> None:
    """Detached socat-only UDP listener inside the container (its python is 2.7).

    ``docker exec`` children reparent to the container's PID 1 once the
    wrapping exec session exits, so ``setsid``-backgrounding here survives
    exactly like the tunnel's own launch does (spec #2b §13).

    Binds ``127.0.0.1`` explicitly (the FWD-egress's hardcoded loopback
    delivery target) rather than the wildcard default -- the SAME container
    also runs this tunnel's REV-ingress, which binds *its own* resolved
    (docker-bridge) ip specifically on this very port; a wildcard bind here
    risks the same overlap the module docstring describes for the host-level
    listeners.

    Removes any stale *outfile* first -- the container fixture path is a
    fixed name (idempotent re-runs against an already-up stack must not read
    back a previous run's leftover content as a false pass).

    Confirms the bind before returning (same launch race as
    :func:`_wait_for_udp_bound`, but centos:7 has no ``ss`` -- only ``socat``
    was installed -- so this parses ``/proc/net/udp`` instead, which needs no
    extra tooling).
    """
    await container.exec(f"rm -f {shlex.quote(outfile)}", timeout=15, log=LogMode.QUIET)
    cmd = (
        f"setsid socat -u UDP4-RECVFROM:{port},bind=127.0.0.1 CREATE:{shlex.quote(outfile)} "
        "</dev/null >/dev/null 2>&1 &"
    )
    await container.exec(cmd, timeout=15, log=LogMode.QUIET)
    await _wait_for_container_udp_bound(container, "127.0.0.1", port)


def _proc_net_udp_needle(ip: str, port: int) -> str:
    """Build the ``/proc/net/udp`` ``local_address`` needle for *ip*:*port*.

    The kernel encodes each address as 4 hex-encoded, byte-reversed octets
    (e.g. ``127.0.0.1`` -> ``0100007F``) joined with the hex port (big-endian,
    uppercase, zero-padded to 4 digits) -- e.g. ``0100007F:3A98`` for
    ``127.0.0.1:15000``.
    """
    octets = [int(o) for o in ip.split(".")]
    hex_ip = "".join(f"{o:02X}" for o in reversed(octets))
    return f"{hex_ip}:{port:04X}"


async def _wait_for_container_udp_bound(
    container: DockerContainerHost, ip: str, port: int, timeout: float = _BIND_CONFIRM_TIMEOUT
) -> None:
    """Poll ``/proc/net/udp`` inside the container until *ip*:*port* is bound."""
    deadline = time.monotonic() + timeout
    needle = _proc_net_udp_needle(ip, port)
    while time.monotonic() < deadline:
        result = await container.exec(
            "cat /proc/net/udp 2>/dev/null || true", timeout=15, log=LogMode.QUIET
        )
        if needle in (result.value or ""):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"container {container.id!r}: no UDP listener bound to {ip}:{port} within {timeout}s"
    )


async def _wait_for_container_file(
    container: DockerContainerHost,
    path: str,
    timeout: float = _LISTEN_TIMEOUT,
    interval: float = _POLL_INTERVAL,
) -> str:
    """Poll a raw file inside the container (socat ``CREATE:`` writes bytes verbatim,
    no source-ip prefix like the python3 listener script)."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = await container.exec(
            f"cat {shlex.quote(path)} 2>/dev/null || true", timeout=15, log=LogMode.QUIET
        )
        last = (result.value or "").strip()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(
        f"container {container.id!r}: timed out after {timeout}s waiting for a datagram in "
        f"{path!r}; last read: {last!r}"
    )


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_container_endpoint_oldos(tunnel_lab, reap_tunnels) -> None:
    """``add_tunnel`` tomato,carrot,<oldos container> (parent carrot): datagram
    from tomato reaches an in-container socat-only listener; discovery sees
    container-origin processes; remove reaps cleanly.

    Doubles as the docker-endpoint proof and the old-OS/setsid proof: the
    centos:7 container has no systemd, so every tunnel process launched
    inside it exercises the ``setsid`` fallback in
    ``otto.host.daemon.launch_command`` rather than ``systemd-run --user``.
    """
    repo = _oldos_repo()
    hosts = await compose_up(
        repo, tunnel_lab, on=_INGRESS, project_name=OLDOS_COMPOSE_PROJECT, build=True
    )
    try:
        container = hosts["oldos"]
        assert isinstance(container, DockerContainerHost)
        port = _PORT_CONTAINER
        outfile = "/tmp/otto-e2e-recv"  # inside the disposable container only
        payload = f"otto-tunnel-e2e-container-{uuid.uuid4().hex}".encode()

        added = await add_tunnel(
            tunnel_lab,
            [(_EXIT, None), (_INGRESS, None), (container.id, None)],
            port=port,
            protocol="udp",
        )
        reap_tunnels.append(added.tunnel.id)

        await _spawn_container_listener(container, port, outfile)
        _send_udp(_resolved_ip("tomato"), port, payload)
        received = await _wait_for_container_file(container, outfile)
        assert received == payload.decode(), (
            f"expected payload {payload.decode()!r} in-container, got {received!r}"
        )

        # --- discovery sees the container-origin processes ---
        discovery = await discover_tunnels(tunnel_lab)
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"tunnel {added.tunnel.id!r} not in discover_tunnels"
        assert found.status == "ok", f"expected status 'ok', got {found.status!r}"
        container_procs = [key for key in found.present if key[0] == container.id]
        assert len(container_procs) == 2, (
            f"expected 2 container-origin processes, got {container_procs!r}"
        )

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"survivors after remove: {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
    finally:
        await compose_down(repo, tunnel_lab, on=_INGRESS, project_name=OLDOS_COMPOSE_PROJECT)


# ---------------------------------------------------------------------------
# Test 4: foreign-socat exclusion + out-of-band kill degrades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_socat_excluded_and_outofband_kill_degrades(tunnel_lab, reap_tunnels) -> None:
    """A plain untagged socat never surfaces in discovery; killing one hop's
    tagged processes out-of-band flips the tunnel's status to ``degraded (...)``."""
    carrot = tunnel_lab.hosts[_INGRESS]
    tomato = tunnel_lab.hosts[_EXIT]
    port = _PORT_DEGRADE
    foreign_port = _foreign_socat_port()

    before = await discover_tunnels(tunnel_lab)
    before_ids = {d.tunnel.id for d in before.tunnels}

    # A bare socat listener with NO otto sentinel (no `exec -a` argv[0] tag).
    foreign_cmd = (
        f"setsid socat TCP4-LISTEN:{foreign_port},fork,reuseaddr - </dev/null >/dev/null 2>&1 &"
    )
    await tomato.exec(foreign_cmd, timeout=15, log=LogMode.QUIET)
    try:
        mid = await discover_tunnels(tunnel_lab)
        assert {d.tunnel.id for d in mid.tunnels} == before_ids, (
            "a plain untagged socat must not surface as a discovered tunnel"
        )
        assert not any(d.tunnel.service_port == foreign_port for d in mid.tunnels), (
            f"discover_tunnels must not reference the rogue socat's port {foreign_port}"
        )

        added = await add_tunnel(
            tunnel_lab, [(_INGRESS, None), (_EXIT, None)], port=port, protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)

        # --- out-of-band kill: reap one hop's tagged processes directly ---
        observations, _unreachable = await discover_observations(tunnel_lab)
        killed_pids = [
            obs.pid
            for origin, obs in observations
            if obs.parsed.tunnel.id == added.tunnel.id and origin == _INGRESS
        ]
        assert killed_pids, f"expected tagged processes on {_INGRESS!r} before kill"
        result = await carrot.exec(kill_command(killed_pids), timeout=15, log=LogMode.QUIET)
        assert result.is_ok, f"out-of-band kill on {_INGRESS!r} failed: {result.value!r}"

        degraded = await discover_tunnels(tunnel_lab)
        found = next((d for d in degraded.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"tunnel {added.tunnel.id!r} vanished after partial kill"
        assert found.status.startswith("degraded ("), (
            f"expected a degraded status after out-of-band kill, got {found.status!r}"
        )

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"survivors after remove: {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
    finally:
        await tomato.exec(
            f"pkill -f {shlex.quote(f'TCP4-LISTEN:{foreign_port},')} || true",
            timeout=15,
            log=LogMode.QUIET,
        )


# ---------------------------------------------------------------------------
# Test 5: full CLI cycle on a lab that DECLARES docker containers (issue #139)
# ---------------------------------------------------------------------------


def _cli_cycle_sut_dir(tmp_path: Path) -> Path:
    """Scaffold a sut dir for the CLI cycle: real veggies lab + declared containers.

    Two deliberate properties:

    - The host entries are the tech1 fixture's, verbatim — INCLUDING the
      declared ``eth2``/``192.168.1.x`` data-plane interfaces. Unlike the
      library-API tests above (whose ``make_host`` never wires ``interfaces``,
      so they bind management ips), the CLI path loads lab data verbatim and
      resolves each endpoint to its declared data-plane ip — so this cycle
      also guards the bed contract that ``192.168.1.x`` is provisioned on the
      peers (Vagrantfile: the dedicated ``eth2`` data-plane NIC); an
      unprovisioned bed fails loudly at the post-add verify.
    - The repo is named ``repo1`` and declares the same ``api`` compose as
      ``tests/repo1``, so lab load registers container-host placeholders —
      the exact issue #139 trigger. Every tunnel command must leave them
      alone: probe liveness quietly, never compose the stack.
    """
    sut = tmp_path / "cli_cycle_sut"
    (sut / ".otto").mkdir(parents=True)
    lab_dir = sut / "lab_data"
    lab_dir.mkdir()
    hosts = [host_data(ne) for ne in ("carrot", "tomato")]
    (lab_dir / "lab.json").write_text(json.dumps({"hosts": hosts, "links": []}))
    (sut / ".otto" / "settings.toml").write_text(
        f'name = "repo1"\n'
        f'version = "1.0.0"\n'
        f'lab_data_type = "json"\n'
        f'labs = ["{lab_dir}"]\n'
        f"\n"
        f"[lab]\n"
        f'backend = "json"\n'
        f"\n"
        f"[[docker.composes]]\n"
        f'path = "{REPO1 / "docker" / "compose.yml"}"\n'
        f'services = ["api"]\n'
        f'default_host = "tomato_seed"\n'
    )
    return sut


def _run_cycle(argv: list[str], sut: Path) -> "subprocess.CompletedProcess[str]":
    return run_otto(argv, sut_dirs=sut, lab="veggies", timeout=180)


def _assert_docker_free(proc: "subprocess.CompletedProcess[str]", step: str) -> None:
    """The issue #139 pin: no tunnel command may run (or even mention) docker."""
    blob = (proc.stdout + proc.stderr).lower()
    for needle in ("docker", "compose"):
        assert needle not in blob, (
            f"tunnel {step}: CLI output mentions {needle!r} — tunnel commands must "
            f"not touch docker (issue #139):\n{proc.stdout}\n{proc.stderr}"
        )


@pytest.mark.timeout(900)
def test_cli_cycle_add_list_remove_list_docker_free(tmp_path: Path) -> None:
    """add → list → remove → list through the real ``otto`` CLI, docker-free.

    The scaffolded lab declares container hosts (placeholders register at lab
    load), so all four invocations exercise the issue #139 surface: pre-fix,
    even a bare ``otto tunnel list`` auto-started compose stacks on the peers
    and flooded the console with docker I/O.
    """
    for ne in ("carrot", "tomato"):
        asyncio.run(_assert_reachable(ne, host_data(ne)["ip"]))
    sut = _cli_cycle_sut_dir(tmp_path)
    cleanup_id = ""
    try:
        add = _run_cycle(
            [
                "tunnel",
                "add",
                "--hosts",
                "carrot_seed,tomato_seed",
                "--port",
                str(_PORT_CLI_CYCLE),
                "--protocol",
                "udp",
            ],
            sut,
        )
        assert add.returncode == 0, f"add failed:\n{add.stdout}\n{add.stderr}"
        # Extract the id BEFORE the docker-free pin: if that assertion fires,
        # the finally below can still reap the tunnel the add just built.
        match = re.search(r"added (tun-[0-9a-f]{12}-\d+)", add.stdout)
        assert match, f"no tunnel id in add output:\n{add.stdout}"
        tid = cleanup_id = match.group(1)
        _assert_docker_free(add, "add")

        listed = _run_cycle(["tunnel", "list"], sut)
        assert listed.returncode == 0, f"list failed:\n{listed.stdout}\n{listed.stderr}"
        _assert_docker_free(listed, "list")
        assert tid in listed.stdout, f"{tid!r} missing from list:\n{listed.stdout}"
        for header in ("ID", "ENDPOINTS", "VIA", "PORT", "PROTO", "AGE", "STATUS"):
            assert header in listed.stdout, f"missing table header {header!r}:\n{listed.stdout}"
        row = next(line for line in listed.stdout.splitlines() if tid in line)
        assert row.rstrip().endswith(" ok"), f"expected status 'ok' in row: {row!r}"

        removed = _run_cycle(["tunnel", "remove", tid], sut)
        assert removed.returncode == 0, f"remove failed:\n{removed.stdout}\n{removed.stderr}"
        _assert_docker_free(removed, "remove")
        assert tid in removed.stdout, f"remove did not name {tid!r}:\n{removed.stdout}"
        cleanup_id = ""  # removed cleanly; disarm the safety net

        relisted = _run_cycle(["tunnel", "list"], sut)
        assert relisted.returncode == 0, f"re-list failed:\n{relisted.stdout}\n{relisted.stderr}"
        _assert_docker_free(relisted, "second list")
        assert tid not in relisted.stdout, f"{tid!r} survived remove:\n{relisted.stdout}"
    finally:
        if cleanup_id:
            with contextlib.suppress(Exception):
                _run_cycle(["tunnel", "remove", cleanup_id], sut)
