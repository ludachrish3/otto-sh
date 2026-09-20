"""A collection interval below 1s is not meaningful — a host must have time to answer."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.models import MIN_INTERVAL_SECONDS, validate_interval
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext
from otto.result import CommandResult, Results
from otto.suite.suite import OttoSuite
from otto.utils import Status, wait_for_async


class TestValidator:
    def test_accepts_the_floor_and_above(self) -> None:
        assert validate_interval(1.0) == 1.0
        assert validate_interval(5.0) == 5.0

    def test_rejects_below_the_floor_naming_the_value_and_the_reason(self) -> None:
        with pytest.raises(ValueError, match="monitor interval"):
            validate_interval(0.5)

    def test_floor_is_one_second(self) -> None:
        assert MIN_INTERVAL_SECONDS == 1.0


class TestLibraryBoundary:
    @pytest.mark.asyncio
    async def test_start_monitor_rejects_a_sub_second_interval(self) -> None:
        suite = OttoSuite()
        with pytest.raises(ValueError, match="interval"):
            await suite.start_monitor(hosts=[], interval=0.1)


class _StubParser(MetricParser):
    """Minimal parser: one data point per tick, no host-specific parsing needed."""

    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint] | None:
        return {"value": MetricDataPoint(value=42.0)}


def _make_instant_host(name: str) -> MagicMock:
    """A mock host whose run() returns immediately — a FAKE host, never a real one."""
    host = MagicMock()
    host.name = name
    host.id = name
    host.log = LogMode.QUIET

    async def _run_cmds(cmds: list[str], timeout: float | None = None) -> Results:
        results = [
            CommandResult(Status.Success, value="42\n", command=cmd, retcode=0) for cmd in cmds
        ]
        return Results.collect(results)

    host.run = AsyncMock(side_effect=_run_cmds)
    return host


class TestEngineIsExempt:
    @pytest.mark.serial_timing
    @pytest.mark.asyncio
    async def test_metric_collector_ticks_faster_than_the_human_floor(self) -> None:
        """The engine is a mechanism, not a human-facing knob.

        Monitor tests drive it at 0.01-0.2s against FAKE hosts; flooring it would
        cost real seconds per tick and protect nobody — no real host is polled on
        that path. This asserts the BEHAVIOUR, not source text: a real
        ``MetricCollector.run()`` loop, driven at a sub-second interval against an
        instant-responding fake host, must reach several ticks in far less time
        than the floor would allow. A collector that silently clamped the
        interval to the floor would need over a second just to complete its
        *second* tick, and could not reach WANTED_TICKS inside the budget below.

        The budget bounds how long the ticks may TAKE; it is not a window the
        ticks are counted inside. Counting inside a fixed window is what this
        test used to do (``duration=0.3s``, then assert on the tally), and a
        single scheduling stall on a loaded runner made it fail with one tick:
        ``run()``'s loop re-checks ``now - start < duration`` before every tick
        after the first, so a stall of a third of a second anywhere in the
        first collection ended the run there, whatever the interval (#407).
        """
        host = _make_instant_host("h1")
        target = MonitorTarget(host=host, parsers={_StubParser.command: _StubParser()})
        collector = MetricCollector(targets=[target])

        requested_interval = 0.05
        wanted_ticks = 5
        budget = 2.0
        # The budget has to sit between the two verdicts with room on each
        # side: honouring the interval reaches wanted_ticks in ~0.2s, clamping
        # to the floor cannot get there in under 4s. Anything in between is a
        # pass for the right reason and a timeout for the right reason.
        floored_cost = (wanted_ticks - 1) * MIN_INTERVAL_SECONDS
        assert requested_interval < MIN_INTERVAL_SECONDS, "the point is staying under the floor"
        assert (wanted_ticks - 1) * requested_interval < budget < floored_cost, (
            f"budget {budget}s no longer discriminates: honouring the interval needs "
            f"{(wanted_ticks - 1) * requested_interval}s, flooring needs {floored_cost}s"
        )

        def ticks() -> int:
            return len(collector.get_series().get("h1/value", []))

        run = asyncio.create_task(collector.run(interval=timedelta(seconds=requested_interval)))
        try:
            await wait_for_async(
                lambda: ticks() >= wanted_ticks,
                budget,
                interval=0.01,
                on_timeout=lambda: (
                    f"expected {wanted_ticks} ticks at a {requested_interval}s interval "
                    f"within {budget}s, got {ticks()} — looks like the interval was "
                    f"floored to MIN_INTERVAL_SECONDS somewhere"
                ),
            )
        finally:
            run.cancel()
            with suppress(asyncio.CancelledError):
                await run
        # The effective interval reported on the wire must be what was asked
        # for, not silently raised.
        assert collector.get_meta_model().interval == requested_interval
