"""Impair/repair/list orchestration — kernel qdisc state is the ONLY state.

Reads go through ``host.exec`` (no privilege needed); mutations through
``host.run(cmd, sudo=host.current_user != "root")``. Every host call is
wrapped in ``asyncio.wait_for(..., _IMPAIR_HOST_TIMEOUT)`` and a down host is
a loud, host-named ``RuntimeError`` — never a skip (spec §9, dev-VM rule).

These four functions — :func:`impair_link`, :func:`repair_link`,
:func:`repair_all`, :func:`read_link_states` — plus :func:`find_link` ARE the
public API (spec's single-API constraint): the CLI, the future GUI topology
overlay, and any direct importer call exactly these. Nothing here prints or
knows about exit codes/colors.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from ..host.detached import launch_command
from ..logger.mode import LogMode
from .impairer import LinkImpairer, build_impairer
from .model import Link
from .params import ImpairmentParams, equivalent
from .placement import (
    FlowDirection,
    Placement,
    endpoint_placements,
    ensure_not_hop_transit,
    ensure_not_local_link,
    ensure_not_mgmt,
    inpath_placements,
    parse_ip_addr,
)
from .sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel, parse_impair_ps

if TYPE_CHECKING:
    from ipaddress import IPv4Interface

    from ..config.lab import Lab

_IMPAIR_HOST_TIMEOUT = 30.0
_BOTH = frozenset({FlowDirection.A_TO_B, FlowDirection.B_TO_A})
_ADDR_SHOW_COMMAND = "ip -o addr show"


@dataclass(frozen=True, slots=True)
class AppliedPlacement:
    """One placement's post-verify impairment state."""

    placement: Placement
    """Where this impairment landed."""

    params: ImpairmentParams
    """The merged params actually verified present after the mutation."""


@dataclass(frozen=True, slots=True)
class ImpairReport:
    """Outcome of :func:`impair_link`: every placement actually mutated."""

    link_id: str
    applied: list[AppliedPlacement] = dc_field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RepairReport:
    """Outcome of :func:`repair_link`: what got cleared, and how many timers died."""

    link_id: str
    cleared: list[Placement] = dc_field(default_factory=list)
    timers_cancelled: int = 0


@dataclass(frozen=True, slots=True)
class LinkState:
    """One link's current impairment state, direction by direction (for ``list``)."""

    link: Link
    by_direction: dict[FlowDirection, ImpairmentParams | None] = dc_field(default_factory=dict)
    impairable: bool = True
    """``False`` when the link structurally can't be impaired (refusal/unnamed/etc)."""

    unreachable: bool = False
    """``True`` when at least one placement host couldn't be reached to read state."""


def find_link(lab: Any, ident: str) -> Link:
    """Resolve *ident* (a link id or its ``name``) against ``lab.static_links()``.

    Raises a :class:`ValueError` listing every known id when *ident* matches
    neither, so a typo'd CLI argument gets a usable hint.
    """
    links = lab.static_links()
    for link in links:
        if link.id == ident or (link.name is not None and link.name == ident):
            return link
    known = ", ".join(sorted(link.id for link in links)) or "<none>"
    raise ValueError(f"no link {ident!r} in the loaded lab (known: {known})")


