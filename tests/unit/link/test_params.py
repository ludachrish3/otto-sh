"""ImpairmentParams: unit parsing (spec §3.1), merge (spec §3.3), coupling rules."""

import re

import pytest

from otto.link.params import (
    ImpairmentParams,
    Selector,
    canonical_key,
    collides,
    equivalent,
    overlaps,
    parse_percent,
    parse_rate,
    parse_time_ms,
    scope_contains,
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


class TestSelector:
    def test_fields_and_describe(self) -> None:
        from otto.link.params import Selector

        assert Selector(5201).describe() == "5201"
        assert Selector(5201, "tcp").describe() == "5201/tcp"
        assert Selector(53, "udp").describe() == "53/udp"

    def test_distinct_keys_proto_none_vs_tcp(self) -> None:
        from otto.link.params import Selector

        assert Selector(5201) != Selector(5201, "tcp")
        assert len({Selector(5201), Selector(5201, "tcp"), Selector(5201)}) == 2

    def test_port_range_validated(self) -> None:
        from otto.link.params import Selector

        with pytest.raises(ValueError, match="port 0 out of range"):
            Selector(0)
        with pytest.raises(ValueError, match="port 65536 out of range"):
            Selector(65536)
        Selector(1)
        Selector(65535)

    def test_proto_vocabulary_validated(self) -> None:
        from otto.link.params import Selector

        with pytest.raises(ValueError, match="must be tcp or udp"):
            Selector(80, "icmp")


class TestSelectorRangesAndSides:
    def test_positional_forms_are_unchanged(self) -> None:
        sel = Selector(5201, "tcp")
        assert [sel.port, sel.proto, sel.end, sel.side] == [5201, "tcp", None, None]

    def test_end_equal_to_port_normalizes_to_a_single_port(self) -> None:
        assert Selector(5000, end=5000) == Selector(5000)
        assert hash(Selector(5000, end=5000)) == hash(Selector(5000))
        assert Selector(5000, end=5000).end is None

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"port": 5010, "end": 5000}, "5010:5000"),
            ({"port": 1, "end": 65536}, "1:65536"),
            ({"port": 80, "side": "both"}, "side 'both' must be src or dst"),
        ],
    )
    def test_invalid_fields_raise(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=re.escape(match)):
            Selector(**kwargs)

    @pytest.mark.parametrize(
        ("sel", "text"),
        [
            (Selector(5201), "5201"),
            (Selector(5000, end=5010), "5000:5010"),
            (Selector(5000, "tcp", end=5010), "5000:5010/tcp"),
            (Selector(5000, "tcp", end=5010, side="dst"), "5000:5010/tcp dst"),
            (Selector(5000, end=5010, side="src"), "5000:5010 src"),
        ],
    )
    def test_describe(self, sel: Selector, text: str) -> None:
        assert sel.describe() == text

    def test_derived_properties(self) -> None:
        sel = Selector(5000, "udp", end=5010, side="src")
        assert sel.last == 5010
        assert Selector(5201).last == 5201
        assert sel.protos == frozenset({"udp"})
        assert Selector(5201).protos == frozenset({"tcp", "udp"})
        assert sel.sides == frozenset({"src"})
        assert Selector(5201).sides == frozenset({"dst", "src"})
        sel_dst = Selector(1, "tcp", side="dst")
        sel_tcp = Selector(1, "tcp")
        sel_any = Selector(1)
        assert [sel_dst.tier, sel_tcp.tier, sel_any.tier] == [0, 1, 2]
        assert Selector(1, side="src").tier == 1

    @pytest.mark.parametrize(
        ("text", "proto", "side", "expected"),
        [
            ("5201", None, None, Selector(5201)),
            ("5000:5010", "tcp", "dst", Selector(5000, "tcp", end=5010, side="dst")),
            ("5000:5000", None, None, Selector(5000)),
            ("1:65535", None, None, Selector(1, end=65535)),
        ],
    )
    def test_parse_accepts(
        self, text: str, proto: str | None, side: str | None, expected: Selector
    ) -> None:
        assert Selector.parse(text, proto, side) == expected

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("", "PORT or START:END"),
            ("5000:", "PORT or START:END"),
            (":5010", "PORT or START:END"),
            ("50 00", "PORT or START:END"),
            (" 5000", "PORT or START:END"),
            ("5000-5010", "'5000-5010'"),
            ("abc", "PORT or START:END"),
            ("٥٠٠٠", "PORT or START:END"),  # noqa: RUF001 Arabic-Indic digits
            ("0", "out of range"),
            ("65536", "out of range"),
            ("5010:5000", "5010:5000"),
        ],
    )
    def test_parse_rejects(self, text: str, match: str) -> None:
        with pytest.raises(ValueError, match=re.escape(match)):
            Selector.parse(text)


COLLISION_TABLE = [
    # spec §5 table, plus identity and disjoint-range rows
    (Selector(5200, end=5220), Selector(5200, end=5210), True),
    (Selector(5200, end=5220), Selector(5200, "tcp", end=5210), False),
    (Selector(5200, end=5220), Selector(5205, side="dst"), False),
    (Selector(5200, "tcp", end=5220), Selector(5205, "tcp", side="dst"), False),
    (Selector(5200, "tcp", end=5220), Selector(5200, "udp", end=5220), False),
    (Selector(5005, "tcp"), Selector(5005, side="dst"), True),
    (Selector(5005, side="dst"), Selector(5005, side="src"), False),
    (Selector(5200, "tcp", end=5220), Selector(5200, "tcp", end=5220), False),
    (Selector(5200, "tcp", end=5220), Selector(5221, "tcp"), False),
]


@pytest.mark.parametrize(("a", "b", "expected"), COLLISION_TABLE)
def test_collision_table_both_orders(a: Selector, b: Selector, expected: bool) -> None:
    assert collides(a, b) is expected
    assert collides(b, a) is expected


def _cells(sel: Selector) -> set:
    protos = [sel.proto] if sel.proto else ["tcp", "udp"]
    sides = [sel.side] if sel.side else ["dst", "src"]
    return {(p, s, n) for p in protos for s in sides for n in range(sel.port, sel.last + 1)}


def _scope_set(sel: Selector) -> set:
    return {(p, s) for p, s, _ in _cells(sel)}


def test_helpers_match_a_brute_force_cell_oracle() -> None:
    scopes = [(p, s) for p in (None, "tcp", "udp") for s in (None, "dst", "src")]
    ranges = [(5000, None), (5000, 5010), (5005, None), (5011, 5020)]
    sels = [Selector(lo, p, end=hi, side=s) for p, s in scopes for lo, hi in ranges]
    for a in sels:
        for b in sels:
            overlap = bool(_cells(a) & _cells(b))
            nested = _scope_set(a) < _scope_set(b) or _scope_set(b) < _scope_set(a)
            assert overlaps(a, b) is overlap, (a.describe(), b.describe())
            expected_collision = a != b and overlap and not nested
            assert collides(a, b) is expected_collision, (a.describe(), b.describe())
            assert scope_contains(a, b) is (_scope_set(b) <= _scope_set(a))
