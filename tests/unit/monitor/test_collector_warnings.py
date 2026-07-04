"""Parser-health warning layers, driven through the real _process_host_results.

Command failures are EDGE-TRIGGERED: every ok->failed transition warns (so
transient/intermittent failures are logged whenever they happen), every
failed->ok transition warns the recovery with the outage length, and a
sustained outage logs once — not once per tick. The never-produced backstop
stays warn-once."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.monitor import snmp
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

    @pytest.mark.asyncio
    async def test_sustained_failure_does_not_trip_silent_backstop(self, collector, caplog):
        """5 ticks of a failing command should warn exactly once about the failure
        (layer 1) and zero times about never-produced (layer 2). Failed ticks
        must not advance the silent-parser backstop counter."""
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 127, "sh: ss: command not found")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(5):
                await _tick(collector, parsers, [failed])
        failure_warnings = [r for r in caplog.records if "failed on test1" in r.message]
        silent_warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(failure_warnings) == 1  # edge-triggered, so exactly 1
        assert len(silent_warnings) == 0  # no silent-parser warning

    @pytest.mark.asyncio
    async def test_failure_message_omits_none_output(self, collector, caplog):
        """When CommandResult.value is None (command never ran), the failure
        warning should not contain the literal string 'None'."""
        parsers = {"ss -s": _NeverParses()}
        never_ran = CommandResult(Status.Error, value=None, command="ss -s", retcode=-1)
        with caplog.at_level("WARNING", logger="otto"):
            await _tick(collector, parsers, [never_ran])
        failure_warnings = [r for r in caplog.records if "failed on test1" in r.message]
        assert len(failure_warnings) == 1
        assert "None" not in failure_warnings[0].message

    @pytest.mark.asyncio
    async def test_failed_command_with_parseable_output_still_records_points(
        self, collector, caplog
    ):
        """Parsing is NOT success-gated: grep-style commands legitimately exit
        nonzero while their partial output still carries series. Only the
        health bookkeeping (never-produced backstop) is success-gated."""
        parsers = {"echo 42": _ParsesFine()}
        failed = _failed("echo 42", 1, "42\n")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(3):
                await _tick(collector, parsers, [failed])
        points = collector._store.series.get("test1/Test")
        assert points is not None
        assert len(points) == 3  # every failed tick still recorded
        failure_warnings = [r for r in caplog.records if "failed on" in r.message]
        silent_warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(failure_warnings) == 1  # edge-triggered
        assert len(silent_warnings) == 0  # health must not tick on failures


class TestSnmpSilentOidWarning:
    @pytest.mark.asyncio
    async def test_never_served_oid_warns_once_by_tick_3(self, collector, caplog):
        from unittest.mock import MagicMock

        from otto.monitor.collector import MonitorTarget
        from otto.monitor.snmp import SnmpClient, SnmpSource

        host = MagicMock()
        host.name = "zeph1"
        target = MonitorTarget(
            host=host,
            snmp=SnmpSource(client=SnmpClient(address="10.0.0.1"), oids=["1.2.3.4.0"]),
        )
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(5):
                await collector._process_snmp_results(target, TS, {"1.2.3.4.0": None})
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 1
        assert "1.2.3.4.0" in warnings[0].message
        assert "zeph1" in warnings[0].message


class TestSnmpRatePlumbing:
    """Guards collector.py's ``rates=target.snmp.rates`` plumbing in
    _process_snmp_results.

    Each :class:`~otto.monitor.snmp.SnmpSource` owns exactly one
    :class:`~otto.monitor.rates.RateTracker`, reused across ticks. If that
    ever regressed to a fresh ``RateTracker()`` built per call instead of the
    target's own, every ``kind="counter"`` OID would re-baseline every tick
    and no SNMP counter chart (network/disk throughput, etc.) would ever
    emit a point — silently, since baselining looks identical to a healthy
    first tick.
    """

    @pytest.fixture
    def clean_registry(self):
        """Unregister any test-added SNMP descriptor after the test.

        Mirrors test_snmp.py's fixture of the same name (diff-based cleanup
        is sufficient since this test only ever registers a *new* oid).
        """
        before = set(snmp.SNMP_METRICS.names())
        yield
        for oid in set(snmp.SNMP_METRICS.names()) - before:
            snmp.SNMP_METRICS.unregister(oid)

    @pytest.mark.asyncio
    async def test_counter_rate_uses_per_target_tracker_across_ticks(
        self, collector, clean_registry
    ):
        from unittest.mock import MagicMock

        from otto.monitor.collector import MonitorTarget
        from otto.monitor.snmp import SnmpClient, SnmpMetric, SnmpSource, register_snmp_metric

        oid = "1.2.3.9.100"  # unique test OID, not used by any other test
        register_snmp_metric(
            SnmpMetric(oid=oid, label="rate-plumb test", chart="Net", kind="counter", unit="B/s")
        )
        host = MagicMock()
        host.name = "zeph3"
        target = MonitorTarget(
            host=host,
            snmp=SnmpSource(client=SnmpClient(address="10.0.0.1"), oids=[oid]),
        )

        # Baseline tick: emits nothing, but must seed target.snmp.rates.
        await collector._process_snmp_results(target, TS, {oid: 1000})
        # Second tick, 5s later: rate = (6000-1000)/5 = 1000.0/s. This only
        # comes out non-None if the SAME RateTracker saw both ticks.
        await collector._process_snmp_results(target, TS + timedelta(seconds=5), {oid: 6000})

        points = collector._store.series["zeph3/rate-plumb test"]
        assert [p.value for p in points] == [1000.0]
