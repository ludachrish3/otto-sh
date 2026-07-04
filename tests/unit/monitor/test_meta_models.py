"""MonitorMeta — the typed /api/meta contract."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.models.monitor import ChartSpec, MonitorMeta, TabSpec
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.log_sourced import RegexLogEventParser
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
    assert set(tab.model_dump(mode="json")) == {"id", "label", "metrics", "kind", "columns"}
    assert tab.kind == "charts"
    assert tab.columns is None


def _table_parser() -> "RegexLogEventParser":
    return RegexLogEventParser(
        "tail -n 200 /var/log/syslog",
        r"^(?P<ts>\S+) (?P<proc>\S+): (?P<message>.*)$",
        tab="syslog",
        tab_label="Syslog",
    )


def test_meta_table_parser_contributes_table_tab_and_no_chart_spec() -> None:
    fake = FakeCollector(extra_parsers=[_table_parser()])
    meta = fake.get_meta_model()
    tab = next(t for t in meta.tabs if t.id == "syslog")
    assert tab.kind == "table"
    assert tab.columns == ["proc", "message"]
    assert tab.metrics == []
    assert all(m.command != "tail -n 200 /var/log/syslog" for m in meta.metrics)
    # Chart tabs keep the default kind.
    assert next(t for t in meta.tabs if t.id == "cpu").kind == "charts"


def test_meta_table_tab_id_collision_raises_both_orders() -> None:
    class _ChartOnSyslogTab(MetricParser):
        y_title = ""
        unit = ""
        command = "echo 1"
        tab = "syslog"
        tab_label = "Syslog"
        chart = "Clash"

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {}

    with pytest.raises(ValueError, match="syslog"):
        FakeCollector(extra_parsers=[_table_parser(), _ChartOnSyslogTab()]).get_meta_model()
    with pytest.raises(ValueError, match="syslog"):
        FakeCollector(extra_parsers=[_ChartOnSyslogTab(), _table_parser()]).get_meta_model()


@pytest.mark.asyncio
async def test_fake_collector_push_log_events_uses_production_path() -> None:
    fake = FakeCollector(extra_parsers=[_table_parser()])
    q = fake.subscribe()
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    await fake.push_log_events(
        "host1", tab="syslog", rows=[(ts, {"proc": "sshd", "message": "hi"})]
    )
    assert q.get_nowait()["type"] == "log_event"
    assert fake.get_log_events()[0]["host"] == "host1"


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
