"""``otto link check --live``: re-prove, on the link itself, the features a real netdev can change.

The sandbox pass (:mod:`otto.link.check`) proves netem on a throwaway veth. A
real link adds what a veth cannot: other qdiscs, VLAN sub-interfaces, NIC
offloads, and whether otto lands on the right host, netdev and direction
(spec 2026-09-24 §4.3). ``--live`` re-runs exactly those features
(:data:`LIVE_FEATURES`) on the declared link, through the same
:func:`~otto.link.manage.impair_link` / :func:`~otto.link.manage.repair_link`
``otto link impair`` uses, so every safety refusal applies — and a refusal is
reported as the result, naming its rule.

The rules the cycle keeps:

- It never clobbers a user's impairment. A link that already carries any
  state, in any direction, is left alone: every live row is ``skipped``.
  (Every direction counts, because a whole-link repair clears them all.)
- Directions run one at a time. Every step impairs with a
  :data:`LIVE_EXPIRE_S` expire — a dead-man switch if otto dies mid-step —
  and repairs in a ``finally``.
- Probes run on the direction's origin endpoint and aim at the far
  endpoint's address. The echo listeners run on the far endpoint, tagged
  ``otto-check-<token>`` and killed in a ``finally``; before anything runs,
  listeners an earlier, killed run left on either endpoint are swept.
- A feature that failed in the sandbox is ``skipped`` live.
- The cycle ends with a heal check: the link must read back clean in every
  direction, or the ``read-back`` row fails.

The measurements themselves are the sandbox's (``otto.link._check_rows``);
only where they aim and how the impairment is applied differ.
"""

import dataclasses
import functools
import math
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..check import FeatureResult, HostFingerprint, Verdict
from ..check.fingerprint import LINK_TOOLS, LINK_VERSIONS, check_root_run, probe_fingerprint
from ..host.errors import UnsupportedOnUserlandError
from . import _check_rows as rows
from ._check_rows import ProbeCtx, far_end, origin_end
from .judge import describe_shape
from .manage import (
    VERIFY_FAILED,
    DirectionState,
    LinkCommandFailedError,
    LinkState,
    impair_link,
    read_link_state,
    repair_link,
)
from .model import Link
from .params import ImpairmentParams, Selector, equivalent
from .placement import FlowDirection
from .probes import pick_backend
from .sandbox import new_sandbox

LIVE_DELAY_MS = 50.0
"""The delay every live step that proves a delay applies."""

LIVE_EXPIRE_S = 60
"""Seconds before each live impairment clears itself, even if otto dies mid-step."""

_ROUND_TRIP_S = 1.0
"""Roughly what the impair, its post-apply verify and the repair add to each step."""

_TAG_RE = re.compile(r"otto-check-[0-9a-f]{6}")
"""Every ``otto-check-*`` tag: a sandbox namespace's name, or a listener's."""
_SWEEP_PATTERN = shlex.quote(rows.kill_pattern("otto-check-") + "[0-9a-f]{6}")
_SWEEP_LIST = f"pgrep -af {_SWEEP_PATTERN}"
_SWEEP_KILL = f"pkill -f {_SWEEP_PATTERN}"


@dataclass(frozen=True)
class LiveResults:
    """What one ``--live`` cycle found."""

    by_direction: dict[FlowDirection, list[FeatureResult]]
    """Each checked direction's live rows, in the requested features' order."""
    swept: list[str] = field(default_factory=list)
    """What the pre-cycle sweep of ``otto-check-*`` processes did, one line each."""


class _RefusedError(Exception):
    """``impair_link`` declined (a safety rule): the direction's remaining rows are skipped."""


class _UnverifiedError(Exception):
    """``impair_link``'s post-apply verify did not match: ``read-back`` fails."""


class _StepFailedError(Exception):
    """``impair_link`` failed on a host that answered: the step's rows fail."""


@dataclass
class _Live:
    """One ``--live`` cycle's shared state."""

    lab: Any
    link: Link
    features: list[str]
    tag: str
    fingerprints: dict[str, HostFingerprint]
    touched: set[FlowDirection] = field(default_factory=set)
    """Directions ``impair_link`` was called for — what the heal check covers."""
    halted: str | None = None
    """Why no further step may run (a repair failed), once one did."""

    async def fingerprint(self, host: Any) -> HostFingerprint:
        """Return *host*'s fingerprint, probing it only the first time."""
        if host.id not in self.fingerprints:
            self.fingerprints[host.id] = await probe_fingerprint(
                host, tools=LINK_TOOLS, versions=LINK_VERSIONS
            )
        return self.fingerprints[host.id]

    async def state(self) -> LinkState:
        """Read this link's current impairment state — this link's hosts only."""
        return await read_link_state(self.lab, self.link)


