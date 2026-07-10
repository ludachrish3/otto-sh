"""ImpairmentParams: unit parsing (spec §3.1), merge (spec §3.3), coupling rules."""

import pytest

from otto.link.params import (
    ImpairmentParams,
    canonical_key,
    equivalent,
    parse_percent,
    parse_rate,
    parse_time_ms,
)


class TestParsing:
    def test_bare_time_is_milliseconds(self) -> None:
        assert parse_time_ms("50", option="--delay") == 50.0

    @pytest.mark.parametrize(
        ("text", "ms"), [("500us", 0.5), ("50ms", 50.0), ("1.5s", 1500.0), ("0", 0.0)]
    )
    def test_time_suffixes(self, text: str, ms: float) -> None:
        assert parse_time_ms(text, option="--delay") == ms

    def test_bad_time_names_option(self) -> None:
        with pytest.raises(ValueError, match=r"--jitter .* not a time value"):
            parse_time_ms("fast", option="--jitter")

    @pytest.mark.parametrize(("text", "pct"), [("2", 2.0), ("2%", 2.0), ("0", 0.0), ("0.5", 0.5)])
    def test_bare_percent_is_percent(self, text: str, pct: float) -> None:
        assert parse_percent(text, option="--loss") == pct

    def test_percent_over_100_rejected(self) -> None:
        with pytest.raises(ValueError, match="over 100"):
            parse_percent("150", option="--loss")

    def test_rate_requires_explicit_unit(self) -> None:
        with pytest.raises(ValueError, match="explicit unit"):
            parse_rate("10")

    def test_rate_unit_accepted_and_lowercased(self) -> None:
        assert parse_rate("10Mbit") == "10mbit"

    def test_rate_bare_zero_is_the_clear_sentinel(self) -> None:
        assert parse_rate("0") == "0"


class TestMerge:
    def test_last_one_wins_per_param(self) -> None:
        base = ImpairmentParams(delay_ms=20.0)
        new = ImpairmentParams(delay_ms=10.0, loss_pct=2.0)
        assert new.merged_over(base) == ImpairmentParams(delay_ms=10.0, loss_pct=2.0)

    def test_unset_params_persist_from_base(self) -> None:
        base = ImpairmentParams(delay_ms=20.0, rate="10mbit")
        assert ImpairmentParams(loss_pct=1.0).merged_over(base) == ImpairmentParams(
            delay_ms=20.0, loss_pct=1.0, rate="10mbit"
        )

    def test_explicit_zero_clears_just_that_param(self) -> None:
        base = ImpairmentParams(delay_ms=20.0, loss_pct=2.0)
        merged = ImpairmentParams(loss_pct=0.0).merged_over(base)
        assert merged == ImpairmentParams(delay_ms=20.0)

    def test_zero_rate_clears_rate(self) -> None:
        merged = ImpairmentParams(rate="0").merged_over(ImpairmentParams(rate="10mbit"))
        assert merged.rate is None

    def test_all_cleared_is_empty(self) -> None:
        assert (
            ImpairmentParams(delay_ms=0.0).merged_over(ImpairmentParams(delay_ms=50.0)).is_empty()
        )


class TestValidateAndDescribe:
    def test_jitter_without_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="--jitter requires a delay"):
            ImpairmentParams(jitter_ms=5.0).validate()

    def test_reorder_without_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="--reorder requires a delay"):
            ImpairmentParams(reorder_pct=5.0).validate()

    def test_jitter_with_merged_delay_ok(self) -> None:
        ImpairmentParams(jitter_ms=5.0).merged_over(ImpairmentParams(delay_ms=50.0)).validate()

    def test_describe(self) -> None:
        p = ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0, rate="10mbit")
        assert p.describe() == "delay 50ms 5ms loss 2% rate 10mbit"

    def test_describe_empty(self) -> None:
        assert ImpairmentParams().describe() == ""


class TestExtendedRateVocabulary:
    @pytest.mark.parametrize("text", ["1tbit", "1tbps", "1kibit", "1mibit", "1gibps", "1tibps"])
    def test_tc_extended_units_accepted(self, text: str) -> None:
        assert parse_rate(text) == text  # already lowercase, echoed back canonical


