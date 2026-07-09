"""Dynamic-link discovery + the all-provenance accessor.

Cost-split by design (foundation spec §6): ``Lab.static_links()`` is free and
synchronous; everything here is async because the dynamic layer costs one
round-trip per lab host. :func:`discover_observations` runs an
``asyncio.gather`` of :data:`otto.link.socat.DISCOVERY_PS_COMMAND` across the
lab's tunnel-hosting hosts (best-effort, spec §10); :func:`parse_process_discovery`
reconstructs each tagged process into an :class:`Observation`; and
:func:`discover_dynamic_links` groups those observations by id into ``Link``
objects (spec §8).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logger import get_logger
from .model import Link, LinkEndpoint
from .sentinel import SENTINEL_PREFIX, parse_sentinel

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

logger = get_logger()


@dataclass(frozen=True, slots=True)
class Observation:
    """One tagged tunnel process seen on one host."""

    pid: int
    age_seconds: int
    link: Link


_ETIME_MAX_FIELDS = 3
"""``etime`` pads out to ``HH:MM:SS`` (3 colon-joined fields) before slicing."""

_PS_MIN_FIELDS = 3
"""A matched ``ps`` line needs at least pid, etime, and one argv word."""


def parse_etime(text: str) -> int:
    """Procps ``etime`` (``[[DD-]HH:]MM:SS`` or bare ``SS``) → seconds.

    Returns ``0`` for anything unparseable rather than raising — one host
    emitting a malformed ``etime`` must not take down the whole discovery
    gather (spec §10).
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


def parse_process_discovery(ps_output: str) -> list[Observation]:
    """Reconstruct per-process observations from ``ps -eo pid=,etime=,args=``.

    Each matched line is ``<pid> <etime> <argv…>`` where the sentinel is a word
    in argv (``exec -a`` put it at ``argv[0]``). Non-otto lines are ignored.
    """
    out: list[Observation] = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < _PS_MIN_FIELDS or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(f"{SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        link = parse_sentinel(token)
        if link is None:
            continue
        out.append(Observation(pid=int(fields[0]), age_seconds=parse_etime(fields[1]), link=link))
    return out


DiscoverFn = Callable[["Lab"], Awaitable[list[Link]]]

_LINK_HOST_TIMEOUT = 30.0
"""Ceiling on any single-host ``oneshot`` on the discovery path. A wedged host
must not hang the whole scan — a timeout is treated exactly like a connection
failure: warn, skip, name it unreachable (spec §10)."""


async def discover_observations(lab: "Lab") -> tuple[list[tuple[str, Observation]], list[str]]:
    """Every tagged tunnel process across the lab's tunnel-hosting hosts.

    Scanned hosts are filtered on the ``has_bash`` capability, not a nominal
    ``isinstance`` type check — the socat tagging shells out via
    ``bash -c 'exec -a …'``, so only hosts that declare (or default to) a
    working bash are scanned. A host with no ``has_bash`` attribute at all is
    treated as ``False``.

    Best-effort + transparent (spec §10): an unreachable host (connection
    failure, timeout, or an unparseable response) is warned about by name and
    skipped, never silently dropped and never fatal to the scan.

    Returns ``(observations, unreachable_host_ids)`` — the second element
    lets callers (``_reap``, the ``list`` CLI) report a partial scan instead
    of silently losing hosts that never contributed an observation.
    """
    import asyncio

    from ..logger.mode import LogMode
    from .socat import DISCOVERY_PS_COMMAND

    hosts = [h for h in lab.hosts.values() if getattr(h, "has_bash", False)]

    async def scan(host: Any) -> tuple[list[tuple[str, Observation]], str | None]:
        try:
            result = await asyncio.wait_for(
                host.oneshot(DISCOVERY_PS_COMMAND, log=LogMode.QUIET), _LINK_HOST_TIMEOUT
            )
            observed = parse_process_discovery(result.value)
        except asyncio.TimeoutError:
            logger.warning(f"otto link: timed out scanning host {host.id!r} for tunnels")
            return [], host.id
        except Exception as e:  # noqa: BLE001 — best-effort scan; name + skip
            logger.warning(f"otto link: could not scan host {host.id!r} for tunnels: {e}")
            return [], host.id
        return [(host.id, obs) for obs in observed], None

    gathered = await asyncio.gather(*(scan(h) for h in hosts))
    observations = [pair for host_pairs, _unreachable in gathered for pair in host_pairs]
    unreachable = [host_id for _pairs, host_id in gathered if host_id is not None]
    return observations, unreachable


def _group_and_resolve(observations: list[tuple[str, Observation]], lab: "Lab") -> list[Link]:
    """Group per-host observations by id, filling endpoint ips from *lab*.

    Shared by :func:`discover_dynamic_links` and
    :func:`discover_dynamic_links_status` so the grouping/ip-resolve logic
    lives in exactly one place.
    """
    by_id: dict[str, Link] = {}
    for _origin, obs in observations:
        by_id.setdefault(obs.link.id, obs.link)
    return [_resolve_link_ips(link, lab) for link in by_id.values()]


async def discover_dynamic_links(lab: "Lab") -> list[Link]:
    """Discover live otto tunnels across the lab's Unix hosts (spec §8).

    Groups per-host observations by id into one ``Link`` per tunnel, filling
    endpoint ips from the live lab hosts. Frozen signature (``-> list[Link]``)
    — a host that went unreachable during the scan is silently absorbed here;
    use :func:`discover_dynamic_links_status` when the caller needs to report
    a partial scan.
    """
    observations, _unreachable = await discover_observations(lab)
    return _group_and_resolve(observations, lab)


async def discover_dynamic_links_status(lab: "Lab") -> tuple[list[Link], list[str]]:
    """Like :func:`discover_dynamic_links`, plus the unreachable host ids.

    For callers (``otto link list``) that must mark a partial scan (spec §10)
    rather than silently drop the hosts that couldn't be reached.
    """
    observations, unreachable = await discover_observations(lab)
    return _group_and_resolve(observations, lab), unreachable


def _resolve_link_ips(link: Link, lab: "Lab") -> Link:
    """Fill each endpoint's ip from the lab host it names.

    The sentinel carries ids + ifaces but empty ips.
    """
    from dataclasses import replace

    def ip_for(ep: LinkEndpoint) -> LinkEndpoint:
        host = lab.hosts.get(ep.host)
        if host is None:
            return ep
        ifaces = getattr(host, "interfaces", {}) or {}
        raw = ifaces.get(ep.interface) if ep.interface else None
        ip = (raw if isinstance(raw, str) else getattr(raw, "ip", None)) or getattr(host, "ip", "")
        return replace(ep, ip=ip)

    return replace(link, a=ip_for(link.a), b=ip_for(link.b))


async def all_links(lab: "Lab", *, discover: DiscoverFn = discover_dynamic_links) -> list[Link]:
    """Every link across provenances, merged by route id.

    Static (implicit and declared) plus dynamic, deduplicated by id. Dynamic
    ids (``lnk-<hex>-<port>``) and static ids (``name``/``a--b``) are disjoint
    id-spaces, so an observed tunnel and the declared route it realizes
    coexist as separate entries — the merge here only dedups a genuine
    same-id duplicate (e.g. a repeated ``discover`` call). *discover* is
    injectable for tests and for #2's live layer.
    """
    merged = {link.id: link for link in lab.static_links()}
    for link in await discover(lab):
        merged[link.id] = link
    return list(merged.values())