@dataclass
class _Scope:
    """One step's impairments on one direction: applied, recorded as evidence, then repaired."""

    live: _Live
    direction: FlowDirection
    ctx: ProbeCtx
    applied: list[Selector | None] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    """The repair calls made on the way out, as row evidence."""

    async def impair(self, params: ImpairmentParams, selector: Selector | None = None) -> None:
        """Impair this direction with *params* (on *selector*'s traffic only, when given)."""
        link = self.live.link
        origin = origin_end(link, self.direction).host
        where = f" on {selector.describe()}" if selector is not None else ""
        self.ctx.ran.append(
            f"impair_link {link.id} from {origin}: {params.describe()}{where}, "
            f"expire {LIVE_EXPIRE_S}s"
        )
        self.live.touched.add(self.direction)
        try:
            await impair_link(
                self.live.lab,
                link.id,
                params,
                from_host=origin,
                expire=LIVE_EXPIRE_S,
                selector=selector,
            )
        except (ValueError, UnsupportedOnUserlandError) as e:
            raise _RefusedError(str(e)) from e
        except LinkCommandFailedError as e:
            if str(e).startswith(VERIFY_FAILED):
                raise _UnverifiedError(str(e)) from e
            raise _StepFailedError(str(e)) from e
        self.applied.append(selector)

    async def repair(self) -> None:
        """Repair everything :meth:`impair` applied; a failure halts the cycle.

        ``repair_link`` cancels the expire timer before it clears, so a repair
        that fails leaves an impairment nothing will remove: no further step
        may run on it, and the heal check reports it.
        """
        link = self.live.link
        for selector in self.applied:
            self.repaired.append(
                f"repair_link {link.id}" + (f" {selector.describe()}" if selector else "")
            )
            try:
                await repair_link(self.live.lab, link.id, selector=selector)
            except (ValueError, LinkCommandFailedError) as e:
                self.live.halted = self.live.halted or f"repair failed: {e}"

    def close(self, result: FeatureResult) -> FeatureResult:
        """Add the repair calls to *result*'s evidence."""
        return dataclasses.replace(result, commands=[*result.commands, *self.repaired])


async def _measure(
    ctx: ProbeCtx, feature: str, runner: Callable[[], Awaitable[FeatureResult]]
) -> FeatureResult:
    return rows.with_evidence(ctx, await rows.guarded(ctx, feature, runner))


def _shape(state: DirectionState | None) -> str:
    if state is None:
        return "unreadable"
    return describe_shape(foreign=state.foreign, whole=state.whole, scoped=state.scoped)


def _same(expected: DirectionState, got: DirectionState | None) -> bool:
    """Compare by meaning: tc canonicalizes what it prints (see ``params.equivalent``)."""
    if got is None or got.foreign != expected.foreign or set(got.scoped) != set(expected.scoped):
        return False
    if (got.whole is None) != (expected.whole is None):
        return False
    if got.whole is not None and expected.whole is not None:
        return equivalent(got.whole, expected.whole)
    return all(equivalent(got.scoped[s], p) for s, p in expected.scoped.items())


def _shapes(by_direction: dict[FlowDirection, DirectionState | None]) -> str:
    return "; ".join(f"{d.value} {_shape(by_direction.get(d))}" for d in FlowDirection)


async def _read_back(scope: _Scope, params: ImpairmentParams) -> FeatureResult:
    """Judge that the link reads back as *params* on the scope's direction, clean elsewhere."""
    expected = {
        d: DirectionState(whole=params) if d is scope.direction else DirectionState()
        for d in FlowDirection
    }
    got = (await scope.live.state()).by_direction
    judged = (
        FeatureResult("read-back", Verdict.PASS)
        if all(_same(expected[d], got.get(d)) for d in FlowDirection)
        else FeatureResult(
            "read-back",
            Verdict.FAIL,
            measured=_shapes(got),
            wanted=_shapes(dict(expected)),
            hint="otto could not read back the tree it wrote — attach --report to an issue",
        )
    )
    return dataclasses.replace(
        judged, commands=list(scope.ctx.ran), output=f"read back: {_shapes(got)}"
    )


