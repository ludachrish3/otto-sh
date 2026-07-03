"""Pins historical-mode chrome, the export → import round-trip, and theming."""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def test_historical_mode_chrome(
    page: Page, historical_dash: DashboardHarness[MetricCollector]
) -> None:
    page.goto(historical_dash.url)
    expect(page.locator("#status-label")).to_have_text("Historical")
    expect(page.locator("body")).to_have_class(re.compile(r"\bhistorical\b"))
    # Historical collectors declare the DEFAULT_PARSERS catalog, so tabs and
    # charts render immediately without host selection (fixed Phase 1 —
    # previously /api/meta had no tabs and nothing rendered).
    expect(page.locator(".tab-btn")).to_have_text(["CPU", "Memory", "Disk"])
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()
    expect(page.locator("#host-select option")).to_have_text(["historical"])
    expect(page.locator("#pause-btn")).to_be_disabled()
    # Fixture series render onto their charts; both fixture events annotate.
    overall_len = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )
    assert overall_len == 3
    labels = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return ((gd?.layout || {}).annotations || []).map(a => a.text);"
        "}"
    )
    assert sorted(labels) == ["Maintenance", "Reboot"]


def test_export_json_reimports_losslessly(
    page: Page, live_dash: DashboardHarness[FakeCollector], tmp_path: Path
) -> None:
    resp = page.request.get(live_dash.url + "/api/export/json")
    assert resp.ok
    exported = resp.json()
    assert set(exported) == {"metrics", "events", "chart_map"}

    out = tmp_path / "exported.json"
    out.write_text(json.dumps(exported))
    reloaded = MetricCollector.from_json(str(out))
    assert reloaded.get_series().keys() == live_dash.collector.get_series().keys()
    assert reloaded.get_chart_map() == live_dash.collector.get_chart_map()


def test_theme_toggle_persists_across_reload(
    page: Page, historical_dash: DashboardHarness[MetricCollector]
) -> None:
    # Class ORDER differs between toggle ("historical light") and reload
    # ("light historical" — the localStorage restore runs before init()
    # adds `historical`), so match with regexes, never full strings.
    light = re.compile(r"\blight\b")
    page.goto(historical_dash.url)
    expect(page.locator("body")).not_to_have_class(light)
    page.click("#theme-btn")
    expect(page.locator("body")).to_have_class(light)
    page.reload()
    expect(page.locator("body")).to_have_class(light)
