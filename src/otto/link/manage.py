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
from ..host.daemon import kill_command, launch_command
from ..logger.mode import LogMode
from .impairer import FIRST_SELECTOR_BAND, MAX_SELECTORS, LinkImpairer, ScopedState, build_impairer
from .model import Link
from .params import ImpairmentParams, Selector, equivalent
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
from .sentinel import (
    IMPAIR_PS_COMMAND,
    encode_impair_sentinel,
    encode_impair_sentinel_v2,
    parse_impair_ps,
)

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

    selector: Selector | None = None
    """Set when this was a port-scoped application (``--port``)."""


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
class DirectionState:
    """One direction's full impairment shape (the ``list``/GUI read feed).

    At most one of :attr:`whole` / :attr:`scoped` / :attr:`foreign` is
    populated (whole-link and port-scoped are exclusive per netdev in v1;
    a foreign tree is opaque). All three empty = clean.
    """

    whole: ImpairmentParams | None = None
    scoped: dict[Selector, ImpairmentParams] = dc_field(default_factory=dict)
    foreign: bool = False


@dataclass(frozen=True, slots=True)
class LinkState:
    """One link's current impairment state, direction by direction (for ``list``)."""

    link: Link
    by_direction: dict[FlowDirection, "DirectionState | None"] = dc_field(default_factory=dict)
    """Per-direction shape; ``None`` = that direction's host couldn't be read."""

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


async def _read_state(host: Any, impairer: LinkImpairer, netdev: str) -> ScopedState:
    """Read *netdev*'s full impairment shape on *host* as a :class:`ScopedState`.

    Scoped-capable impairers read qdisc + filters and discriminate all four
    kinds; legacy impairers keep their single-command read and can only ever
    report ``clean`` or ``whole`` (their read contract predates selectors).
    """
    if impairer.supports_selectors:
        qdisc_cmd, filter_cmd = impairer.scoped_read_commands(netdev)
        qdisc_out = (await _exec(host, qdisc_cmd)).value
        filter_out = (await _exec(host, filter_cmd)).value
        return impairer.parse_scoped(qdisc_out, filter_out)
    params = impairer.parse_read((await _exec(host, impairer.read_command(netdev))).value)
    return ScopedState.whole_link(params) if params is not None else ScopedState.clean()


def _ensure_not_foreign(host: Any, netdev: str, state: ScopedState) -> None:
    """Refuse to mutate a root qdisc otto did not generate (spec §1)."""
    if state.kind == "foreign":
        raise RuntimeError(
            f"{host.id}/{netdev} has a foreign qdisc otto did not create — "
            "refusing to modify it (clear it manually with tc if it is expendable)"
        )


async def _apply_or_clear(
    host: Any, impairer: LinkImpairer, netdev: str, merged: ImpairmentParams
) -> None:
    """Replace *netdev*'s qdisc with *merged*, or clear it when *merged* is empty."""
    if merged.is_empty():
        await _root_run(host, impairer.clear_command(netdev))
    else:
        await _root_run(host, impairer.apply_command(netdev, merged))


async def _cancel_timers(
    host: Any,
    link_id: str,
    netdev: str,
    *,
    selector: Selector | None = None,
    everything: bool = False,
) -> int:
    """Kill live expire-timers for (*link_id*, *netdev*) on *host*, scoped.

    ``everything=True`` reaps every v1 AND v2 timer (bare repair). Otherwise
    ``selector=None`` matches only v1 whole-link timers (today's exact
    semantics — scoped state can't hold v1 timers, exclusivity guarantees
    it) and ``selector=S`` matches only S's own v2 timer. Best-effort: a
    scan failure returns 0 rather than raising — cancellation is a hygiene
    step, not the operation itself.
    """
    try:
        result = await _exec(host, IMPAIR_PS_COMMAND)
    except RuntimeError:
        return 0
    pids = [
        t.pid
        for t in parse_impair_ps(result.value)
        if t.link_id == link_id and t.netdev == netdev and (everything or t.selector == selector)
    ]
    if not pids:
        return 0
    await _root_run(host, kill_command(pids))
    return len(pids)


async def _launch_timer(
    host: Any, link: Link, placement: Placement, impairer: LinkImpairer, expire: int
) -> None:
    """Launch a detached, sentinel-tagged timer that clears *placement* after *expire*s."""
    sentinel = encode_impair_sentinel(link.id, placement.netdev)
    argv = ["bash", "-c", f"sleep {int(expire)} && {impairer.clear_command(placement.netdev)}"]
    await _root_run(host, launch_command(sentinel, argv))


