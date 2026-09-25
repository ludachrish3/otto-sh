"""The probe-row machinery both passes of ``otto link check`` share.

The sandbox pass (:mod:`otto.link.check`) and the ``--live`` pass
(:mod:`otto.link.check_live`) measure the same things the same way: a control
ping, timed TCP connects and transfers against echo listeners, and the same
verdict per feature. They differ only in WHERE — a throwaway netns behind a
veth, or the real link's far endpoint — and in HOW an impairment is applied
(raw ``tc`` on the veth, or :func:`~otto.link.manage.impair_link`). This
module is everything that does not depend on either, so a rule changes in one
place: :class:`ProbeCtx` says where the probes go, and each ``*_verdict``
measures a feature once its impairment is in place (``port range`` and
``side`` take the apply as a callback, because they measure a baseline
before it).

Every probe records the command it ran and what it printed on the
:class:`ProbeCtx`, and :func:`guarded` turns a probe's failure modes into the
verdict each one means, so both passes report evidence the same way.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..check import FeatureResult, HostFingerprint, UnmeasuredReason, Verdict
from ..check.fingerprint import check_read, check_root_run
from ..host import CommandResult
from .judge import judge_control, judge_delay, judge_loss, judge_port_range, judge_rate, judge_side
from .model import Link, LinkEndpoint
from .params import ImpairmentParams, Selector
from .placement import FlowDirection
from .probes import (
    PROBE_TIMEOUT_S,
    PingStats,
    ProbeBackend,
    listener_command,
    parse_elapsed_ms,
    parse_ping,
    pick_backend,
    ping_command,
    timed_connect_command,
    timed_transfer_command,
)

MEASURED = [
    "delay", "jitter", "loss", "duplicate", "reorder", "corrupt", "rate", "port range", "side",
]  # fmt: skip
"""Rows whose verdict compares against the control ping (so a noisy control voids them)."""

PROBE_ROWS = ["rate", "port range", "side"]
"""Rows that need the TCP echo listeners, and so a probe backend on both ends."""

LOSS_PCT = 30.0
RATE = "1mbit"
RATE_KBIT = 1000.0
RATE_BYTES = 262144
IN_RANGE_PORT = 5205
OUT_OF_RANGE_PORT = 5211
RATE_PORT = 5299
LISTEN_PORTS = [IN_RANGE_PORT, OUT_OF_RANGE_PORT, RATE_PORT]
RANGE_SELECTOR = Selector(5200, "tcp", end=5210, side="dst")
SIDE_SELECTOR = Selector(IN_RANGE_PORT, "tcp", side="src")
_READY_TRIES = 10
_READY_INTERVAL_S = 0.5
"""Listener readiness: about five seconds of connects before the probe rows are skipped."""
_READY_PROBE_S = 1
"""One readiness connect's bound: nothing is impaired yet, so an echo answers at once."""
_CLOCK_SLACK_S = 0.5
"""How far under its bound a failed probe's own clock may read and still count as timed out."""

NETEM_MISSING_HINT = (
    "sch_netem module not found — load it with `modprobe sch_netem` (Ubuntu ships it in "
    "linux-modules-extra-$(uname -r)); see docs/cli/link/check (When a row isn't pass)"
)
NETEM_UNKNOWN_HINT = (
    "otto could not tell whether the sch_netem module is available — if tc says the qdisc "
    "kind is unknown, load it with `modprobe sch_netem`; see docs/cli/link/check "
    "(When a row isn't pass)"
)


@dataclass(frozen=True)
class PingPlan:
    """One ping run: how many probes, how far apart."""

    count: int
    interval: float

    @property
    def seconds(self) -> float:
        """How long the run takes, near enough (the last reply's wait is ignored)."""
        return self.count * self.interval


CONTROL_PING = PingPlan(10, 0.2)
DELAY_PING = PingPlan(10, 0.2)
LOSS_PING = PingPlan(200, 0.01)
"""Sub-0.2s intervals need root, which is one reason every ping runs privileged."""

sleep = asyncio.sleep
"""Every wait goes through here, so tests can make a 30-second pass instant."""

ScopedImpair = Callable[[ImpairmentParams, Selector], Awaitable[None]]
"""Apply *params* to the traffic *selector* matches (raw ``tc``, or ``impair_link``)."""


@dataclass
class ProbeCtx:
    """Where one pass's probes run and aim, plus the evidence the current row collected."""

    host: Any
    """The host every probe client (ping, timed connect) runs on."""
    fp: HostFingerprint
    """:attr:`host`'s fingerprint: which tools the probes may use."""
    target: str
    """The address every probe aims at."""
    control: PingStats | None = None
    backend: ProbeBackend | None = None
    ran: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)


