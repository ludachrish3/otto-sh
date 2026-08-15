"""Async orchestration for tunnels — the callable library API (spec §6-§8, §10-§12).

The CLI is a thin consumer of ``add_tunnel`` / ``remove_tunnel`` /
``remove_all_tunnels``; each is usable standalone from any Python code.
"""

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any, TypeGuard

from ..host.daemon import kill_command, launch_command
from ..host.errors import HostCommandError, HostUnreachableError
from ..host.host import is_dry_run
from ..logger.mode import LogMode
from .carrier import DEFAULT_CARRIER, TunnelCarrier, build_carrier
from .discovery import (
    _TUNNEL_HOST_TIMEOUT,
    TunnelDiscovery,
    _device_read,
    _device_running,
    _scan_hosts,
    discover_observations,
    discover_tunnels,
)
from .model import Direction, ProcKey, Role, Tunnel, TunnelHop
from .sentinel import encode_sentinel
from .socat import FREE_PORT_PROBE_COMMAND, parse_listening_ports, pick_free_port

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..host.docker_host import DockerContainerHost

logger = logging.getLogger(__name__)

EndpointSpec = tuple[str, str | None]

_VERIFY_RETRY_DELAY = 1.0
"""One settle-then-retry before declaring a just-launched process missing."""

_LOOPBACK = "127.0.0.1"

# Two concurrent add_tunnel calls for the SAME (path, port, protocol) share a
# deterministic tunnel id; their rollback reaps BY id, so an unserialized race
# lets the loser's rollback kill the winner's processes. Serialize per id —
# the second entrant then sees the first's processes in _check_conflicts and
# fails loud, which is the intended exactly-one-wins contract. In-process
# only by design: cross-process racers are adjudicated by the endpoint
# socat's specific-ip bind failing, and its own verify-rollback (which we do
# NOT reach here) — see tests/e2e/tunnel_stability/test_concurrency.py.
# Remove paths (remove_tunnel / remove_all_tunnels) deliberately do not
# participate: only add-vs-add shares a deterministic id's fate, and an add
# racing a remove just fails its verify and rolls back clean.
_ADD_LOCKS: dict[str, asyncio.Lock] = {}


def _add_lock(tunnel_id: str) -> asyncio.Lock:
    return _ADD_LOCKS.setdefault(tunnel_id, asyncio.Lock())


@dataclass(frozen=True, slots=True)
class ResolvedHop:
    """One chain position resolved against the live lab."""

    hop: TunnelHop
    ip: str
    host: Any


def _is_container(host: Any) -> "TypeGuard[DockerContainerHost]":
    from ..host.docker_host import DockerContainerHost

    return isinstance(host, DockerContainerHost)


async def _container_ip(container: Any) -> str:
    """Resolve the container's bridge ip via ``docker inspect`` on the parent (spec §8)."""
    cmd = (
        "docker inspect -f "
        "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "
        f"{container.container_id}"
    )
    result = await _device_read(container.parent, cmd)
    if result.timed_out:
        raise HostUnreachableError(
            f"host {container.parent.id!r} timed out inspecting container {container.id!r}"
        )
    ip = result.value.strip() if result.is_ok else ""
    if not ip:
        raise ValueError(f"container {container.id!r} has no resolvable network address")
    return ip


def _host_or_raise(lab: "Lab", host_id: str) -> Any:
    """Look one host up, or raise the lab-data error a real run raises."""
    host = lab.hosts.get(host_id)
    if host is None:
        raise ValueError(f"unknown host {host_id!r}")
    return host


def _resolve_static(host_id: str, host: Any, iface: str | None) -> ResolvedHop:
    """Resolve a NON-container hop from declared lab data alone — no device (spec §6.3).

    The pure half of :func:`_resolve_one`, split out so
    :func:`_planned_hop` can reach it: a normal host's tunnel address is its
    declared ``interfaces`` entry (or its management ``ip``), which a
    ``--dry-run`` reads perfectly well. A container's is not — see
    :func:`_planned_hop`.
    """
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


