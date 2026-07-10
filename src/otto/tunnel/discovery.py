"""Live tunnel discovery — the processes on the hosts ARE the record (spec §9).

``_scan_hosts`` gathers :data:`otto.tunnel.socat.DISCOVERY_PS_COMMAND` across
hosts (best-effort, bounded, transparent about unreachables);
``parse_process_discovery`` decodes each tagged process; and
``discover_tunnels`` groups observations by tunnel id, comparing what was
observed against what the sentinel-encoded path says must exist.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logger import get_logger
from ..logger.mode import LogMode
from .model import ProcKey, Tunnel
from .sentinel import SENTINEL_PREFIX, ParsedSentinel, parse_sentinel
from .socat import DISCOVERY_PS_COMMAND

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

logger = get_logger()

_TUNNEL_HOST_TIMEOUT = 30.0
"""Ceiling on any single-host ``oneshot`` on the discovery path (spec §6.4)."""

_ETIME_MAX_FIELDS = 3
_PS_MIN_FIELDS = 3


def parse_etime(text: str) -> int:
    """Procps ``etime`` (``[[DD-]HH:]MM:SS`` or bare ``SS``) → seconds.

    Returns ``0`` for anything unparseable rather than raising — one host
    emitting a malformed ``etime`` must not take down the whole scan.
    """
    try:
        days = 0
        if "-" in text:
            d, _, text = text.partition("-")
            days = int(d)
        parts = [int(p) for p in text.split(":")]
        while len(parts) < _ETIME_MAX_FIELDS:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return days * 86400 + h * 3600 + m * 60 + s
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class Observation:
    """One tagged tunnel process seen on one host."""

    pid: int
    age_seconds: int
    parsed: ParsedSentinel


def parse_process_discovery(ps_output: str) -> list[Observation]:
    """Reconstruct observations from ``ps -eo pid=,etime=,args=`` output."""
    out: list[Observation] = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < _PS_MIN_FIELDS or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(f"{SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        parsed = parse_sentinel(token)
        if parsed is None:
            continue
        out.append(
            Observation(pid=int(fields[0]), age_seconds=parse_etime(fields[1]), parsed=parsed)
        )
    return out


async def _scan_hosts(hosts: list[Any]) -> tuple[list[tuple[str, Observation]], list[str]]:
    """Gather the discovery command over *hosts*; best-effort + transparent.

    Returns ``(observations_by_origin, unreachable_host_ids)``. Also the
    verify primitive for the manage layer, which scans just a chain's hosts.
    """

    async def scan(host: Any) -> tuple[list[tuple[str, Observation]], str | None]:
        try:
            result = await asyncio.wait_for(
                host.oneshot(DISCOVERY_PS_COMMAND, log=LogMode.QUIET), _TUNNEL_HOST_TIMEOUT
            )
            observed = parse_process_discovery(result.value)
        except asyncio.TimeoutError:
            logger.warning(f"otto tunnel: timed out scanning host {host.id!r}")
            return [], host.id
        except Exception as e:  # noqa: BLE001 — best-effort scan; name + skip
            logger.warning(f"otto tunnel: could not scan host {host.id!r}: {e}")
            return [], host.id
        return [(host.id, obs) for obs in observed], None

    gathered = await asyncio.gather(*(scan(h) for h in hosts))
    observations = [pair for host_pairs, _u in gathered for pair in host_pairs]
    unreachable = [host_id for _pairs, host_id in gathered if host_id is not None]
    return observations, unreachable


async def discover_observations(lab: "Lab") -> tuple[list[tuple[str, Observation]], list[str]]:
    """Every tagged tunnel process across the lab's ``has_bash`` hosts."""
    hosts = [h for h in lab.hosts.values() if getattr(h, "has_bash", False)]
    return await _scan_hosts(hosts)


@dataclass(frozen=True, slots=True)
class DiscoveredTunnel:
    """One live tunnel: intended shape + what was actually observed."""

    tunnel: Tunnel
    present: set[ProcKey]
    missing: set[ProcKey]
    """Expected-but-absent processes on hosts that WERE scanned. Absence on
    an unreachable host is unknown, not missing (spec §9)."""
    age_seconds: int
    """Oldest observed process age (max etime) — the tunnel's creation age."""
    uncertain: bool
    """True when >=1 chain host was unreachable during the scan."""

    @property
    def status(self) -> str:
        """``ok`` / ``degraded (<present>/<expected>)``, ``?``-suffixed if uncertain."""
        expected = len(self.tunnel.expected_processes())
        base = "ok" if not self.missing else f"degraded ({len(self.present)}/{expected})"
        return f"{base}?" if self.uncertain else base


@dataclass(frozen=True, slots=True)
class TunnelDiscovery:
    """A full scan: the tunnels seen plus the hosts that couldn't be scanned."""

    tunnels: list[DiscoveredTunnel]
    unreachable: list[str]


def group_observations(
    observations: list[tuple[str, Observation]], unreachable: list[str]
) -> list[DiscoveredTunnel]:
    """Group per-host observations by tunnel id and compute per-tunnel status."""
    unreachable_set = set(unreachable)
    by_id: dict[str, list[tuple[str, Observation]]] = {}
    for origin, obs in observations:
        by_id.setdefault(obs.parsed.tunnel.id, []).append((origin, obs))
    out: list[DiscoveredTunnel] = []
    for _tid, group in sorted(by_id.items()):
        tunnel = group[0][1].parsed.tunnel
        present: set[ProcKey] = {(origin, o.parsed.direction, o.parsed.role) for origin, o in group}
        expected = tunnel.expected_processes()
        missing = {k for k in expected - present if k[0] not in unreachable_set}
        chain_hosts = {hop.host for hop in tunnel.path}
        out.append(
            DiscoveredTunnel(
                tunnel=tunnel,
                present=present,
                missing=missing,
                age_seconds=max(o.age_seconds for _origin, o in group),
                uncertain=bool(chain_hosts & unreachable_set),
            )
        )
    return out


async def discover_tunnels(lab: "Lab") -> TunnelDiscovery:
    """Discover live otto tunnels across the lab (the monitor-facing surface)."""
    observations, unreachable = await discover_observations(lab)
    return TunnelDiscovery(
        tunnels=group_observations(observations, unreachable), unreachable=unreachable
    )
