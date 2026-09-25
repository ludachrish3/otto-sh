"""``otto link check``: prove each netem feature on the hosts that would impair a link.

The link's placement decides which hosts are checked (spec 2026-09-24 §4.1):
each endpoint's host, or the in-path middlebox. On each one otto builds a
throwaway ``otto-check-<id>`` namespace (:mod:`otto.link.sandbox`), applies
every feature to the root-side veth through the same
:class:`~otto.link.netem.NetEmImpairer` builders ``impair`` uses, and measures
the effect with the probe kit (:mod:`otto.link.probes`), turning each
measurement into a verdict (:mod:`otto.link.judge`). The user's interfaces are
never touched by this pass; ``--live`` then re-proves the features a real
netdev can change on the link itself (:mod:`otto.link.check_live`).

Each feature is one small ``_run_<feature>`` coroutine over a
``_SandboxCtx``, and every row is wrapped by ``_run_row``, which
records the commands it ran and what they printed (the evidence ``-v`` and
``--report`` show) and clears the veth afterwards so no row leaks into the
next. The measurements themselves are shared with ``--live``
(``otto.link._check_rows``).
"""

import dataclasses
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

from ..check import (
    CheckRow,
    CheckSection,
    FeatureResult,
    HostFingerprint,
    Verdict,
    label_against_range,
    load_proven_range,
)
from ..check.fingerprint import (
    LINK_TOOLS,
    LINK_VERSIONS,
    Elevation,
    check_read,
    check_root_run,
    probe_elevation,
    probe_fingerprint,
)
from ..host import CommandResult
from ..host.errors import UnsupportedOnUserlandError
from ..host.host import is_dry_run
from . import _check_rows as rows
from ._check_rows import ProbeCtx, far_end, origin_end
from .check_live import LIVE_EXPIRE_S, LIVE_FEATURES, combine_directions, live_seconds, run_live
from .impairer import FIRST_SELECTOR_BAND, ScopedState
from .judge import (
    judge_corrupt,
    judge_duplicate,
    judge_expire,
    judge_jitter,
    judge_readback,
    judge_reorder,
)
from .manage import find_link, lab_host, launch_clear_timer, resolve_directions
from .model import Link
from .netem import NetEmImpairer
from .params import ImpairmentParams, Selector
from .placement import (
    FlowDirection,
    Placement,
    endpoint_placements,
    impairment_refusal,
    inpath_placements,
    parse_ip_addr,
)
from .probes import PingStats, pick_backend
from .sandbox import Sandbox, new_sandbox, open_sandbox, sweep_stale

if TYPE_CHECKING:
    from ..config.lab import Lab

FEATURES = [
    "read-back", "delay", "jitter", "loss", "duplicate", "reorder",
    "corrupt", "rate", "port range", "side", "expire",
]  # fmt: skip
"""Every feature ``otto link check`` knows, in report order."""

NA_LEGEND = "n/a: sandbox result applies; a real link doesn't change this feature"

REFUSAL_HINT = (
    "declare this link in lab.json with an interface on each endpoint — "
    "see docs/configuration/lab-config (lab-links)"
)

_ADDR_SHOW_COMMAND = "ip -o addr show"
_DELAY_MS = 100.0
_EXPIRE_S = 3
_EXPIRE_WAIT_S = 6
_SCOPED_NOT_APPLIED = "port-scoped tree not applied: tc rejected it (see port range)"


@dataclass(frozen=True)
class LinkCheckHost:
    """One placement host's results."""

    host_id: str
    placements: list[Placement]
    fingerprint: HostFingerprint | None
    range_labels: dict[str, str]
    """Component (``iproute2``, ``kernel``, ``isa``, ``userland``) -> its
    :class:`~otto.check.RangeLabel` value."""
    sandbox: list[FeatureResult]
    """One result per requested feature, in :data:`FEATURES` order."""
    live: list[FeatureResult]
    """``--live`` results for :data:`~otto.link.check_live.LIVE_FEATURES` that were requested."""
    swept: list[str]
    """Stale ``otto-check-*`` namespaces removed before this host's sandbox."""
    addresses: dict[str, str] = field(default_factory=dict)
    """Placement netdev -> the address that netdev carries on this link; a
    netdev whose address otto does not know is absent, never guessed."""