async def _resolve_one(lab: "Lab", spec: EndpointSpec) -> ResolvedHop:
    """Resolve ``(host_id, iface)`` off the live lab (iface rules per spec §6.3/§8)."""
    host_id, iface = spec
    host = _host_or_raise(lab, host_id)
    if _is_container(host):
        if iface is not None:
            raise ValueError(
                f"container {host_id!r} takes no @interface (containers have no modeled interfaces)"
            )
        # Probe, never start: a tunnel add must not compose a docker stack
        # (issue #139) — the container is a lab member only while it runs.
        try:
            running = await _device_running(host)
        except asyncio.TimeoutError as e:
            raise HostUnreachableError(
                f"host {host.parent.id!r} timed out probing container {host_id!r}"
            ) from e
        if not running:
            raise ValueError(
                f"container {host_id!r} is not running — tunnel commands never start "
                f"containers; run `otto docker up` for project {host.project!r} first"
            )
        return ResolvedHop(hop=TunnelHop(host=host_id), ip=await _container_ip(host), host=host)
    return _resolve_static(host_id, host, iface)


def _validate_chain_shape(lab: "Lab", specs: list[EndpointSpec]) -> None:
    """Every chain refusal that needs no device — length, duplicates, containers, bash.

    Split out of :func:`_resolve_chain` so the ``--dry-run`` planner
    (:func:`_planned_chain`) makes the SAME refusals from the SAME data rather
    than an approximation of them. All four read declared lab fields only, so
    a dry run answers each one completely; a chain refused here is not
    "not measured", it is decided.

    THE ``has_bash`` REFUSAL IS THE LOAD-BEARING ONE. It is what makes
    ``otto.tunnel.manage.add_tunnel`` a PROTECTED path in the
    ``daemon-launch`` gap record (:data:`otto.host.userland.GAPS`) rather than
    an open hole: the ``launch_command`` further down is unreachable on exactly
    the hosts that record covers. Both callers route through here, so adding a
    third cannot bypass it.
    """
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
            continue  # unknown-host error is raised by the per-hop resolution
        if not getattr(host, "has_bash", False):
            raise ValueError(
                f"host {host_id!r} cannot be part of a tunnel path (has_bash=False) — "
                "it cannot run the tagged socat processes, and discovery/remove only "
                "scan has_bash hosts, so it would leak un-reapable processes"
            )


async def _resolve_chain(lab: "Lab", specs: list[EndpointSpec]) -> list[ResolvedHop]:
    """Resolve + validate the whole ordered chain (spec §6, §8 container rules)."""
    _validate_chain_shape(lab, specs)
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
    argv: list[str]


