"""Async orchestration for tunnels — the callable library API (spec §6-§8, §10-§12).

The CLI is a thin consumer of ``add_tunnel`` / ``remove_tunnel`` /
``remove_all_tunnels``; each is usable standalone from any Python code.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logger.mode import LogMode
from .discovery import (
    _TUNNEL_HOST_TIMEOUT,
    TunnelDiscovery,
    _scan_hosts,
    discover_observations,
    discover_tunnels,
)
from .model import Direction, ProcKey, Role, Tunnel, TunnelHop
from .sentinel import encode_sentinel
from .socat import (
    FREE_PORT_PROBE_COMMAND,
    egress_socat_args,
    ingress_socat_args,
    launch_command,
    parse_listening_ports,
    pick_free_port,
    relay_socat_args,
)

if TYPE_CHECKING:
    from ..config.lab import Lab

logger = logging.getLogger(__name__)

EndpointSpec = tuple[str, str | None]

_VERIFY_RETRY_DELAY = 1.0
"""One settle-then-retry before declaring a just-launched process missing."""

_SUPPORTED_PROTOCOLS = ("tcp", "udp")

_LOOPBACK = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class ResolvedHop:
    """One chain position resolved against the live lab."""

    hop: TunnelHop
    ip: str
    host: Any


def _is_container(host: Any) -> bool:
    from ..host.docker_host import DockerContainerHost

    return isinstance(host, DockerContainerHost)


async def _container_ip(container: Any) -> str:
    """Resolve the container's bridge ip via ``docker inspect`` on the parent (spec §8)."""
    cmd = (
        "docker inspect -f "
        "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "
        f"{container.container_id}"
    )
    try:
        result = await asyncio.wait_for(
            container.parent.exec(cmd, log=LogMode.QUIET), _TUNNEL_HOST_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            f"host {container.parent.id!r} timed out inspecting container {container.id!r}"
        ) from e
    ip = result.value.strip() if result.is_ok else ""
    if not ip:
        raise ValueError(f"container {container.id!r} has no resolvable network address")
    return ip


async def _resolve_one(lab: "Lab", spec: EndpointSpec) -> ResolvedHop:
    """Resolve ``(host_id, iface)`` off the live lab (iface rules per spec §6.3/§8)."""
    host_id, iface = spec
    host = lab.hosts.get(host_id)
    if host is None:
        raise ValueError(f"unknown host {host_id!r}")
    if _is_container(host):
        if iface is not None:
            raise ValueError(
                f"container {host_id!r} takes no @interface (containers have no modeled interfaces)"
            )
        return ResolvedHop(hop=TunnelHop(host=host_id), ip=await _container_ip(host), host=host)
    ifaces = getattr(host, "interfaces", {}) or {}
    if iface is not None:
        raw = ifaces.get(iface)
        if raw is None:
            known = ", ".join(sorted(ifaces)) or "<none>"
            raise ValueError(f"host {host_id!r} has no interface {iface!r} (known: {known})")
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        resolved = ResolvedHop(hop=TunnelHop(host=host_id, interface=iface), ip=ip, host=host)
    elif len(ifaces) > 1:
        raise ValueError(
            f"host {host_id!r}: ambiguous interface, specify one of: {', '.join(sorted(ifaces))}"
        )
    elif len(ifaces) == 1:
        ((name, raw),) = ifaces.items()
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        resolved = ResolvedHop(hop=TunnelHop(host=host_id, interface=name), ip=ip, host=host)
    else:
        resolved = ResolvedHop(hop=TunnelHop(host=host_id), ip=getattr(host, "ip", ""), host=host)
    if not resolved.ip:
        raise ValueError(f"host {host_id!r} has no usable address for tunneling")
    return resolved


async def _resolve_chain(lab: "Lab", specs: list[EndpointSpec]) -> list[ResolvedHop]:
    """Resolve + validate the whole ordered chain (spec §6, §8 container rules)."""
    min_hosts = 2
    if len(specs) < min_hosts:
        raise ValueError(f"--hosts needs at least {min_hosts} hosts (the ordered path)")
    seen: set[str] = set()
    for host_id, _iface in specs:
        if host_id in seen:
            raise ValueError(f"host {host_id!r} appears more than once in the chain")
        seen.add(host_id)
    last = len(specs) - 1
    for i, (host_id, _iface) in enumerate(specs):
        host = lab.hosts.get(host_id)
        if host is None or not _is_container(host):
            continue
        if i not in (0, last):
            raise ValueError(
                f"container {host_id!r} can only be a tunnel endpoint, not a relay hop"
            )
        neighbor = specs[1][0] if i == 0 else specs[last - 1][0]
        parent_id = getattr(getattr(host, "parent", None), "id", None)
        if neighbor != parent_id:
            raise ValueError(
                f"container {host_id!r} must neighbor its parent host {parent_id!r} "
                f"in the chain (got {neighbor!r})"
            )
    for host_id, _iface in specs:
        host = lab.hosts.get(host_id)
        if host is None:
            continue  # unknown-host error is raised below by _resolve_one
        if not getattr(host, "has_bash", False):
            raise ValueError(
                f"host {host_id!r} cannot be part of a tunnel path (has_bash=False) — "
                "it cannot run the tagged socat processes, and discovery/remove only "
                "scan has_bash hosts, so it would leak un-reapable processes"
            )
    return [await _resolve_one(lab, spec) for spec in specs]