async def _exec(host: Any, cmd: str) -> Any:
    """Run a read-only *cmd* on *host*; timeout/transport errors are host-named."""
    try:
        result = await asyncio.wait_for(host.exec(cmd, log=LogMode.QUIET), _IMPAIR_HOST_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    if not result.is_ok:
        raise RuntimeError(f"{cmd!r} failed on {host.id!r}: {result.msg or result.value}")
    return result


async def _root_run(host: Any, cmd: str) -> Any:
    """Run a mutating *cmd* on *host*, sudo'd unless already root.

    Only transport-level failures (timeout/OSError/ConnectionError) raise
    here — a command that reaches the host but reports failure is caught by
    the caller's own re-read (:func:`impair_link`'s post-apply verify,
    :func:`repair_link`'s post-clear re-read), never silently swallowed here.
    """
    need_sudo = host.current_user != "root"
    try:
        results = await asyncio.wait_for(
            host.run(cmd, sudo=need_sudo, log=LogMode.QUIET), _IMPAIR_HOST_TIMEOUT
        )
    except (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    return results[0]


def _host(lab: Any, host_id: str) -> Any:
    """Look up *host_id* in *lab*; a missing host is a rich :class:`ValueError`."""
    try:
        return lab.hosts[host_id]
    except KeyError as e:
        raise ValueError(f"link references host {host_id!r} not in the loaded lab") from e


def _impairer_for(host: Any) -> LinkImpairer:
    """Resolve *host*'s pinned impairer (spec §5/§10 — the registry round-trip)."""
    name = getattr(host, "impairer", None)
    if not name:
        raise ValueError(f"host {host.id!r} has no impairer support")
    return build_impairer(name)()


def _directions(link: Link, from_host: str | None) -> frozenset[FlowDirection]:
    """Both directions by default; ``--from`` narrows to the originating one."""
    if from_host is None:
        return _BOTH
    if from_host == link.a.host:
        return frozenset({FlowDirection.A_TO_B})
    if from_host == link.b.host:
        return frozenset({FlowDirection.B_TO_A})
    raise ValueError(
        f"--from {from_host!r} is not an endpoint of link {link.id!r} "
        f"(a={link.a.host!r}, b={link.b.host!r})"
    )


def _hop_chain_includes(lab: Any, host: Any, transit_host_id: str) -> bool:
    """Walk *host*'s ``hop`` chain; ``True`` if it passes through *transit_host_id*.

    Cycle-guarded (visited set); the built-in local id terminates a chain. A
    host with no ``hop`` (or no such attribute — duck-typed for test fakes)
    depends on nothing.
    """
    visited: set[str] = set()
    current = getattr(host, "hop", None)
    while current and current != BUILTIN_LOCAL_HOST_ID and current not in visited:
        if current == transit_host_id:
            return True
        visited.add(current)
        nxt = lab.hosts.get(current)
        current = getattr(nxt, "hop", None) if nxt is not None else None
    return False


def _hop_dependents(lab: Any, transit_host_id: str) -> list[tuple[str, str]]:
    """Every lab host whose hop CHAIN passes through *transit_host_id*, with its mgmt ip.

    These are the hosts that reach otto only by hopping through
    *transit_host_id*: impairing the facing netdev on that host would cut them
    off, so their management ip must be protected there (:func:`ensure_not_hop_transit`).
    """
    dependents: list[tuple[str, str]] = []
    for host in lab.hosts.values():
        host_id = getattr(host, "id", "")
        if host_id in ("", transit_host_id, BUILTIN_LOCAL_HOST_ID):
            continue
        ip = getattr(host, "ip", "") or ""
        if ip and _hop_chain_includes(lab, host, transit_host_id):
            dependents.append((host_id, ip))
    return dependents


async def _resolve_placements(
    lab: Any, link: Link, directions: frozenset[FlowDirection]
) -> list[Placement]:
    """Endpoint or in-path placements for *link*, refusals enforced first (spec §9).

    ``ensure_not_local_link`` runs before any host is touched. Then, in-path
    mode fetches the middlebox's addresses to place directions on the facing
    netdev; endpoint mode needs no such fetch. Finally every resulting
    placement is checked against its OWN host's management interface AND
    against any other host whose hop transit rides that netdev — caching each
    host's address table and hop-dependents so a shared host is queried once.
    """
    ensure_not_local_link(link)
    tables: dict[str, dict[str, list["IPv4Interface"]]] = {}
    if link.impair:
        middlebox = _host(lab, link.impair)
        table = parse_ip_addr((await _exec(middlebox, _ADDR_SHOW_COMMAND)).value)
        tables[link.impair] = table
        placements = inpath_placements(link, link.impair, table, directions)
    else:
        placements = endpoint_placements(link, directions)
    dependents: dict[str, list[tuple[str, str]]] = {}
    for placement in placements:
        host = _host(lab, placement.host_id)
        if placement.host_id not in tables:
            addr_output = (await _exec(host, _ADDR_SHOW_COMMAND)).value
            tables[placement.host_id] = parse_ip_addr(addr_output)
        if placement.host_id not in dependents:
            dependents[placement.host_id] = _hop_dependents(lab, placement.host_id)
        ensure_not_mgmt(placement, tables[placement.host_id], host.ip)
        ensure_not_hop_transit(placement, tables[placement.host_id], dependents[placement.host_id])
    return placements


async def _read_placement(
    host: Any, impairer: LinkImpairer, netdev: str
) -> ImpairmentParams | None:
    """Read + parse *netdev*'s current impairment on *host* (``None`` = clean)."""
    result = await _exec(host, impairer.read_command(netdev))
    return impairer.parse_read(result.value)


async def _apply_or_clear(
    host: Any, impairer: LinkImpairer, netdev: str, merged: ImpairmentParams
) -> None:
    """Replace *netdev*'s qdisc with *merged*, or clear it when *merged* is empty."""
    if merged.is_empty():
        await _root_run(host, impairer.clear_command(netdev))
    else:
        await _root_run(host, impairer.apply_command(netdev, merged))


async def _cancel_timers(host: Any, link_id: str, netdev: str) -> int:
    """Kill any live expire-timer tagged for (*link_id*, *netdev*) on *host*.

    Best-effort: a scan failure (host flaky mid-operation) returns 0 rather
    than raising — cancellation is a hygiene step, not the operation itself.
    """
    try:
        result = await _exec(host, IMPAIR_PS_COMMAND)
    except RuntimeError:
        return 0
    pids = sorted(
        pid for pid, lid, dev in parse_impair_ps(result.value) if lid == link_id and dev == netdev
    )
    if not pids:
        return 0
    await _root_run(host, f"kill {' '.join(str(pid) for pid in pids)}")
    return len(pids)


async def _launch_timer(
    host: Any, link: Link, placement: Placement, impairer: LinkImpairer, expire: int
) -> None:
    """Launch a detached, sentinel-tagged timer that clears *placement* after *expire*s."""
    sentinel = encode_impair_sentinel(link.id, placement.netdev)
    argv = ["bash", "-c", f"sleep {int(expire)} && {impairer.clear_command(placement.netdev)}"]
    await _root_run(host, launch_command(sentinel, argv))


_RollbackEntry = tuple[Placement, Any, LinkImpairer, ImpairmentParams | None]


async def _rollback(link_id: str, entries: list[_RollbackEntry]) -> None:
    """Best-effort restoration of already-applied placements after a mid-way failure.

    Restores in reverse application order: prior params re-applied where they
    existed, cleared where there was nothing before. Any timer this run may
    have launched on the placement is cancelled first, matching the ordinary
    cancel-before-mutate invariant. One placement's restore failing must not
    stop the others from being attempted.
    """
    for placement, host, impairer, prior in reversed(entries):
        with contextlib.suppress(Exception):
            await _cancel_timers(host, link_id, placement.netdev)
            restore = prior if prior is not None else ImpairmentParams()
            await _apply_or_clear(host, impairer, placement.netdev, restore)


def _describe_state(params: ImpairmentParams | None) -> str:
    """Human summary of a placement state for error text (``None`` = clean)."""
    return params.describe() if params is not None else "clean"


def _raise_verify_mismatch(
    host: Any,
    placement: Placement,
    expected: ImpairmentParams | None,
    observed: ImpairmentParams | None,
) -> None:
    """Raise for a post-apply verify mismatch (TRY301: kept out of the try body)."""
    raise RuntimeError(
        f"post-apply verify failed on {host.id}/{placement.netdev}: "
        f"expected [{_describe_state(expected)}], observed [{_describe_state(observed)}]"
    )


async def impair_link(
    lab: "Lab",
    ident: str,
    params: ImpairmentParams,
    *,
    from_host: str | None = None,
    expire: int | None = None,
) -> ImpairReport:
    """Impair link *ident* with *params* (merge-read-modify-replace, verified).

    *params* merges over each placement's CURRENTLY-applied state
    (:meth:`~otto.link.params.ImpairmentParams.merged_over`) — a bare re-impair layers onto
    what's already there, an explicit zero clears just that one param. Every
    placement's existing expire-timer is cancelled before the mutation runs;
    a fresh one is launched after a successful verify when *expire* is given.

    ``--from`` (*from_host*) narrows endpoint mode to the direction
    originating at that host; omitted, both directions are impaired. In-path
    links (``link.impair`` set) ignore endpoint selection and always place on
    the middlebox's facing interfaces.

    No half-impairments: if any placement fails mid-way (mutation doesn't
    verify, host unreachable, etc.), every placement touched in this call —
    INCLUDING the one whose own mutation just failed — is restored to its
    PRIOR state before the error propagates.
    """
    link = find_link(lab, ident)
    directions = _directions(link, from_host)
    placements = await _resolve_placements(lab, link, directions)

    applied: list[AppliedPlacement] = []
    rollback_entries: list[_RollbackEntry] = []
    try:
        for placement in placements:
            host = _host(lab, placement.host_id)
            impairer = _impairer_for(host)
            await _cancel_timers(host, link.id, placement.netdev)
            prior = await _read_placement(host, impairer, placement.netdev)
            # Register the rollback entry BEFORE mutating: a verify or timer
            # failure on THIS placement must roll its own just-applied mutation
            # back too, not only the earlier placements' (final-review 2026-07-10).
            rollback_entries.append((placement, host, impairer, prior))
            base = prior if prior is not None else ImpairmentParams()
            merged = params.merged_over(base)
            merged.validate()
            await _apply_or_clear(host, impairer, placement.netdev, merged)
            observed = await _read_placement(host, impairer, placement.netdev)
            expected = None if merged.is_empty() else merged
            # tc canonicalizes on display, so `observed` may spell the same
            # impairment differently than `expected`; compare by MEANING.
            observed_params = observed if observed is not None else ImpairmentParams()
            expected_params = expected if expected is not None else ImpairmentParams()
            if not equivalent(observed_params, expected_params):
                _raise_verify_mismatch(host, placement, expected, observed)
            if expire is not None:
                await _launch_timer(host, link, placement, impairer, expire)
            applied.append(AppliedPlacement(placement, merged))
    except Exception:
        await _rollback(link.id, rollback_entries)
        raise
    return ImpairReport(link.id, applied)


async def repair_link(lab: "Lab", ident: str) -> RepairReport:
    """Clear every impaired placement of link *ident* and cancel its timers.

    Unlike :func:`impair_link`, a clear is unconditional per placement that
    currently has ANY impairment present — no merge. The ``tc qdisc del`` is
    still verified by a post-clear re-read: a clear that silently didn't take
    (state still present) is a loud, host-named failure, never reported as
    ``cleared``.
    """
    link = find_link(lab, ident)
    directions = _directions(link, None)
    placements = await _resolve_placements(lab, link, directions)

    cleared: list[Placement] = []
    timers_cancelled = 0
    for placement in placements:
        host = _host(lab, placement.host_id)
        impairer = _impairer_for(host)
        timers_cancelled += await _cancel_timers(host, link.id, placement.netdev)
        current = await _read_placement(host, impairer, placement.netdev)
        if current is not None:
            await _root_run(host, impairer.clear_command(placement.netdev))
            # _root_run ignores command-level failure, so re-read: a clear that
            # silently didn't take (still parses as impaired) is a loud,
            # host-named failure, never reported as `cleared` (final-review 2026-07-10).
            still = await _read_placement(host, impairer, placement.netdev)
            if still is not None:
                raise RuntimeError(
                    f"repair failed on {host.id}/{placement.netdev}: impairment still present"
                )
            cleared.append(placement)
    return RepairReport(link.id, cleared, timers_cancelled)


async def repair_all(lab: "Lab") -> tuple[list[RepairReport], list[str]]:
    """Repair every static link in *lab*; never raises.

    A link that structurally can't be impaired (:class:`ValueError` — no
    named interface, local-link, mgmt refusal, ...) is silently skipped: it
    was never impaired in the first place. A link whose repair fails for a
    live reason (:class:`RuntimeError` — host down, command failed) is
    collected into *failures* instead of aborting the rest.
    """
    reports: list[RepairReport] = []
    failures: list[str] = []
    for link in lab.static_links():
        try:
            reports.append(await repair_link(lab, link.id))
        except ValueError:  # noqa: PERF203 — per-item resilience
            continue
        except RuntimeError as e:
            failures.append(f"{link.id}: {e}")
    return reports, failures


async def _link_state(lab: Any, link: Link) -> LinkState:
    """Read one link's impairment state.

    Structural refusals and unreachable hosts are reported as flags, never
    raised (spec §9 — ``list`` never dies).
    """
    try:
        placements = await _resolve_placements(lab, link, _BOTH)
        by_direction: dict[FlowDirection, ImpairmentParams | None] = {}
        unreachable = False
        for placement in placements:
            host = _host(lab, placement.host_id)
            impairer = _impairer_for(host)
            try:
                by_direction[placement.direction] = await _read_placement(
                    host, impairer, placement.netdev
                )
            except RuntimeError:
                unreachable = True
                by_direction[placement.direction] = None
        return LinkState(link, by_direction, impairable=True, unreachable=unreachable)
    except ValueError:
        return LinkState(link, {}, impairable=False, unreachable=False)
    except RuntimeError:
        return LinkState(link, {}, impairable=True, unreachable=True)


async def read_link_states(lab: "Lab") -> list[LinkState]:
    """Read the current impairment state of every static link.

    This is the ``list``/GUI-overlay feed.

    Reads only (``exec``, no sudo); never raises per-link, so one bad host
    can't hide the rest of the fleet's state from a caller like ``otto link
    list`` or a topology overlay.
    """
    return [await _link_state(lab, link) for link in lab.static_links()]
