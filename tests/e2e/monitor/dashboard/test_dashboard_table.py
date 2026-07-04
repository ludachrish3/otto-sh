"""Event-table pins: render, live SSE append, filter, display cap, historical --db."""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _open_table(page: Page, url: str, host: str = "host1") -> None:
    page.goto(url)
    page.select_option("#host-select", host)
    page.click('.tab-btn[data-tab="syslog"]')


def test_table_renders_scripted_rows_newest_first(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url)
    expect(page.locator(".event-table thead th")).to_have_text(
        ["Time", "loghost", "proc", "message"]
    )
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("session 2 opened")
    expect(rows.nth(2)).to_contain_text("session 0 opened")


def test_table_scopes_rows_to_selected_host(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url, host="host2")
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.nth(0)).to_contain_text("job ran")


def test_table_appends_live_sse_rows(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url)
    expect(page.locator(".event-table tbody tr")).to_have_count(3)
    table_dash.run(
        table_dash.collector.push_log_events(
            "host1",
            tab="syslog",
            rows=[
                (
                    datetime.now(tz=timezone.utc),
                    {"loghost": "vm1", "proc": "sshd", "message": "live append"},
                )
            ],
        )
    )
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(4)
    expect(rows.nth(0)).to_contain_text("live append")  # newest-first


def test_table_substring_filter(table_dash: DashboardHarness[FakeCollector], page: Page) -> None:
    _open_table(page, table_dash.url)
    page.fill(".event-table-filter", "session 1")
    expect(page.locator(".event-table tbody tr")).to_have_count(1)
    page.fill(".event-table-filter", "")
    expect(page.locator(".event-table tbody tr")).to_have_count(3)


def test_table_display_cap_500(table_dash: DashboardHarness[FakeCollector], page: Page) -> None:
    t0 = datetime.now(tz=timezone.utc)
    table_dash.run(
        table_dash.collector.push_log_events(
            "host1",
            tab="syslog",
            rows=[
                (
                    t0 + timedelta(milliseconds=i),
                    {"loghost": "vm1", "proc": "bulk", "message": f"bulk {i}"},
                )
                for i in range(520)
            ],
        )
    )
    _open_table(page, table_dash.url)
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(500)
    expect(rows.nth(0)).to_contain_text("bulk 519")


def test_table_renders_from_historical_db(
    historical_table_dash: DashboardHarness, page: Page
) -> None:
    _open_table(page, historical_table_dash.url)
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("historical row 2")
