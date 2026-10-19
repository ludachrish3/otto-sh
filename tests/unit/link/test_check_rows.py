"""The connect-differential rows read the median of several connects, not one (#658).

``side`` and ``port range`` compare connect times against a threshold of one
delay. With one sample, a single slow round trip on a loaded lab VM failed a
correct ``side`` selector (74 ms against a 50 ms delay, with no packet in the
netem band). These tests feed each row a sequence of connect times through
the module's own ``connect``.
"""

from collections.abc import Iterator

import pytest

from otto.check import Verdict
from otto.link import _check_rows as rows
from otto.link import check_live

DELAY_MS = 50.0


def _feed(monkeypatch: pytest.MonkeyPatch, samples: "list[float]") -> "list[int]":
    """Make ``rows.connect`` return *samples* in order; return the ports it was asked for."""
    it: Iterator[float] = iter(samples)
    ports: "list[int]" = []

    async def connect(_ctx: object, port: int) -> float:
        ports.append(port)
        return next(it)

    monkeypatch.setattr(rows, "connect", connect)
    return ports


async def _no_impair(_params: object, _selector: object) -> None:
    return None


@pytest.mark.asyncio
async def test_one_slow_round_trip_no_longer_fails_a_clean_side_probe(monkeypatch):
    """The #658 run: base 3.5 ms, then one 74.5 ms sample among clean ones.

    A single-connect reading takes the first two samples as base and
    impaired, so it judged this row on the 74.5 ms outlier and failed it.
    """
    _feed(monkeypatch, [3.5, 74.5, 3.5, 3.6, 3.5, 3.7])

    result = await rows.side_verdict(object(), _no_impair, DELAY_MS)

    assert result.verdict == Verdict.PASS


@pytest.mark.asyncio
async def test_one_huge_outlier_is_ignored_where_a_mean_would_fail_the_row(monkeypatch):
    """A 300 ms sample would lift a mean of three by about 100 ms, past one delay."""
    _feed(monkeypatch, [3.5, 3.5, 3.5, 300.0, 3.5, 3.6])

    result = await rows.side_verdict(object(), _no_impair, DELAY_MS)

    assert result.verdict == Verdict.PASS


@pytest.mark.parametrize("at", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.asyncio
async def test_an_outlier_anywhere_in_a_reading_is_ignored(monkeypatch, at):
    """No position in a reading decides it, so no single-sample pick survives."""
    impaired = [3.5, 3.6, 3.7]
    impaired[at] = 74.5
    _feed(monkeypatch, [3.5, 3.5, 3.5, *impaired])
    assert (await rows.side_verdict(object(), _no_impair, DELAY_MS)).verdict == Verdict.PASS

    out_of_range = [3.5, 3.6, 3.7]
    out_of_range[at] = 80.0
    _feed(monkeypatch, [3.4, 3.5, 3.6, 103.0, 104.0, 102.5, *out_of_range])
    result = await rows.port_range_verdict(object(), _no_impair, DELAY_MS)
    assert result.verdict == Verdict.PASS


@pytest.mark.asyncio
async def test_a_side_probe_the_selector_really_delays_still_fails(monkeypatch):
    """Every packet delayed: each sample rises by about two delays, one outlier or not."""
    _feed(monkeypatch, [3.4, 3.5, 3.6, 103.5, 3.9, 104.0])

    result = await rows.side_verdict(object(), _no_impair, DELAY_MS)

    assert result.verdict == Verdict.FAIL


@pytest.mark.asyncio
async def test_every_reading_is_the_median_of_connect_samples_probes(monkeypatch):
    ports = _feed(monkeypatch, [3.5] * (2 * rows.CONNECT_SAMPLES))

    await rows.side_verdict(object(), _no_impair, DELAY_MS)

    assert ports == [rows.IN_RANGE_PORT] * (2 * rows.CONNECT_SAMPLES)


@pytest.mark.asyncio
async def test_the_port_range_readings_probe_base_in_range_then_out_of_range(monkeypatch):
    n = rows.CONNECT_SAMPLES
    ports = _feed(monkeypatch, [3.5] * n + [103.0] * n + [3.5] * n)

    await rows.port_range_verdict(object(), _no_impair, DELAY_MS)

    out, inside = rows.OUT_OF_RANGE_PORT, rows.IN_RANGE_PORT
    assert ports == [out] * n + [inside] * n + [out] * n


@pytest.mark.asyncio
async def test_one_slow_out_of_range_sample_does_not_fail_the_port_range_row(monkeypatch):
    base = [3.4, 3.5, 3.6]
    in_range = [103.0, 104.0, 102.5]
    out_of_range = [3.5, 80.0, 3.6]
    _feed(monkeypatch, [*base, *in_range, *out_of_range])

    result = await rows.port_range_verdict(object(), _no_impair, DELAY_MS)

    assert result.verdict == Verdict.PASS


@pytest.mark.asyncio
async def test_a_port_range_that_delays_the_out_of_range_port_still_fails(monkeypatch):
    base = [3.4, 3.5, 3.6]
    in_range = [103.0, 104.0, 102.5]
    out_of_range = [103.5, 3.6, 104.0]
    _feed(monkeypatch, [*base, *in_range, *out_of_range])

    result = await rows.port_range_verdict(object(), _no_impair, DELAY_MS)

    assert result.verdict == Verdict.FAIL


def test_the_worst_case_budgets_count_every_sample():
    """A budget that still counted one connect per reading would time the row out early."""
    worst = rows.connect_worst_s()
    live = {step.run: step for step in check_live._STEPS}
    budget = check_live._step_worst_s(["side"]) - check_live._step_worst_s([])
    assert budget >= 2 * rows.CONNECT_SAMPLES * worst
    assert live[check_live._step_side].impaired_s == rows.CONNECT_SAMPLES * check_live._CONNECT_S
    assert (
        live[check_live._step_port_range].impaired_s
        == 2 * rows.CONNECT_SAMPLES * check_live._CONNECT_S
    )
    assert rows.CONNECT_SAMPLES >= 3, "a median of fewer than three ignores no outlier"
