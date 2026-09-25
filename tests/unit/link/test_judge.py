"""Pure verdict rules for link check; each bound has a pass case and a fail case."""

import pytest

from otto.check import UnmeasuredReason, Verdict
from otto.link.impairer import ScopedState
from otto.link.judge import (
    describe,
    judge_control,
    judge_corrupt,
    judge_delay,
    judge_duplicate,
    judge_expire,
    judge_jitter,
    judge_loss,
    judge_port_range,
    judge_rate,
    judge_readback,
    judge_reorder,
    judge_side,
)
from otto.link.params import ImpairmentParams, Selector
from otto.link.probes import PingStats


def _stats(rtts: list[float], *, tx: int | None = None, dups: int = 0) -> PingStats:
    return PingStats(
        transmitted=tx if tx is not None else len(rtts),
        rtts=rtts,
        seqs=list(range(len(rtts))),
        duplicates=dups,
    )


QUIET = _stats([0.10, 0.12, 0.11, 0.10])


class TestControl:
    def test_quiet_baseline_passes(self) -> None:
        assert judge_control(QUIET) is None

    def test_noisy_baseline_is_unmeasured(self) -> None:
        result = judge_control(_stats([1.0, 30.0, 2.0, 25.0]))
        assert result is not None
        assert result.verdict is Verdict.UNMEASURED
        assert result.reason is UnmeasuredReason.NOISY_BASELINE
        assert result.feature == "control"
        # pstdev([1, 30, 2, 25]) = 13.124... -> "13.1"; loss_pct = 0 (all 4 replied).
        assert result.measured == "loss 0%, σ 13.1 ms"  # noqa: RUF001 — sigma is the SI symbol for standard deviation

    def test_lossy_baseline_is_unmeasured(self) -> None:
        result = judge_control(_stats([0.1, 0.1], tx=4))
        assert result is not None
        assert result.reason is UnmeasuredReason.NOISY_BASELINE

    def test_sd_at_exactly_5ms_passes(self) -> None:
        # pstdev([95, 105]) = 5.0 (mean 100, deviations +-5) -- the sd<=5.0
        # bound's PASS edge, with loss_pct == 0 since both probes replied.
        assert judge_control(_stats([95.0, 105.0])) is None

    def test_sd_just_over_5ms_is_unmeasured(self) -> None:
        # pstdev([94.9, 105.1]) = 5.1 -- one hand-nudged past the 5.0 bound.
        result = judge_control(_stats([94.9, 105.1]))
        assert result is not None
        assert result.verdict is Verdict.UNMEASURED
        assert result.reason is UnmeasuredReason.NOISY_BASELINE

    def test_missing_baseline_is_unmeasured(self) -> None:
        result = judge_control(None)
        assert result is not None
        assert result.verdict is Verdict.UNMEASURED
        assert result.reason is UnmeasuredReason.NOISY_BASELINE


