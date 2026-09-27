"""otto.check.sweep: the age rule both checks' leftover sweeps follow, and its bound.

The bound is only right while it exceeds the longest either check can run. The
worst cases are computed here from each check's own budget function, which is
built from the same timeouts the checks run with, so raising a timeout (or
adding a probe) past the bound fails this module instead of letting a sweep
take a running check's leftovers.
"""

import ast
import re
from pathlib import Path

import pytest

from otto.check import CHECK_HOST_TIMEOUT, SWEEP_MIN_AGE_S
from otto.check import sweep as sweep_module
from otto.check.sweep import (
    COMMAND_ALLOWANCE_S,
    age_text,
    left_line,
    probe_worst_s,
    run_end_worst_s,
    sweepable,
    swept_note,
)
from otto.link import check as link_check
from otto.link.check import FEATURES
from otto.link.check import worst_case_run_s as link_worst_case_run_s
from otto.link.check_live import LIVE_FEATURES, live_worst_case_s
from otto.tunnel._tunnel_plan import worst_case_run_s as tunnel_worst_case_run_s

BOTH = ["tcp", "udp"]


class TestTheRule:
    def test_a_leftover_at_the_bound_is_left_and_one_second_older_is_swept(self) -> None:
        assert not sweepable(SWEEP_MIN_AGE_S)
        assert sweepable(SWEEP_MIN_AGE_S + 1)

    def test_a_young_leftover_is_left(self) -> None:
        assert not sweepable(0)
        assert not sweepable(40)

    def test_an_unknown_age_is_swept_so_the_self_heal_never_stops(self) -> None:
        assert sweepable(None)


class TestTheLines:
    def test_a_left_leftover_says_how_old_and_why(self) -> None:
        assert left_line("otto-check echo fwd-echo", "test2", 40) == (
            "left otto-check echo fwd-echo on test2 (started 40 s ago — may be a running check)"
        )
        assert left_line("otto-check-3fa9c1 namespace", "test1", 40, verb="created") == (
            "left otto-check-3fa9c1 namespace on test1 (created 40 s ago — may be a running check)"
        )

    def test_a_swept_leftover_says_how_old_or_that_its_age_is_unknown(self) -> None:
        assert swept_note(2760) == "earlier or concurrent run, started 46 min ago"
        assert swept_note(None) == "earlier or concurrent run, age unknown"

    @pytest.mark.parametrize(
        ("seconds", "said"), [(0, "0 s"), (119, "119 s"), (120, "2 min"), (2700, "45 min")]
    )
    def test_ages_under_two_minutes_are_seconds_then_whole_minutes(self, seconds, said) -> None:
        assert age_text(seconds) == said


class TestTheCharges:
    def test_a_probe_is_charged_its_timers_plus_a_command_capped_at_the_cut_off(self) -> None:
        assert probe_worst_s(7) == 7 + COMMAND_ALLOWANCE_S
        assert probe_worst_s(10 * CHECK_HOST_TIMEOUT) == CHECK_HOST_TIMEOUT

    def test_a_run_end_waits_the_cut_off_once_more_per_teardown_command(self) -> None:
        assert run_end_worst_s(3) == 4 * CHECK_HOST_TIMEOUT


class TestTheBoundClearsEveryWorstCase:
    """The shapes :data:`SWEEP_MIN_AGE_S`'s docstring derives, each held under the bound."""

    @pytest.mark.parametrize("echo_hosts", [2, 3, 4, 5])
    @pytest.mark.parametrize("split_dest", [False, True])
    def test_the_tunnel_check(self, echo_hosts, split_dest) -> None:
        worst = tunnel_worst_case_run_s(echo_hosts, BOTH, split_dest=split_dest)
        assert worst < SWEEP_MIN_AGE_S, f"{echo_hosts} echo hosts: {worst:.0f} s"

    @pytest.mark.parametrize("hosts", [1, 2])
    @pytest.mark.parametrize("live", [False, True])
    def test_the_link_check(self, hosts, live) -> None:
        worst = link_worst_case_run_s(FEATURES, hosts=hosts, live=live)
        assert worst < SWEEP_MIN_AGE_S, f"{hosts} hosts, live={live}: {worst:.0f} s"

    def test_the_bound_clears_the_longest_by_half_again(self) -> None:
        longest = max(
            tunnel_worst_case_run_s(5, BOTH, split_dest=True),
            link_worst_case_run_s(FEATURES, hosts=2, live=True),
        )
        assert 1.5 * longest <= SWEEP_MIN_AGE_S

    def test_every_sandbox_feature_has_a_charge(self) -> None:
        charged = {f: link_check._row_worst_s(f) for f in FEATURES}
        assert set(charged) == set(link_check._RUNNERS)
        assert all(cost > 0 for cost in charged.values())

    def test_every_live_step_has_a_charge(self) -> None:
        one = live_worst_case_s(LIVE_FEATURES, 1)
        assert live_worst_case_s(LIVE_FEATURES, 2) > one > 0

    def test_the_docstring_s_totals_are_the_computed_ones(self) -> None:
        """The arithmetic written into the bound's docstring may not drift from the code."""
        text = _bound_docstring()
        totals = [int(t) for t in re.findall(r"= (\d+) s\b", text)]
        assert totals == [
            round(tunnel_worst_case_run_s(3, BOTH)),
            round(tunnel_worst_case_run_s(5, BOTH)),
            round(link_worst_case_run_s(FEATURES, hosts=2, live=True)),
        ]
        assert f"{SWEEP_MIN_AGE_S // 60} minutes" in text

    def test_the_docstring_s_addends_sum_to_its_totals(self) -> None:
        """Each itemised worst case adds up: ``2 x 354 (…) + 32 (…) + … = 1697 s``."""
        flat = " ".join(_bound_docstring().split())
        sums = re.findall(r": ([^:]*?) = (\d+) s\b", flat)
        assert len(sums) == 3, sums
        for addends, total in sums:
            terms = re.sub(r"\([^)]*\)", "", addends).split("+")
            added = 0.0
            for term in terms:
                factors = [float(f) for f in term.split(" x ")]
                added += factors[0] * (factors[1] if len(factors) > 1 else 1)
            assert round(added) == int(total), f"{addends} = {total} s"


def _bound_docstring() -> str:
    """``SWEEP_MIN_AGE_S``'s docstring, read from the source (a constant has no ``__doc__``)."""
    source = Path(sweep_module.__file__).read_text(encoding="utf-8")
    body = ast.parse(source).body
    index = next(
        i
        for i, node in enumerate(body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SWEEP_MIN_AGE_S" for t in node.targets)
    )
    doc = body[index + 1]
    assert isinstance(doc, ast.Expr)
    assert isinstance(doc.value, ast.Constant)
    return str(doc.value.value)
