"""Turn probe measurements into a per-feature verdict for ``otto link check``.

Pure functions only: no host I/O, no orchestration. Each ``judge_*`` reads
:class:`~otto.link.probes.PingStats` (or, for the rate/port range/side/
read-back/expire probes, the plain numbers or
:class:`~otto.link.impairer.ScopedState` a probe already parsed) and returns a
:class:`~otto.check.FeatureResult`. ``judge_control`` is the one exception,
returning ``None`` when the baseline is quiet enough to trust the rest of the
run, or an ``unmeasured`` result naming why it isn't.

A ping-based judge that gets ``None`` stats, or stats with no replies at all
(``avg is None``), always fails with ``detail="no ping replies"`` rather than
running its own formula: by the time any of these run, ``judge_control`` has
already passed, so silence under impairment is otto or the host misbehaving,
never a legitimate "no evidence" case (spec 2026-09-24 §3.1's ``fail`` vs
``unmeasured`` split).
"""

import math

from ..check import FeatureResult, UnmeasuredReason, Verdict
from .impairer import ScopedState
from .params import ImpairmentParams, Selector, equivalent
from .probes import PingStats

_NOISY_BASELINE_SD_MS = 5.0
_NO_PING_REPLIES = "no ping replies"


def _no_reply(feature: str) -> FeatureResult:
    """Build the shared FAIL for a ping-based judge that saw no replies at all."""
    return FeatureResult(feature, Verdict.FAIL, detail=_NO_PING_REPLIES)


def judge_control(baseline: PingStats | None) -> FeatureResult | None:
    """``None`` when *baseline* is quiet enough to trust; else an unmeasured verdict.

    Quiet means every probe got a reply (``loss_pct == 0``) and the replies
    were tight (``sd <= 5.0`` ms). A missing baseline (``None``, e.g. ``ping``
    printed no summary at all) is treated as total loss.
    """
    if baseline is not None:
        sd = baseline.sd
        if baseline.loss_pct == 0.0 and sd is not None and sd <= _NOISY_BASELINE_SD_MS:
            return None
    loss = baseline.loss_pct if baseline is not None else 100.0
    sd = baseline.sd if baseline is not None else None
    sd_text = f"{sd:.1f}" if sd is not None else "n/a"
    return FeatureResult(
        "control",
        Verdict.UNMEASURED,
        reason=UnmeasuredReason.NOISY_BASELINE,
        measured=f"loss {loss:.0f}%, σ {sd_text} ms",  # noqa: RUF001 — sigma is the SI symbol for standard deviation
    )


def judge_delay(
    baseline: PingStats | None,
    impaired: PingStats | None,
    want_ms: float,
    *,
    feature: str = "delay",
) -> FeatureResult:
    """PASS when the measured extra RTT matches *want_ms* within tolerance."""
    if baseline is None or impaired is None:
        return _no_reply(feature)
    b_avg, i_avg, b_sd = baseline.avg, impaired.avg, baseline.sd
    if b_avg is None or i_avg is None or b_sd is None:
        return _no_reply(feature)
    delta = i_avg - b_avg
    tol = max(10.0, 0.1 * want_ms + 3 * b_sd)
    if abs(delta - want_ms) <= tol:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        measured=f"+{delta:.1f}ms",
        wanted=f"{want_ms:g} ±{tol:.0f}",
    )


def judge_jitter(
    baseline: PingStats | None, impaired: PingStats | None, jitter_ms: float
) -> FeatureResult:
    """PASS when the impaired spread grew by at least a quarter of the requested jitter."""
    feature = "jitter"
    if baseline is None or impaired is None:
        return _no_reply(feature)
    b_sd, i_sd = baseline.sd, impaired.sd
    if b_sd is None or i_sd is None:
        return _no_reply(feature)
    threshold = b_sd + 0.25 * jitter_ms
    if i_sd >= threshold:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        measured=f"σ {i_sd:.1f}ms",  # noqa: RUF001 — sigma is the SI symbol for standard deviation
        wanted=f"σ ≥ {threshold:.1f}ms",  # noqa: RUF001 — sigma is the SI symbol for standard deviation
    )


def judge_loss(stats: PingStats | None, want_pct: float, *, feature: str = "loss") -> FeatureResult:
    """PASS when the observed loss is within a sample-size-scaled band of *want_pct*."""
    if stats is None or stats.avg is None:
        return _no_reply(feature)
    n = stats.transmitted
    p = want_pct / 100
    band = 300 * math.sqrt(p * (1 - p) / n) + 2
    loss = stats.loss_pct
    if abs(loss - want_pct) <= band:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        measured=f"{loss:.0f}% of {n}",
        wanted=f"{want_pct:g} ±{band:.0f}",
    )


def judge_duplicate(stats: PingStats | None, want_pct: float) -> FeatureResult:
    """PASS when at least a quarter of the requested duplicate rate was observed."""
    feature = "duplicate"
    if stats is None or stats.avg is None:
        return _no_reply(feature)
    threshold = max(1.0, 0.25 * want_pct / 100 * stats.transmitted)
    if stats.duplicates >= threshold:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature, Verdict.FAIL, measured=f"{stats.duplicates} duplicates of {stats.transmitted}"
    )


def judge_reorder(stats: PingStats | None) -> FeatureResult:
    """PASS when at least one reply arrived out of sequence-number order."""
    feature = "reorder"
    if stats is None or stats.avg is None:
        return _no_reply(feature)
    if stats.out_of_order >= 1:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(feature, Verdict.FAIL, measured="0 out-of-order replies")