# --------------------------------------------------------------------------
# The steps: each impairs, then measures its rows; the caller repairs
# --------------------------------------------------------------------------

Step = Callable[[_Scope, list[str]], Awaitable[dict[str, FeatureResult]]]


async def _step_delay(scope: _Scope, wanted: list[str]) -> dict[str, FeatureResult]:
    params = ImpairmentParams(delay_ms=LIVE_DELAY_MS)
    await scope.impair(params)
    out = {}
    if "read-back" in wanted:
        out["read-back"] = await _read_back(scope, params)
    if "delay" in wanted:
        runner = functools.partial(rows.delay_verdict, scope.ctx, LIVE_DELAY_MS)
        out["delay"] = await _measure(scope.ctx, "delay", runner)
    return out


async def _step_port_range(scope: _Scope, _wanted: list[str]) -> dict[str, FeatureResult]:
    runner = functools.partial(rows.port_range_verdict, scope.ctx, scope.impair, LIVE_DELAY_MS)
    return {"port range": await _measure(scope.ctx, "port range", runner)}


async def _step_side(scope: _Scope, _wanted: list[str]) -> dict[str, FeatureResult]:
    runner = functools.partial(rows.side_verdict, scope.ctx, scope.impair, LIVE_DELAY_MS)
    return {"side": await _measure(scope.ctx, "side", runner)}


async def _step_rate(scope: _Scope, _wanted: list[str]) -> dict[str, FeatureResult]:
    await scope.impair(ImpairmentParams(rate=rows.RATE))
    runner = functools.partial(rows.rate_verdict, scope.ctx)
    return {"rate": await _measure(scope.ctx, "rate", runner)}


async def _step_loss(scope: _Scope, _wanted: list[str]) -> dict[str, FeatureResult]:
    await scope.impair(ImpairmentParams(loss_pct=rows.LOSS_PCT))
    runner = functools.partial(rows.loss_verdict, scope.ctx)
    return {"loss": await _measure(scope.ctx, "loss", runner)}


@dataclass(frozen=True)
class _StepPlan:
    features: list[str]
    run: Step
    impaired_s: float
    """How long the step's probes keep the link impaired."""


_CONNECT_S = 2 * LIVE_DELAY_MS / 1000
"""One timed connect through a :data:`LIVE_DELAY_MS` delay: the SYN and the echoed byte."""

_STEPS = [
    _StepPlan(["read-back", "delay"], _step_delay, rows.DELAY_PING.seconds),
    _StepPlan(["port range"], _step_port_range, 2 * _CONNECT_S),
    _StepPlan(["side"], _step_side, _CONNECT_S),
    _StepPlan(["rate"], _step_rate, rows.RATE_BYTES * 8 / (rows.RATE_KBIT * 1000)),
    _StepPlan(["loss"], _step_loss, rows.LOSS_PING.seconds),
]

LIVE_FEATURES = [feature for step in _STEPS for feature in step.features]
"""The features ``--live`` re-proves on the real link (spec 2026-09-24 §4.3)."""


def live_seconds(features: list[str]) -> int:
    """Roughly how long one direction's cycle keeps the link impaired, for *features*."""
    steps = [step for step in _STEPS if any(f in features for f in step.features)]
    return math.ceil(sum(_ROUND_TRIP_S + step.impaired_s for step in steps))


# --------------------------------------------------------------------------
# One direction, and the whole cycle
# --------------------------------------------------------------------------


def _skip_all(features: list[str], **kw: Any) -> dict[str, FeatureResult]:
    return {f: FeatureResult(f, Verdict.SKIPPED, **kw) for f in features}


def _unreachable_end(live: _Live, direction: FlowDirection) -> str | None:
    """Why *direction* cannot be probed from lab data alone, or ``None``."""
    for end in (origin_end(live.link, direction), far_end(live.link, direction)):
        if end.host not in live.lab.hosts:
            return f"endpoint host {end.host!r} of link {live.link.id} is not in the loaded lab"
    far = far_end(live.link, direction)
    if not far.ip:
        return (
            f"endpoint {far.host} of link {live.link.id} has no ip to probe — "
            "declare one in lab.json"
        )
    return None


