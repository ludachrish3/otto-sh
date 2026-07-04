"""Parser-health warning layers, driven through the real _process_host_results.

Command failures are EDGE-TRIGGERED: every ok->failed transition warns (so
transient/intermittent failures are logged whenever they happen), every
failed->ok transition warns the recovery with the outage length, and a
sustained outage logs once — not once per tick. The never-produced backstop
stays warn-once."""

from datetime import datetime, timezone

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext
from otto.result import CommandResult
from otto.utils import Status

TS = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class _NeverParses(MetricParser):
    chart = "Sockets"
    y_title = "Sockets"
    unit = ""
    command = "ss -s"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}


class _ParsesFine(MetricParser):
    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {"Test": MetricDataPoint(42.0)}


def _failed(cmd: str, retcode: int, output: str) -> CommandResult:
    return CommandResult(Status.Error, value=output, command=cmd, retcode=retcode)


def _ok(cmd: str, output: str = "42\n") -> CommandResult:
    return CommandResult(Status.Success, value=output, command=cmd, retcode=0)


@pytest.fixture
def collector() -> MetricCollector:
    return MetricCollector(targets=[])


async def _tick(collector, parsers, results):
    await collector._process_host_results("test1", TS, results, parsers, ctx=ParseContext())


class TestCommandFailedWarning:
    @pytest.mark.asyncio
    async def test_sustained_failure_warns_once_with_details(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 127, "sh: ss: command not found")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(4):
                await _tick(collector, parsers, [failed])
        warnings = [r for r in caplog.records if "failed on test1" in r.message]
        assert len(warnings) == 1  # edge-triggered: one warning per outage, not per tick
        msg = warnings[0].message
        assert "'ss -s'" in msg
        assert "(exit 127)" in msg
        assert "sh: ss: command not found" in msg
        assert "Sockets metrics will be missing" in msg

    @pytest.mark.asyncio
    async def test_transient_failures_warn_every_time(self, collector, caplog):
        """fail -> ok -> fail is TWO outages: each transition logs. Intermittent
        issues after collection starts must never be swallowed."""
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 1, "read: connection reset")
        with caplog.at_level("WARNING", logger="otto"):
            await _tick(collector, parsers, [failed])
            await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
            await _tick(collector, parsers, [failed])
        failures = [r for r in caplog.records if "failed on test1" in r.message]
        recoveries = [r for r in caplog.records if "recovered on test1" in r.message]
        assert len(failures) == 2
        assert len(recoveries) == 1

    @pytest.mark.asyncio
    async def test_recovery_reports_outage_length(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 1, "transient")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(3):
                await _tick(collector, parsers, [failed])
            await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        recoveries = [r for r in caplog.records if "recovered on test1" in r.message]
        assert len(recoveries) == 1
        assert "after 3 failed tick(s)" in recoveries[0].message

    @pytest.mark.asyncio
    async def test_late_first_failure_still_warns(self, collector, caplog):
        """A command that worked for many ticks then breaks (network blip long
        after startup) warns on that transition."""
        parsers = {"echo 42": _ParsesFine()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(10):
                await _tick(collector, parsers, [_ok("echo 42")])
            await _tick(collector, parsers, [_failed("echo 42", 1, "blip")])
        assert [r for r in caplog.records if "failed on test1" in r.message]

    @pytest.mark.asyncio
    async def test_success_never_warns(self, collector, caplog):
        parsers = {"echo 42": _ParsesFine()}
        with caplog.at_level("WARNING", logger="otto"):
            await _tick(collector, parsers, [_ok("echo 42")])
        assert not [r for r in caplog.records if "failed on" in r.message]
        assert not [r for r in caplog.records if "recovered on" in r.message]


class TestSilentParserWarning:
    @pytest.mark.asyncio
    async def test_never_produced_by_tick_3_warns_once(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(5):
                await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 1
        assert "_NeverParses" in warnings[0].message
        assert "after 3 ticks" in warnings[0].message

    @pytest.mark.asyncio
    async def test_two_empty_ticks_do_not_warn(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(2):
                await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        assert not [r for r in caplog.records if "has produced no data" in r.message]

    @pytest.mark.asyncio
    async def test_early_data_disarms_later_droughts(self, collector, caplog):
        """Rule is never-produced-by-tick-3, NOT consecutive empties: sparse
        sources legitimately go quiet between writes."""

        class _SparseParser(MetricParser):
            chart = "Sparse"
            y_title = "V"
            unit = ""
            command = "cat sparse"
            _tick = 0

            def parse(self, output, *, ctx):
                self._tick += 1
                return {"Sparse": MetricDataPoint(1.0)} if self._tick == 1 else {}

        parsers = {"cat sparse": _SparseParser()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(6):
                await _tick(collector, parsers, [_ok("cat sparse", "x")])
        assert not [r for r in caplog.records if "has produced no data" in r.message]

    @pytest.mark.asyncio
    async def test_state_is_per_host(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for host in ("test1", "test2"):
                for _ in range(3):
                    await collector._process_host_results(
                        host, TS, [_ok("ss -s", "unparseable")], parsers, ctx=ParseContext()
                    )
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 2  # one per host
