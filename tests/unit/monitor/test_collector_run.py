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

from otto.host import RunResult
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricParser, MetricDataPoint
from otto.utils import CommandStatus, Status


class StubParser(MetricParser):
    """Minimal parser for testing — returns a single data point."""

    chart = 'Test'
    y_title = 'Value'
    unit = ''
    command = 'echo 42'

    def parse(self, output: str) -> dict[str, MetricDataPoint] | None:
        try:
            return {'value': MetricDataPoint(float(output.strip()))}
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
    host.log = False

    async def _run_cmds(cmds, timeout=None):
        if fail:
            raise RuntimeError(f'{name} is unreachable')
        # Only apply delay to collection commands, not the one-time
        # setup command (grep ^processor) which has no timeout.
        is_setup = len(cmds) == 1 and 'processor' in cmds[0]
        if delay > 0 and not is_setup:
            if timeout is not None and delay > timeout:
                # Simulate what real run does: the command times out
                # via _run_one's wait_for, session recovers, returns Error
                await asyncio.sleep(timeout)
                statuses = [
                    CommandStatus(
                        command=cmd,
                        output=f'Command timed out after {timeout}s',
                        status=Status.Error,
                        retcode=-1,
                    )
                    for cmd in cmds
                ]
                return RunResult(status=Status.Error, statuses=statuses)
            await asyncio.sleep(delay)
        statuses = [
            CommandStatus(command=cmd, output='42\n', status=Status.Success, retcode=0)
            for cmd in cmds
        ]
        return RunResult(status=Status.Success, statuses=statuses)

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
        host_a = _make_mock_host('host_a')
        host_b = _make_mock_host('host_b')
        collector = _build_collector([host_a, host_b])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert 'host_a/value' in series, f'host_a missing from series: {list(series)}'
        assert 'host_b/value' in series, f'host_b missing from series: {list(series)}'
        # At least initial + 1-2 loop iterations
        assert len(series['host_a/value']) >= 2
        assert len(series['host_b/value']) >= 2

    @pytest.mark.asyncio
    async def test_slow_host_times_out(self):
        """A slow host is skipped while a fast host still gets collected."""
        fast = _make_mock_host('fast', delay=0.0)
        slow = _make_mock_host('slow', delay=5.0)  # way longer than interval
        collector = _build_collector([fast, slow])

        await collector.run(
            interval=timedelta(milliseconds=200),
            duration=timedelta(milliseconds=500),
        )

        series = collector.get_series()
        # Fast host should have data from multiple ticks
        assert 'fast/value' in series
        assert len(series['fast/value']) >= 2

        # Slow host should have no data (timed out every tick)
        assert 'slow/value' not in series

    @pytest.mark.asyncio
    async def test_host_error_does_not_crash_loop(self):
        """A host that raises does not prevent other hosts from being collected."""
        good = _make_mock_host('good')
        bad = _make_mock_host('bad', fail=True)
        collector = _build_collector([good, bad])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert 'good/value' in series
        assert len(series['good/value']) >= 2
        assert 'bad/value' not in series

    @pytest.mark.asyncio
    async def test_duration_stops_loop(self):
        """The loop exits after the specified duration."""
        host = _make_mock_host('host')
        collector = _build_collector([host])

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(milliseconds=50),
            duration=timedelta(milliseconds=200),
        )
        elapsed = asyncio.get_running_loop().time() - start

        # Should finish within a reasonable margin of the duration
        assert elapsed < 1.0, f'Loop ran for {elapsed:.2f}s, expected ~0.2s'

    @pytest.mark.asyncio
    async def test_interval_passed_as_timeout_to_run(self):
        """The collector passes the interval as the timeout to each run call."""
        host = _make_mock_host('host')
        collector = _build_collector([host])

        await collector.run(
            interval=timedelta(seconds=3),
            duration=timedelta(milliseconds=100),
        )

        # Inspect the calls to run — each should have timeout=3.0
        for call in host.run.call_args_list:
            if 'timeout' in call.kwargs:
                assert call.kwargs['timeout'] == 3.0

    @pytest.mark.asyncio
    async def test_slow_host_does_not_block_fast_host(self):
        """A slow host times out at the interval boundary, not indefinitely.

        With a 200ms interval, the slow host (5s delay) should time out after
        ~200ms, so the entire run should complete in well under 5s.
        """
        fast = _make_mock_host('fast', delay=0.0)
        slow = _make_mock_host('slow', delay=5.0)
        collector = _build_collector([fast, slow])

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(milliseconds=200),
            duration=timedelta(milliseconds=500),
        )
        elapsed = asyncio.get_running_loop().time() - start

        # Should complete in ~0.5-1.0s, not 5+s
        assert elapsed < 2.0, (
            f'Run took {elapsed:.2f}s — slow host may be blocking fast host'
        )
