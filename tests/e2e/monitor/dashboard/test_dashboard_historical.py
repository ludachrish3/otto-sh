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
    expect(page.locator("body")).to_have_class("historical")
    expect(page.locator("#host-select option")).to_have_text(["historical"])
    expect(page.locator("#pause-btn")).to_be_disabled()
    # DISCREPANCY vs. the brief: MetricCollector.from_json(...) (collector.py
    # from_json, used verbatim by `otto monitor --file x.json` in
    # cli/monitor.py's _load_historical) constructs the collector with
    # hosts=[], so __init__ (collector.py ~125-164) builds zero MonitorTargets
    # and leaves `_views`/`_parsers` empty. `/api/meta` therefore reports
    # `tabs: []` and `metrics: []`. dashboard.js's initTabCharts() (~378-427)
    # only creates tab buttons/panels/charts by iterating `state.meta.tabs`,
    # so with zero tabs NO chart ever gets created — `#tab-cpu` never exists
    # in the DOM. The 2 fixture events still load into `state.events` (via
    # /api/data) but are never rendered anywhere: annotations/shapes are only
    # attached to a metric-plot's Plotly layout, and none exists. This isn't
    # a fixture artifact — it reproduces on the exact production code path.
    expect(page.locator(".tab-btn")).to_have_count(0)
    expect(page.locator(".metric-plot")).to_have_count(0)


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
