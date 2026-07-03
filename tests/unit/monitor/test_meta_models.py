"""MonitorMeta — the typed /api/meta contract."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.models.monitor import ChartSpec, MonitorMeta, TabSpec
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext
from otto.result import CommandResult, Results
from otto.utils import Status
from tests._fixtures._fake_collector import FakeCollector


@pytest.mark.asyncio
async def test_get_meta_model_matches_dict_form() -> None:
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    model = fake.get_meta_model()
    assert isinstance(model, MonitorMeta)
    assert model.model_dump(mode="json") == fake.get_meta()


def test_chart_spec_wire_shape() -> None:
    spec = ChartSpec(label="CPU", y_title="Usage %", unit="%", command="top", chart="CPU")
    assert set(spec.model_dump(mode="json")) == {
        "label",
        "y_title",
        "unit",
        "command",
        "chart",
        "interval",
    }
    assert spec.interval is None


def test_tab_spec_wire_shape() -> None:
    tab = TabSpec(id="cpu", label="CPU", metrics=["CPU", "Load"])
    assert set(tab.model_dump(mode="json")) == {"id", "label", "metrics"}


@pytest.mark.asyncio
async def test_meta_interval_is_none_before_run() -> None:
    """A collector that never called run() (FakeCollector; historical) reports interval=None."""
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    assert fake.get_meta_model().interval is None


class _StubParser(MetricParser):
    """Minimal parser: one data point per tick."""

    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint] | None:
        return {"value": MetricDataPoint(value=42.0)}


def _make_mock_host(name: str) -> MagicMock:
    """A mock host whose run() returns instantly, for a short real run()."""
    host = MagicMock()
    host.name = name
    host.log = LogMode.QUIET

    async def _run_cmds(cmds: list[str], timeout: float | None = None) -> Results:
        results = [
            CommandResult(Status.Success, value="42\n", command=cmd, retcode=0) for cmd in cmds
        ]
        return Results.collect(results)

    host.run = AsyncMock(side_effect=_run_cmds)
    return host


@pytest.mark.asyncio
async def test_meta_interval_matches_configured_seconds_after_run() -> None:
    """After a short live run(), get_meta_model().interval reports the configured seconds."""
    host = _make_mock_host("host")
    target = MonitorTarget(host=host, parsers={_StubParser.command: _StubParser()})
    collector = MetricCollector(targets=[target])

    await collector.run(interval=timedelta(seconds=0.05), duration=timedelta(seconds=0.1))

    assert collector.get_meta_model().interval == 0.05
