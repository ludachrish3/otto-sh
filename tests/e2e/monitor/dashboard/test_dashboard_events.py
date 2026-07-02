"""Pins event CRUD round-trips: UI → API → SSE → chart shapes/annotations."""

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _annotation_labels(page: Page) -> list[str]:
    return page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return ((gd?.layout || {}).annotations || []).map(a => a.text);"
        "}"
    )


def _open(page: Page, dash: DashboardHarness[FakeCollector]) -> None:
    page.goto(dash.url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", "host1")
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()


def test_mark_event_via_ui_draws_annotation(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open(page, live_dash)
    page.fill("#event-label", "Router rebooted")
    page.click("#event-btn")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const anns = ((gd?.layout || {}).annotations || []);"
        "  return anns.length === 1 && anns[0].text === 'Router rebooted';"
        "}"
    )
    events = live_dash.collector.get_events()
    assert [e.label for e in events] == ["Router rebooted"]
    assert events[0].source == "manual"


def test_span_event_draws_shaded_region(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open(page, live_dash)
    page.fill("#span-label", "Maintenance")
    page.click("#span-btn")
    expect(page.locator("#span-btn")).to_have_text("End event")
    page.click("#span-btn")
    expect(page.locator("#span-btn")).to_have_text("Start event")
    # Span = borderless rect + two edge lines (3 shapes) once end_ts round-trips.
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const shapes = ((gd?.layout || {}).shapes || []);"
        "  return shapes.some(s => s.type === 'rect');"
        "}"
    )
    assert live_dash.collector.get_events()[0].end_timestamp is not None


def test_backend_event_update_reflects_in_ui(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    ev = live_dash.run(live_dash.collector.add_event(label="before", color="#ff0000", dash="dash"))
    _open(page, live_dash)
    assert _annotation_labels(page) == ["before"]

    live_dash.run(
        live_dash.collector.update_event(ev.id, label="after", color="#00ff00", dash="dot")
    )
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const anns = ((gd?.layout || {}).annotations || []);"
        "  return anns.length === 1 && anns[0].text === 'after';"
        "}"
    )


def test_clear_events_deletes_after_confirm(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    live_dash.run(live_dash.collector.add_event(label="one", color="#ff0000", dash="dash"))
    live_dash.run(live_dash.collector.add_event(label="two", color="#00ff00", dash="dot"))
    _open(page, live_dash)
    assert len(_annotation_labels(page)) == 2

    page.on("dialog", lambda dialog: dialog.accept())  # confirm() dialog
    page.click("#clear-events-btn")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return (((gd?.layout || {}).annotations || []).length === 0);"
        "}"
    )
    assert live_dash.collector.get_events() == []
