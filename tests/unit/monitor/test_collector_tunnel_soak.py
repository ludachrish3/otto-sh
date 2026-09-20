"""Long-sequence soak of the collector's tunnel loop (spec §5): last-known
state under alternating healthy/raising scans, loop survival, warn-once latch,
no state growth. Single-shot guards live in test_collector_tunnels.py — these
are the N-tick extensions. Marked `concurrency`: no-VM, rides stability-unit."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any

import pytest

from otto.models.monitor import TunnelRecord
from otto.monitor.collector import MetricCollector
from otto.utils import wait_for_async

pytestmark = pytest.mark.concurrency


def _rec(tid: str, status: str = "ok") -> TunnelRecord:
    return TunnelRecord(
        id=tid,
        protocol="udp",
        service_port=15160,
        hops=["a", "b"],
        status=status,  # type: ignore[arg-type]
        carriers_present=4,
        carriers_expected=4,
    )


class _Script:
    """A tunnel_source scripted per call: a list -> that set; an Exception -> raise.
    Unlike test_collector_tunnels.py's, this one CYCLES its script forever."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> list[TunnelRecord]:
        result = self._results[self.calls % len(self._results)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _collector(source: _Script) -> MetricCollector:
    c = MetricCollector(hosts=[], tunnel_source=source)
    c.session_id = "soak"
    c._publish = lambda frag: None  # type: ignore[method-assign]
    return c


def test_long_alternating_sequence_never_blanks() -> None:
    """60 ticks of healthy/failing/degraded/empty interleave: after every tick
    the retained set equals the LAST SUCCESSFUL scan — a failure never blanks,
    a success always replaces."""
    healthy = [_rec("tun-a-1")]
    degraded = [_rec("tun-a-1", status="degraded"), _rec("tun-b-2")]
    script = [healthy, RuntimeError("down"), degraded, RuntimeError("down"), [], healthy]
    source = _Script(*script)
    c = _collector(source)
    expected_last: list[TunnelRecord] = []
    for tick in range(60):
        step = script[tick % len(script)]
        if not isinstance(step, Exception):
            expected_last = step
        asyncio.run(c._tunnel_pass())
        assert c.get_tunnel_records() == expected_last, f"tick {tick}: retained set wrong"
        assert len(c.get_tunnel_records()) <= 2, f"tick {tick}: state grew"


def test_warn_latch_across_repeated_failure_bursts(caplog: pytest.LogCaptureFixture) -> None:
    """Each failure BURST warns exactly once (plus one recovery warn); a
    10-tick burst is not 10 warnings."""
    source = _Script(
        [_rec("tun-a-1")],
        *([RuntimeError("down")] * 10),
        [_rec("tun-a-1")],
        *([RuntimeError("down")] * 10),
        [_rec("tun-a-1")],
    )
    c = _collector(source)
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.collector"):
        for _ in range(23):
            asyncio.run(c._tunnel_pass())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # burst1 fail + burst1 recovery + burst2 fail + burst2 recovery = 4
    assert len(warnings) == 4, [w.message for w in warnings]


def test_raising_source_never_kills_the_run_loop() -> None:
    """run() with a source that raises on every other call: the loop keeps
    ticking instead of dying at the first raise.

    The budget bounds how long the four scans may TAKE; it is not a window
    they are counted inside. This test used to spell that the other way
    round (``duration=120ms``, then ``assert source.calls >= 4``), and
    ``run()``'s loops re-check ``now - start < duration`` before every tick
    after the first: one scheduling stall inside that 120ms ended the run at
    a single scan, so no floor above 1 survived it (#407). Waiting for the
    scans instead makes a stalled runner slow, not red — and a loop that
    really died at the first raise still fails, on the timeout, naming the
    count it reached.
    """
    from otto.monitor.collector import MonitorTarget

    class _Host:
        name = "h1"
        id = "h1"

        async def run(self, commands: list[str], **kwargs: Any) -> Any:
            raise RuntimeError("no shell in this test")

    source = _Script([_rec("tun-a-1")], RuntimeError("down"))
    c = MetricCollector(
        targets=[MonitorTarget(host=_Host(), parsers={})],  # type: ignore[arg-type]
        tunnel_source=source,
    )
    wanted_scans = 4  # two full success/raise cycles of _Script

    async def _drive() -> None:
        # No duration: the loop runs until cancelled, so nothing can end it
        # early but this test.
        run = asyncio.create_task(c.run(interval=timedelta(milliseconds=10)))
        try:
            await wait_for_async(
                lambda: source.calls >= wanted_scans,
                5.0,
                interval=0.005,
                on_timeout=lambda: (
                    f"loop died early: only {source.calls} scans in 5.0s at a 10ms "
                    f"interval (wanted {wanted_scans})"
                ),
            )
        finally:
            run.cancel()
            with suppress(asyncio.CancelledError):
                await run

    asyncio.run(_drive())
    # The loop above can stop on either parity of source.calls (a
    # success tick or a failure tick), and only stopping on a failure tick
    # exercises the "never blank on failure" path this assertion checks. Land
    # deterministically on a scripted failure by adding one more _tunnel_pass
    # call whenever the loop happened to stop on a success (odd call count) —
    # _Script's cycle guarantees the next call is the RuntimeError branch.
    if source.calls % 2 == 1:
        asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == [_rec("tun-a-1")]