def _process_plan(
    tunnel: Tunnel,
    ips: list[str],
    p_fwd: int,
    p_rev: int,
    deliver_fwd: str,
    carrier: TunnelCarrier,
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
            carrier.egress_args(proto, svc, deliver_fwd, p_fwd),
        )
    )
    plan.extend(
        [
            _ProcSpec(i, Direction.FWD, Role.RELAY, p_fwd, carrier.relay_args(p_fwd, ips[i + 1]))
            for i in range(last - 1, 0, -1)
        ]
    )
    plan.append(
        _ProcSpec(
            0,
            Direction.FWD,
            Role.INGRESS,
            p_fwd,
            carrier.ingress_args(proto, svc, ips[0], ips[1], p_fwd),
        )
    )
    # REV: egress at 0, relays 1..last-1, ingress at `last`.
    plan.append(
        _ProcSpec(
            0, Direction.REV, Role.EGRESS, p_rev, carrier.egress_args(proto, svc, _LOOPBACK, p_rev)
        )
    )
    plan.extend(
        [
            _ProcSpec(i, Direction.REV, Role.RELAY, p_rev, carrier.relay_args(p_rev, ips[i - 1]))
            for i in range(1, last)
        ]
    )
    plan.append(
        _ProcSpec(
            last,
            Direction.REV,
            Role.INGRESS,
            p_rev,
            carrier.ingress_args(proto, svc, ips[last], ips[last - 1], p_rev),
        )
    )
    return plan


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    """What a ``--dry-run`` would do, and what it could not check.

    Built from lab data and the command's own arguments — a dry run makes no
    device contact, so there is nothing else to build it from.

    BOTH HALVES ARE THE PRODUCT. :attr:`would` on its own reads as a promise
    otto has verified and cannot keep: the carrier ports in every argv here
    were picked without probing a single host, and the conflict, tooling and
    liveness refusals that would stop the call outright have not run.
    :attr:`unchecked` on its own is a dry run with no preview at all, which is
    the useless-rather-than-safe shape ``5e2041a6`` argued against. Renderers
    print both or neither.

    A DELIBERATE TWIN of :class:`otto.link.manage.DryRunPlan`, not a shared type.
    ``otto.link`` and ``otto.tunnel`` are decoupled by design — "NEITHER
    imports the other" (:mod:`otto.tunnel.model`) — and a common base would
    have to live in a third module that exists only to be inherited from. The
    vocabulary is shared instead, so ``otto -n link list`` and ``otto -n
    tunnel list`` read as one command family; the CLI renderers stay separate
    because the two packages' console surfaces already are.
    """

    would: list[str] = dc_field(default_factory=list)
    """One line per action a real run would take, in the order it would take
    them.

    A container endpoint SHORTENS this rather than emptying it: the 2n argv
    lines drop out (the neighbouring hops connect to an address only ``docker
    inspect`` knows), and the tunnel id, the chain and the provisional carrier
    pair — none of which is read off a device — stay. So the command never
    answers with only caveats."""

    unchecked: list[str] = dc_field(default_factory=list)
    """One line per fact a real run reads off a device and this one did not,
    each saying what the missing read would have decided. Never empty: a dry
    run that reached this type read nothing at all."""


@dataclass(frozen=True, slots=True)
class AddedTunnel:
    """A successfully added + verified tunnel — or, under ``--dry-run``, a plan."""

    tunnel: Tunnel
    """The tunnel that WAS built, or (with :attr:`plan` set) the one that would
    be. Honest in both cases: :func:`~otto.tunnel.model.make_tunnel_id` hashes
    the ordered path, the protocol and the service port, none of which is read
    off a device, so a preview names the id a real run would produce."""

    carrier_fwd: int | None
    carrier_rev: int | None
    """The two carrier ports actually allocated — and ``None`` under a
    ``--dry-run``, where :attr:`plan` carries the provisional pair instead.

    NOT the provisional numbers, deliberately. A real run picks these from the
    union of every chain host's LISTENING ports (``_probe_used_ports``)
    plus the service port; a dry run has only the service port, so it would
    hand back 49152/49153 on every lab in the world and a caller reading these
    two fields could not tell that from an allocation. The preview's own
    ``would`` line says the numbers AND says they are provisional, which a
    bare ``int`` cannot."""

    plan: "DryRunPlan | None" = None
    """Set, and nothing launched, when this call was a ``--dry-run``.

    Appended, because this is a public dataclass and inserting a field
    mid-order silently rebinds positional construction instead of failing."""


def _proc_host_name(resolved: list[ResolvedHop], proc: "_ProcSpec") -> str:
    """Return the host id a plan entry launches on."""
    return resolved[proc.hop_index].hop.host


async def _require_tools(host: Any, carrier: TunnelCarrier) -> None:
    """Fail loud + name the host when the carrier's required tools are missing.

    Satisfied means a bare ``ok`` line in the probe's output (the carrier
    contract) — a whole-line match, NOT a substring check, so a carrier whose
    failure text happens to contain ``ok`` ("not ok", "broken") cannot slip
    past the fail-fast check and die opaquely at launch time instead.
    """
    result = await _device_read(host, carrier.requirements_command)
    if result.timed_out:
        raise HostUnreachableError(
            f"host {host.id!r} timed out checking for {carrier.tools_description}"
        )
    if not any(line.strip() == "ok" for line in result.value.splitlines()):
        raise HostCommandError(
            f"host {host.id!r} is missing {carrier.tools_description} (required for tunnels)"
        )