def _check_conflicts(discovery: TunnelDiscovery, tunnel: Tunnel) -> None:
    """Reject id duplicates and endpoint service-port bind collisions (spec §7)."""
    new_endpoints = {tunnel.path[0].host, tunnel.path[-1].host}
    for live in discovery.tunnels:
        if live.tunnel.id == tunnel.id:
            raise ValueError(f"a tunnel {tunnel.id!r} already exists on this path+port")
        if (
            live.tunnel.service_port == tunnel.service_port
            and live.tunnel.protocol.lower() == tunnel.protocol.lower()
        ):
            theirs = {live.tunnel.path[0].host, live.tunnel.path[-1].host}
            clash = sorted(theirs & new_endpoints)
            if clash:
                raise ValueError(
                    f"tunnel {live.tunnel.id!r} already binds "
                    f"{tunnel.protocol}:{tunnel.service_port} on {', '.join(clash)}"
                )


@dataclass(frozen=True, slots=True)
class _ProcSpec:
    """One process to launch: where, which chain, what argv."""

    hop_index: int
    direction: Direction
    role: Role
    carrier_port: int
    socat_args: list[str]


def _process_plan(
    tunnel: Tunnel, ips: list[str], p_fwd: int, p_rev: int, deliver_fwd: str
) -> list[_ProcSpec]:
    """Build the 2n launch specs, downstream-first per direction (spec §6.1/§6.4).

    FWD rides ``p_fwd`` toward the last hop; REV rides ``p_rev`` toward the
    first. Launch order guarantees every listener exists before its upstream
    connects: FWD = index descending, REV = index ascending.
    """
    last = len(ips) - 1
    proto, svc = tunnel.protocol, tunnel.service_port
    plan: list[_ProcSpec] = []
    # FWD: egress at `last`, relays last-1..1, ingress at 0.
    plan.append(
        _ProcSpec(
            last,
            Direction.FWD,
            Role.EGRESS,
            p_fwd,
            egress_socat_args(proto, svc, deliver_fwd, p_fwd),
        )
    )
    plan.extend(
        [
            _ProcSpec(i, Direction.FWD, Role.RELAY, p_fwd, relay_socat_args(p_fwd, ips[i + 1]))
            for i in range(last - 1, 0, -1)
        ]
    )
    plan.append(
        _ProcSpec(
            0,
            Direction.FWD,
            Role.INGRESS,
            p_fwd,
            ingress_socat_args(proto, svc, ips[0], ips[1], p_fwd),
        )
    )
    # REV: egress at 0, relays 1..last-1, ingress at `last`.
    plan.append(
        _ProcSpec(
            0, Direction.REV, Role.EGRESS, p_rev, egress_socat_args(proto, svc, _LOOPBACK, p_rev)
        )
    )
    plan.extend(
        [
            _ProcSpec(i, Direction.REV, Role.RELAY, p_rev, relay_socat_args(p_rev, ips[i - 1]))
            for i in range(1, last)
        ]
    )
    plan.append(
        _ProcSpec(
            last,
            Direction.REV,
            Role.INGRESS,
            p_rev,
            ingress_socat_args(proto, svc, ips[last], ips[last - 1], p_rev),
        )
    )
    return plan


@dataclass(frozen=True, slots=True)
class AddedTunnel:
    """A successfully added + verified tunnel."""

    tunnel: Tunnel
    carrier_fwd: int
    carrier_rev: int


def _proc_host_name(resolved: list[ResolvedHop], proc: "_ProcSpec") -> str:
    """Return the host id a plan entry launches on."""
    return resolved[proc.hop_index].hop.host


async def _require_tools(host: Any) -> None:
    """Fail loud + name the host when socat or bash is missing."""
    try:
        result = await asyncio.wait_for(
            host.exec(
                "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 "
                "&& echo ok || echo no",
                log=LogMode.QUIET,
            ),
            _TUNNEL_HOST_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"host {host.id!r} timed out checking for socat/bash") from e
    if "ok" not in result.value:
        raise RuntimeError(f"host {host.id!r} is missing socat and/or bash (required for tunnels)")


