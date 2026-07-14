"""A collection interval below 1s is not meaningful — a host must have time to answer."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.models import MIN_INTERVAL_SECONDS, validate_interval
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext
from otto.result import CommandResult, Results
from otto.suite.suite import OttoSuite
from otto.utils import Status


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
    @pytest.mark.asyncio
    async def test_metric_collector_ticks_faster_than_the_human_floor(self) -> None:
        """The engine is a mechanism, not a human-facing knob.

        Monitor tests drive it at 0.01-0.2s against FAKE hosts; flooring it would
        cost real seconds per tick and protect nobody — no real host is polled on
        that path. This asserts the BEHAVIOUR, not source text: a real
        ``MetricCollector.run()`` loop, driven at a sub-second interval against an
        instant-responding fake host, must land several ticks well inside
        ``MIN_INTERVAL_SECONDS``'s own time budget. A collector that silently
        clamped the interval to the floor would need over a second just to
        complete its *second* tick, blowing every assertion below.
        """
        host = _make_instant_host("h1")
        target = MonitorTarget(host=host, parsers={_StubParser.command: _StubParser()})
        collector = MetricCollector(targets=[target])

        requested_interval = 0.05
        run_duration = 0.3
        assert run_duration < MIN_INTERVAL_SECONDS, "the point is staying under the floor"

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(seconds=requested_interval),
            duration=timedelta(seconds=run_duration),
        )
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed < MIN_INTERVAL_SECONDS, (
            f"run() took {elapsed:.2f}s to cover a {run_duration}s duration at a "
            f"{requested_interval}s interval — looks like the interval was floored "
            "to MIN_INTERVAL_SECONDS somewhere"
        )
        ticks = len(collector.get_series()["h1/value"])
        assert ticks >= 5, (
            f"expected several sub-second ticks within {run_duration}s at a "
            f"{requested_interval}s interval, got {ticks}"
        )
        # The effective interval reported on the wire must be what was asked
        # for, not silently raised.
        assert collector.get_meta_model().interval == requested_interval
