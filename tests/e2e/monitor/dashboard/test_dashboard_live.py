"""Pins the live-dashboard behaviors that must survive the React port."""

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _overall_cpu_len(page: Page) -> int:
    """Length of the 'Overall CPU' trace on the CPU chart (-1 if absent)."""
    return page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )


def _open_host(page: Page, dash: DashboardHarness[FakeCollector], host: str = "host1") -> None:
    page.goto(dash.url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", host)
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()


def test_loads_live_and_renders_after_host_selection(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    page.goto(live_dash.url)
    expect(page).to_have_title("Otto Monitor")
    expect(page.locator("#status-label")).to_have_text("Live")
    # Live multi-host mode defers chart creation until a host is chosen.
    expect(page.locator(".metric-plot")).to_have_count(0)

    page.select_option("#host-select", "host1")
    expect(page.locator(".tab-btn")).to_have_text(["CPU", "Memory", "Disk"])
    # CPU tab holds two chart groups: CPU (overall+procs) and Load.
    expect(page.locator("#tab-cpu .metric-plot")).to_have_count(2)
    assert _overall_cpu_len(page) == 3  # the three preloaded ticks

    # Close the page so its open SSE (EventSource) connection drops before the
    # live_dash fixture tears down: uvicorn's graceful shutdown waits for
    # in-flight connections to close, and pytest finalizes fixtures in reverse
    # of the order they were requested — live_dash (declared second in the
    # signature) would otherwise call harness.stop() while the browser still
    # holds /api/stream open, timing out the harness thread join.
    page.close()
