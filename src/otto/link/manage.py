"""Async orchestration for dynamic tunnels — the callable library API (spec §5).

The CLI is a thin consumer of ``add_link`` / ``remove_link`` /
``remove_all_links``; each is usable standalone from any Python code.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logger import get_logger
from ..logger.mode import LogMode
from .discovery import all_links, discover_observations
from .model import Link, LinkEndpoint, Provenance, make_dynamic_link_id
from .sentinel import encode_sentinel
from .socat import (
    FREE_PORT_PROBE_COMMAND,
    egress_socat_args,
    ingress_socat_args,
    launch_command,
    parse_listening_ports,
    pick_free_port,
)

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

logger = get_logger()

EndpointSpec = tuple[str, str | None]

_LINK_HOST_TIMEOUT = 30.0
"""Ceiling on any single-host ``oneshot`` on the add/remove path. A wedged
host must not hang the whole command: on ``add`` (which can't proceed without
the host) a timeout raises a clear, host-named error; on ``remove`` it is
treated exactly like a connection failure (unreachable, not fatal)."""

_SUPPORTED_PROTOCOLS = ("tcp", "udp")


@dataclass(frozen=True, slots=True)
class AddedTunnel:
    """Where a newly added tunnel ended up running."""

    link: Link
    ingress_host: str
    exit_host: str
    carrier_port: int


def _resolve_endpoint(lab: "Lab", spec: EndpointSpec, port: int) -> LinkEndpoint:
    """Resolve ``(host_id, iface)`` to a ``LinkEndpoint`` off the live lab host.

    Applies the single-interface auto-resolution rule: an explicit *iface*
    must exist on the host; omitted with exactly one interface, that one is
    used; omitted with more than one, resolution fails loud. Also fails loud
    when resolution lands on an empty ip (e.g. the builtin ``local`` host, or
    a host with neither a usable interface nor a top-level ``ip``) — a tunnel
    endpoint with no address can never actually be reached.
    """
    host_id, iface = spec
    host = lab.hosts.get(host_id)
    if host is None:
        raise ValueError(f"unknown host {host_id!r}")
    ifaces = getattr(host, "interfaces", {}) or {}
    if iface is not None:
        raw = ifaces.get(iface)
        if raw is None:
            known = ", ".join(sorted(ifaces)) or "<none>"
            raise ValueError(f"host {host_id!r} has no interface {iface!r} (known: {known})")
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        endpoint = LinkEndpoint(host=host_id, interface=iface, ip=ip, port=port)
    elif len(ifaces) > 1:
        raise ValueError(
            f"host {host_id!r}: ambiguous interface, specify one of: {', '.join(sorted(ifaces))}"
        )
    elif len(ifaces) == 1:
        ((name, raw),) = ifaces.items()
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        endpoint = LinkEndpoint(host=host_id, interface=name, ip=ip, port=port)
    else:
        endpoint = LinkEndpoint(host=host_id, interface=None, ip=getattr(host, "ip", ""), port=port)
    if not endpoint.ip:
        raise ValueError(f"host {host_id!r} has no usable address for tunneling")
    return endpoint


async def _alloc_carrier_port(host: Any, port: int) -> int:
    """Pick a free carrier port on *host*, excluding the service *port* itself.

    Without the exclusion, a service port that happens to fall in the
    ephemeral range (e.g. ``--port 49200``) could be handed back as its own
    carrier — the ingress socat would then try to dial the exit host on the
    exact port the exit's egress socat is about to bind for something else.
    """
    try:
        result = await asyncio.wait_for(
            host.oneshot(FREE_PORT_PROBE_COMMAND, log=LogMode.QUIET), _LINK_HOST_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"host {host.id!r} timed out probing for a free carrier port") from e
    return pick_free_port(parse_listening_ports(result.value) | {port})


async def add_link(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "tcp",
    dest: EndpointSpec | None = None,
) -> AddedTunnel:
    """Build a host-resident tunnel and return where it runs (spec §7).

    Spawns the tagged processes and reports which started; it does not
    pre-validate reachability or guarantee delivery (see spec §7.5).
    """
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported protocol {protocol!r} (use tcp or udp)")
    if len(hosts) != 2:  # noqa: PLR2004
        raise ValueError("multi-hop paths arrive with the hop-aware phase; give exactly 2 hosts")
    ingress_spec, exit_spec = hosts[0], hosts[-1]
    dest_spec = dest or exit_spec

    a = _resolve_endpoint(lab, ingress_spec, port)  # ingress (logical a)
    b = _resolve_endpoint(lab, dest_spec, port)  # destination (logical b)
    exit_ep = _resolve_endpoint(lab, exit_spec, port)  # tunnel exit host

    link = Link(
        a=a,
        b=b,
        protocol=protocol,
        provenance=Provenance.DYNAMIC,
        id=make_dynamic_link_id(a, b, protocol, port),
    )

    existing = {existing_link.id for existing_link in await all_links(lab)}
    if link.id in existing:
        raise ValueError(f"a tunnel {link.id!r} already exists on this route+port")

    ingress_host = lab.hosts[ingress_spec[0]]
    exit_host = lab.hosts[exit_spec[0]]
    for tool_host in (ingress_host, exit_host):
        await _require_tools(tool_host)

    carrier = await _alloc_carrier_port(exit_host, port)
    sentinel = encode_sentinel(link)

    # Egress first (so the carrier is listening before ingress connects).
    try:
        await asyncio.wait_for(
            exit_host.oneshot(
                launch_command(sentinel, egress_socat_args(protocol, port, b.ip, carrier)),
                log=LogMode.QUIET,
            ),
            _LINK_HOST_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"host {exit_host.id!r} timed out spawning the tunnel") from e
    try:
        await asyncio.wait_for(
            ingress_host.oneshot(
                launch_command(sentinel, ingress_socat_args(protocol, port, exit_ep.ip, carrier)),
                log=LogMode.QUIET,
            ),
            _LINK_HOST_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"host {ingress_host.id!r} timed out spawning the tunnel") from e
    return AddedTunnel(link=link, ingress_host=a.host, exit_host=exit_spec[0], carrier_port=carrier)


async def _require_tools(host: Any) -> None:
    """Fail loud + name the host when socat or bash is missing."""
    try:
        result = await asyncio.wait_for(
            host.oneshot(
                "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 "
                "&& echo ok || echo no",
                log=LogMode.QUIET,
            ),
            _LINK_HOST_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"host {host.id!r} timed out checking for socat/bash") from e
    if "ok" not in result.value:
        raise RuntimeError(f"host {host.id!r} is missing socat and/or bash (required for tunnels)")


@dataclass(frozen=True, slots=True)
class RemovedReport:
    """What a reap pass tore down (spec §7 removal counterpart)."""

    removed_ids: list[str]
    killed: dict[str, list[int]]
    unreachable: list[str]


async def _reap(lab: "Lab", predicate: Callable[[Link], bool]) -> RemovedReport:
    """Discover, then kill the pids of tunnels matching *predicate*, per host.

    Seeds ``unreachable`` from discovery-time failures (spec §10 — a host
    that couldn't be scanned at all must still be named, even though it never
    contributes an observation to reap), then unions in any host whose
    ``kill`` itself failed or timed out (#1/#3).
    """
    observations, unreachable_discovery = await discover_observations(lab)
    ids: set[str] = set()
    by_host: dict[str, list[int]] = {}
    for origin, obs in observations:
        if predicate(obs.link):
            ids.add(obs.link.id)
            by_host.setdefault(origin, []).append(obs.pid)

    killed: dict[str, list[int]] = {}
    unreachable: set[str] = set(unreachable_discovery)
    for host_id, pids in by_host.items():
        host = lab.hosts[host_id]
        kill_cmd = f"kill {' '.join(str(p) for p in sorted(pids))}"
        try:
            result = await asyncio.wait_for(
                host.oneshot(kill_cmd, log=LogMode.QUIET), _LINK_HOST_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"otto link: timed out reaping host {host_id!r}")
            unreachable.add(host_id)
            continue
        except Exception as e:  # noqa: BLE001 — transparent partial reap (spec §10)
            logger.warning(f"otto link: could not reap on host {host_id!r}: {e}")
            unreachable.add(host_id)
            continue
        if not result.is_ok:
            logger.warning(f"otto link: kill failed on host {host_id!r}: {result.value!r}")
            unreachable.add(host_id)
            continue
        killed[host_id] = sorted(pids)
    return RemovedReport(removed_ids=sorted(ids), killed=killed, unreachable=sorted(unreachable))


async def remove_link(lab: "Lab", link_id: str) -> RemovedReport:
    """Reap the tunnel with *link_id* (its ``-<port>`` suffix targets one tunnel)."""
    return await _reap(lab, lambda link: link.id == link_id)


async def remove_all_links(lab: "Lab") -> RemovedReport:
    """Reap every otto tunnel (owner-agnostic)."""
    return await _reap(lab, lambda _link: True)