@dataclass(frozen=True)
class LinkCheckReport:
    """Everything one ``otto link check`` found — what the CLI renders and ``--report`` writes."""

    link_id: str
    link_name: str | None
    live_requested: bool
    features: list[str]
    hosts: list[LinkCheckHost]
    refusal: str | None = None
    refusal_hint: str | None = None
    dry_run_plan: list[str] = field(default_factory=list)
    live_swept: list[str] = field(default_factory=list)
    """What ``--live``'s sweep of ``otto-check-*`` processes on the link's
    endpoint hosts did before it started, one display line each."""

    def results(self) -> list[FeatureResult]:
        """Every sandbox and live result, host by host."""
        return [r for host in self.hosts for r in (*host.sandbox, *host.live)]

    def failed(self) -> list[FeatureResult]:
        """Return the results that fail the run (``fail`` or ``unsupported``)."""
        return [r for r in self.results() if r.verdict.fails]

    @property
    def ok(self) -> bool:
        """True when the link was not refused and nothing failed."""
        return self.refusal is None and not self.failed()


# --------------------------------------------------------------------------
# The sandbox pass: one runner per feature
# --------------------------------------------------------------------------


@dataclass
class _SandboxCtx(ProbeCtx):
    """The probe context plus the sandbox whose veth every row impairs."""

    sb: Sandbox = field(kw_only=True)
    imp: NetEmImpairer = field(kw_only=True)
    dev: str = field(kw_only=True)


async def _apply_whole(ctx: _SandboxCtx, params: ImpairmentParams) -> None:
    await rows.apply(ctx, ctx.imp.apply_command(ctx.dev, params))


async def _apply_scoped(ctx: _SandboxCtx, params: ImpairmentParams, selector: Selector) -> None:
    await rows.apply(
        ctx,
        ctx.imp.scoped_root_command(ctx.dev),
        ctx.imp.scoped_band_command(ctx.dev, FIRST_SELECTOR_BAND, params),
        *ctx.imp.scoped_filter_commands(ctx.dev, FIRST_SELECTOR_BAND, selector),
    )


async def _read(ctx: _SandboxCtx) -> ScopedState:
    """Read the veth's tree back; a read the host fails is the row's ``fail``, not an abort."""
    return ctx.imp.parse_scoped(*await rows.read(ctx, ctx.imp.scoped_read_commands(ctx.dev)))


async def _impaired_ping(
    ctx: _SandboxCtx, params: ImpairmentParams, plan: rows.PingPlan
) -> PingStats | None:
    await _apply_whole(ctx, params)
    return await rows.ping(ctx, plan)


async def _run_read_back(ctx: _SandboxCtx) -> FeatureResult:
    params = ImpairmentParams(delay_ms=_DELAY_MS)
    await _apply_whole(ctx, params)
    whole = judge_readback(ScopedState.whole_link(params), await _read(ctx))
    await rows.root(ctx, ctx.imp.clear_command(ctx.dev))
    refused = await rows.rejection(_apply_scoped(ctx, params, rows.RANGE_SELECTOR))
    if refused is not None:
        # A tc that cannot build the port-scoped tree has nothing to read
        # back: `port range` and `side` report it as their own `unsupported`.
        found = [whole.detail] if whole.detail else []
        if whole.verdict.fails:
            found.append("the whole-link tree did not read back as written")
        return dataclasses.replace(
            whole,
            detail="; ".join([*found, _SCOPED_NOT_APPLIED]),
            commands=list(ctx.ran),
            output="\n".join(ctx.said) or refused.value,
        )
    expected = ScopedState.from_selectors({rows.RANGE_SELECTOR: (FIRST_SELECTOR_BAND, params)})
    scoped = judge_readback(expected, await _read(ctx))
    failed = [
        (name, r) for name, r in [("whole-link", whole), ("port-scoped", scoped)] if r.verdict.fails
    ]
    if not failed:
        return whole
    names = " and ".join(name for name, _ in failed)
    first = failed[0][1]
    shape = f"the {names} tree did not read back as written"
    return dataclasses.replace(first, detail=f"{first.detail}; {shape}" if first.detail else shape)


async def _run_delay(ctx: _SandboxCtx) -> FeatureResult:
    await _apply_whole(ctx, ImpairmentParams(delay_ms=_DELAY_MS))
    return await rows.delay_verdict(ctx, _DELAY_MS)


async def _run_jitter(ctx: _SandboxCtx) -> FeatureResult:
    params = ImpairmentParams(delay_ms=_DELAY_MS, jitter_ms=20.0)
    return judge_jitter(
        ctx.control, await _impaired_ping(ctx, params, rows.PingPlan(20, 0.05)), 20.0
    )


