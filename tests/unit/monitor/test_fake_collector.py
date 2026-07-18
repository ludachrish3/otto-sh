"""FakeCollector sanity: pushes ride the real MetricCollector record path."""

import pytest

from otto.monitor.parsers import DEFAULT_PARSERS
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
async def test_meta_serves_production_catalog_with_forced_live() -> None:
    """Drift guard: FakeCollector meta must carry the real DEFAULT_PARSERS catalog."""
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)

    meta = fake.get_meta()
    assert meta["live"] is True
    assert meta["hosts"] == ["host1"]  # derived from pushed series keys

    # The metrics/tabs catalog must be exactly what production parsers declare.
    expected_charts = [p.chart for p in DEFAULT_PARSERS.values()]
    assert [m["chart"] for m in meta["metrics"]] == expected_charts
    expected_tabs: list[str] = []
    for p in DEFAULT_PARSERS.values():
        if p.tab not in expected_tabs:
            expected_tabs.append(p.tab)
    assert [t["id"] for t in meta["tabs"]] == expected_tabs
    # PerCoreCpuParser leads DEFAULT_PARSERS (CPU consolidation onto /proc/stat
    # restored cpu-first ordering), so "cpu" is the first-encountered tab.
    assert [t["id"] for t in meta["tabs"]] == ["cpu", "memory", "disk", "network"]
    # CPU (overall + per-core), Load, and Processes share the cpu tab
    assert meta["tabs"][0]["metrics"] == ["CPU", "Load", "Processes"]


@pytest.mark.asyncio
async def test_push_publishes_sse_payload() -> None:
    fake = FakeCollector()
    q = fake.subscribe()
    await fake.push("host1", "Overall CPU", 42.5)
    msg = q.get_nowait()
    assert msg["format"] == 1
    assert msg["metrics"][0]["host"] == "host1"
    assert msg["metrics"][0]["label"] == "Overall CPU"
    # A brand-new label ships its chart_map + meta with the same fragment.
    assert msg["chart_map"]["Overall CPU"] == "CPU"
