"""MonitorMeta — the typed /api/meta contract."""

import pytest

from otto.models.monitor import ChartSpec, MonitorMeta, TabSpec
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