class TestCanonicalEquivalence:
    """tc reformats values on display; verify must compare meaning, not spelling."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1.5mbit", "1500kbit"),
            ("10mbps", "80mbit"),
            ("1tbit", "1000gbit"),
            ("1kibit", "1024bit"),
        ],
    )
    def test_rate_equivalence_pairs(self, a: str, b: str) -> None:
        assert equivalent(
            ImpairmentParams(rate=parse_rate(a)), ImpairmentParams(rate=parse_rate(b))
        )

    def test_genuinely_different_rates_not_equivalent(self) -> None:
        assert not equivalent(
            ImpairmentParams(rate=parse_rate("10mbit")), ImpairmentParams(rate=parse_rate("20mbit"))
        )

    def test_sub_ms_time_equivalent_across_spellings(self) -> None:
        # 0.7ms and 700us are the SAME time; the 700*0.001 float-dust must round away.
        a = ImpairmentParams(delay_ms=parse_time_ms("0.7", option="--delay"))
        b = ImpairmentParams(delay_ms=parse_time_ms("700us", option="--delay"))
        assert equivalent(a, b)
        assert canonical_key(a) == canonical_key(b)

    def test_genuinely_different_delay_not_equivalent(self) -> None:
        assert not equivalent(ImpairmentParams(delay_ms=50.0), ImpairmentParams(delay_ms=51.0))

    def test_none_rate_stays_none_in_key(self) -> None:
        key = canonical_key(ImpairmentParams(delay_ms=1.0))
        assert key[-1] is None  # rate slot

    def test_percent_equivalence_ignores_float_dust(self) -> None:
        assert equivalent(ImpairmentParams(loss_pct=2.0), ImpairmentParams(loss_pct=2.0000000001))


class TestTickQuantizationTolerance:
    """netem quantizes delay/jitter to 64ns psched ticks; tc's display is
    µs-truncated, so ``0.7ms`` can read back as ``0.699ms`` (observed live,
    2026-07-10). ``equivalent()`` tolerates this for TIME fields only."""

    def test_tick_quantized_delay_within_tolerance_is_equivalent(self) -> None:
        # 700us vs 699us: 1us delta, well under the 2us floor.
        assert equivalent(ImpairmentParams(delay_ms=0.7), ImpairmentParams(delay_ms=0.699))

    def test_delay_beyond_tolerance_is_not_equivalent(self) -> None:
        # 700us vs 690us: 10us delta > the 2us/0.5% tolerance -- a real mismatch.
        assert not equivalent(ImpairmentParams(delay_ms=0.7), ImpairmentParams(delay_ms=0.690))

    def test_none_vs_present_time_field_is_never_tolerance_close(self) -> None:
        # A missing field must never be treated as tolerance-close to a present one.
        assert not equivalent(ImpairmentParams(delay_ms=None), ImpairmentParams(delay_ms=0.001))

    def test_second_scale_relative_tolerance_within_bound(self) -> None:
        # 2000ms vs 2003ms: 3000us delta, 0.15% of 2,000,000us -- within the 0.5% band.
        assert equivalent(ImpairmentParams(delay_ms=2000.0), ImpairmentParams(delay_ms=2003.0))

    def test_second_scale_relative_tolerance_exceeded(self) -> None:
        # 2000ms vs 2050ms: 50000us delta, 2.5% of 2,000,000us -- beyond the 0.5% band.
        assert not equivalent(ImpairmentParams(delay_ms=2000.0), ImpairmentParams(delay_ms=2050.0))

    def test_jitter_also_gets_tolerance(self) -> None:
        assert equivalent(
            ImpairmentParams(delay_ms=50.0, jitter_ms=0.7),
            ImpairmentParams(delay_ms=50.0, jitter_ms=0.699),
        )

    def test_percent_stays_exact_not_tolerant(self) -> None:
        assert not equivalent(ImpairmentParams(loss_pct=2.0), ImpairmentParams(loss_pct=2.001))

    def test_rate_stays_exact_not_tolerant(self) -> None:
        assert not equivalent(
            ImpairmentParams(rate=parse_rate("10mbit")),
            ImpairmentParams(rate=parse_rate("10000001bit")),
        )
