"""Impair/repair/list orchestration — kernel qdisc state is the ONLY state.

Reads go through ``host.exec`` (no privilege needed); mutations through
``host.run(cmd, sudo=host.current_user != "root")``. Every host call passes
``timeout=_IMPAIR_HOST_TIMEOUT`` and a down host is a loud, host-named
:class:`LinkHostUnreachableError` — never a skip (spec §9, dev-VM rule). A
host that ANSWERS and reports failure is the other class,
:class:`LinkCommandFailedError`; both subclass ``RuntimeError``, so a caller
that only wants "something went wrong" is unaffected, but ``list`` and the
timer-cancel hygiene step both act on the difference.

These four functions — :func:`impair_link`, :func:`repair_link`,
:func:`repair_all`, :func:`read_link_states` — plus :func:`find_link` ARE the
public API (spec's single-API constraint): the CLI, the future GUI topology
overlay, and any direct importer call exactly these. Nothing here prints or
knows about exit codes/colors.
"""

import contextlib
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any

from ..errors import OttoError
from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from ..host.daemon import kill_command, launch_command, refuse_if_launch_wrapper_needs_bash
from ..host.errors import exec_or_raise
from ..host.host import is_dry_run
from ..logger.mode import LogMode
from .impairer import FIRST_SELECTOR_BAND, MAX_SELECTORS, LinkImpairer, ScopedState, build_impairer
from .model import Link
from .params import ImpairmentParams, Selector, equivalent
from .placement import (
    BOTH_DIRECTIONS,
    FlowDirection,
    Placement,
    endpoint_placements,
    ensure_not_hop_transit,
    ensure_not_local_link,
    ensure_not_mgmt,
    impairment_refusal,
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
_BOTH = BOTH_DIRECTIONS
_ADDR_SHOW_COMMAND = "ip -o addr show"


class LinkHostUnreachableError(OttoError, RuntimeError):
    """A placement host could not be reached, or never answered in time.

    Nothing was learned about the netdev's qdisc state — which is why
    ``list`` reports the link as unreachable and why the best-effort
    timer-cancel step is allowed to skip on it.
    """


class LinkCommandFailedError(OttoError, RuntimeError):
    """The host answered and the command failed (or the result was wrong).

    A reachable host whose ``tc``/``ps`` is broken, a mutation that did not
    take, a post-apply verify that observed the wrong shape. Distinct from
    :class:`LinkHostUnreachableError` because it is a real failure about a
    reachable host, never a scan the caller may quietly skip.

    Domain-named rather than reusing
    :class:`~otto.host.errors.HostCommandError`: an ``except`` around
    ``impair_link`` should be able to mean "link work failed" without also
    catching every unrelated host command a future implementation runs.
    """


class LinkNotMeasuredError(OttoError, RuntimeError):
    """A device read was attempted under ``--dry-run``, where none may happen.

    A THIRD outcome, and neither of the two above: the host was not reached
    AND did not answer — it was never asked. ``--dry-run`` is documented as
    "Preview without running commands", so every answer this package gives
    under one has to come from lab data and the user's own arguments.

    ``_exec`` raises this rather than letting ``BaseHost.exec``'s synthetic
    reply through, because that reply is ``Status.Skipped`` (``is_ok`` is
    ``True``) with the literal value ``"[DRY RUN] Command not executed"`` and
    this package PARSES it: the impairer sees ``"[DRY"`` as ``tokens[0]`` and
    reports the netdev CLEAN, ``parse_ip_addr`` yields an EMPTY address table
    so the two self-lockout refusals cannot match, and ``impair_link``'s
    post-apply verify compares one fabrication against another. A raise cannot
    be mistaken for a measurement; a clean read can.

    ``RuntimeError``, like the two errors above, because three consumers bucket
    on that base and all three must keep working: :func:`read_link_states`
    promises never to raise per link, :func:`repair_all` collects a
    ``RuntimeError`` as a failure rather than aborting the sweep, and the CLI
    catches ``ValueError``/``RuntimeError``. A class outside that hierarchy
    would escape all three.
    """


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    """What a ``--dry-run`` would do, and what it could not check.

    Built from lab data and the command's own arguments — a dry run makes no
    device contact, so there is nothing else to build it from.

    BOTH HALVES ARE THE PRODUCT. :attr:`would` on its own reads as a promise
    otto has verified and cannot keep: every command line here is the one a
    CLEAN netdev would get, and the two self-lockout refusals that would stop
    the whole call have not run. :attr:`unchecked` on its own is a dry run with
    no preview at all, which is the useless-rather-than-safe shape
    :meth:`~otto.host.file_ops.PosixFileOps.write_file`'s dry-run arm exists to
    avoid. Renderers print both or neither.
    """

    would: list[str] = dc_field(default_factory=list)
    """One line per action a real run would take, in the order it would take
    them. Empty is a legitimate answer — an in-path link's placements cannot be
    resolved without the middlebox's live address table, so there is nothing to
    name."""

    unchecked: list[str] = dc_field(default_factory=list)
    """One line per fact a real run reads off a device and this one did not,
    each saying what the missing read would have decided. Never empty: a dry
    run that reached this type read nothing at all."""


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

    plan: "DryRunPlan | None" = None
    """Set, and :attr:`applied` EMPTY, when this call was a ``--dry-run``.

    The two are exclusive by construction and that is the point:
    :class:`AppliedPlacement` documents its ``params`` as "actually verified
    present after the mutation", so a dry run may not put anything in
    :attr:`applied` — there was no mutation and no verify. A caller that reads
    :attr:`applied` alone therefore cannot mistake a preview for a result, and
    one that wants the preview reads this.

    Appended, per the rule :attr:`LinkState.refusal` states: these are public
    dataclasses and inserting a field mid-order silently rebinds positional
    construction instead of failing."""


@dataclass(frozen=True, slots=True)
class RepairReport:
    """Outcome of :func:`repair_link`: what got cleared, and how many timers died."""

    link_id: str
    cleared: list[Placement] = dc_field(default_factory=list)
    timers_cancelled: int = 0

    plan: "DryRunPlan | None" = None
    """Set when this call was a ``--dry-run``; see :attr:`ImpairReport.plan`.

    :attr:`cleared` is then empty and :attr:`timers_cancelled` is ``0`` BY
    CONSTRUCTION rather than by measurement — nothing was cleared because
    nothing was looked at, and no ``ps`` scan ran. ``0`` is the honest value
    for "did not cancel any" and is the wrong thing to render as a result,
    which is why this field exists for the renderer to branch on."""


@dataclass(frozen=True, slots=True)
class RepairAllReport:
    """Outcome of :func:`repair_all`: repaired, failed, and declined.

    A dataclass rather than the ``(reports, failures)`` tuple this used to
    return, because the third bucket is exactly the widening
    ``.ast-grep/rules/no-tuple-return.yml`` exists to stop — its own motivating
    defect was a 2-tuple grown to a 3-tuple, breaking every unpacking site.
    Adding a field here breaks nobody.
    """

    repaired: list[RepairReport] = dc_field(default_factory=list)
    """One entry per link whose impairment was cleared and verified gone."""

    failures: list[str] = dc_field(default_factory=list)
    """``"<link id>: <why>"`` per link that FAILED to repair — host down, a
    command that errored, a clear that did not take. These set the exit code."""

    skipped: list[str] = dc_field(default_factory=list)
    """``"<link id>: <why>"`` per link otto DECLINED to touch — no named
    interface, a local link, a management-interface refusal, a foreign qdisc.

    Reported rather than merely skipped: this bucket used to be a bare
    ``continue``, so ``repair --all`` against a link carrying a foreign qdisc
    printed ``repaired 0 link(s)`` and exited 0, saying nothing about the one
    link it had refused. Naming them is verbose on a lab full of implicit
    links (every one lands here) and that is the accepted cost — a sweep that
    silently does nothing is the worse failure."""

    planned: list[RepairReport] = dc_field(default_factory=list)
    """One entry per link a ``--dry-run`` sweep PREVIEWED; every one carries a
    :attr:`RepairReport.plan` and cleared nothing.

    A separate bucket rather than letting these land in :attr:`repaired`,
    because that field says "cleared and verified gone" and a dry run did
    neither — the sweep's own summary line counts :attr:`repaired`, so a
    preview filed there is exactly the ``repaired N link(s)`` fabrication this
    commit removes. Empty on every real run."""

    dry_run: bool = False
    """``True`` when this sweep contacted nothing; see :attr:`RepairReport.plan`.

    NOT derivable from ``bool(planned)``, which is why it is recorded
    separately. On a lab whose links are ALL structurally refused — every
    implicit link is, so an N-host lab with no declared links is exactly this —
    a dry-run sweep files every one under :attr:`skipped` and previews nothing,
    and a renderer keying off :attr:`planned` would fall through to
    ``repaired 0 link(s)``: output byte-identical to a real sweep, which is the
    indistinguishable-from-real shape in vacuous form.

    Recorded by :func:`repair_all` from the run's own context rather than
    inferred from its results, for that reason: a flag derived from the output
    cannot describe a run that produced none."""


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

    refusal: str | None = None
    """Why, when ``impairable`` is False — so ``otto link list`` can say it
    rather than leaving the user to infer it from a bare ``n/a``.

    Appended rather than placed next to ``impairable`` it explains: this is a
    public dataclass, and inserting a field mid-order silently rebinds any
    positional construction instead of failing."""

    read_errors: dict[FlowDirection, str] = dc_field(default_factory=dict)
    """Per-direction message when that direction's read failed on a host that
    ANSWERED.

    Distinct from :attr:`unreachable`, and the distinction is the point: a
    host replying "``tc``: command not found" was reached, so reporting it as
    unreachable sends the operator to look at the network instead of at the
    host's tooling. Both leave :attr:`by_direction` at ``None`` for that
    direction; only this one carries a message to print.

    Per DIRECTION, not per link, because the two directions land on different
    hosts: one endpoint down and the other's ``tc`` broken is a real shape,
    and a single link-wide string had to pick one story for both cells. The
    whole-link failure path (placement resolution itself failed, so neither
    direction has a shape) records the same message under both keys; the CLI
    dedupes by message when it prints.

    Appended for the same reason :attr:`refusal` was — see above."""

    not_measured: bool = False
    """``True`` when NOTHING was asked: no read was issued, on any host.

    A THIRD state, and it exists because this dataclass already argues for it.
    :attr:`unreachable` means "couldn't be reached" and :attr:`read_errors`
    means "answered, and the read failed"; the docstring above says why they
    were split — reporting a host that ANSWERED as unreachable "sends the
    operator to look at the network instead of at the host's tooling". A
    ``--dry-run`` is neither, and by that same argument reporting it as either
    sends the operator somewhere wrong: to the network, or to a ``tc`` that is
    fine. What actually happened is that otto declined to look.

    Reporting it as CLEAN — which is what happened before this field existed,
    because the synthetic dry-run reply parses to an empty qdisc — is worse
    than all three: it is the only wrong answer that is indistinguishable from
    a real one.

    A ``bool``, not a message, unlike :attr:`refusal`: that field carries a
    string because the reasons vary, and there is exactly one reason to be
    here. The sentence belongs to the renderer.

    Appended for the same reason :attr:`refusal` was — see above."""

    @property
    def read_failed(self) -> bool:
        """True when any direction's read failed on a reachable host."""
        return bool(self.read_errors)


def find_link(lab: Any, ident: str) -> Link:
    """Resolve *ident* (a link id or its ``name``) against ``lab.static_links()``.

    Resolving is NOT the same as being actionable, and the gap is wide: every
    IMPLICIT link resolves here and none can be impaired (no named interface,
    or an endpoint on the local host). Ask
    :func:`~otto.link.placement.impairment_refusal` before assuming a resolved
    link can be acted on — a completer that offered everything this accepts
    would offer mostly guaranteed errors.

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
    """Run a read-only *cmd* on *host*; timeout/transport errors are host-named.

    The shared :func:`~otto.host.errors.exec_or_raise` sequence with this
    package's OWN pair of errors substituted — transport, then timed-out, then
    non-ok. The messages are unchanged because they are where that helper's
    pattern came from.

    **THE DRY-RUN BACKSTOP LIVES HERE**, at the single funnel every read in
    this package goes through — :func:`_read_state`, :func:`_resolve_placements`
    and :func:`_cancel_timers` all arrive by it. A dry run reaching this line
    means some path above forgot to short-circuit, and the ONLY safe answer is
    to fail loudly: ``BaseHost.exec`` would otherwise hand back an ``is_ok``
    ``CommandResult`` whose value is a banner, and every caller here PARSES
    what it gets. That is the guarantee that no read path added later can
    quietly start fabricating — see :class:`LinkNotMeasuredError`.

    It is a backstop and NOT the answer. On its own it turns every command
    into an error, which is honest and is not a product: a dry run with no
    preview is useless rather than safe. It also cannot build one — the
    preview needs the caller's whole intent (params, ``--port``, ``--expire``,
    the directions), none of which is visible from a command string. So
    :func:`impair_link` and :func:`repair_link` short-circuit ABOVE this, where
    that intent lives, and never reach it.

    :func:`_link_state` is the deliberate exception: it has no plan to build —
    reading IS its whole job — so it lets this fire and catches it, which is
    what gives ``list`` its "not read" state and keeps this arm live rather
    than a tripwire nothing ever trips. :func:`_root_run`, the MUTATING twin,
    gets the opposite treatment: nothing parses its output, so it needs no
    backstop, and a dry run never reaches it either.
    """
    if is_dry_run():
        raise LinkNotMeasuredError(
            f"{cmd!r} was not run on host {host.id!r}: a dry run makes no device contact, "
            f"so nothing about this host's state was measured"
        )
    return await exec_or_raise(
        host,
        cmd,
        timeout=_IMPAIR_HOST_TIMEOUT,
        unreachable=LinkHostUnreachableError,
        failed=LinkCommandFailedError,
    )


async def _root_run(host: Any, cmd: str) -> Any:
    """Run a mutating *cmd* on *host*, sudo'd unless already root.

    A non-ok result is deliberately NOT raised here — a command that reaches the
    host but reports failure is caught by the caller's own re-read
    (:func:`impair_link`'s post-apply verify, :func:`repair_link`'s post-clear
    re-read), never silently swallowed here.
    """
    need_sudo = host.current_user != "root"
    try:
        results = await host.run(
            cmd, sudo=need_sudo, timeout=_IMPAIR_HOST_TIMEOUT, log=LogMode.QUIET
        )
    except (OSError, ConnectionError) as e:
        raise LinkHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: {e!r}"
        ) from e
    if results[0].timed_out:
        raise LinkHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {_IMPAIR_HOST_TIMEOUT}s"
        )
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
    """Refuse to mutate a root qdisc otto did not generate (spec §1).

    A ``ValueError``, matching this module's convention for a STRUCTURAL
    refusal (``find_link``, ``_directions``, the exclusivity raises): nothing
    failed, otto is declining. That is what makes :func:`repair_all` skip the
    link rather than collect it as a failure — a foreign qdisc was never
    otto's impairment, so "repair every link" has nothing to do here. The
    single-link paths still refuse loudly: the CLI catches ``ValueError`` and
    ``RuntimeError`` alike.
    """
    if state.kind == "foreign":
        raise ValueError(
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
    it) and ``selector=S`` matches only S's own v2 timer.

    Best-effort ONLY against an unreachable host: cancellation is a hygiene
    step, not the operation itself, and every caller's next ``_exec`` on the
    same host raises loudly anyway. A reachable host whose ``ps`` FAILED is
    not hygiene — it means the scan cannot see the timers that are about to
    fire against state this call is changing — so that propagates, as does
    any bare ``RuntimeError`` from the host stack beneath.

    Deliberately NOT widened the way :func:`_link_state`'s read arms were:
    this is a MUTATING path, where raising IS the right answer. The outcome is
    unchanged either way — the caller's very next ``_read_state`` hits the
    same dead session and raises — so the only difference is that the failure
    now names the step that actually failed rather than the one after it.
    """
    try:
        result = await _exec(host, IMPAIR_PS_COMMAND)
    except LinkHostUnreachableError:
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


