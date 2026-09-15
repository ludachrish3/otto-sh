"""The external sweep: a bounded port set, two dials in flight, injected timeouts."""

import asyncio

import pytest

from otto.host.survey.dial import DialOutcome
from otto.host.survey.sweep import SweepRow, parse_scan_ports, sweep_port_set, sweep_ports


@pytest.mark.parametrize(
    ("text", "ports"),
    [
        (None, []),
        ("", []),
        ("  ", []),
        ("2323", [2323]),
        ("2323,8023", [2323, 8023]),
        ("2000-2003,8080", [2000, 2001, 2002, 2003, 8080]),
        ("23,23,24", [23, 24]),
        (" 23 , 24 ", [23, 24]),
    ],
)
def test_scan_ports_parse(text, ports):
    assert parse_scan_ports(text) == ports


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("abc", "not a port"),
        ("0", "outside 1..65535"),
        ("70000", "outside 1..65535"),
        ("30-20", "reversed"),
        ("1-65535", "wider than 1024"),
        ("1-", "'1-' is not a port range"),
        ("-5", "'-5' is not a port range"),
    ],
)
def test_scan_ports_refuses_bad_input_naming_the_flag(text, reason):
    """Mutation: accept 1-65535 and the sweep becomes a range scan by default."""
    with pytest.raises(ValueError, match=rf"--scan-ports: .*{reason}"):
        parse_scan_ports(text)


def test_the_port_set_is_declared_then_defaults_then_alternates_then_extra_deduplicated():
    declared, defaults, alternates, extra = [2325], [23, 161], [2323, 8023, 1161], [23, 9000]
    assert sweep_port_set(declared, defaults, alternates, extra) == [
        2325,
        23,
        161,
        2323,
        8023,
        1161,
        9000,
    ]


@pytest.mark.asyncio
async def test_at_most_two_dials_are_in_flight_and_order_is_kept():
    """Mutation: drop the semaphore and `peak` reaches 5 (a Zephyr socket pool has ~4 slots)."""
    in_flight = 0
    peak = 0

    async def dial(port):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return DialOutcome(state="closed", detail=str(port))

    rows = await sweep_ports([1, 2, 3, 4, 5], dial, concurrency=2)
    assert peak == 2
    assert [r.port for r in rows] == [1, 2, 3, 4, 5]
    assert rows[2] == SweepRow(port=3, outcome=DialOutcome(state="closed", detail="3"))


@pytest.mark.asyncio
async def test_a_dial_that_raises_is_not_checkable_not_fatal():
    async def dial(port):
        if port == 2:
            raise OSError("no route")
        return DialOutcome(state="open")

    rows = await sweep_ports([1, 2], dial, concurrency=2)
    assert rows[1].outcome.state == "not-checkable"
    assert "no route" in rows[1].outcome.detail
    assert rows[0].outcome.state == "open"


@pytest.mark.asyncio
async def test_a_cancelled_dial_propagates_it_is_not_a_not_checkable_row():
    """Mutation: widen `except Exception` to `except BaseException` and cancellation
    is absorbed into a plausible row set instead of aborting the sweep."""

    async def dial(port):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await sweep_ports([1], dial, concurrency=2)


@pytest.mark.asyncio
async def test_sweep_ports_refuses_non_positive_concurrency():
    async def dial(port):
        return DialOutcome(state="open")

    with pytest.raises(ValueError, match="concurrency must be at least 1"):
        await sweep_ports([1], dial, concurrency=0)
