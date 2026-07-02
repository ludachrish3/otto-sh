"""Dashboard e2e fixtures: a scripted live server and a historical server."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

HISTORICAL_JSON = Path(__file__).parent / "data" / "historical.json"

_PROC_META = {
    "Command": "stress",
    "User": "root",
    "Mem": "1.0%",
    "RSS": "10 M",
    "Stat": "R",
    "CPU Time": "0:01.00",
}


def _preload(harness: DashboardHarness[FakeCollector]) -> None:
    """Three 5s-spaced ticks for two hosts: overall CPU, two procs, memory, load."""
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=15)
    push = harness.collector.push
    for tick in range(3):
        ts = t0 + timedelta(seconds=5 * tick)
        for host in ("host1", "host2"):
            harness.run(push(host, "Overall CPU", 20.0 + tick, ts=ts))
            harness.run(push(host, "proc/101", 5.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "proc/202", 3.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "Memory Usage", 40.0 + tick, chart="memory", ts=ts))
            harness.run(push(host, "Load (1m)", 0.5 + tick, chart="load", ts=ts))


@pytest.fixture
def live_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    harness = DashboardHarness(FakeCollector()).start()
    _preload(harness)
    yield harness
    harness.stop()


@pytest.fixture
def historical_dash() -> Iterator[DashboardHarness[MetricCollector]]:
    harness = DashboardHarness(MetricCollector.from_json(str(HISTORICAL_JSON))).start()
    yield harness
    harness.stop()