async def _run_steps(
    live: _Live,
    ctx: ProbeCtx,
    direction: FlowDirection,
    out: dict[str, FeatureResult],
    voided: FeatureResult | None,
    blocked: FeatureResult | None,
) -> None:
    """Run every step that still has a row to fill, recording each row in *out*."""
    for step in _STEPS:
        wanted = []
        for feature in (f for f in step.features if f in live.features and f not in out):
            pre = rows.preempted(feature, voided, blocked)
            if pre is not None:
                out[feature] = pre
            else:
                wanted.append(feature)
        if not wanted:
            continue
        if live.halted is not None:
            break
        ctx.ran, ctx.said = [], []
        scope = _Scope(live, direction, ctx)
        try:
            try:
                found = await step.run(scope, wanted)
            finally:
                await scope.repair()
        except _RefusedError as e:
            out.update(_skip_all([f for f in live.features if f not in out], hint=str(e)))
            return
        except _UnverifiedError as e:
            out["read-back"] = FeatureResult(
                "read-back", Verdict.FAIL, detail=str(e), commands=list(ctx.ran)
            )
            out.update(
                _skip_all(
                    [f for f in wanted if f != "read-back"],
                    detail="the impairment did not verify — see read-back",
                )
            )
        except _StepFailedError as e:
            out.update(
                {
                    f: FeatureResult(f, Verdict.FAIL, detail=str(e), commands=list(ctx.ran))
                    for f in wanted
                }
            )
        else:
            out.update({f: scope.close(r) for f, r in found.items()})
    if live.halted is not None:
        out.update(_skip_all([f for f in live.features if f not in out], hint=live.halted))


async def _run_direction(
    live: _Live, direction: FlowDirection, sandbox: list[FeatureResult]
) -> dict[str, FeatureResult]:
    failed = {r.feature for r in sandbox if r.verdict.fails}
    out = _skip_all([f for f in live.features if f in failed], detail="failed in sandbox")
    todo = [f for f in live.features if f not in out]
    hint = live.halted or _unreachable_end(live, direction)
    if hint is not None or not todo:
        out.update(_skip_all(todo, hint=hint))
        return out
    origin = live.lab.hosts[origin_end(live.link, direction).host]
    far = live.lab.hosts[far_end(live.link, direction).host]
    fp = await live.fingerprint(origin)
    ctx = ProbeCtx(origin, fp, far_end(live.link, direction).ip, backend=pick_backend(fp.tools))
    voided = await rows.control(ctx)
    listening = voided is None and any(f in rows.PROBE_ROWS for f in todo)
    try:
        blocked = None
        if listening:
            # Bound to the far endpoint's address on THIS link: the probes aim
            # there, and nothing else on the host can reach the listeners.
            blocked = await rows.start_listeners(
                ctx, far, await live.fingerprint(far), tag=live.tag, bind=ctx.target
            )
        await _run_steps(live, ctx, direction, out, voided, blocked)
    finally:
        if listening:
            await check_root_run(far, f"pkill -f {shlex.quote(rows.kill_pattern(live.tag))}")
    return out


_SAFETY_DOCS = "docs/cli/link/safety (Clearing is not creating)"


def _occupied(link: Link, state: LinkState) -> str | None:
    """Why the link must not be touched live (it carries state, or cannot be read), or ``None``."""
    for direction, shape in state.by_direction.items():
        if shape is not None and shape.foreign:
            # A repair refuses a tree otto did not write, so suggesting one would dead-end.
            return (
                f"link {link.id} carries a qdisc otto did not create on {direction.value} — "
                f"otto will not touch it, so --live cannot run; see {_SAFETY_DOCS}"
            )
    shapes = list(state.by_direction.values())
    if any(s is not None and (s.whole is not None or s.scoped) for s in shapes):
        return (
            f"link {link.id} already carries an impairment — repair it first, or run without --live"
        )
    if not state.impairable:
        return state.refusal or f"link {link.id} cannot be impaired"
    if state.unreachable or state.read_errors or None in shapes:
        errors = "; ".join(dict.fromkeys(state.read_errors.values())) or "a host did not answer"
        return f"could not read link {link.id}'s impairment state: {errors}"
    return None


