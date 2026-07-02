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


def test_sse_metric_extends_live_trace(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    assert _overall_cpu_len(page) == 3

    live_dash.run(live_dash.collector.push("host1", "Overall CPU", 77.0))
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr && tr.x.length === 4;"
        "}"
    )

    # A point for the *unselected* host must not touch host1's charts.
    live_dash.run(live_dash.collector.push("host2", "Overall CPU", 90.0))
    page.wait_for_timeout(300)
    assert _overall_cpu_len(page) == 4


def test_tab_switching_lazily_initializes_charts(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    # Memory panel exists but is inactive (charts deferred until visible).
    expect(page.locator("#tab-memory")).not_to_have_class("tab-panel active")

    page.click(".tab-btn[data-tab='memory']")
    expect(page.locator("#tab-memory")).to_have_class("tab-panel active")
    memory_len = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-memory .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Memory Usage');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )
    assert memory_len == 3


def test_pause_freezes_and_resume_catches_up(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    page.click("#pause-btn")
    expect(page.locator("#status-label")).to_have_text("Paused")

    live_dash.run(live_dash.collector.push("host1", "Overall CPU", 77.0))
    page.wait_for_timeout(500)  # SSE delivery window — chart must NOT move
    assert _overall_cpu_len(page) == 3

    page.click("#pause-btn")  # resume triggers a full refreshPlot from state
    expect(page.locator("#status-label")).to_have_text("Live")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr && tr.x.length === 4;"
        "}"
    )


def test_server_shutdown_shows_disconnected(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """The SSE-error path: status flips to Disconnected and pause disables."""
    _open_host(page, live_dash)
    live_dash.stop()  # idempotent with the fixture finalizer
    expect(page.locator("#status-label")).to_have_text("Disconnected")
    expect(page.locator("#pause-btn")).to_be_disabled()
