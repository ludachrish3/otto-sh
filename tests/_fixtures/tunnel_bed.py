"""Shared live-bed helpers for tunnel e2e + stability suites.

Extracted verbatim from tests/e2e/test_tunnel_e2e.py (2026-07-16) so the
single-pass e2e module and tests/e2e/tunnel_stability/ share ONE source of
truth for bed hygiene. See that module's docstring for the veggies topology
and the management-ip resolution story.

The CLI-cycle harness (``cli_sut_dir``, ``run_tunnel_cli``) was added
2026-07-17 so stability tests can drive the real ``otto tunnel`` CLI against
the same veggies bed, not just the library API.
"""

import asyncio
import contextlib
import json
import shlex
import socket
import time
import uuid
from pathlib import Path

from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery
from tests._fixtures.labdata import host_data, make_host
from tests.e2e._otto_subprocess import REPO1, run_otto

VEGGIES = ("carrot", "tomato", "pepper")

SSH_PORT = 22
REACHABLE_TIMEOUT = 10
LISTEN_TIMEOUT = 20.0
POLL_INTERVAL = 1.0
BIND_CONFIRM_TIMEOUT = 5.0


def build_bed_host(ne: str, **overrides) -> UnixHost:
    """Build a real ``UnixHost`` from the veggies lab data (mgmt-ip resolution)."""
    kwargs = {"term": "ssh", "transfer": "scp", "log": LogMode.QUIET}
    kwargs.update(overrides)
    return make_host(ne, **kwargs)


def resolved_ip(ne: str) -> str:
    """The ip ``otto.tunnel.manage._resolve_one`` picks for these test-built hosts.

    These ``UnixHost`` objects never carry a populated ``interfaces`` dict (see
    the module docstring), so ``_resolve_one`` always falls back to the host's
    own management ip -- regardless of what ``lab.json`` declares.
    """
    return host_data(ne)["ip"]


async def assert_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) on a down VM -- never skip (dev-VM rule)."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, SSH_PORT), timeout=REACHABLE_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element}_seed ({ip}) unreachable on :{SSH_PORT} -- bed down? "
            f"(otto tunnel e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def assert_no_leftover_tunnel_processes() -> None:
    r"""Scan all three peers for ``otto-tunnel:`` tagged processes; raise if any remain.

    Must decode each line through :func:`parse_process_discovery` (the same
    strict sentinel parser production discovery uses) rather than treat any
    non-empty ``DISCOVERY_PS_COMMAND`` output as a leak: that command's own
    ``ps | \grep -a ' otto-tunnel:'`` pipeline always shows up in its own `ps`
    snapshot (the grep argv literally contains the search string), which is a
    self-match, not a real tagged tunnel process. ``parse_process_discovery``
    requires the full 11-segment sentinel and correctly ignores that noise.
    """
    hosts = [build_bed_host(ne) for ne in ("carrot", "tomato", "pepper")]
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


def listener_script(port: int, outfile: str, timeout: float) -> str:
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


async def wait_for_udp_bound(
    host: UnixHost, ip: str, port: int, timeout: float = BIND_CONFIRM_TIMEOUT
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


async def spawn_udp_listener(host: UnixHost, port: int, outfile: str, timeout: float) -> None:
    """Start a detached UDP listener on *host* and confirm it is bound before returning.

    See :func:`wait_for_udp_bound` for why the confirmation is required.
    """
    script = listener_script(port, outfile, timeout)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    await host.exec(cmd, timeout=15, log=LogMode.QUIET)
    await wait_for_udp_bound(host, "127.0.0.1", port)


async def wait_for_listener_output(
    host: UnixHost,
    outfile: str,
    timeout: float = LISTEN_TIMEOUT,
    interval: float = POLL_INTERVAL,
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


async def remove_remote_file(host: UnixHost, path: str) -> None:
    """Best-effort remote cleanup of a scratch file."""
    await host.exec(f"rm -f {shlex.quote(path)}", timeout=15, log=LogMode.QUIET)


def send_udp(ip: str, port: int, payload: bytes) -> None:
    """Send one UDP datagram from this (dev-VM) process to *ip*:*port*.

    Valid because the resolved ingress ip is each host's real, dev-VM-reachable
    management ip for every host built in this module (see the module
    docstring) -- the ingress socat's specific bind still accepts a datagram
    addressed to exactly that ip.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(payload, (ip, port))


def random_outfile() -> str:
    return f"/tmp/otto_tunnel_e2e_{uuid.uuid4().hex}.out"


def cli_sut_dir(tmp_path: Path) -> Path:
    """Scaffold a sut dir for CLI-driven tunnel tests: real veggies lab + declared containers.

    Extracted verbatim (2026-07-17) from
    ``tests/e2e/test_tunnel_e2e.py::_cli_cycle_sut_dir`` so
    ``tests/e2e/tunnel_stability/`` can reuse the same CLI-facing SUT without
    duplicating it. Two deliberate properties:

    - The host entries are the tech1 fixture's, verbatim — INCLUDING the
      declared ``eth2``/``192.168.1.x`` data-plane interfaces. Unlike the
      library-API tests (whose ``make_host`` never wires ``interfaces``, so
      they bind management ips), the CLI path loads lab data verbatim and
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


def run_tunnel_cli(sut_dir: Path, *args: str) -> str:
    """Invoke the real ``otto tunnel`` CLI against *sut_dir* and return stdout.

    Same invocation mechanism as
    ``tests/e2e/test_tunnel_e2e.py::test_cli_cycle_add_list_remove_list_docker_free``
    (``run_otto`` with ``lab="veggies"`` and a 180s timeout): *args* are the
    tokens after ``tunnel`` (e.g. ``run_tunnel_cli(sut, "list")`` runs
    ``otto --lab veggies tunnel list``). Asserts a zero exit so a broken CLI
    invocation fails loud with the real stdout/stderr rather than silently
    mismatching whatever a caller asserts against the returned string.
    """
    proc = run_otto(["tunnel", *args], sut_dirs=sut_dir, lab="veggies", timeout=180)
    assert proc.returncode == 0, (
        f"otto tunnel {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    )
    return proc.stdout