class _RejectedError(Exception):
    """The host's tc refused an apply: the row is ``unsupported``."""

    def __init__(self, cmd: str, result: CommandResult) -> None:
        super().__init__(cmd)
        self.cmd = cmd
        self.result = result


class _NoClockError(Exception):
    """A timed probe printed no elapsed time: the row is ``unmeasured (no-clock)``."""


class _MissingToolError(Exception):
    """A probe row needs a tool the host lacks; the message names it and the host."""


class _ProbeFailedError(Exception):
    """A timed probe ran and failed (it could not connect, or its echo came back wrong)."""

    def __init__(self, cmd: str, result: CommandResult) -> None:
        super().__init__(cmd)
        self.cmd = cmd
        self.result = result


class _ProbeTimedOutError(_ProbeFailedError):
    """A timed probe gave up at its own bound: something on the path stalled it."""


class _ReadFailedError(Exception):
    """Reading the tree back failed on a host that answered: the row fails with the output."""

    def __init__(self, cmd: str, result: CommandResult) -> None:
        super().__init__(cmd)
        self.cmd = cmd
        self.result = result


def origin_end(link: Link, direction: FlowDirection) -> LinkEndpoint:
    """Return the endpoint *direction*'s traffic leaves from."""
    return link.a if direction is FlowDirection.A_TO_B else link.b


def far_end(link: Link, direction: FlowDirection) -> LinkEndpoint:
    """Return the endpoint *direction*'s traffic travels toward."""
    return link.b if direction is FlowDirection.A_TO_B else link.a


async def root(ctx: ProbeCtx, cmd: str) -> CommandResult:
    """Run *cmd* privileged on the probe host, recording it and what it printed."""
    ctx.ran.append(cmd)
    result = await check_root_run(ctx.host, cmd)
    if result.value:
        ctx.said.append(result.value)
    return result


async def ping(ctx: ProbeCtx, plan: PingPlan) -> PingStats | None:
    """Ping the target per *plan*; ``None`` when ping printed no summary."""
    result = await root(ctx, ping_command(ctx.target, count=plan.count, interval=plan.interval))
    return parse_ping(result.value or "")


async def timed(ctx: ProbeCtx, cmd: str) -> float:
    """Run a timed probe client and return its elapsed milliseconds.

    Every client prints its own clock even when it fails, so a failed probe
    whose clock reached :data:`~otto.link.probes.PROBE_TIMEOUT_S` gave up at
    its bound: the path stalled it.
    """
    result = await root(ctx, cmd)
    ms = parse_elapsed_ms(result.value or "")
    if not result.is_ok:
        if ms is not None and ms >= (PROBE_TIMEOUT_S - _CLOCK_SLACK_S) * 1000:
            raise _ProbeTimedOutError(cmd, result)
        raise _ProbeFailedError(cmd, result)
    if ms is None:
        raise _NoClockError(cmd)
    return ms


async def apply(ctx: ProbeCtx, *cmds: str) -> None:
    """Run each impairment command in turn; one the host rejects makes the row ``unsupported``."""
    for cmd in cmds:
        result = await root(ctx, cmd)
        if not result.is_ok:
            raise _RejectedError(cmd, result)


def _missing_client(host_id: str, fp: HostFingerprint, backend: ProbeBackend | None) -> str | None:
    """Say what *host_id* lacks to run a timed probe client, or return ``None``.

    The socat client times itself with bash's ``$EPOCHREALTIME``; the python3
    client needs nothing else.
    """
    if backend is None:
        return f"needs socat or python3 on {host_id}"
    if backend == "socat" and not fp.tools.get("bash"):
        return f"needs bash on {host_id}"
    return None


async def rejection(applying: Awaitable[None]) -> CommandResult | None:
    """Await an :func:`apply`; return what the host rejected it with, or ``None`` if it applied.

    For a row that carries on past a rejected apply instead of becoming
    ``unsupported`` itself.
    """
    try:
        await applying
    except _RejectedError as e:
        return e.result
    return None


async def read(ctx: ProbeCtx, cmds: list[str]) -> list[str]:
    """Run each read-only command, recording it; a read the host fails makes the row ``fail``."""
    outputs = []
    for cmd in cmds:
        ctx.ran.append(cmd)
        result = await check_read(ctx.host, cmd)
        if not result.is_ok:
            raise _ReadFailedError(cmd, result)
        outputs.append(result.value or "")
        if result.value:
            ctx.said.append(result.value)
    return outputs


