"""Verdict vocabulary shared by every otto check (spec 2026-09-24 §3.1)."""

import pytest

from otto.check import FeatureResult, UnmeasuredReason, Verdict, count_verdicts


class TestVerdict:
    def test_values_are_the_spec_words(self) -> None:
        assert [v.value for v in Verdict] == [
            "pass",
            "fail",
            "unsupported",
            "unmeasured",
            "skipped",
        ]

    @pytest.mark.parametrize(
        ("verdict", "fails"),
        [
            (Verdict.PASS, False),
            (Verdict.FAIL, True),
            (Verdict.UNSUPPORTED, True),
            (Verdict.UNMEASURED, False),
            (Verdict.SKIPPED, False),
        ],
    )
    def test_only_fail_and_unsupported_fail_the_run(self, verdict: Verdict, fails: bool) -> None:
        assert verdict.fails is fails

    def test_reason_codes_are_the_spec_words(self) -> None:
        assert [r.value for r in UnmeasuredReason] == [
            "missing-tool",
            "noisy-baseline",
            "no-reply-oracle",
            "no-clock",
        ]


class TestFeatureResult:
    def test_unmeasured_requires_a_reason(self) -> None:
        with pytest.raises(ValueError, match="unmeasured needs a reason"):
            FeatureResult("rate", Verdict.UNMEASURED)

    @pytest.mark.parametrize("verdict", [v for v in Verdict if v is not Verdict.UNMEASURED])
    def test_only_unmeasured_may_carry_a_reason(self, verdict: Verdict) -> None:
        with pytest.raises(ValueError, match="only unmeasured carries a reason"):
            FeatureResult("rate", verdict, reason=UnmeasuredReason.MISSING_TOOL)

    def test_a_well_formed_unmeasured(self) -> None:
        r = FeatureResult(
            "rate",
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.MISSING_TOOL,
            detail="needs socat or python3",
        )
        assert r.reason is UnmeasuredReason.MISSING_TOOL
        assert r.commands == []


def test_count_verdicts_is_zero_filled_in_enum_order() -> None:
    results = [
        FeatureResult("delay", Verdict.PASS),
        FeatureResult("loss", Verdict.PASS),
        FeatureResult("rate", Verdict.FAIL),
    ]
    counts = count_verdicts(results)
    assert list(counts) == list(Verdict)
    assert counts[Verdict.PASS] == 2
    assert counts[Verdict.FAIL] == 1
    assert counts[Verdict.SKIPPED] == 0