async def _run_loss(ctx: _SandboxCtx) -> FeatureResult:
    await _apply_whole(ctx, ImpairmentParams(loss_pct=rows.LOSS_PCT))
    return await rows.loss_verdict(ctx)


async def _run_duplicate(ctx: _SandboxCtx) -> FeatureResult:
    params = ImpairmentParams(duplicate_pct=50.0)
    return judge_duplicate(await _impaired_ping(ctx, params, rows.PingPlan(50, 0.01)), 50.0)


async def _run_reorder(ctx: _SandboxCtx) -> FeatureResult:
    params = ImpairmentParams(delay_ms=50.0, reorder_pct=50.0)
    return judge_reorder(await _impaired_ping(ctx, params, rows.PingPlan(100, 0.01)))


async def _run_corrupt(ctx: _SandboxCtx) -> FeatureResult:
    params = ImpairmentParams(corrupt_pct=50.0)
    return judge_corrupt(await _impaired_ping(ctx, params, rows.PingPlan(100, 0.01)), 50.0)


async def _run_rate(ctx: _SandboxCtx) -> FeatureResult:
    rows.need_backend(ctx)
    await _apply_whole(ctx, ImpairmentParams(rate=rows.RATE))
    return await rows.rate_verdict(ctx)


async def _run_port_range(ctx: _SandboxCtx) -> FeatureResult:
    return await rows.port_range_verdict(ctx, functools.partial(_apply_scoped, ctx), _DELAY_MS)


async def _run_side(ctx: _SandboxCtx) -> FeatureResult:
    return await rows.side_verdict(ctx, functools.partial(_apply_scoped, ctx), _DELAY_MS)


async def _run_expire(ctx: _SandboxCtx) -> FeatureResult:
    if not ctx.fp.tools.get("bash"):
        # The timer is the same `bash -c 'sleep … && tc …'` `impair --expire` launches.
        return rows.missing_tool("expire", f"needs bash on {ctx.host.id}")
    await _apply_whole(ctx, ImpairmentParams(delay_ms=_DELAY_MS))
    clear = ctx.imp.clear_command(ctx.dev)
    try:
        launch = await launch_clear_timer(ctx.host, ctx.sb.name, ctx.dev, clear, _EXPIRE_S)
    except UnsupportedOnUserlandError as e:
        return FeatureResult("expire", Verdict.SKIPPED, hint=str(e))
    ctx.ran.append(launch.command)
    if launch.result.value:
        ctx.said.append(launch.result.value)
    await rows.sleep(_EXPIRE_WAIT_S)
    return judge_expire(await _read(ctx), waited_s=_EXPIRE_WAIT_S, expire_s=_EXPIRE_S)


_RUNNERS: dict[str, Callable[[_SandboxCtx], Awaitable[FeatureResult]]] = {
    "read-back": _run_read_back,
    "delay": _run_delay,
    "jitter": _run_jitter,
    "loss": _run_loss,
    "duplicate": _run_duplicate,
    "reorder": _run_reorder,
    "corrupt": _run_corrupt,
    "rate": _run_rate,
    "port range": _run_port_range,
    "side": _run_side,
    "expire": _run_expire,
}


async def _run_row(ctx: _SandboxCtx, feature: str) -> FeatureResult:
    """Run one feature's runner, attach its evidence, then clear the veth."""
    ctx.ran, ctx.said = [], []
    result = await rows.guarded(ctx, feature, functools.partial(_RUNNERS[feature], ctx))
    await check_root_run(ctx.host, ctx.imp.clear_command(ctx.dev))
    return rows.with_evidence(ctx, result)


async def _sandbox_rows(
    host: Any, sb: Sandbox, fp: HostFingerprint, features: list[str]
) -> list[FeatureResult]:
    """Every requested row, run inside the (already built) sandbox *sb*."""
    ctx = _SandboxCtx(
        host,
        fp,
        Sandbox.NS_IP,
        backend=pick_backend(fp.tools),
        sb=sb,
        imp=NetEmImpairer(),
        dev=sb.veth,
    )
    voided = await rows.control(ctx)
    blocked: FeatureResult | None = None
    if voided is None and any(f in rows.PROBE_ROWS for f in features):
        blocked = await rows.start_listeners(ctx, host, fp, tag=sb.name, netns=sb.name)
    results = []
    for feature in features:
        pre = rows.preempted(feature, voided, blocked)
        results.append(pre if pre is not None else await _run_row(ctx, feature))
    return results