def need_backend(ctx: ProbeCtx) -> ProbeBackend:
    """Return the probe backend, or raise :class:`_MissingToolError` when the host can't probe."""
    missing = _missing_client(ctx.host.id, ctx.fp, ctx.backend)
    if missing is not None or ctx.backend is None:
        raise _MissingToolError(missing)
    return ctx.backend


async def connect(ctx: ProbeCtx, port: int) -> float:
    """One timed connect + 1-byte echo to the target's *port*."""
    return await timed(ctx, timed_connect_command(need_backend(ctx), ctx.target, port))


async def delay_verdict(ctx: ProbeCtx, want_ms: float) -> FeatureResult:
    """Ping through an applied *want_ms* delay and judge it against the control."""
    return judge_delay(ctx.control, await ping(ctx, DELAY_PING), want_ms)


async def loss_verdict(ctx: ProbeCtx) -> FeatureResult:
    """Ping through an applied :data:`LOSS_PCT` loss and judge the drop rate."""
    return judge_loss(await ping(ctx, LOSS_PING), LOSS_PCT)


async def rate_verdict(ctx: ProbeCtx) -> FeatureResult:
    """Time a :data:`RATE_BYTES` echo through an applied :data:`RATE` and judge the throughput."""
    cmd = timed_transfer_command(need_backend(ctx), ctx.target, RATE_PORT, RATE_BYTES)
    return judge_rate(await timed(ctx, cmd), RATE_BYTES, RATE_KBIT)


async def port_range_verdict(ctx: ProbeCtx, impair: ScopedImpair, delay_ms: float) -> FeatureResult:
    """Connect-time differential: only ports inside :data:`RANGE_SELECTOR` get *delay_ms*."""
    base = await connect(ctx, OUT_OF_RANGE_PORT)
    await impair(ImpairmentParams(delay_ms=delay_ms), RANGE_SELECTOR)
    in_ms = await connect(ctx, IN_RANGE_PORT)
    out_ms = await connect(ctx, OUT_OF_RANGE_PORT)
    return judge_port_range(in_ms, out_ms, base, delay_ms)


async def side_verdict(ctx: ProbeCtx, impair: ScopedImpair, delay_ms: float) -> FeatureResult:
    """Check that a ``side=src`` selector leaves destination-port traffic undelayed."""
    base = await connect(ctx, IN_RANGE_PORT)
    await impair(ImpairmentParams(delay_ms=delay_ms), SIDE_SELECTOR)
    return judge_side(await connect(ctx, IN_RANGE_PORT), base, delay_ms)


def missing_tool(feature: str, detail: str) -> FeatureResult:
    """Build the ``unmeasured (missing-tool)`` row; *detail* names the tool and the host."""
    return FeatureResult(
        feature, Verdict.UNMEASURED, reason=UnmeasuredReason.MISSING_TOOL, detail=detail
    )


def _netem_hint(fp: HostFingerprint) -> str | None:
    """Return the hint an ``unsupported`` row carries when *fp* found no ``sch_netem`` module."""
    if fp.netem_module is False:
        return NETEM_MISSING_HINT
    if fp.netem_module is None:
        return NETEM_UNKNOWN_HINT
    return None


async def guarded(
    ctx: ProbeCtx, feature: str, runner: Callable[[], Awaitable[FeatureResult]]
) -> FeatureResult:
    """Run one row's *runner*, turning each probe failure into its verdict.

    The caller starts the row's evidence (``ctx.ran``/``ctx.said``): a live
    step's rows share the impairment the step applied before them.
    """
    try:
        return await runner()
    except _RejectedError as e:
        return FeatureResult(
            feature,
            Verdict.UNSUPPORTED,
            commands=[e.cmd],
            output=e.result.value,
            hint=_netem_hint(ctx.fp),
        )
    except _MissingToolError as e:
        return missing_tool(feature, str(e))
    except _NoClockError:
        return FeatureResult(feature, Verdict.UNMEASURED, reason=UnmeasuredReason.NO_CLOCK)
    except _ReadFailedError as e:
        return FeatureResult(
            feature,
            Verdict.FAIL,
            detail="reading the tree back failed",
            commands=[e.cmd],
            output=e.result.value,
        )
    except _ProbeTimedOutError as e:
        return FeatureResult(
            feature,
            Verdict.FAIL,
            detail=f"probe did not finish within {PROBE_TIMEOUT_S} s",
            commands=[e.cmd],
            output=e.result.value,
        )
    except _ProbeFailedError as e:
        return FeatureResult(
            feature,
            Verdict.FAIL,
            detail="the timed probe did not complete",
            commands=[e.cmd],
            output=e.result.value,
        )