async def _sweep(live: _Live) -> list[str]:
    """Kill ``otto-check-*`` processes on either endpoint; return a line saying what went.

    The pattern matches ANY otto check's tag, so a check running concurrently
    against the same host loses its listeners too — which is why the lines
    say "earlier or concurrent run" rather than presume a crashed one.
    """
    said: list[str] = []
    for host_id in dict.fromkeys([live.link.a.host, live.link.b.host]):
        host = live.lab.hosts.get(host_id)
        if host is None:
            continue
        listed = await check_root_run(host, _SWEEP_LIST)
        output = (listed.value or "").strip()
        tags = list(dict.fromkeys(_TAG_RE.findall(output)))
        if not listed.is_ok and output and not tags:
            # pgrep exits 1 silently when nothing matches; output AND no match
            # is its usage text: this pgrep cannot list command lines (-a).
            await check_root_run(host, _SWEEP_KILL)
            said.append(
                f"could not list otto-check processes on {host_id} (pgrep -a said: "
                f"{output.splitlines()[-1]}); killed any without naming them"
            )
        elif tags:
            await check_root_run(host, _SWEEP_KILL)
            said += [
                f"swept otto-check process {tag} on {host_id} (earlier or concurrent run)"
                for tag in tags
            ]
    return said


_NOT_HEALED = "link did not heal after repair"


def _heal_failed(link: Link, result: FeatureResult, state: LinkState) -> FeatureResult:
    """Turn *result* into a ``fail``: the link still carries state after the last repair."""
    kept = result.detail if result.verdict.fails and result.detail else None
    return dataclasses.replace(
        result,
        verdict=Verdict.FAIL,
        reason=None,
        detail=f"{kept}; {_NOT_HEALED}" if kept else _NOT_HEALED,
        measured=_shapes(state.by_direction),
        wanted="clean",
        hint=f"run `otto link repair {link.id}` to clear it — a repair that failed has "
        "already cancelled the expire timer, so nothing else will",
    )


def _healed(state: LinkState) -> bool:
    return all(_same(DirectionState(), state.by_direction.get(d)) for d in FlowDirection)


async def run_live(
    lab: Any,
    link: Link,
    directions: frozenset[FlowDirection],
    features: list[str],
    *,
    sandbox: dict[FlowDirection, list[FeatureResult]],
    fingerprints: dict[str, HostFingerprint],
) -> LiveResults:
    """Run the ``--live`` cycle for *features* on *link*, one direction at a time.

    *sandbox* is each direction's placement host's sandbox rows (a feature
    that failed there is skipped live); *fingerprints* are hosts already
    fingerprinted, reused rather than probed again.

    Raises:
        ~otto.check.CheckHostUnreachableError: a probe host did not answer.
        ~otto.link.manage.LinkHostUnreachableError: a placement host did not
            answer ``impair`` or ``repair``. A down host is never a skip.
    """
    live = _Live(lab, link, features, new_sandbox().name, dict(fingerprints))
    ordered = [d for d in FlowDirection if d in directions]
    hint = _occupied(link, await live.state())
    if hint is not None:
        return LiveResults({d: list(_skip_all(features, hint=hint).values()) for d in ordered})
    swept = await _sweep(live)
    found = {d: await _run_direction(live, d, sandbox.get(d, [])) for d in ordered}
    if live.touched:
        state = await live.state()
        if not _healed(state):
            for d in live.touched:
                if "read-back" in found[d]:
                    found[d]["read-back"] = _heal_failed(link, found[d]["read-back"], state)
    by_direction = {d: [found[d][f] for f in features] for d in ordered}
    return LiveResults(by_direction, swept)


_RANK = {Verdict.FAIL: 0, Verdict.UNSUPPORTED: 0, Verdict.UNMEASURED: 1, Verdict.SKIPPED: 2}


def combine_directions(
    by_direction: dict[FlowDirection, list[FeatureResult]],
) -> list[FeatureResult]:
    """One row per feature for a host that carries several directions (an in-path middlebox).

    The worst verdict wins, and a non-passing row names the direction it
    came from; a single direction's rows come back unchanged.
    """
    if len(by_direction) == 1:
        [only] = by_direction.values()
        return only
    combined = []
    for results in zip(*by_direction.values(), strict=True):
        pairs = list(zip(by_direction, results, strict=True))
        direction, worst = min(pairs, key=lambda p: _RANK.get(p[1].verdict, 3))
        if worst.verdict is not Verdict.PASS:
            detail = f"{direction.value}: {worst.detail}" if worst.detail else direction.value
            worst = dataclasses.replace(worst, detail=detail)
        combined.append(worst)
    return combined