# --------------------------------------------------------------------------
# Per host, and the whole check
# --------------------------------------------------------------------------


def _all_rows(features: list[str], **kw: Any) -> list[FeatureResult]:
    return [FeatureResult(feature, Verdict.SKIPPED, **kw) for feature in features]


def _range_labels(fp: HostFingerprint) -> dict[str, str]:
    proven = load_proven_range()
    versions = {
        "iproute2": fp.versions.get("iproute2"),
        "kernel": fp.kernel,
        "isa": fp.isa,
        "userland": fp.userland,
    }
    return {name: label_against_range(proven, name, v).value for name, v in versions.items()}


def _unready(fp: HostFingerprint, elevation: Elevation) -> str | None:
    """Why *fp*'s host cannot run a sandbox at all, or ``None``."""
    if not elevation.ok and elevation.hint is not None:
        return f"otto could not become root on {fp.host_id}: {elevation.hint}"
    if not elevation.ok:
        said = (elevation.output or "no output").strip().splitlines()[-1]
        return (
            f"otto could not become root on {fp.host_id}: it ran `{elevation.command}` "
            f"through the host's sudo or su with the lab's credentials, which said: {said}"
        )
    if not fp.netns:
        return f"needs ip netns support on {fp.host_id}"
    if not fp.tools.get("tc"):
        return f"tc not found on {fp.host_id}"
    return None


async def _check_host(
    host: Any, placements: list[Placement], addresses: dict[str, str], features: list[str]
) -> LinkCheckHost:
    fp = await probe_fingerprint(host, tools=LINK_TOOLS, versions=LINK_VERSIONS)
    elevation = await probe_elevation(host)
    fp = dataclasses.replace(fp, privileged=elevation.ok)
    labels = _range_labels(fp)
    swept: list[str] = []
    hint = _unready(fp, elevation)
    if hint is not None:
        evidence = (
            {} if elevation.ok else {"commands": [elevation.command], "output": elevation.output}
        )
        sandbox = _all_rows(features, hint=hint, **evidence)
    else:
        swept = await sweep_stale(host)
        sb = new_sandbox()
        async with open_sandbox(host, sb) as failure:
            if failure is not None:
                sandbox = _all_rows(
                    features,
                    output=failure.value,
                    hint=f"could not build the sandbox netns on {host.id}",
                )
            else:
                sandbox = await _sandbox_rows(host, sb, fp, features)
    return LinkCheckHost(host.id, placements, fp, labels, sandbox, [], swept, addresses)


def _unresolved_host(
    link: Link, features: list[str], failed: CommandResult, *, live: bool
) -> LinkCheckHost:
    """Build the host whose address table could not be read: every row skipped, with the read."""
    middlebox = link.impair or ""
    evidence: dict[str, Any] = {
        "hint": f"could not resolve {middlebox}'s interfaces from its address table",
        "commands": [_ADDR_SHOW_COMMAND],
        "output": failed.value or failed.msg or None,
    }
    live_rows = _all_rows([f for f in features if f in LIVE_FEATURES], **evidence) if live else []
    return LinkCheckHost(middlebox, [], None, {}, _all_rows(features, **evidence), live_rows, [])


def requested_features(features: list[str] | None) -> list[str]:
    """Expand *features* to :data:`FEATURES` order, with ``read-back`` always included.

    Public so a caller (the CLI) can validate ``--feature`` on its own, before
    doing anything that would count as this check's *result* — every other
    :func:`check_link` failure (an unknown link, a down host, ...) is that
    result, not a usage error, and only this one is a usage error to catch
    early and separately.

    Raises:
        ValueError: *features* names something outside :data:`FEATURES`.
    """
    if features is None:
        return list(FEATURES)
    unknown = [f for f in features if f not in FEATURES]
    if unknown:
        raise ValueError(
            f"unknown feature(s) {', '.join(repr(f) for f in unknown)}; "
            f"valid: {', '.join(FEATURES)}"
        )
    return [f for f in FEATURES if f == "read-back" or f in features]


@dataclass(frozen=True)
class _Resolved:
    """A link's placements, and the address each placement's netdev carries (when known)."""

    placements: list[Placement]
    addresses: dict[Placement, str]
    unresolved: CommandResult | None = None
    """The middlebox's address-table read, when it failed: no placement is known."""