def with_evidence(ctx: ProbeCtx, result: FeatureResult) -> FeatureResult:
    """Attach the commands the row ran and what they printed, unless it already names its own."""
    if result.commands:
        return result
    return dataclasses.replace(
        result, commands=list(ctx.ran), output=result.output or "\n".join(ctx.said) or None
    )


def preempted(
    feature: str, voided: FeatureResult | None, blocked: FeatureResult | None
) -> FeatureResult | None:
    """Return the result *feature* takes instead of running, or ``None`` to run it.

    *voided* is what a failed control gives every measured row; *blocked* is
    what listeners that never came up give every probe row.
    """
    if voided is not None and feature in MEASURED:
        return dataclasses.replace(voided, feature=feature)
    if blocked is not None and feature in PROBE_ROWS:
        return dataclasses.replace(blocked, feature=feature)
    return None


async def start_listeners(
    ctx: ProbeCtx,
    listen_host: Any,
    listen_fp: HostFingerprint,
    *,
    tag: str,
    netns: str | None = None,
    bind: str | None = None,
) -> FeatureResult | None:
    """Start the echo listeners on *listen_host*'s *bind* and wait until the probe host reaches one.

    Returns the result every probe row takes instead (a tool missing on
    either end, or listeners that never answered), or ``None`` when they are
    up. Each listener is tagged *tag* so ``pkill -f`` can find it. A
    readiness connect passes only when otto's own echo answers it, so a
    foreign service on the port reads as a listener that did not start.
    """
    missing = _missing_client(ctx.host.id, ctx.fp, ctx.backend)
    listen_backend = pick_backend(listen_fp.tools)
    if missing is None and listen_backend is None:
        missing = f"needs socat or python3 on {listen_host.id}"
    if missing is None and not listen_fp.tools.get("bash"):
        # Both backends' listeners are started detached through ``setsid bash``.
        missing = f"needs bash on {listen_host.id}"
    if missing is not None or ctx.backend is None or listen_backend is None:
        return missing_tool("rate", missing or "")
    ran, said = [], []
    for port in LISTEN_PORTS:
        cmd = listener_command(listen_backend, port, tag=tag, netns=netns, bind=bind)
        ran.append(cmd)
        started = await check_root_run(listen_host, cmd)
        if started.value:
            said.append(started.value)
    probe = timed_connect_command(ctx.backend, ctx.target, RATE_PORT, within=_READY_PROBE_S)
    ran.append(probe)
    result: CommandResult | None = None
    for attempt in range(_READY_TRIES):
        if attempt:
            await sleep(_READY_INTERVAL_S)
        result = await check_root_run(ctx.host, probe)
        if result.is_ok:
            return None
    if result is not None and result.value:
        said.append(result.value)
    return FeatureResult(
        "rate",
        Verdict.SKIPPED,
        detail="listener did not start",
        commands=ran,
        output="\n".join(said) or None,
    )


async def control(ctx: ProbeCtx) -> FeatureResult | None:
    """Run the control ping; the result every measured row takes instead, or ``None``.

    Without ``ping`` there is no control to compare against, so the measured
    rows are ``unmeasured (missing-tool)`` and no ping is attempted.
    """
    if not ctx.fp.tools.get("ping"):
        return FeatureResult(
            "control",
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.MISSING_TOOL,
            detail=f"needs ping on {ctx.host.id}",
        )
    cmd = ping_command(ctx.target, count=CONTROL_PING.count, interval=CONTROL_PING.interval)
    run = await check_root_run(ctx.host, cmd)
    ctx.control = parse_ping(run.value or "")
    noisy = judge_control(ctx.control)
    if noisy is None:
        return None
    return dataclasses.replace(noisy, commands=[cmd], output=run.value)


def kill_pattern(tag: str) -> str:
    """Build a ``pkill -f`` / ``pgrep -f`` regex for *tag* that cannot match its own command.

    ``pkill -f`` excludes only itself, not the ``sudo`` or shell that ran it —
    and their command lines contain the pattern, so a plain one kills its own
    parent. ``[o]tto-check-…`` matches ``otto-check-…`` but not its own text.
    Unquoted: the caller quotes the finished pattern.
    """
    return f"[{tag[0]}]{tag[1:]}"
