"""Tests for MetricCollector.run() backpressure behavior.

These tests verify that the collection loop:
  - Collects from multiple hosts each tick
  - Times out slow hosts without blocking fast hosts (via run timeout)
  - Continues collecting after host errors
  - Respects the duration parameter
  - Passes the interval as a cumulative timeout to run
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricDataPoint, MetricParser
from otto.monitor.snmp import OID_SYS_UPTIME, SnmpSource
from otto.result import CommandResult, Results
from otto.utils import Status


class StubParser(MetricParser):
    """Minimal parser for testing — returns a single data point."""

    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str) -> dict[str, MetricDataPoint] | None:
        try:
            return {"value": MetricDataPoint(float(output.strip()))}
        except ValueError:
            return None


def _make_mock_host(name: str, delay: float = 0.0, fail: bool = False) -> MagicMock:
    """Create a mock host whose run returns after *delay* seconds.

    The mock respects the ``timeout`` kwarg: if the delay exceeds the timeout,
    the command returns ``Status.Error`` with a timeout message — mimicking
    the real ``run`` deadline behavior.

    If *fail* is True, run raises RuntimeError instead.
    """
    host = MagicMock()
    host.name = name
    host.log = LogMode.QUIET

    async def _run_cmds(cmds, timeout=None):
        if fail:
            raise RuntimeError(f"{name} is unreachable")
        # Only apply delay to collection commands, not the one-time
        # setup command (grep ^processor) which has no timeout.
        is_setup = len(cmds) == 1 and "processor" in cmds[0]
        if delay > 0 and not is_setup:
            if timeout is not None and delay > timeout:
                # Simulate what real run does: the command times out
                # via _run_one's wait_for, session recovers, returns Error
                await asyncio.sleep(timeout)
                results = [
                    CommandResult(
                        Status.Error,
                        value=f"Command timed out after {timeout}s",
                        command=cmd,
                        retcode=-1,
                    )
                    for cmd in cmds
                ]
                return Results.collect(results)
            await asyncio.sleep(delay)
        results = [
            CommandResult(Status.Success, value="42\n", command=cmd, retcode=0) for cmd in cmds
        ]
        return Results.collect(results)

    host.run = AsyncMock(side_effect=_run_cmds)
    return host


def _build_collector(hosts: list[MagicMock]) -> MetricCollector:
    """Build a MetricCollector with mock targets and no DB."""
    parsers = {StubParser.command: StubParser()}
    targets = [MonitorTarget(host=h, parsers=parsers) for h in hosts]
    return MetricCollector(targets=targets)


class TestCollectorRun:
    @pytest.mark.asyncio
    async def test_normal_collection(self):
        """Two fast hosts both produce data within a short run."""
        host_a = _make_mock_host("host_a")
        host_b = _make_mock_host("host_b")
        collector = _build_collector([host_a, host_b])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert "host_a/value" in series, f"host_a missing from series: {list(series)}"
        assert "host_b/value" in series, f"host_b missing from series: {list(series)}"
        # At least initial + 1-2 loop iterations
        assert len(series["host_a/value"]) >= 2
        assert len(series["host_b/value"]) >= 2

    @pytest.mark.asyncio
    async def test_slow_host_times_out(self):
        """A slow host is skipped while a fast host still gets collected."""
        fast = _make_mock_host("fast", delay=0.0)
        slow = _make_mock_host("slow", delay=5.0)  # way longer than interval
        collector = _build_collector([fast, slow])

        await collector.run(
            interval=timedelta(milliseconds=200),
            duration=timedelta(milliseconds=500),
        )

        series = collector.get_series()
        # Fast host should have data from multiple ticks
        assert "fast/value" in series
        assert len(series["fast/value"]) >= 2

        # Slow host should have no data (timed out every tick)
        assert "slow/value" not in series

    @pytest.mark.asyncio
    async def test_host_error_does_not_crash_loop(self):
        """A host that raises does not prevent other hosts from being collected."""
        good = _make_mock_host("good")
        bad = _make_mock_host("bad", fail=True)
        collector = _build_collector([good, bad])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert "good/value" in series
        assert len(series["good/value"]) >= 2
        assert "bad/value" not in series

    @pytest.mark.asyncio
    async def test_duration_stops_loop(self):
        """The loop exits after the specified duration."""
        host = _make_mock_host("host")
        collector = _build_collector([host])

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(milliseconds=50),
            duration=timedelta(milliseconds=200),
        )
        elapsed = asyncio.get_running_loop().time() - start

        # Should finish within a reasonable margin of the duration
        assert elapsed < 1.0, f"Loop ran for {elapsed:.2f}s, expected ~0.2s"

    @pytest.mark.asyncio
    async def test_interval_passed_as_timeout_to_run(self):
        """The collector passes the interval as the timeout to each run call."""
        host = _make_mock_host("host")
        collector = _build_collector([host])

        # Patch asyncio.sleep inside the collector module so the inter-
        # iteration wait completes instantly. Without this the test waits
        # the full 3-second interval between iterations even though
        # duration is only 100ms.
        from unittest.mock import patch

        with patch("otto.monitor.collector.asyncio.sleep", new=AsyncMock()):
            await collector.run(
                interval=timedelta(seconds=3),
                duration=timedelta(milliseconds=100),
            )

        # Inspect the calls to run — each should have timeout=3.0
        for call in host.run.call_args_list:
            if "timeout" in call.kwargs:
                assert call.kwargs["timeout"] == 3.0

    @pytest.mark.asyncio
    async def test_slow_host_does_not_block_fast_host(self):
        """A slow host times out at the interval boundary, not indefinitely.

        With a 200ms interval, the slow host (5s delay) should time out after
        ~200ms, so the entire run should complete in well under 5s.
        """
        fast = _make_mock_host("fast", delay=0.0)
        slow = _make_mock_host("slow", delay=5.0)
        collector = _build_collector([fast, slow])

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(milliseconds=200),
            duration=timedelta(milliseconds=500),
        )
        elapsed = asyncio.get_running_loop().time() - start

        # Should complete in ~0.5-1.0s, not 5+s
        assert elapsed < 2.0, f"Run took {elapsed:.2f}s — slow host may be blocking fast host"


class _FakeSnmpClient:
    """Duck-typed SnmpClient: returns canned varbind values, records calls."""

    def __init__(self, values: dict[str, float | None]) -> None:
        self._values = values
        self.calls = 0

    async def get(self, oids: list[str]) -> dict[str, float | None]:
        self.calls += 1
        return {oid: self._values.get(oid) for oid in oids}


class TestSnmpCollection:
    """An SNMP target collects via its client, not the host shell."""

    def _make_snmp_target(
        self, name: str, client: _FakeSnmpClient
    ) -> tuple[MagicMock, MonitorTarget]:
        host = MagicMock()
        host.name = name
        host.log = LogMode.QUIET
        host.run = AsyncMock()  # must NOT be called for an SNMP target
        target = MonitorTarget(
            host=host,
            parsers={},
            snmp=SnmpSource(client=client, oids=[OID_SYS_UPTIME]),  # type: ignore[arg-type]
        )
        return host, target

    @pytest.mark.asyncio
    async def test_snmp_target_populates_series_from_oids(self):
        client = _FakeSnmpClient({OID_SYS_UPTIME: 12345})
        host, target = self._make_snmp_target("sprout", client)
        collector = MetricCollector(targets=[target])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        # sysUpTime (1/100 s) scaled to seconds by the descriptor: 12345 -> 123.45
        assert "sprout/Uptime" in series, f"series: {list(series)}"
        assert series["sprout/Uptime"][0].value == 123.45
        assert client.calls >= 2  # initial + at least one loop tick
        host.run.assert_not_called()  # no shell, no core-count probe

    @pytest.mark.asyncio
    async def test_snmp_and_shell_targets_coexist(self):
        client = _FakeSnmpClient({OID_SYS_UPTIME: 100})
        _, snmp_target = self._make_snmp_target("sprout", client)
        shell_host = _make_mock_host("carrot")
        shell_target = MonitorTarget(host=shell_host, parsers={StubParser.command: StubParser()})
        collector = MetricCollector(targets=[snmp_target, shell_target])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert "sprout/Uptime" in series  # SNMP path
        assert "carrot/value" in series  # shell path, unaffected