async def _placements(lab: Any, link: Link, directions: frozenset[FlowDirection]) -> _Resolved:
    if not link.impair:
        placements = endpoint_placements(link, directions)
        # Each direction lands on its ORIGIN endpoint, whose ip is on that netdev.
        origins = {p: origin_end(link, p.direction) for p in placements}
        return _Resolved(placements, {p: end.ip for p, end in origins.items() if end.ip})
    middlebox = lab_host(lab, link.impair)
    addr = await check_read(middlebox, _ADDR_SHOW_COMMAND)
    if not addr.is_ok:
        return _Resolved([], {}, unresolved=addr)
    table = parse_ip_addr(addr.value or "")
    placements = inpath_placements(link, link.impair, table, directions)
    addresses = {}
    for p in placements:
        target = ip_address(far_end(link, p.direction).ip)
        facing = [a for a in table.get(p.netdev, []) if target in a.network]
        if facing:
            addresses[p] = str(facing[0].ip)
    return _Resolved(placements, addresses)


def _dry_run_plan(
    lab: Any, link: Link, directions: frozenset[FlowDirection], features: list[str], *, live: bool
) -> list[str]:
    """Build the lines saying what a real run would do, from lab data alone."""
    if link.impair:
        lab_host(lab, link.impair)
        middlebox = link.impair
        lines = [
            f"placement on middlebox {middlebox}: resolved from its live address table at run time"
        ]
        wheres = dict.fromkeys(directions, f"{middlebox}/<resolved at run time>")
        host_ids = [link.impair]
    else:
        placements = endpoint_placements(link, directions)
        lines = [f"placement {p.direction.value} on {p.host_id}/{p.netdev}" for p in placements]
        wheres = {p.direction: f"{p.host_id}/{p.netdev}" for p in placements}
        host_ids = list(dict.fromkeys(p.host_id for p in placements))
    probing = any(f in rows.PROBE_ROWS for f in features)
    ports = ", ".join(str(p) for p in rows.LISTEN_PORTS)
    for host_id in host_ids:
        lab_host(lab, host_id)
        lines += [
            f"would fingerprint {host_id} (one read-only command) and check it can become root",
            f"would sweep leftover otto-check-* namespaces on {host_id}",
            (
                f"would build netns otto-check-<id> on {host_id} "
                f"({Sandbox.ROOT_IP}/30 on ock<id> ↔ {Sandbox.NS_IP} inside) "
                f"and test: {', '.join(features)}"
            ),
        ]
        if probing:
            lines.append(f"would run echo listeners inside it on tcp {ports}")
    if live:
        steps = [f for f in LIVE_FEATURES if f in features]
        seconds = live_seconds(steps)
        for direction in FlowDirection:
            if direction in wheres:
                lines.append(
                    f"would impair {link.id} {direction.value} on {wheres[direction]} for about "
                    f"{seconds}s ({', '.join(steps)}), each step with expire {LIVE_EXPIRE_S}s"
                )
                far = far_end(link, direction)
                if any(f in rows.PROBE_ROWS for f in steps):
                    lines.append(
                        f"would run echo listeners on {far.host} {far.ip or '<no ip>'} "
                        f"tcp {ports} for {direction.value}"
                    )
    lines.append("no device was contacted — nothing was measured")
    return lines


