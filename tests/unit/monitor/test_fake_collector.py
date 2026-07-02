"""FakeCollector sanity: pushes ride the real MetricCollector record path."""

import pytest

from otto.monitor.collector import MetricCollector
from tests._fixtures._fake_collector import FakeCollector


@pytest.mark.asyncio
async def test_push_stores_series_and_chart_map() -> None:
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    await fake.push("host1", "Memory Usage", 61.0, chart="memory", meta={"Used": "1 G"})

    series = fake.get_series()
    assert [p.value for p in series["host1/Overall CPU"]] == [42.5]
    assert series["host1/Memory Usage"][0].meta == {"Used": "1 G"}
    assert fake.get_chart_map()["Overall CPU"] == "CPU"
    assert fake.get_chart_map()["Memory Usage"] == "Memory Usage"


@pytest.mark.asyncio
async def test_meta_matches_real_collector_except_forced_live() -> None:
    """Drift guard: FakeCollector must present exactly the real collector's meta."""
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    real = MetricCollector(hosts=[])

    fake_meta = fake.get_meta()
    real_meta = real.get_meta()
    assert fake_meta["live"] is True
    assert real_meta["live"] is False  # hosts=[] means historical for the real one
    assert fake_meta["hosts"] == ["host1"]  # derived from pushed series keys
    # Everything except live/hosts is byte-identical to production meta.
    for key in ("metrics", "tabs"):
        assert fake_meta[key] == real_meta[key]


@pytest.mark.asyncio
async def test_push_publishes_sse_payload() -> None:
    fake = FakeCollector()
    q = fake.subscribe()
    await fake.push("host1", "Overall CPU", 42.5)
    msg = q.get_nowait()
    assert msg["type"] == "metric"
    assert msg["key"] == "host1/Overall CPU"
    assert msg["chart"] == "CPU"