def judge_corrupt(stats: PingStats | None, corrupt_pct: float) -> FeatureResult:
    """PASS when enough loss is observed (corrupted ICMP fails its checksum and is dropped)."""
    feature = "corrupt"
    if stats is None or stats.avg is None:
        return _no_reply(feature)
    threshold = 0.5 * corrupt_pct
    if stats.loss_pct >= threshold:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        measured=f"{stats.loss_pct:.1f}% lost",
        wanted=f"≥ {threshold:g}% lost",
    )


def judge_rate(elapsed_ms: float | None, nbytes: int, want_kbit: float) -> FeatureResult:
    """PASS when the observed transfer rate is within 25% of *want_kbit*."""
    feature = "rate"
    if elapsed_ms is None:
        return FeatureResult(feature, Verdict.UNMEASURED, reason=UnmeasuredReason.NO_CLOCK)
    kbit = nbytes * 8 / elapsed_ms
    tol = 0.25 * want_kbit
    if abs(kbit - want_kbit) <= tol:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature, Verdict.FAIL, measured=f"{kbit:.0f} kbit/s", wanted=f"{want_kbit:g} kbit/s ±25%"
    )


def judge_port_range(in_ms: float, out_ms: float, base_ms: float, delay_ms: float) -> FeatureResult:
    """PASS when in-range traffic is delayed and out-of-range traffic is not.

    ``in_ms``/``out_ms``/``base_ms`` are connect times for a probe inside the
    impaired range, outside it, and against the unimpaired baseline
    respectively; deltas are taken against ``base_ms``.
    """
    feature = "port range"
    tol = max(20.0, 0.25 * delay_ms)
    in_delta = in_ms - base_ms
    out_delta = out_ms - base_ms
    lo, hi = 2 * delay_ms - tol, 3 * delay_ms
    in_ok = lo <= in_delta < hi
    out_ok = out_delta < tol
    if in_ok and out_ok:
        return FeatureResult(feature, Verdict.PASS)
    problems: list[str] = []
    if not in_ok:
        problems.append(f"in-range delta +{in_delta:.0f}ms not in [{lo:.0f}, {hi:.0f})ms")
    if not out_ok:
        problems.append(f"out-of-range delta +{out_delta:.0f}ms >= tol {tol:.0f}ms")
    return FeatureResult(feature, Verdict.FAIL, detail="; ".join(problems))


def judge_side(src_ms: float, base_ms: float, delay_ms: float) -> FeatureResult:
    """PASS when a ``--side`` selector left source-port traffic on this egress undelayed."""
    feature = "side"
    tol = max(20.0, 0.25 * delay_ms)
    if src_ms - base_ms < tol:
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        detail="a src-side selector delayed destination-port traffic",
    )


def _scoped_equivalent(expected: ScopedState, got: ScopedState) -> bool:
    """Compare two :class:`ScopedState` trees by MEANING, not by exact float equality.

    ``ScopedState``'s generated ``__eq__`` compares its ``ImpairmentParams``
    fields exactly. That is too strict for a post-write read-back: netem
    quantizes delay/jitter to 64ns kernel ticks, so a value like ``0.7`` ms
    can genuinely read back as ``0.699`` ms with no error at all (see
    :mod:`otto.link.params`'s ``equivalent`` docstring). ``otto.link.manage``
    already compares its own post-apply verify this way (``_verify_scoped``,
    the whole-link branch of ``impair``); read-back must use the same
    tolerance or it would flag a healthy kernel round-trip as a otto bug.
    """
    if expected.kind != got.kind:
        return False
    if expected.kind == "whole":
        if expected.whole is None or got.whole is None:
            return expected.whole == got.whole
        return equivalent(expected.whole, got.whole)
    if expected.kind == "scoped":
        if set(expected.selectors) != set(got.selectors):
            return False
        for selector, (band, params) in expected.selectors.items():
            got_band, got_params = got.selectors[selector]
            if band != got_band or not equivalent(params, got_params):
                return False
        return True
    return True  # "clean" and "foreign" carry no extra data beyond kind


def describe_shape(
    *,
    foreign: bool = False,
    whole: ImpairmentParams | None = None,
    scoped: dict[Selector, ImpairmentParams] | None = None,
) -> str:
    """Say what one netdev's tree holds, in the words a check row prints.

    ``clean``, ``foreign``, ``whole [<params>]``, or ``<selector> [<params>]``
    per port-scoped selector. The single home for this wording: the sandbox
    read-back (:func:`describe`) and the live pass both say a shape through it.
    """
    if foreign:
        return "foreign"
    if whole is not None:
        return f"whole [{whole.describe()}]"
    if scoped:
        return ", ".join(f"{s.describe()} [{p.describe()}]" for s, p in scoped.items())
    return "clean"


def describe(state: ScopedState) -> str:
    """Say what the read-back *state* holds (see :func:`describe_shape`)."""
    return describe_shape(
        foreign=state.kind == "foreign",
        whole=state.whole,
        scoped={selector: params for selector, (_band, params) in state.selectors.items()},
    )


def judge_readback(expected: ScopedState, got: ScopedState) -> FeatureResult:
    """PASS when the tree read back from the host means the same thing otto wrote."""
    feature = "read-back"
    if _scoped_equivalent(expected, got):
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        measured=describe(got),
        wanted=describe(expected),
        hint="otto could not read back the tree it wrote — attach --report to an issue",
    )


def judge_expire(after: ScopedState, *, waited_s: int, expire_s: int) -> FeatureResult:
    """PASS when the tree is clean *waited_s* seconds after an *expire_s* expire timer."""
    feature = "expire"
    if after.kind == "clean":
        return FeatureResult(feature, Verdict.PASS)
    return FeatureResult(
        feature,
        Verdict.FAIL,
        detail=f"tree still present {waited_s}s after a {expire_s}s expire",
    )