def _expire_refusal_context(sentinel: str | None = None) -> str:
    """Build the ``attempted=`` text for the ``daemon-launch`` refusal — ONE home.

    Two callers ask :func:`~otto.host.daemon.refuse_if_launch_wrapper_needs_bash`
    the same question and must get the same sentence back:
    :func:`_launch_daemon`, at the moment of the real launch, and
    :func:`_plan_impair`, which asks UP FRONT because a dry run has nothing to
    roll back and an operator asking "what would this do" is owed "it would be
    refused" before any of the rest.

    *sentinel* is omitted only by the in-path planner, which has no netdev to
    build one from (:func:`_planned_placements` explains why) and so cannot
    name the tag without inventing it.
    """
    tag = f" tagged {sentinel!r}" if sentinel is not None else ""
    return (
        f"an expire-timer daemon{tag}, which otto launches through "
        f"`bash -c 'exec -a …'`. Impair without `--expire` and repair the link when "
        f"you are done — the impairment itself needs no bash, only the timer does"
    )


async def _launch_daemon(host: Any, sentinel: str, argv: list[str]) -> None:
    """Launch one sentinel-tagged daemon on *host*, refusing what it cannot run.

    THE ONLY PLACE ``otto.link`` reaches
    :func:`~otto.host.daemon.launch_command`. Both expire-timer flavours (v1
    whole-link, v2 per-selector) funnel through here so the ``daemon-launch``
    refusal cannot be bypassed by adding a third launch:
    :func:`~otto.host.daemon.launch_command` takes no host and so cannot check
    for itself.

    THE REFUSAL FIRES HERE, not at the top of :func:`impair_link`, and the cost
    of that is real and worth stating: this call's own qdisc mutation has
    already been applied and verified by the time we get here, so the refusal
    propagates into ``impair_link``'s no-half-impairments handler and the
    mutation is rolled back. What it replaces is worse than a wasted round
    trip. :func:`_root_run` deliberately does not raise on a non-ok result -- a
    qdisc mutation's failure is caught by the caller's own re-read instead --
    and NOTHING re-reads after a timer launch, so on a bash-less host the launch
    line used to come back ``bash: not found``, be discarded, and leave
    ``impair_link`` reporting SUCCESS for an impairment whose timer does not
    exist and which therefore never expires. That is the silent failure this
    converts into a refusal.

    A ``--dry-run`` asks the same guard the same question from
    :func:`_plan_impair`, before anything else, and never arrives here. That
    is a SECOND caller of the guard and not a second path to the gapped
    surface: it launches nothing. The call stays spelled out here rather than
    behind a shared wrapper because the ``daemon-launch`` record's
    ``GapPath(site="otto.link.manage._launch_daemon", checked_by=…)`` is pinned
    by an AST scan of THIS function for THAT name
    (``tests/unit/host/test_gap_registry.py::…::test_the_site_calls_the_guard_it_names``);
    only the message is shared, via :func:`_expire_refusal_context`.
    """
    refuse_if_launch_wrapper_needs_bash(host, attempted=_expire_refusal_context(sentinel))
    await _root_run(host, launch_command(sentinel, argv))