async def _probe_used_ports(resolved: list[ResolvedHop]) -> set[int]:
    """Union of listening ports across the chain (spec §6.2).

    A probe that *times out* raises (wedged host — the launch would hang
    anyway); a probe whose command fails contributes nothing (minimal hosts
    without ss/netstat — the post-add verify catches a real collision).
    """

    async def probe(r: ResolvedHop) -> set[int]:
        result = await _device_read(r.host, FREE_PORT_PROBE_COMMAND)
        if result.timed_out:
            raise HostUnreachableError(f"host {r.hop.host!r} timed out probing for free ports")
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
        kill_cmd = kill_command(pids)
        try:
            await host_by_id[host_id].exec(
                kill_cmd, timeout=_TUNNEL_HOST_TIMEOUT, log=LogMode.QUIET
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
    raise HostCommandError(
        f"host {_proc_host_name(resolved, proc)!r} failed to launch "
        f"{proc.direction.value}/{proc.role.value}: {result.value!r}"
    )


def _raise_launch_timeout(resolved: list[ResolvedHop], proc: "_ProcSpec") -> None:
    """Raise for a launch ``exec`` that timed out (TRY301: kept out of the try body)."""
    raise HostUnreachableError(
        f"host {_proc_host_name(resolved, proc)!r} timed out spawning the tunnel"
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
    raise HostCommandError(
        f"tunnel {tunnel.id!r} failed post-add verify — not running: {pretty}{unreachable_note}"
    )


####################
#  --dry-run planning: what lab data and the given arguments alone can say
####################


@dataclass(frozen=True, slots=True)
class _PlannedHop:
    """One chain position resolved as far as lab data goes, and no further."""

    hop: TunnelHop
    """Always known: the hop's identity is declared, not discovered."""

    ip: str | None
    """``None`` when only a device could say — a container endpoint, whose
    address comes from ``docker inspect`` on its parent."""

    host: Any


def _planned_hop(lab: "Lab", spec: EndpointSpec) -> _PlannedHop:
    """Resolve one hop without contacting anything, or say the address is unknowable.

    The pure half of :func:`_resolve_one`, and the split between the two hop
    kinds is the sharpest constraint on this whole feature:

    * A NORMAL host is pure. :func:`_resolve_static` reads the DECLARED
      ``interfaces`` map (or the management ``ip``), so a dry run names the
      address exactly, with no approximation, and makes every one of that
      function's refusals — unknown interface, ambiguous interface, no usable
      address — from the same data a real run uses.
    * A CONTAINER is not. Its tunnel address is its docker bridge ip, read by
      ``docker inspect`` on the parent, so a dry run cannot resolve it and must
      SAY that. Guessing is not available and neither is the old behaviour: the
      synthetic reply's ``is_ok`` is ``True`` and its value is non-empty, so
      :func:`_container_ip`'s "no resolvable network address" guard did not
      fire and the literal string ``"[DRY RUN] Command not executed"`` became a
      container's IP address inside a socat argv.

    The container's ``@interface`` refusal still fires here: it needs no lab
    lookup beyond the one already done, and a real run makes it too.
    """
    host_id, iface = spec
    host = _host_or_raise(lab, host_id)
    if _is_container(host):
        if iface is not None:
            raise ValueError(
                f"container {host_id!r} takes no @interface (containers have no modeled interfaces)"
            )
        return _PlannedHop(hop=TunnelHop(host=host_id), ip=None, host=host)
    static = _resolve_static(host_id, host, iface)
    return _PlannedHop(hop=static.hop, ip=static.ip, host=static.host)


def _planned_chain(lab: "Lab", specs: list[EndpointSpec]) -> list[_PlannedHop]:
    """Validate and resolve the whole chain purely — :func:`_resolve_chain`'s twin, no device."""
    _validate_chain_shape(lab, specs)
    return [_planned_hop(lab, spec) for spec in specs]


def _unresolved_addresses(unresolved: list[str]) -> str:
    """Why a container endpoint leaves a chain with no argv to show."""
    names = ", ".join(repr(h) for h in unresolved)
    return (
        f"every process argv, because {names} is a container endpoint. A container's tunnel "
        f"address is its docker bridge ip, read with `docker inspect` on its parent host, and "
        f"the hops either side of it connect TO that address — so with it unread there is no "
        f"command line to show for any hop, not just for the container. Whether the container "
        f"is even running is a second read that did not happen: a real run probes it and "
        f"refuses the add outright if it is down, never starting it"
    )


_UNCHECKED_FREE_PORTS = (
    "which ports are already bound anywhere on the chain. A real run probes every hop with "
    "`ss -Htln` / `netstat -tln` first and skips what is listening, so the carrier pair above "
    "is PROVISIONAL — it was picked from the service port alone. Every argv above names those "
    "two ports, so a real run that finds either one taken emits different command lines"
)

_UNCHECKED_CONFLICTS = (
    "whether this tunnel already exists. A real run scans the lab for tagged processes first "
    "and refuses two ways: an id already present on this exact path+protocol+port, or any "
    "live tunnel already binding this protocol and service port on either endpoint. Both are "
    "decided from running processes, so neither was evaluated"
)


def _unchecked_tools(carrier_obj: TunnelCarrier) -> str:
    return (
        f"whether the chain hosts actually have {carrier_obj.tools_description}. A real run "
        f"runs the carrier's requirements probe on every hop and refuses the whole add, naming "
        f"the host, if one answers no. Before this preview existed that refusal is exactly what "
        f"a dry run PRINTED — the probe's synthetic reply carries no `ok` line — accusing a host "
        f"it had never contacted"
    )


_WOULD_LAUNCH_SHAPE = (
    "start each process below detached, with its argv[0] replaced by an `otto-tunnel:v1` "
    "sentinel naming this tunnel's id, direction, role, hop index and carrier port — that tag "
    "is the whole record, and it is how `tunnel list` and `tunnel remove` find the process "
    'again. `otto.host.daemon.launch_command` wraps each one in `bash -c \'exec -a "$1" '
    '"${@:2}"\'`, run under `systemd-run --user --collect` where that works and `setsid` '
    "where it does not"
)
"""Said ONCE, above the per-process lines, and this is a deliberate departure
from ``otto.link``, whose ``would`` lines are the exact command strings.

A ``tc qdisc replace …`` line is 60 characters and copy-pastes. The tunnel
equivalent is :func:`~otto.host.daemon.launch_command`'s output — a ~700
character portability conditional carrying the sentinel twice — and it is
IDENTICAL in shape on all 2n lines. Printing it verbatim six times buries the
part that differs (the host, the direction, the bind address, the two ports)
and buries the ``not checked`` block under it entirely. So the wrapper is
stated once, in full, and each line below carries the argv that varies.
"""


def _unchecked_launch_and_verify(count: int) -> str:
    return (
        f"the {count} launches themselves and the post-add verify. A real run starts them "
        f"downstream-first, re-scans the chain for their sentinels, and reaps EVERYTHING it "
        f"started if any launch fails or any process is missing — so this preview is a plan, "
        f"not a promise that the tunnel comes up"
    )


def _plan_add(
    lab: "Lab",
    specs: list[EndpointSpec],
    *,
    port: int,
    protocol: str,
    dest: EndpointSpec | None,
    carrier_obj: TunnelCarrier,
) -> AddedTunnel:
    """Preview :func:`add_tunnel` without contacting anything.

    Everything reachable here is pure — the chain's four structural refusals
    (:func:`_validate_chain_shape`), per-hop address resolution for normal
    hosts, the ``--dest``-in-chain refusal, the tunnel id, free-port selection
    given a ``used`` set, the whole 2n process plan and every argv, the
    sentinel codec and :func:`~otto.host.daemon.launch_command`. Each refusal
    is raised, not collected: they are the same refusals a real run makes, from
    the same data, so a dry run that swallowed them would be less faithful,
    not safer.

    The carrier ports are the one place a pure computation is not a
    measurement. :func:`pick_free_port` is deterministic given ``used``, and
    a real run's ``used`` is the union of every hop's listening ports; a dry
    run's is ``{service_port}``. The numbers are still worth printing — they
    are what a clean chain would get — but they are marked provisional where
    they appear AND named in :data:`_UNCHECKED_FREE_PORTS`, and they are kept
    out of :attr:`AddedTunnel.carrier_fwd` so no caller can read them as an
    allocation.
    """
    planned = _planned_chain(lab, specs)
    dest_hop = _planned_hop(lab, dest) if dest else None
    if dest_hop is not None:
        _ensure_dest_outside_chain(dest_hop.hop.host, {p.hop.host for p in planned})
    tunnel = Tunnel(
        protocol=protocol,
        service_port=port,
        path=tuple(p.hop for p in planned),
        dest=dest_hop.hop.host if dest_hop else None,
    )
    used = {port}
    carrier_fwd = pick_free_port(used)
    carrier_rev = pick_free_port(used | {carrier_fwd})

    chain = " -> ".join(
        f"{p.hop.host}@{p.hop.interface}" if p.hop.interface else p.hop.host for p in planned
    )
    delivery = (
        f"{dest_hop.hop.host} ({dest_hop.ip or 'address unread'})"
        if dest_hop is not None
        else f"{_LOOPBACK} on {planned[-1].hop.host}"
    )
    would = [
        f"build {tunnel.id}: {chain}, {protocol}:{port}, delivering to {delivery}",
        (
            f"carry fwd traffic on port {carrier_fwd} and rev on {carrier_rev} — PROVISIONAL, "
            f"see the first `not checked` line"
        ),
    ]
    unresolved = [p.hop.host for p in planned if p.ip is None]
    if dest_hop is not None and dest_hop.ip is None:
        unresolved.append(dest_hop.hop.host)
    unchecked: list[str] = []
    if unresolved:
        unchecked.append(_unresolved_addresses(unresolved))
    else:
        ips = [p.ip or "" for p in planned]
        deliver_fwd = dest_hop.ip if dest_hop is not None and dest_hop.ip else _LOOPBACK
        procs = _process_plan(tunnel, ips, carrier_fwd, carrier_rev, deliver_fwd, carrier_obj)
        would.append(_WOULD_LAUNCH_SHAPE)
        would.extend(
            f"{planned[proc.hop_index].hop.host} {proc.direction.value}/{proc.role.value}: "
            f"{' '.join(proc.argv)}"
            for proc in procs
        )
        unchecked.append(_UNCHECKED_FREE_PORTS)
    unchecked.append(_UNCHECKED_CONFLICTS)
    unchecked.append(_unchecked_tools(carrier_obj))
    unchecked.append(_unchecked_launch_and_verify(2 * len(planned)))
    return AddedTunnel(
        tunnel=tunnel,
        carrier_fwd=None,
        carrier_rev=None,
        plan=DryRunPlan(would, unchecked),
    )


def _sentinel_for(tunnel: Tunnel, proc: "_ProcSpec") -> str:
    """Build the argv[0] tag for one plan entry — one home for the real and planned launches."""
    return encode_sentinel(
        tunnel,
        direction=proc.direction,
        role=proc.role,
        hop_index=proc.hop_index,
        carrier_port=proc.carrier_port,
    )


def _ensure_dest_outside_chain(dest_host: str, chain_host_ids: set[str]) -> None:
    """Refuse a ``--dest`` that names a host already in the path (spec §6.3)."""
    if dest_host in chain_host_ids:
        raise ValueError(
            f"--dest {dest_host!r} names a host already in the tunnel path "
            f"({', '.join(sorted(chain_host_ids))}) — --dest must be a host OUTSIDE "
            "the tunnel path: delivering to a chain endpoint's own service IP feeds "
            "the reverse ingress and creates a forwarding loop the post-add verify "
            "cannot detect (spec §6.3 requires a third host)"
        )


async def add_tunnel(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "tcp",
    dest: EndpointSpec | None = None,
    carrier: str = DEFAULT_CARRIER,
) -> AddedTunnel:
    """Build a bidirectional host-resident tunnel and verify it came up (spec §6).

    Launch order is downstream-first per direction; any launch failure or a
    failed post-add verify reaps everything already started — no half-tunnels
    survive a failed add. "Started" is tracked from the moment a launch is
    *attempted*, not from a confirmed ack: a launch ``exec`` that times out
    only bounds how long we waited for the reply, not whether the command
    reached the host, so even a first-launch timeout triggers rollback.
    The *carrier* names a registered :class:`~otto.tunnel.carrier.TunnelCarrier`
    (chain-wide; default ``"socat"``).

    Under ``--dry-run`` nothing below the short-circuit runs: the report comes
    back with :attr:`~AddedTunnel.plan` set and both carrier ports ``None``.
    The short-circuit sits ABOVE ``_resolve_chain`` — above the read
    backstop in ``otto.tunnel.discovery._device_read``, which would
    otherwise raise — because the preview needs this call's whole intent and a
    command string does not carry it. It sits BELOW the carrier lookup and the
    protocol check, which are pure and refuse identically either way.
    """
    protocol = protocol.lower()
    carrier_obj = build_carrier(carrier)()
    if protocol not in carrier_obj.supported_protocols:
        supported = ", ".join(sorted(carrier_obj.supported_protocols))
        raise ValueError(
            f"carrier {carrier!r} does not support protocol {protocol!r} (use {supported})"
        )
    if is_dry_run():
        return _plan_add(
            lab, hosts, port=port, protocol=protocol, dest=dest, carrier_obj=carrier_obj
        )
    resolved = await _resolve_chain(lab, hosts)
    dest_hop = await _resolve_one(lab, dest) if dest else None
    if dest_hop is not None:
        _ensure_dest_outside_chain(dest_hop.hop.host, {r.hop.host for r in resolved})
    tunnel = Tunnel(
        protocol=protocol,
        service_port=port,
        path=tuple(r.hop for r in resolved),
        dest=dest_hop.hop.host if dest_hop else None,
    )
    async with _add_lock(tunnel.id):
        _check_conflicts(await discover_tunnels(lab), tunnel)
        for r in resolved:
            await _require_tools(r.host, carrier_obj)

        used = await _probe_used_ports(resolved) | {port}
        carrier_fwd = pick_free_port(used)
        carrier_rev = pick_free_port(used | {carrier_fwd})

        ips = [r.ip for r in resolved]
        deliver_fwd = dest_hop.ip if dest_hop else _LOOPBACK
        plan = _process_plan(tunnel, ips, carrier_fwd, carrier_rev, deliver_fwd, carrier_obj)

        launched = False
        try:
            for proc in plan:
                sentinel = _sentinel_for(tunnel, proc)
                host = resolved[proc.hop_index].host
                # Attempting a launch is enough to warrant rollback: the timeout
                # below bounds the ack, not the send, so the command may have
                # already reached the host even if we never see success.
                launched = True
                result = await host.exec(
                    launch_command(sentinel, proc.argv),
                    timeout=_TUNNEL_HOST_TIMEOUT,
                    log=LogMode.QUIET,
                )
                if result.timed_out:
                    _raise_launch_timeout(resolved, proc)
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
                # Shielded: a Ctrl+C landing during the rollback itself must
                # not tear it — the reap runs to completion (bounded by the
                # teardown deadline) before the cancellation continues
                # (chaos spec: shielded compensating actions).
                # Imported here, not at module scope: otto.lifecycle is only
                # needed once a compensating action actually runs, and a
                # top-level import drags it onto every CLI --help path
                # (import-budget guard).
                from ..lifecycle import compensate

                await compensate(
                    _kill_tunnel_on([r.host for r in resolved], tunnel.id),
                    what=f"tunnel {tunnel.id} rollback",
                )
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

    plan: "DryRunPlan | None" = None
    """Set when this call was a ``--dry-run``; the four fields above are then
    all EMPTY BY CONSTRUCTION rather than by measurement.

    That distinction is the whole reason the field exists. Before it, a dry-run
    remove printed ``removed (none found)`` and exited 0 — a claim about live
    processes, made without scanning a single host, and byte-identical to the
    answer a real sweep of a clean lab gives. Empty is what a reap that never
    happened LOOKS like; a renderer needs something else to branch on.

    Appended, per the rule :attr:`AddedTunnel.plan` states."""


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
        kill_cmd = kill_command(pids)
        try:
            result = await host.exec(kill_cmd, timeout=_TUNNEL_HOST_TIMEOUT, log=LogMode.QUIET)
        except Exception as e:  # noqa: BLE001 — transparent partial reap
            logger.warning(f"otto tunnel: could not reap on host {host_id!r}: {e}")
            unreachable.add(host_id)
            continue
        if result.timed_out:
            logger.warning(f"otto tunnel: timed out reaping host {host_id!r}")
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


_UNCHECKED_WHICH_TUNNELS = (
    "which tunnels are live, or whether any are. The processes on the hosts ARE the record, so "
    "a real run learns the answer from the scan above and from nowhere else — this preview "
    "cannot tell you the reap would match anything"
)

_UNCHECKED_REAP_OUTCOME = (
    "whether the kills land. A real run re-scans every host it killed on and reports any "
    "process still present as a SURVIVOR, exiting 1. It also names the hosts it could not "
    "reach — a tunnel can outlive a partial reap on exactly those"
)


def _plan_remove(lab: "Lab", *, tunnel_id: str | None) -> RemovedReport:
    """Preview :func:`remove_tunnel` / :func:`remove_all_tunnels` without contacting anything.

    What lab data alone proves is the SCOPE of the sweep, and that is the part
    worth previewing: which hosts get scanned is decided by the declared
    ``has_bash`` flag, not by anything on the wire, so a host silently excluded
    from the reap — the way a tunnel leaks un-reapable processes — is visible
    here. Everything after that is a measurement.

    Nothing else can be said, and pretending otherwise is what this replaces:
    the old dry run printed ``removed (none found)`` and exited 0, which is a
    statement about live processes on hosts nobody scanned.
    """
    scannable = sorted(h.id for h in lab.hosts.values() if getattr(h, "has_bash", False))
    target = f"tunnel {tunnel_id!r}" if tunnel_id is not None else "EVERY otto tunnel"
    hosts = ", ".join(scannable) or "<none: no lab host declares has_bash>"
    would = [
        f"scan {len(scannable)} has_bash host(s) for tagged tunnel processes: {hosts}",
        f"kill every process whose sentinel names {target} (all hops, both directions)",
        "re-scan the hosts it killed on to verify those pids are gone",
    ]
    unchecked = [_UNCHECKED_WHICH_TUNNELS, _UNCHECKED_REAP_OUTCOME]
    return RemovedReport(
        removed_ids=[],
        killed={},
        unreachable=[],
        survivors=[],
        plan=DryRunPlan(would, unchecked),
    )


async def remove_tunnel(lab: "Lab", tunnel_id: str) -> RemovedReport:
    """Reap one tunnel by id, then verify its processes are actually gone.

    Under ``--dry-run`` this returns a plan and kills nothing — see
    :attr:`RemovedReport.plan`.
    """
    if is_dry_run():
        return _plan_remove(lab, tunnel_id=tunnel_id)
    return await _reap(lab, lambda t: t.id == tunnel_id)


async def remove_all_tunnels(lab: "Lab") -> RemovedReport:
    """Reap every otto tunnel (owner-agnostic), with the same verify pass.

    Under ``--dry-run`` this returns a plan and kills nothing. The
    short-circuit is here rather than in ``_reap`` because the two entry
    points differ in exactly the thing the preview has to state — the SCOPE of
    the reap — and a predicate is not a sentence.
    """
    if is_dry_run():
        return _plan_remove(lab, tunnel_id=None)
    return await _reap(lab, lambda _t: True)
