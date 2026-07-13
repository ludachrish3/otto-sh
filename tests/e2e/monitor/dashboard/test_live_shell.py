"""Live mode in a real browser: hydrate, stream, pause, dim (Plan 5b Task 13).

Everything up to here (SSE client, coalesced-flush store, health/dimming,
the chart-option memo and its Task 11 x-axis follow-up) is proven only in
unit tests against a mocked ECharts and a mocked EventSource. This module is
the first place any of it is driven end-to-end: a real browser, a real
``MonitorServer``, real SSE, real ECharts.

Contract: ``data-testid`` attributes only (Task 9) -- styling and DOM
structure are free to change. ``live_stream_dash`` (conftest.py) declares
every host id these specs push to; a host only gets a tile/subject-link once
it's a member of the live snapshot's ``lab.hosts`` (deriveElements(),
web/src/data/exportDoc.ts) -- pushing a point for an undeclared host records
it server-side but never surfaces a clickable element.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _push_tick(
    dash: DashboardHarness[FakeCollector], host: str, ts: datetime, value: float
) -> None:
    """Push one CPU-chart point for *host* -- ``push()``'s default ``chart="cpu"``
    resolves to :class:`~otto.monitor.parsers.TopCpuParser`, whose ``chart``
    attribute is ``"CPU"`` (capitalized) -- that's the ``chartKey`` the
    ``chart-${chartKey}`` testid actually carries (see ``chart-CPU`` below),
    not the lowercase ``chart="cpu"`` friendly-name parameter.
    """
    dash.run(dash.collector.push(host, "cpu", value, ts=ts))


def test_live_boots_hydrated_without_an_import_step(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    # No Import front door: live hydrates from /api/monitor_sessions on boot
    # (data/bootstrap.ts's bootstrapFromServer), then opens the SSE stream.
    expect(_tid(page, "status-text")).to_have_text("Live", ignore_case=True)
    expect(_tid(page, "empty-review")).to_have_count(0)


def test_no_historical_badge_in_live_mode(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """Plan 5b final review, Finding C1: the review bar (and its HISTORICAL
    tag) must not render at all in live mode -- live mode gets its own
    "Live"/pause chrome in AppBar instead (ReviewBar.tsx)."""
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    expect(_tid(page, "status-text")).to_have_text("Live", ignore_case=True)
    expect(_tid(page, "review-bar")).to_have_count(0)
    expect(_tid(page, "historical-tag")).to_have_count(0)


def test_streamed_points_grow_the_chart(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    _tid(page, "subject-link-r1").click()
    chart = _tid(page, "chart-CPU")  # chart-${chartKey}
    expect(chart).to_be_visible()

    before = chart.get_attribute("data-point-count")
    for i in range(1, 6):
        _push_tick(live_stream_dash, "r1", NOW + timedelta(seconds=5 * i), 10.0 + i)
    # SSE -> coalesced flush (16ms, data/stream.ts) -> store -> re-render.
    # Playwright retries this assertion until it's true.
    expect(chart).not_to_have_attribute("data-point-count", before or "")


def test_pause_freezes_the_view_and_resume_follows_again(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    _tid(page, "subject-link-r1").click()

    _tid(page, "pause-toggle").click()
    chart = _tid(page, "chart-CPU")  # chart-${chartKey}
    frozen_window = chart.get_attribute("data-window-to")

    for i in range(1, 6):
        _push_tick(live_stream_dash, "r1", NOW + timedelta(seconds=5 * i), 10.0 + i)

    # Pause is a VIEW control (reviewStore.ts's togglePause): ingestion never
    # stops -- points keep arriving behind the frozen window, but the window
    # itself does not move.
    expect(chart).to_have_attribute("data-window-to", frozen_window or "")

    _tid(page, "pause-toggle").click()
    expect(chart).not_to_have_attribute("data-window-to", frozen_window or "")


def test_a_silent_host_dims(page: Page, live_stream_dash: DashboardHarness[FakeCollector]) -> None:
    """No SSE message ever announces silence -- only the clock can reveal it."""
    stale = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    # live_stream_dash's FakeCollector reports a 5s cadence; HEALTH_K=3 ->
    # down once the gap since the last sample passes 15s. 60s old is well
    # past that the instant the page loads -- no clock tick needs to fire.
    _push_tick(live_stream_dash, "r1", stale, 10.0)
    page.goto(live_stream_dash.url)
    expect(_tid(page, "host-tile-r1")).to_have_attribute("data-health", "down")


def test_a_quiet_hosts_chart_window_advances_when_only_another_host_ticks(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """Task 11 regression pin -- against REAL ECharts, not the mocked vitest one.

    ``session.endMs`` (and so the live-follow window, ``liveRange`` in
    data/time.ts) is GLOBAL: ``applyFragment`` extends it from ANY host's
    fragment, not just the one the viewer happens to be looking at. Before
    the Task 11 follow-up fix (commit cbab89b), ``ChartPanel`` only ever
    called ECharts' ``setOption()`` from an effect gated on the memoized
    ``option`` object, which only changes when the VIEWED host's own series
    gets a new point -- so a quiet host's chart kept a stale x-axis window
    forever while every other host kept the session moving, and would have
    silently emptied at the ``windowMs`` (15 min) cliff. The fix added a
    second, cheap effect in ChartPanel gated directly on the window's own
    bounds, independent of that memo.

    Host A (r1, the one we view) gets exactly one point; host B (r2) keeps
    ticking. The pairing matters: point count flat + window advancing is
    "same data, later window" -- proof the gap is scrolling into view
    rather than the chart just having gotten lucky with fresh data.

    A Task 13 review found the window half of that pairing could not fail:
    ``chart-CPU``'s ``data-window-to`` (SubjectPage.tsx's ChartSection) is
    computed in React's render body straight off the ``window_`` prop, so
    it advances on every render regardless of whether ChartPanel's own
    ``setOption()`` patch effect ever ran -- the reviewer reverted that
    effect's dep list (dropping ``win.from``/``win.to``, reintroducing the
    exact Task 11 bug) and all 5 live-shell specs, including this one,
    stayed green. This spec now reads ``chart-panel-CPU``'s
    ``data-echarts-window-to`` instead -- an attribute ChartPanel stamps
    from *inside* that patch effect itself (see ChartPanel.tsx), so it can
    only advance when the real ECharts merge patch actually ran.
    """
    _push_tick(live_stream_dash, "r1", NOW, 10.0)  # host A: the one we view
    _push_tick(live_stream_dash, "r2", NOW, 20.0)  # host B: keeps ticking
    page.goto(live_stream_dash.url)
    _tid(page, "subject-link-r1").click()
    chart = _tid(page, "chart-CPU")  # chart-${chartKey}: host A's own chart
    panel = _tid(page, "chart-panel-CPU")  # chart-panel-${chartKey}: ChartPanel's own div
    expect(chart).to_be_visible()
    # Wait for ChartPanel's mount-time patch effect to have stamped a value
    # before capturing the baseline (get_attribute below is a one-shot read,
    # not a retrying assertion -- this expect() is what does the retrying).
    expect(panel).to_have_attribute("data-echarts-window-to", re.compile(r"\d+"))

    before_window = panel.get_attribute("data-echarts-window-to")
    before_count = chart.get_attribute("data-point-count")

    for i in range(1, 6):
        _push_tick(live_stream_dash, "r2", NOW + timedelta(seconds=5 * i), 20.0 + i)

    # Host A's own series never ticked...
    expect(chart).to_have_attribute("data-point-count", before_count or "")
    # ...yet ECharts' real axis, as patched by ChartPanel's own effect,
    # scrolled the gap into view rather than freezing.
    expect(panel).not_to_have_attribute("data-echarts-window-to", before_window or "")