async def _probe_used_ports(resolved: list[ResolvedHop]) -> set[int]:
    """Union of listening ports across the chain (spec §6.2).

    A probe that *times out* raises (wedged host — the launch would hang
    anyway); a probe whose command fails contributes nothing (minimal hosts
    without ss/netstat — the post-add verify catches a real collision).
    """

    async def probe(r: ResolvedHop) -> set[int]:
        try:
            result = await asyncio.wait_for(
                r.host.exec(FREE_PORT_PROBE_COMMAND, log=LogMode.QUIET), _TUNNEL_HOST_TIMEOUT
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"host {r.hop.host!r} timed out probing for free ports") from e
        return parse_listening_ports(result.value) if result.is_ok else set()

    return set().union(*await asyncio.gather(*(probe(r) for r in resolved)))


async def _kill_tunnel_on(hosts: list[Any], tunnel_id: str) -> None:
    """Best-effort reap of *tunnel_id*'s processes on *hosts* (rollback path)."""
    observations, _unreachable = await _scan_hosts(hosts)
    by_host: dict[str, list[int]] = {}
    host_by_id = {h.id: h for h in hosts}
    for origin, obs in observations:
        if obs.parsed.tunnel.id == tunnel_id:
            by_host.setdefault(origin, []).append(obs.pid)
    for host_id, pids in by_host.items():
        kill_cmd = f"kill {' '.join(str(p) for p in sorted(pids))}"
        try:
            await asyncio.wait_for(
                host_by_id[host_id].exec(kill_cmd, log=LogMode.QUIET), _TUNNEL_HOST_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 — rollback is best-effort by design
            logger.warning(f"otto tunnel: rollback reap failed on {host_id!r}: {e}")


async def _verify_chain(
    resolved: list[ResolvedHop], tunnel: Tunnel
) -> tuple[set[ProcKey], list[str]]:
    """Scan just the chain hosts; return (present, unreachable)."""
    observations, unreachable = await _scan_hosts([r.host for r in resolved])
    present = {
        (origin, obs.parsed.direction, obs.parsed.role)
        for origin, obs in observations
        if obs.parsed.tunnel.id == tunnel.id
    }
    return present, unreachable


def _raise_launch_failure(resolved: list[ResolvedHop], proc: "_ProcSpec", result: Any) -> None:
    """Raise for a launch that ran but reported failure (TRY301: kept out of the try body)."""
    raise RuntimeError(
        f"host {_proc_host_name(resolved, proc)!r} failed to launch "
        f"{proc.direction.value}/{proc.role.value}: {result.value!r}"
    )


def _raise_verify_failure(tunnel: Tunnel, missing: set[ProcKey], unreachable: list[str]) -> None:
    """Raise for a post-add verify that never converged (TRY301: kept out of the try body)."""
    pretty = ", ".join(
        f"{h}/{d.value}/{r.value}"
        for h, d, r in sorted(missing, key=lambda k: (k[0], k[1].value, k[2].value))
    )
    unreachable_note = (
        f" (unreachable during verify: {', '.join(unreachable)})" if unreachable else ""
    )
    raise RuntimeError(
        f"tunnel {tunnel.id!r} failed post-add verify — not running: {pretty}{unreachable_note}"
    )


async def add_tunnel(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "tcp",
    dest: EndpointSpec | None = None,
) -> AddedTunnel:
    """Build a bidirectional host-resident tunnel and verify it came up (spec §6).

    Launch order is downstream-first per direction; any launch failure or a
    failed post-add verify reaps everything already started — no half-tunnels
    survive a failed add. "Started" is tracked from the moment a launch is
    *attempted*, not from a confirmed ack: a launch ``exec`` that times out
    only bounds how long we waited for the reply, not whether the command
    reached the host, so even a first-launch timeout triggers rollback.
    """
    protocol = protocol.lower()
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported protocol {protocol!r} (use tcp or udp)")
    resolved = await _resolve_chain(lab, hosts)
    dest_hop = await _resolve_one(lab, dest) if dest else None
    if dest_hop is not None:
        chain_host_ids = {r.hop.host for r in resolved}
        if dest_hop.hop.host in chain_host_ids:
            raise ValueError(
                f"--dest {dest_hop.hop.host!r} names a host already in the tunnel path "
                f"({', '.join(sorted(chain_host_ids))}) — --dest must be a host OUTSIDE "
                "the tunnel path: delivering to a chain endpoint's own service IP feeds "
                "the reverse ingress and creates a forwarding loop the post-add verify "
                "cannot detect (spec §6.3 requires a third host)"
            )
    tunnel = Tunnel(
        protocol=protocol,
        service_port=port,
        path=tuple(r.hop for r in resolved),
        dest=dest_hop.hop.host if dest_hop else None,
    )
    _check_conflicts(await discover_tunnels(lab), tunnel)
    for r in resolved:
        await _require_tools(r.host)

    used = await _probe_used_ports(resolved) | {port}
    carrier_fwd = pick_free_port(used)
    carrier_rev = pick_free_port(used | {carrier_fwd})

    ips = [r.ip for r in resolved]
    deliver_fwd = dest_hop.ip if dest_hop else _LOOPBACK
    plan = _process_plan(tunnel, ips, carrier_fwd, carrier_rev, deliver_fwd)

    launched = False
    try:
        for proc in plan:
            sentinel = encode_sentinel(
                tunnel,
                direction=proc.direction,
                role=proc.role,
                hop_index=proc.hop_index,
                carrier_port=proc.carrier_port,
            )
            host = resolved[proc.hop_index].host
            # Attempting a launch is enough to warrant rollback: the timeout
            # below bounds the ack, not the send, so the command may have
            # already reached the host even if we never see success.
            launched = True
            try:
                result = await asyncio.wait_for(
                    host.exec(launch_command(sentinel, proc.socat_args), log=LogMode.QUIET),
                    _TUNNEL_HOST_TIMEOUT,
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"host {_proc_host_name(resolved, proc)!r} timed out spawning the tunnel"
                ) from e
            if not result.is_ok:
                _raise_launch_failure(resolved, proc, result)

        present, unreachable = await _verify_chain(resolved, tunnel)
        expected = tunnel.expected_processes()
        if expected - present:
            await asyncio.sleep(_VERIFY_RETRY_DELAY)
            present, unreachable = await _verify_chain(resolved, tunnel)
        missing = expected - present
        if missing:
            _raise_verify_failure(tunnel, missing, unreachable)
    except BaseException:
        if launched:
            await _kill_tunnel_on([r.host for r in resolved], tunnel.id)
        raise
    return AddedTunnel(tunnel=tunnel, carrier_fwd=carrier_fwd, carrier_rev=carrier_rev)


@dataclass(frozen=True, slots=True)
class RemovedReport:
    """What a reap pass tore down — and what refused to die (spec §10)."""

    removed_ids: list[str]
    killed: dict[str, list[int]]
    unreachable: list[str]
    survivors: list[tuple[str, int]]
    """``(host_id, pid)`` processes still present in the post-kill verify scan."""


async def _reap(lab: "Lab", predicate: Any) -> RemovedReport:
    """Discover, kill matching pids per host, then re-scan to verify (spec §10)."""
    observations, unreachable_discovery = await discover_observations(lab)
    ids: set[str] = set()
    by_host: dict[str, list[int]] = {}
    for origin, obs in observations:
        if predicate(obs.parsed.tunnel):
            ids.add(obs.parsed.tunnel.id)
            by_host.setdefault(origin, []).append(obs.pid)

    killed: dict[str, list[int]] = {}
    unreachable: set[str] = set(unreachable_discovery)
    for host_id, pids in by_host.items():
        host = lab.hosts[host_id]
        kill_cmd = f"kill {' '.join(str(p) for p in sorted(pids))}"
        try:
            result = await asyncio.wait_for(
                host.exec(kill_cmd, log=LogMode.QUIET), _TUNNEL_HOST_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"otto tunnel: timed out reaping host {host_id!r}")
            unreachable.add(host_id)
            continue
        except Exception as e:  # noqa: BLE001 — transparent partial reap
            logger.warning(f"otto tunnel: could not reap on host {host_id!r}: {e}")
            unreachable.add(host_id)
            continue
        if not result.is_ok:
            logger.warning(f"otto tunnel: kill failed on host {host_id!r}: {result.value!r}")
            unreachable.add(host_id)
            continue
        killed[host_id] = sorted(pids)

    # Post-remove verify (spec §10): re-scan only the hosts we killed on.
    survivors: list[tuple[str, int]] = []
    if killed:
        verify_hosts = [lab.hosts[h] for h in killed]
        post, _post_unreachable = await _scan_hosts(verify_hosts)
        survivors = sorted((origin, obs.pid) for origin, obs in post if obs.parsed.tunnel.id in ids)
    return RemovedReport(
        removed_ids=sorted(ids),
        killed=killed,
        unreachable=sorted(unreachable),
        survivors=survivors,
    )


async def remove_tunnel(lab: "Lab", tunnel_id: str) -> RemovedReport:
    """Reap one tunnel by id, then verify its processes are actually gone."""
    return await _reap(lab, lambda t: t.id == tunnel_id)


async def remove_all_tunnels(lab: "Lab") -> RemovedReport:
    """Reap every otto tunnel (owner-agnostic), with the same verify pass."""
    return await _reap(lab, lambda _t: True)