class TestDelay:
    def test_on_target(self) -> None:
        assert judge_delay(QUIET, _stats([100.2, 100.1, 99.9]), 100.0).verdict is Verdict.PASS

    def test_just_outside_tolerance(self) -> None:
        # tol = max(10, 0.1*100 + 3*sd) ≈ 10.03 for QUIET
        result = judge_delay(QUIET, _stats([111.0, 111.0, 111.0]), 100.0)
        assert result.verdict is Verdict.FAIL
        assert result.measured == "+110.9ms"
        assert result.feature == "delay"

    def test_no_reply_under_impairment_fails(self) -> None:
        # avg is None with no rtts at all -- silence is always a failure,
        # never attributed as "delay measured 0".
        result = judge_delay(QUIET, _stats([], tx=5), 100.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestJitter:
    # baseline sd == 0 (three identical rtts) keeps the threshold formula
    # (baseline.sd + 0.25*jitter_ms) a clean number: 0 + 0.25*20 = 5.0.
    STEADY = _stats([100.0, 100.0, 100.0])

    def test_at_threshold_passes(self) -> None:
        # pstdev([95, 105]) = 5.0 == threshold -> PASS (>=).
        result = judge_jitter(self.STEADY, _stats([95.0, 105.0]), 20.0)
        assert result.verdict is Verdict.PASS

    def test_just_under_threshold_fails(self) -> None:
        # pstdev([95.1, 104.9]) = 4.9 < 5.0 threshold.
        result = judge_jitter(self.STEADY, _stats([95.1, 104.9]), 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.measured == "σ 4.9ms"  # noqa: RUF001 — sigma is the SI symbol for standard deviation
        assert result.wanted == "σ ≥ 5.0ms"  # noqa: RUF001 — sigma is the SI symbol for standard deviation
        assert result.feature == "jitter"

    def test_no_reply_under_impairment_fails(self) -> None:
        result = judge_jitter(self.STEADY, _stats([], tx=5), 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestLoss:
    def test_inside_band(self) -> None:
        stats = _stats([0.1] * 140, tx=200)  # 30% loss
        assert judge_loss(stats, 30.0).verdict is Verdict.PASS

    def test_outside_band(self) -> None:
        stats = _stats([0.1] * 190, tx=200)  # 5% loss vs want 30%
        result = judge_loss(stats, 30.0)
        assert result.verdict is Verdict.FAIL
        assert result.feature == "loss"

    def test_no_reply_fails(self) -> None:
        result = judge_loss(_stats([], tx=5), 30.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestDuplicate:
    # want_pct=20, transmitted=100 -> threshold = max(1, 0.25*20/100*100) = 5.0
    def test_at_threshold_passes(self) -> None:
        stats = _stats([0.1] * 95, tx=100, dups=5)
        result = judge_duplicate(stats, 20.0)
        assert result.verdict is Verdict.PASS

    def test_just_under_threshold_fails(self) -> None:
        stats = _stats([0.1] * 96, tx=100, dups=4)
        result = judge_duplicate(stats, 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.measured == "4 duplicates of 100"
        assert result.feature == "duplicate"

    def test_no_reply_fails(self) -> None:
        result = judge_duplicate(_stats([], tx=5), 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestReorder:
    def test_one_out_of_order_reply_passes(self) -> None:
        stats = PingStats(transmitted=3, rtts=[0.1, 0.1, 0.1], seqs=[0, 2, 1], duplicates=0)
        assert judge_reorder(stats).verdict is Verdict.PASS

    def test_ascending_sequence_fails(self) -> None:
        stats = PingStats(transmitted=3, rtts=[0.1, 0.1, 0.1], seqs=[0, 1, 2], duplicates=0)
        result = judge_reorder(stats)
        assert result.verdict is Verdict.FAIL
        assert result.measured == "0 out-of-order replies"
        assert result.feature == "reorder"

    def test_no_reply_fails(self) -> None:
        result = judge_reorder(_stats([], tx=5))
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestCorrupt:
    # corrupt_pct=20 -> threshold = 0.5*20 = 10.0% lost.
    def test_at_threshold_passes(self) -> None:
        stats = _stats([0.1] * 90, tx=100)  # loss = 10.0%
        assert judge_corrupt(stats, 20.0).verdict is Verdict.PASS

    def test_just_under_threshold_fails(self) -> None:
        # tx=1000, received=901 -> loss = 9.9%, just under the 10.0% bound.
        stats = _stats([0.1] * 901, tx=1000)
        result = judge_corrupt(stats, 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.wanted == "≥ 10% lost"
        assert result.measured == "9.9% lost", "a near miss must not round up to the threshold"
        assert result.feature == "corrupt"

    def test_no_reply_fails(self) -> None:
        result = judge_corrupt(_stats([], tx=5), 20.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "no ping replies"


class TestRate:
    def test_on_target(self) -> None:
        # 262144 bytes at 1000 kbit/s = 2097.152 ms
        assert judge_rate(2097.152, 262144, 1000.0).verdict is Verdict.PASS

    def test_unshaped(self) -> None:
        result = judge_rate(40.0, 262144, 1000.0)
        assert result.verdict is Verdict.FAIL
        assert result.feature == "rate"

    def test_no_clock(self) -> None:
        result = judge_rate(None, 262144, 1000.0)
        assert result.verdict is Verdict.UNMEASURED
        assert result.reason is UnmeasuredReason.NO_CLOCK


class TestPortRange:
    # delay_ms=100 -> tol = max(20, 25) = 25; in-range window is
    # [2*100-25, 3*100) = [175, 300); out-of-range must stay < 25.
    def test_both_halves_pass(self) -> None:
        result = judge_port_range(in_ms=250.0, out_ms=60.0, base_ms=50.0, delay_ms=100.0)
        assert result.verdict is Verdict.PASS
        assert result.feature == "port range"

    def test_in_range_half_fails_at_exclusive_upper_bound(self) -> None:
        # in_delta = 300 hits the upper bound exactly, which is EXCLUSIVE
        # ("< 3*delay_ms"), so it fails even though 299.9 would pass.
        # out_delta = 10 stays comfortably under tol (25) and passes.
        result = judge_port_range(in_ms=350.0, out_ms=60.0, base_ms=50.0, delay_ms=100.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail is not None
        assert "in-range" in result.detail
        assert "out-of-range" not in result.detail

    def test_out_of_range_half_fails_at_tolerance(self) -> None:
        # out_delta = 25 == tol, which fails ("< tol" is strict).
        # in_delta = 200 is inside [175, 300) and passes.
        result = judge_port_range(in_ms=250.0, out_ms=75.0, base_ms=50.0, delay_ms=100.0)
        assert result.verdict is Verdict.FAIL
        assert result.detail is not None
        assert "out-of-range" in result.detail
        assert "in-range" not in result.detail


class TestSide:
    # delay_ms=100 -> tol = max(20, 25) = 25.
    def test_just_under_tolerance_passes(self) -> None:
        result = judge_side(src_ms=74.0, base_ms=50.0, delay_ms=100.0)  # delta=24 < 25
        assert result.verdict is Verdict.PASS

    def test_at_tolerance_fails(self) -> None:
        result = judge_side(src_ms=75.0, base_ms=50.0, delay_ms=100.0)  # delta=25, not <25
        assert result.verdict is Verdict.FAIL
        assert result.detail == "a src-side selector delayed destination-port traffic"
        assert result.feature == "side"


class TestReadback:
    def test_matching_trees_pass(self) -> None:
        expected = ScopedState.clean()
        got = ScopedState.clean()
        assert judge_readback(expected, got).verdict is Verdict.PASS

    def test_tick_quantized_delay_still_passes(self) -> None:
        # Documented real-world example (otto.link.params docstring): a
        # 0.7ms delay reads back as 699us after 64ns netem tick
        # quantization. Plain dataclass `==` would call this a mismatch
        # (0.7 != 0.699); judge_readback must use `equivalent()` instead.
        sel = Selector(5000)
        expected = ScopedState.from_selectors({sel: (4, ImpairmentParams(delay_ms=0.7))})
        got = ScopedState.from_selectors({sel: (4, ImpairmentParams(delay_ms=0.699))})
        assert expected != got  # proves plain == would wrongly fail this
        assert judge_readback(expected, got).verdict is Verdict.PASS

    def test_mismatched_trees_fail(self) -> None:
        sel = Selector(5000)
        expected = ScopedState.from_selectors({sel: (4, ImpairmentParams(delay_ms=50.0))})
        got = ScopedState.clean()
        result = judge_readback(expected, got)
        assert result.verdict is Verdict.FAIL
        assert result.measured == "clean"
        assert result.wanted == "5000 [delay 50ms]"
        assert result.hint == (
            "otto could not read back the tree it wrote — attach --report to an issue"
        )
        assert result.feature == "read-back"


class TestDescribe:
    def test_every_shape_reads_as_prose(self) -> None:
        sel = Selector(5200, "tcp", end=5210, side="dst")
        params = ImpairmentParams(delay_ms=100.0)
        assert describe(ScopedState.clean()) == "clean"
        assert describe(ScopedState.foreign()) == "foreign"
        assert describe(ScopedState.whole_link(params)) == "whole [delay 100ms]"
        scoped = ScopedState.from_selectors({sel: (4, params)})
        assert describe(scoped) == "5200:5210/tcp dst [delay 100ms]"


class TestExpire:
    def test_clean_after_wait_passes(self) -> None:
        assert judge_expire(ScopedState.clean(), waited_s=6, expire_s=3).verdict is Verdict.PASS

    def test_tree_still_present_fails(self) -> None:
        after = ScopedState.whole_link(ImpairmentParams(delay_ms=50.0))
        result = judge_expire(after, waited_s=9, expire_s=4)
        assert result.verdict is Verdict.FAIL
        assert result.detail == "tree still present 9s after a 4s expire"
        assert result.feature == "expire"


@pytest.mark.parametrize(
    "make_result",
    [
        lambda: judge_delay(QUIET, None, 100.0),
        lambda: judge_jitter(QUIET, None, 20.0),
        lambda: judge_loss(None, 30.0),
        lambda: judge_duplicate(None, 20.0),
        lambda: judge_reorder(None),
        lambda: judge_corrupt(None, 20.0),
    ],
)
def test_none_ping_stats_is_always_fail_no_ping_replies(make_result) -> None:
    result = make_result()
    assert result.verdict is Verdict.FAIL
    assert result.detail == "no ping replies"