def _assign_band(link_id: str, host: Any, netdev: str, state: ScopedState) -> int:
    """Lowest free selector band; a full tree is a loud cap error (spec §1)."""
    used = {band for band, _params in state.selectors.values()}
    for band in range(FIRST_SELECTOR_BAND, FIRST_SELECTOR_BAND + MAX_SELECTORS):
        if band not in used:
            return band
    raise ValueError(
        f"link {link_id} already has {MAX_SELECTORS} port-scoped impairments on "
        f"{host.id}/{netdev} (limit {MAX_SELECTORS}) — repair one first"
    )


def _ensure_selector_capable(host: Any, impairer: LinkImpairer) -> None:
    """--port routed to a non-supporting impairer is a loud capability error."""
    if not impairer.supports_selectors:
        name = getattr(host, "impairer", None) or type(impairer).__name__
        raise ValueError(
            f"impairer {name!r} does not support port-scoped impairment (--port); "
            f"host {host.id!r} needs a selector-capable impairer"
        )


async def _launch_selector_timer(
    host: Any,
    link: Link,
    placement: Placement,
    impairer: LinkImpairer,
    selector: Selector,
    band: int,
    expire: int,
) -> None:
    """Detached v2 timer clearing one selector after *expire* seconds.

    The timer can't know whether it will be the LAST selector when it fires,
    so the script ends with a conditional root cleanup: if no filters remain
    under the scoped root, delete the root — restoring pristine, per spec §2
    'clearing the last selector deletes the root'.
    """
    sentinel = encode_impair_sentinel_v2(link.id, placement.netdev, selector)
    clear_seq = " && ".join(
        impairer.scoped_clear_selector_commands(placement.netdev, band, selector)
    )
    filter_show = impairer.scoped_read_commands(placement.netdev)[1]
    root_del = impairer.clear_command(placement.netdev)
    script = (
        f'sleep {int(expire)} && {clear_seq} && if [ -z "$({filter_show})" ]; then {root_del}; fi'
    )
    await _root_run(host, launch_command(sentinel, ["bash", "-c", script]))


def _expected_scoped_mapping(
    state: ScopedState, selector: Selector, merged: ImpairmentParams
) -> dict[Selector, ImpairmentParams]:
    """Build the post-mutation selector->params mapping the verify re-read must show."""
    expected = {sel: params for sel, (_band, params) in state.selectors.items()}
    if merged.is_empty():
        expected.pop(selector, None)
    else:
        expected[selector] = merged
    return expected


def _verify_scoped(
    host: Any,
    placement: Placement,
    expected: dict[Selector, ImpairmentParams],
    observed: ScopedState,
) -> None:
    """Post-apply verify for a scoped mutation: same selectors, equivalent params."""
    observed_map = {sel: params for sel, (_band, params) in observed.selectors.items()}
    ok = (
        (observed.kind == "scoped" or (observed.kind == "clean" and not expected))
        and set(observed_map) == set(expected)
        and all(equivalent(observed_map[sel], expected[sel]) for sel in expected)
    )
    if not ok:
        exp_text = ", ".join(f"{s.describe()} [{p.describe()}]" for s, p in expected.items()) or (
            "clean"
        )
        obs_text = (
            ", ".join(f"{s.describe()} [{p.describe()}]" for s, p in observed_map.items())
            or observed.kind
        )
        raise RuntimeError(
            f"post-apply verify failed on {host.id}/{placement.netdev}: "
            f"expected [{exp_text}], observed [{obs_text}]"
        )


async def _apply_selector(
    host: Any,
    link: Link,
    placement: Placement,
    impairer: LinkImpairer,
    state: ScopedState,
    selector: Selector,
    merged: ImpairmentParams,
) -> int | None:
    """One selector's mutation on one placement (state already exclusivity-checked).

    Returns the band the selector landed in, or ``None`` when the call was a
    clear (merged-to-empty). The caller launches any expire timer AFTER its
    own verify succeeds — the fresh-timer-only-after-verify invariant is
    today's rule, unchanged.
    """
    netdev = placement.netdev
    prior = state.selectors.get(selector)
    if merged.is_empty():
        if prior is None:
            return None
        if len(state.selectors) == 1:
            await _root_run(host, impairer.clear_command(netdev))
        else:
            for cmd in impairer.scoped_clear_selector_commands(netdev, prior[0], selector):
                await _root_run(host, cmd)
        return None
    band = prior[0] if prior is not None else _assign_band(link.id, host, netdev, state)
    if state.kind == "clean":
        await _root_run(host, impairer.scoped_root_command(netdev))
    await _root_run(host, impairer.scoped_band_command(netdev, band, merged))
    if prior is None:
        for cmd in impairer.scoped_filter_commands(netdev, band, selector):
            await _root_run(host, cmd)
    return band