async def check_link(
    lab: "Lab",
    ident: str,
    *,
    live: bool = False,
    features: list[str] | None = None,
    from_host: str | None = None,
) -> LinkCheckReport:
    """Check how completely netem works on the hosts that would impair link *ident*.

    *features* narrows the check (``read-back`` always runs; every other
    verdict depends on it); *from_host* narrows it to the one direction that
    host originates. A link placement refuses is reported as the result, and a
    dry run returns the plan without contacting any host. *live* adds a short
    real impair/measure/repair cycle on the link itself after the sandbox
    pass (:func:`~otto.link.check_live.run_live`).

    A command that fails on a host that answered is a row's verdict, never an
    exception: a probe that stalls fails its row, a tree that cannot be read
    back fails its row, and a middlebox whose address table cannot be read
    skips its rows.

    Raises:
        ValueError: an unknown feature, link, ``--from`` host or lab host.
        ~otto.check.CheckHostUnreachableError: a host the check needed did not
            answer. A down host is never reported as a skip.
        ~otto.link.manage.LinkHostUnreachableError: under *live*, a placement
            host did not answer ``impair`` or ``repair``.
    """
    wanted = requested_features(features)
    link = find_link(lab, ident)
    directions = resolve_directions(link, from_host)

    def report(hosts: list[LinkCheckHost], **kw: Any) -> LinkCheckReport:
        return LinkCheckReport(link.id, link.name, live, wanted, hosts, **kw)

    refusal = impairment_refusal(link, directions)
    if refusal is not None:
        return report([], refusal=refusal, refusal_hint=REFUSAL_HINT)
    if is_dry_run():
        return report([], dry_run_plan=_dry_run_plan(lab, link, directions, wanted, live=live))
    resolved = await _placements(lab, link, directions)
    if resolved.unresolved is not None:
        return report([_unresolved_host(link, wanted, resolved.unresolved, live=live)])
    by_host: dict[str, list[Placement]] = {}
    for placement in resolved.placements:
        by_host.setdefault(placement.host_id, []).append(placement)
    hosts = []
    for host_id, group in by_host.items():
        addresses = {p.netdev: resolved.addresses[p] for p in group if p in resolved.addresses}
        hosts.append(await _check_host(lab_host(lab, host_id), group, addresses, wanted))
    if not live:
        return report(hosts)
    outcome = await run_live(
        lab,
        link,
        directions,
        [f for f in wanted if f in LIVE_FEATURES],
        sandbox={p.direction: h.sandbox for h in hosts for p in h.placements},
        fingerprints={h.host_id: h.fingerprint for h in hosts if h.fingerprint is not None},
    )
    hosts = [
        dataclasses.replace(
            h,
            live=combine_directions(
                {p.direction: outcome.by_direction[p.direction] for p in h.placements}
            ),
        )
        for h in hosts
    ]
    return report(hosts, live_swept=outcome.swept)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _privilege(fp: HostFingerprint) -> str:
    if fp.user == "root":
        return "root"
    return "elevated" if fp.privileged else "no privilege"


_NETEM_STATE = {False: "no sch_netem", None: "sch_netem ?"}
"""What the heading says about a ``sch_netem`` module the fingerprint did not find."""


def _heading(host: LinkCheckHost) -> str:
    fp = host.fingerprint

    def netdev(p: Placement) -> str:
        address = host.addresses.get(p.netdev)
        return f"{p.netdev} {address}" if address else p.netdev

    if not host.placements:
        where = "(interfaces unresolved)"
    elif len(host.placements) == 1:
        [p] = host.placements
        where = f"{netdev(p)}  ({p.direction.value})"
    else:
        where = ", ".join(f"{netdev(p)} ({p.direction.value})" for p in host.placements)
    if fp is None:
        return f"{host.host_id}  {where}"
    facts = [
        f"iproute2 {fp.versions.get('iproute2') or '?'}",
        f"kernel {fp.kernel or '?'}",
        fp.isa or "?",
        fp.userland,
        _privilege(fp),
    ]
    if fp.netem_module is not True:
        facts.append(_NETEM_STATE[fp.netem_module])
    return f"{host.host_id}  {where}   {' · '.join(facts)}"


def _subheadings(host: LinkCheckHost) -> list[str]:
    lines = []
    fp, labels = host.fingerprint, host.range_labels
    if fp is not None:
        parts = [
            f"iproute2 {labels['iproute2']}",
            f"kernel {labels['kernel']}",
            f"{fp.isa or 'isa'} {labels['isa']}",
            f"{fp.userland} {labels['userland']}",
        ]
        lines.append(f"proven range: {' · '.join(parts)}")
    if host.swept:
        lines.append(f"swept leftover sandbox {', '.join(host.swept)} from an earlier run")
    return lines


def link_sections(report: LinkCheckReport) -> list[CheckSection]:
    """One :class:`~otto.check.CheckSection` per checked host, ready for ``render_sections``."""
    columns = ["sandbox", "live"] if report.live_requested else ["sandbox"]
    sections = []
    for index, host in enumerate(report.hosts):
        sandbox = {r.feature: r for r in host.sandbox}
        live = {r.feature: r for r in host.live}
        rows = [
            CheckRow(
                feature,
                [sandbox.get(feature), live.get(feature)]
                if report.live_requested
                else [sandbox.get(feature)],
            )
            for feature in report.features
        ]
        sections.append(
            CheckSection(
                heading=_heading(host),
                # The listener sweep is link-wide (both endpoints), so it is said once.
                subheadings=_subheadings(host) + (report.live_swept if index == 0 else []),
                columns=columns,
                rows=rows,
                summary_name=host.host_id,
                legend=[NA_LEGEND] if report.live_requested else [],
            )
        )
    return sections
