"""Collector routing for the parse_tick() contract: data-carried timestamps + log events."""

from datetime import datetime, timezone
from typing import ClassVar

import pytest
from typing_extensions import override

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import (
    LogEvent,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    TickResult,
    TimedSample,
)
from otto.result import CommandResult
from otto.utils import Status

TS1 = datetime(2026, 7, 4, 11, 0, tzinfo=timezone.utc)
TS2 = datetime(2026, 7, 4, 11, 5, tzinfo=timezone.utc)
TICK = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class _BackdatedParser(MetricParser):
    """Emits two samples carrying their own (older) timestamps."""

    y_title = "V"
    unit = ""
    command = "cat /var/log/perf.csv"
    tab = "metrics"
    tab_label = "Metrics"
    chart = "Perf"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        return TickResult(
            samples=[
                TimedSample(ts=TS1, series={"Perf": MetricDataPoint(1.0)}),
                TimedSample(ts=TS2, series={"Perf": MetricDataPoint(2.0)}),
            ],
            events=[],
        )


class _EventParser(MetricParser):
    """Emits only log events."""

    y_title = ""
    unit = ""
    command = "tail -n 200 /var/log/app.log"
    tab = "applog"
    tab_label = "App log"
    chart = "App log"
    table_columns: ClassVar = ["message"]

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        return TickResult(samples=[], events=[LogEvent(ts=TS1, fields={"message": "hello"})])


def _result(parser: MetricParser) -> CommandResult:
    return CommandResult(Status.Success, value="raw", command=parser.command, retcode=0)


async def _process(collector: MetricCollector, parser: MetricParser) -> None:
    await collector._process_host_results(
        "host1",
        TICK,
        [_result(parser)],
        {parser.command: parser},
        ctx=ParseContext(ts=TICK),
    )


@pytest.mark.asyncio
async def test_backdated_samples_keep_their_own_timestamps() -> None:
    collector = MetricCollector(hosts=[])
    await _process(collector, _BackdatedParser())
    pts = collector.get_series()["host1/Perf"]
    assert [(p.ts, p.value) for p in pts] == [(TS1, 1.0), (TS2, 2.0)]


@pytest.mark.asyncio
async def test_untimed_sample_gets_tick_timestamp() -> None:
    class _Plain(MetricParser):
        y_title = ""
        unit = ""
        command = "echo 1"
        chart = "Plain"

        @override
        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {"Plain": MetricDataPoint(1.0)}

    collector = MetricCollector(hosts=[])
    await _process(collector, _Plain())
    assert collector.get_series()["host1/Plain"][0].ts == TICK


@pytest.mark.asyncio
async def test_events_land_in_store_ring_tagged_with_parser_tab() -> None:
    collector = MetricCollector(hosts=[])
    await _process(collector, _EventParser())
    assert collector._store.snapshot_log_events() == [
        ("host1", "applog", LogEvent(ts=TS1, fields={"message": "hello"}))
    ]


@pytest.mark.asyncio
async def test_csv_parser_backfills_store_with_data_timestamps() -> None:
    from otto.monitor.log_sourced import CsvMetricParser

    parser = CsvMetricParser("cat /var/log/perf.csv", columns=["v"], chart="Perf")
    out = "2026-07-04T11:00:00,1\n2026-07-04T11:05:00,2\n"
    collector = MetricCollector(hosts=[])
    await collector._process_host_results(
        "host1",
        TICK,
        [CommandResult(Status.Success, value=out, command=parser.command, retcode=0)],
        {parser.command: parser},
        ctx=ParseContext(ts=TICK),
    )
    pts = collector.get_series()["host1/v"]
    assert [(p.ts, p.value) for p in pts] == [(TS1, 1.0), (TS2, 2.0)]