_RollbackEntry = tuple[Placement, Any, LinkImpairer, ScopedState]


async def _restore_state(
    host: Any, impairer: LinkImpairer, netdev: str, state: ScopedState
) -> None:
    """Rebuild *netdev* to exactly *state* (clean / whole params / full scoped mapping)."""
    if state.kind == "whole" and state.whole is not None:
        await _root_run(host, impairer.apply_command(netdev, state.whole))
        return
    await _root_run(host, impairer.clear_command(netdev))
    if state.kind != "scoped":
        return
    await _root_run(host, impairer.scoped_root_command(netdev))
    for selector, (band, params) in state.selectors.items():
        await _root_run(host, impairer.scoped_band_command(netdev, band, params))
        for cmd in impairer.scoped_filter_commands(netdev, band, selector):
            await _root_run(host, cmd)


async def _rollback(
    link_id: str, entries: list[_RollbackEntry], *, selector: Selector | None
) -> None:
    """Best-effort restoration of already-applied placements after a mid-way failure.

    Restores in reverse application order to each placement's full pre-call
    shape — clean, whole-link params, or a complete scoped mapping. Any timer
    this run may have launched on the placement is cancelled first, matching
    the ordinary cancel-before-mutate invariant — scoped to the SAME
    *selector* the run's own pre-mutation cancel used (spec: a bare run only
    ever owns v1 timers, a scoped run only ever owns its own selector's v2
    timer), so a sibling selector's still-live expire timer is left running.

    Note the inherent, acceptable race this leaves: if a sibling's detached
    timer fires between this run's read and its verify, the post-apply
    verify will observe the sibling's now-cleared state, fail, and this
    rollback will resurrect the selector that just legitimately expired —
    loud (a verify-mismatch RuntimeError), vanishingly unlikely, and
    unavoidable under the kernel-qdisc-is-the-only-state model (no locking
    primitive spans "read state" and "verify state" across a detached timer).

    One placement's restore failing must not stop the others from being
    attempted.
    """
    for placement, host, impairer, prior in reversed(entries):
        with contextlib.suppress(Exception):
            await _cancel_timers(host, link_id, placement.netdev, selector=selector)
            await _restore_state(host, impairer, placement.netdev, prior)


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


def _raise_scoped_exclusivity(link_id: str) -> None:
    """Raise for a bare impair hitting port-scoped state (TRY301: kept out of the try body)."""
    raise ValueError(
        f"link {link_id} has port-scoped impairments — repair them first or impair with --port"
    )


def _raise_whole_link_exclusivity(link_id: str) -> None:
    """Raise for a scoped impair hitting whole-link state (TRY301: kept out of the try body)."""
    raise ValueError(f"link {link_id} has a whole-link impairment — repair it first")