async def _launch_timer(
    host: Any, link: Link, placement: Placement, impairer: LinkImpairer, expire: int
) -> None:
    """Launch a detached, sentinel-tagged timer that clears *placement* after *expire*s."""
    sentinel = encode_impair_sentinel(link.id, placement.netdev)
    argv = ["bash", "-c", f"sleep {int(expire)} && {impairer.clear_command(placement.netdev)}"]
    await _launch_daemon(host, sentinel, argv)


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
    await _launch_daemon(host, sentinel, ["bash", "-c", script])


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
        raise LinkCommandFailedError(
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
    raise LinkCommandFailedError(
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


####################
#  --dry-run planning: what lab data and the given arguments alone can say
####################

_UNCHECKED_LOCKOUT = (
    "the two self-lockout refusals. otto refuses a placement whose netdev carries the "
    "management address it reaches that host through, or the transit path of a host that "
    "hops through it — and BOTH refuse only on a positive match against the host's live "
    "`ip -o addr show`, which was not run. A dry run therefore CANNOT tell you this "
    "impairment would be allowed, let alone that it is safe"
)
"""The headline. Stated on every plan that names a placement, impair or repair.

Worth its own constant because it is the one thing an operator would otherwise
take from silence: before this, ``--dry-run`` of an impair that would cut otto
off from the bed printed no refusal at all — the guards parse an EMPTY address
table out of the synthetic reply, and an empty table can never match.
"""

_UNCHECKED_FOREIGN = (
    "the netdev's current SHAPE, which carries two more refusals — a foreign qdisc otto did "
    "not create is never touched, and whole-link and port-scoped impairment never mix on one "
    "netdev. Both are read off the device, so either could turn this call into a refusal"
)

_UNCHECKED_TIMERS_AND_VERIFY = (
    "live expire timers (a real run scans `ps` on each host and cancels this link's own "
    "before mutating) and the post-apply verify (a real run re-reads the netdev and fails "
    "loudly, rolling everything back, if the impairment did not take)"
)

_UNCHECKED_BAND = (
    "the prio band this selector would land in. It is the lowest band still free on the "
    "netdev, so the exact `tc qdisc replace … parent 1:<band>` and `tc filter add … pref "
    "<n>` lines cannot be shown without reading the netdev's current selectors"
)


def _inpath_unresolved(link: Link) -> str:
    """Why an in-path link's placements cannot be named without a device."""
    return (
        f"every placement. {link.impair!r} is this link's in-path middlebox, and which of its "
        f"interfaces faces each endpoint is resolved by subnet-matching the middlebox's live "
        f"`ip -o addr show`, which was not run. With no netdev there is no command line, no "
        f"current state and no self-lockout check to show for this link"
    )


def _planned_placements(
    lab: Any, link: Link, directions: frozenset[FlowDirection]
) -> list[Placement] | None:
    """Placements a dry run can name, or ``None`` when only a device could say.

    The pure half of :func:`_resolve_placements`, and the split between the two
    modes is the sharpest constraint on this whole feature:

    * ENDPOINT mode is pure. :func:`~otto.link.placement.endpoint_placements`
      reads the DECLARED ``link.a.interface`` / ``link.b.interface``, so a dry
      run names host and netdev exactly, with no approximation.
    * IN-PATH mode is not. :func:`~otto.link.placement.inpath_placements`
      subnet-matches the middlebox's live address table to find the netdev
      facing each endpoint, so a dry run cannot resolve a single placement and
      must SAY that. Guessing is not available and neither is the old
      behaviour, which raised ``'mb' has no interface on 'x's subnet`` — a real
      sentence about a fabricated measurement, and the wrong story for the
      actual cause.

    ``ensure_not_local_link`` runs here, exactly as it does at the top of
    :func:`_resolve_placements`: it needs no lab and no device, so a dry run
    refuses a local link for the same reason and with the same message a real
    run does.
    """
    ensure_not_local_link(link)
    if link.impair:
        # Resolves the middlebox so a lab-data error (a link naming a host that
        # is not in the lab) is still the loud ValueError a real run raises,
        # rather than being lost behind "could not be resolved".
        _host(lab, link.impair)
        return None
    return endpoint_placements(link, directions)


def _plan_one_placement(
    placement: Placement,
    impairer: LinkImpairer,
    params: ImpairmentParams,
    selector: Selector | None,
    expire: int | None,
) -> list[str]:
    """Render the ``would`` lines for one placement, from the given params alone.

    The params are merged over an EMPTY base rather than used as given, which
    is what makes ``--delay 0`` plan a ``tc qdisc del`` instead of a nonsense
    ``netem delay 0ms``: merging is where an explicit zero becomes "clear this
    one param" (:meth:`~otto.link.params.ImpairmentParams.merged_over`). An
    empty base is the honest model of "as if the netdev were clean", which is
    exactly the caveat :data:`_UNCHECKED_MERGE` carries.

    A coupling failure is reported, not raised.
    :meth:`~otto.link.params.ImpairmentParams.validate` documents itself as
    evaluated AFTER the merge — ``--jitter`` may be joining a delay applied by
    an earlier call — so a dry run must not turn "cannot be shown" into a
    refusal a real run would not make. It also must not render the command
    anyway: ``describe()`` drops a jitter with no delay, so the line would come
    out as a truncated ``tc qdisc replace … root netem`` that no run emits.
    """
    where = f"{placement.direction.value} on {placement.host_id}/{placement.netdev}"
    as_if_clean = params.merged_over(ImpairmentParams())
    try:
        as_if_clean.validate()
    except ValueError as e:
        return [
            (
                f"{where}: no command line can be shown — {e}, and what is already "
                f"applied is precisely what a dry run does not read"
            )
        ]
    if selector is not None:
        desc = as_if_clean.describe() or "clear this selector"
        lines = [f"{where}: {selector.describe()} {desc}"]
    elif as_if_clean.is_empty():
        lines = [f"{where}: {impairer.clear_command(placement.netdev)}"]
    else:
        lines = [f"{where}: {impairer.apply_command(placement.netdev, as_if_clean)}"]
    if expire is not None:
        lines.append(f"{where}: launch an expire timer that clears it after {expire}s")
    return lines


_UNCHECKED_MERGE = (
    "what is CURRENTLY applied to the netdev. A real run merges the given parameters over "
    "it per-param, so any command line above is the one a CLEAN netdev would get and "
    "nothing else — re-impairing a link that already carries an impairment produces a "
    "different command"
)


def _plan_impair(
    lab: Any,
    link: Link,
    directions: frozenset[FlowDirection],
    params: ImpairmentParams,
    *,
    expire: int | None,
    selector: Selector | None,
) -> DryRunPlan:
    """Preview :func:`impair_link` without contacting anything.

    Everything reachable here is pure — the placement resolution above, the
    per-host impairer lookup, the ``--port`` capability check, the
    ``daemon-launch`` refusal (it reads a DECLARED ``has_bash``), and every
    command-string builder. Each of those refusals is raised, not collected:
    they are the same refusals a real run makes, from the same data, so a dry
    run that swallowed them would be less faithful, not safer.

    The ``--expire`` refusal is the one that MOVES. In a real run it fires from
    :func:`_launch_daemon`, after this placement's qdisc mutation has already
    been applied and verified, and is then rolled back. A dry run has no
    mutation to roll back, so it asks the question first — which is also the
    only order in which an operator previewing the command learns the answer.
    """
    would: list[str] = []
    unchecked: list[str] = []
    placements = _planned_placements(lab, link, directions)
    if placements is None:
        middlebox = _host(lab, link.impair or "")
        impairer = _impairer_for(middlebox)
        if selector is not None:
            _ensure_selector_capable(middlebox, impairer)
        if expire is not None:
            refuse_if_launch_wrapper_needs_bash(middlebox, attempted=_expire_refusal_context())
        unchecked.append(_inpath_unresolved(link))
        unchecked.append(_UNCHECKED_TIMERS_AND_VERIFY)
        return DryRunPlan(would, unchecked)
    for placement in placements:
        host = _host(lab, placement.host_id)
        impairer = _impairer_for(host)
        if selector is not None:
            _ensure_selector_capable(host, impairer)
        if expire is not None:
            sentinel = (
                encode_impair_sentinel_v2(link.id, placement.netdev, selector)
                if selector is not None
                else encode_impair_sentinel(link.id, placement.netdev)
            )
            refuse_if_launch_wrapper_needs_bash(host, attempted=_expire_refusal_context(sentinel))
        would.extend(_plan_one_placement(placement, impairer, params, selector, expire))
    if selector is not None:
        unchecked.append(_UNCHECKED_BAND)
    unchecked.append(_UNCHECKED_MERGE)
    unchecked.append(_UNCHECKED_LOCKOUT)
    unchecked.append(_UNCHECKED_FOREIGN)
    unchecked.append(_UNCHECKED_TIMERS_AND_VERIFY)
    return DryRunPlan(would, unchecked)


_UNCHECKED_ANYTHING_TO_CLEAR = (
    "whether any of these placements carries an impairment at all. A bare repair SKIPS a "
    "netdev that is already clean, so the clears above are conditional and the count a real "
    "run reports cannot be known here"
)

_UNCHECKED_TIMER_COUNT = (
    "how many expire timers are live. The count comes from a `ps` scan on each placement "
    "host, so a dry run reporting `timers cancelled 0` would be claiming a scan it never ran"
)

_UNCHECKED_REPAIR_REFUSALS = (
    "the refusals. `repair` resolves placements exactly as `impair` does, so a netdev that "
    "turns out to be a management or hop-transit interface, or to carry a foreign qdisc, is "
    "REFUSED — this repair would fail rather than run, and only a device read decides that"
)


def _plan_repair(lab: Any, link: Link, *, selector: Selector | None) -> DryRunPlan:
    """Preview :func:`repair_link` without contacting anything.

    Every clear is conditional in a way an impair's is not: a bare repair skips
    a placement whose netdev is already clean, and a scoped repair skips a
    selector that is not present. So these lines say "only if", and the count
    the real command prints — ``cleared …, timers cancelled N`` — is exactly
    what a dry run has no way to produce.
    """
    would: list[str] = []
    unchecked: list[str] = []
    placements = _planned_placements(lab, link, _BOTH)
    if placements is None:
        middlebox = _host(lab, link.impair or "")
        impairer = _impairer_for(middlebox)
        if selector is not None:
            _ensure_selector_capable(middlebox, impairer)
        unchecked.append(_inpath_unresolved(link))
        unchecked.append(_UNCHECKED_TIMER_COUNT)
        return DryRunPlan(would, unchecked)
    for placement in placements:
        host = _host(lab, placement.host_id)
        impairer = _impairer_for(host)
        where = f"{placement.host_id}/{placement.netdev}"
        if selector is None:
            would.append(
                f"{where}: {impairer.clear_command(placement.netdev)} "
                f"(only if the netdev carries otto impairment)"
            )
            would.append(f"{where}: cancel every live expire timer for this link")
        else:
            _ensure_selector_capable(host, impairer)
            would.append(
                f"{where}: clear {selector.describe()} (only if that selector is present; "
                f"clearing the last one deletes the scoped root)"
            )
            would.append(f"{where}: cancel {selector.describe()}'s own expire timer")
    if selector is not None:
        unchecked.append(_UNCHECKED_BAND)
    unchecked.append(_UNCHECKED_ANYTHING_TO_CLEAR)
    unchecked.append(_UNCHECKED_TIMER_COUNT)
    unchecked.append(_UNCHECKED_REPAIR_REFUSALS)
    return DryRunPlan(would, unchecked)


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

    An *expire* against a host declaring ``has_bash=False`` is REFUSED with
    :class:`~otto.host.errors.UnsupportedOnUserlandError` (the ``daemon-launch``
    gap — see :func:`~otto.host.daemon.refuse_if_launch_wrapper_needs_bash`),
    and the refusal takes the no-half-impairments path below, so the link is
    left as it was found. An impair with NO *expire* on the same host is
    untouched: only the timer needs bash.

    No half-impairments: if any placement fails mid-way (mutation doesn't
    verify, host unreachable, etc.), every placement touched in this call —
    INCLUDING the one whose own mutation just failed — is restored to its
    PRIOR state before the error propagates.

    Under ``--dry-run`` nothing below the short-circuit runs: the report comes
    back with :attr:`~ImpairReport.applied` empty and
    :attr:`~ImpairReport.plan` set. The short-circuit sits ABOVE
    ``_resolve_placements`` — above the read backstop in ``_exec``, which would
    otherwise raise — because the preview needs this call's whole intent and a
    command string does not carry it.
    """
    link = find_link(lab, ident)
    directions = _directions(link, from_host)
    if is_dry_run():
        return ImpairReport(
            link.id,
            plan=_plan_impair(lab, link, directions, params, expire=expire, selector=selector),
        )
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
    except BaseException:
        # BaseException, not Exception: a Ctrl+C (CancelledError) mid-impair
        # must trigger the same no-half-impairments restore. compensate()
        # shields the restore from a further interrupt (chaos spec: shielded
        # compensating actions) and re-raises the cancellation after.
        # Imported here, not at module scope: otto.lifecycle is only needed once
        # a compensating action actually runs, and a top-level import drags it
        # onto every CLI --help path (import-budget guard).
        from ..lifecycle import compensate

        await compensate(
            _rollback(link.id, rollback_entries, selector=selector),
            what=f"link {link.id} rollback",
        )
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

    Under ``--dry-run`` this returns a plan and clears nothing — see
    :attr:`RepairReport.plan`. Before the short-circuit existed the synthetic
    reply parsed as a CLEAN netdev, so every placement took the
    ``state.kind == "clean"`` skip and the command reported
    ``cleared (nothing to clear), timers cancelled 0`` and exited 0 — three
    measurements, none of them taken.
    """
    link = find_link(lab, ident)
    directions = _directions(link, None)
    if is_dry_run():
        return RepairReport(link.id, plan=_plan_repair(lab, link, selector=selector))
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
                raise LinkCommandFailedError(
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
            raise LinkCommandFailedError(
                f"repair failed on {host.id}/{placement.netdev}: impairment still present"
            )
        cleared.append(placement)
    return RepairReport(link.id, cleared, timers_cancelled)


async def repair_all(lab: "Lab") -> RepairAllReport:
    """Repair every static link in *lab*; never raises.

    A link that structurally can't be impaired (:class:`ValueError` — no
    named interface, local-link, mgmt refusal, a FOREIGN qdisc otto did not
    create) is SKIPPED and named: it was never impaired in the first place,
    so a sweep has nothing to do about it, but a sweep that declines a link
    must say so. A link whose repair fails for a live reason
    (:class:`RuntimeError` — host down, command failed) is collected into
    :attr:`~RepairAllReport.failures` instead of aborting the rest.

    Under ``--dry-run`` each link's preview lands in
    :attr:`~RepairAllReport.planned` and NOT in
    :attr:`~RepairAllReport.repaired`, so the sweep's ``repaired N link(s)``
    count stays a count of links actually repaired. Structural refusals still
    fill :attr:`~RepairAllReport.skipped`, unchanged: they are decided from lab
    data and are as true under a dry run as under a real one — which is why
    :attr:`~RepairAllReport.dry_run` is read from the CONTEXT here rather than
    inferred from ``planned``: a lab whose links are all refused previews
    nothing, and a sweep that produced no output still has to be able to say it
    was a dry run.
    """
    report = RepairAllReport(dry_run=is_dry_run())
    for link in lab.static_links():
        try:
            outcome = await repair_link(lab, link.id)
        except ValueError as e:  # noqa: PERF203 — per-item resilience
            report.skipped.append(f"{link.id}: {e}")
        except RuntimeError as e:
            report.failures.append(f"{link.id}: {e}")
        else:
            bucket = report.planned if outcome.plan is not None else report.repaired
            bucket.append(outcome)
    return report


async def _link_state(lab: Any, link: Link) -> LinkState:
    """Read one link's impairment state.

    Structural refusals, unreachable hosts and failed reads are reported as
    flags/messages, never raised (spec §9 — ``list`` never dies). The last
    two are separate fields because they send the operator to different
    places: :attr:`LinkState.unreachable` means the host never answered,
    :attr:`LinkState.read_errors` means it answered and the read failed.

    "Never dies" is why the read arms end at bare ``RuntimeError`` rather
    than at this module's two classes. The host stack underneath still
    raises unnamed ``RuntimeError``s that no rule in
    ``.ast-grep/rules/`` covers — a dead session
    (``host/session.py``), a declared-but-not-running container
    (``host/docker_host.py``), an unresolvable hop
    (``host/remote_host.py``) — and this function is the last place that can
    catch them: ``read_link_states`` promises never to raise per link, and
    ``otto link list`` has no ``try`` of its own.

    The structural question is ASKED first rather than only inferred from the
    ValueError it would raise, because the two answers differ in the only way
    a table cell cares about: the predicate names EVERY endpoint at fault and
    nothing else, while ``endpoint_placements`` raises on the first one and
    prefixes the link id already printed in the row. The ``except ValueError``
    below still stands — the live refusals (management interface, hop transit,
    an in-path placement) are not structural and only a scan can find them.

    ``_BOTH`` is spelled out rather than left to the predicate's default —
    same value, but the directions are what make the answer true: the
    predicate refuses PER DIRECTION, and ``list`` reads both, so a
    half-interfaced link is correctly reported unimpairable here while
    ``impair --from <the interfaced end>`` still works.

    UNDER ``--dry-run`` THIS FUNCTION HAS NO SHORT-CIRCUIT OF ITS OWN, on
    purpose. :func:`impair_link` and :func:`repair_link` need one because they
    have a plan to build; ``list`` has none — reading IS its job — so the
    honest answer is whatever the read attempt produces, and the backstop in
    :func:`_exec` produces exactly it. The structural refusal above is still
    answered first (it is pure, and it is real information a dry run CAN
    give); everything after it raises :class:`LinkNotMeasuredError` from the
    first address-table read, and the arm below turns that into
    :attr:`LinkState.not_measured`. Routing it this way rather than adding a
    second ``is_dry_run()`` here is what keeps that arm LIVE — a hand-written
    short-circuit would make it a branch nothing ever takes.
    """
    refusal = impairment_refusal(link, _BOTH)
    if refusal is not None:
        return LinkState(link, {}, impairable=False, refusal=refusal)
    try:
        placements = await _resolve_placements(lab, link, _BOTH)
        by_direction: dict[FlowDirection, DirectionState | None] = {}
        unreachable = False
        read_errors: dict[FlowDirection, str] = {}
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
            except LinkNotMeasuredError:
                # UNREACHABLE TODAY, and closed anyway. The proof, so the next
                # reader does not have to rebuild it: under a dry run this loop
                # is entered only if `_resolve_placements` RETURNED, and that
                # function always issues at least one `_exec` first — the
                # middlebox's address table in in-path mode, the first
                # placement's in endpoint mode — and `placements` is never
                # empty, because `impairment_refusal` above already passed. So
                # the backstop always fires up there and the OUTER arm wins.
                #
                # The premise is one plausible refactor from changing: cache
                # address tables across links and `_resolve_placements` goes
                # pure for a warm host. Without this line the wide arm below
                # would then file a backstop raise as a read failure — `!`
                # cells and "host reachable, read command failed", both false,
                # and precisely the operator misdirection `not_measured`
                # exists to prevent. "No read path added later can quietly
                # start fabricating" must not weaken to "fails into the wrong
                # bucket".
                #
                # `raise`, not a second `LinkState(...)`: the mapping from this
                # error to `not_measured` keeps ONE home, the outer arm.
                raise
            except LinkHostUnreachableError:
                unreachable = True
                by_direction[placement.direction] = None
            except LinkCommandFailedError as e:
                # The host ANSWERED. Reporting this as unreachable — which is
                # what one shared `except RuntimeError` did — points the
                # operator at the network when the fault is the host's own tc
                # (missing, wrong version, unprivileged).
                read_errors.setdefault(placement.direction, str(e))
                by_direction[placement.direction] = None
            except RuntimeError as e:
                # Deliberately WIDE, and it must stay that way: the arm above
                # covers what THIS module raises, but the host stack below it
                # raises unnamed RuntimeErrors no rule scopes (dead session,
                # container not running, unresolvable hop). Narrowing here let
                # those escape `read_link_states`, which promises never to
                # raise per link. Filed as a read failure rather than as
                # unreachable: whether the host answered is exactly what such
                # an error does NOT tell us, and the cell that claims less is
                # the safer wrong answer.
                read_errors.setdefault(placement.direction, str(e))
                by_direction[placement.direction] = None
        return LinkState(
            link,
            by_direction,
            impairable=True,
            unreachable=unreachable,
            read_errors=read_errors,
        )
    except ValueError as e:
        # A refusal only a scan can find: the management-interface and
        # hop-transit checks read each placement host's live address table,
        # and an in-path link's placements are derived from the middlebox's.
        return LinkState(link, {}, impairable=False, refusal=str(e), unreachable=False)
    except LinkNotMeasuredError:
        # Nothing was asked. NOT `unreachable` (no host was contacted to fail
        # to answer) and NOT `read_errors` (no host answered), for the reason
        # those two are themselves separate — see LinkState.not_measured.
        # `impairable` stays True because the structural predicate above said
        # so and that answer needed no device; what it does NOT cover is the
        # live refusals, which is exactly what `not_measured` warns about.
        return LinkState(link, {}, impairable=True, not_measured=True)
    except LinkHostUnreachableError:
        return LinkState(link, {}, impairable=True, unreachable=True)
    except RuntimeError as e:
        # Placement resolution failed, so NEITHER direction has a shape —
        # both keys get the same message and the CLI dedupes when printing.
        # Same two cases as the per-placement arms, one level up:
        # LinkCommandFailedError (a reachable host whose `ip addr` failed is
        # not an unreachable host) and any bare RuntimeError the host stack
        # raised on the way (see this function's docstring).
        return LinkState(link, {}, impairable=True, read_errors=dict.fromkeys(_BOTH, str(e)))


async def read_link_states(lab: "Lab") -> list[LinkState]:
    """Read the current impairment state of every static link.

    This is the ``list``/GUI-overlay feed.

    Reads only (``exec``, no sudo); never raises per-link, so one bad host
    can't hide the rest of the fleet's state from a caller like ``otto link
    list`` or a topology overlay.
    """
    return [await _link_state(lab, link) for link in lab.static_links()]