async def impair_link(
    lab: "Lab",
    ident: str,
    params: ImpairmentParams,
    *,
    from_host: str | None = None,
    expire: int | None = None,
    selector: Selector | None = None,
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

    *selector* (``--port``) routes the mutation through the port-scoped path
    instead: *params* merges over just THAT selector's currently-applied
    state (not the whole netdev's), landing in its own prio band (assigned on
    first use, kept across re-impairs, capped at :data:`~otto.link.impairer.MAX_SELECTORS`
    per netdev) with its own pair of u32 filters. Whole-link and port-scoped
    impairment are exclusive per netdev (spec §1): a bare impair against
    scoped state, or a scoped impair against whole-link state, is a loud
    :class:`ValueError` telling the operator to repair first. A host whose
    impairer doesn't declare :attr:`~otto.link.impairer.LinkImpairer.supports_selectors`
    is also a loud capability error — never a silent fallback to whole-link.
    Expire-timers follow the same split: a bare impair only ever cancels/launches
    v1 whole-link timers; a scoped impair only ever cancels/launches its OWN
    selector's v2 timer, leaving every other selector's timer (and any v1
    timer, which scoped state can't have anyway) untouched.

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
            if selector is not None:
                _ensure_selector_capable(host, impairer)
            await _cancel_timers(host, link.id, placement.netdev, selector=selector)
            state = await _read_state(host, impairer, placement.netdev)
            _ensure_not_foreign(host, placement.netdev, state)
            if selector is None and state.kind == "scoped":
                _raise_scoped_exclusivity(link.id)
            if selector is not None and state.kind == "whole":
                _raise_whole_link_exclusivity(link.id)
            # Register the rollback entry BEFORE mutating: a verify or timer
            # failure on THIS placement must roll its own just-applied mutation
            # back too, not only the earlier placements' (final-review 2026-07-10).
            rollback_entries.append((placement, host, impairer, state))
            if selector is not None:
                prior_entry = state.selectors.get(selector)
                base = prior_entry[1] if prior_entry is not None else ImpairmentParams()
                merged = params.merged_over(base)
                merged.validate()
                band = await _apply_selector(
                    host, link, placement, impairer, state, selector, merged
                )
                expected_map = _expected_scoped_mapping(state, selector, merged)
                observed_state = await _read_state(host, impairer, placement.netdev)
                _verify_scoped(host, placement, expected_map, observed_state)
                if expire is not None and band is not None:
                    await _launch_selector_timer(
                        host, link, placement, impairer, selector, band, expire
                    )
                applied.append(AppliedPlacement(placement, merged, selector))
                continue
            base = state.whole if state.whole is not None else ImpairmentParams()
            merged = params.merged_over(base)
            merged.validate()
            await _apply_or_clear(host, impairer, placement.netdev, merged)
            observed_state = await _read_state(host, impairer, placement.netdev)
            observed = observed_state.whole
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
        await _rollback(link.id, rollback_entries, selector=selector)
        raise
    return ImpairReport(link.id, applied)


async def repair_link(lab: "Lab", ident: str, *, selector: Selector | None = None) -> RepairReport:
    """Clear link *ident*'s impairment state and cancel its timers.

    Bare (``selector=None``): clears EVERYTHING per placement that has any
    otto state — whole-link or the entire scoped tree, each a single root
    delete — and cancels every v1 and v2 timer. With *selector*: clears just
    that selector (deleting the root when it is the last one) and cancels
    only its own v2 timer; a selector that isn't present clears nothing.

    Every clear is verified by a post-clear re-read: a clear that silently
    didn't take is a loud, host-named failure, never reported as ``cleared``.
    """
    link = find_link(lab, ident)
    directions = _directions(link, None)
    placements = await _resolve_placements(lab, link, directions)

    cleared: list[Placement] = []
    timers_cancelled = 0
    for placement in placements:
        host = _host(lab, placement.host_id)
        impairer = _impairer_for(host)
        if selector is not None:
            _ensure_selector_capable(host, impairer)
        timers_cancelled += await _cancel_timers(
            host, link.id, placement.netdev, selector=selector, everything=selector is None
        )
        state = await _read_state(host, impairer, placement.netdev)
        _ensure_not_foreign(host, placement.netdev, state)
        if selector is None:
            if state.kind == "clean":
                continue
            await _root_run(host, impairer.clear_command(placement.netdev))
            still = await _read_state(host, impairer, placement.netdev)
            if still.kind != "clean":
                raise RuntimeError(
                    f"repair failed on {host.id}/{placement.netdev}: impairment still present"
                )
            cleared.append(placement)
            continue
        if state.kind == "whole":
            raise ValueError(
                f"link {link.id} has a whole-link impairment — repair it without --port"
            )
        entry = state.selectors.get(selector)
        if entry is None:
            continue
        if len(state.selectors) == 1:
            await _root_run(host, impairer.clear_command(placement.netdev))
        else:
            for cmd in impairer.scoped_clear_selector_commands(
                placement.netdev, entry[0], selector
            ):
                await _root_run(host, cmd)
        still = await _read_state(host, impairer, placement.netdev)
        if selector in still.selectors or still.kind in ("whole", "foreign"):
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
        by_direction: dict[FlowDirection, DirectionState | None] = {}
        unreachable = False
        for placement in placements:
            host = _host(lab, placement.host_id)
            impairer = _impairer_for(host)
            try:
                state = await _read_state(host, impairer, placement.netdev)
                by_direction[placement.direction] = DirectionState(
                    whole=state.whole,
                    scoped={sel: params for sel, (_band, params) in state.selectors.items()},
                    foreign=state.kind == "foreign",
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
