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
]

NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _push_tick(
    dash: DashboardHarness[FakeCollector], host: str, ts: datetime, value: float
) -> None:
    """Push one CPU-chart point for *host* -- ``push()``'s default ``chart="cpu"``
    resolves to :class:`~otto.monitor.parsers.PerCoreCpuParser`, whose ``chart``
    attribute is ``"CPU"`` (capitalized) -- that's the ``chartKey`` the
    ``chart-${chartKey}`` testid actually carries (see ``chart-CPU`` below),
    not the lowercase ``chart="cpu"`` friendly-name parameter.
    """
    dash.run(dash.collector.push(host, "cpu", value, ts=ts))


def _format_outage(ms: float) -> str:
    """Python mirror of ``data/time.ts``'s ``formatOutage`` -- lets a spec that
    drives the browser's clock deterministically (``page.clock``) compute the
    EXACT banner text a given, fully-known outage gap should produce, instead
    of polling and waiting for a real-time value to show up."""
    if ms < 60_000:
        return f"{round(ms / 1000)}s"
    mins = round(ms / 60_000)
    if mins < 60:
        return f"{mins}m"
    hours = mins / 60
    return f"{hours}h" if hours == int(hours) else f"{hours:.1f}h"


def test_live_boots_hydrated_without_an_import_step(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    # No Import front door: live hydrates from /api/monitor_sessions on boot
    # (data/bootstrap.ts's bootstrapFromServer), then opens the SSE stream.
    # status-text/status-dot are gone (AppBar rework, spec 2026-07-17 decision
    # 9); the pause glyph only renders in live mode (AppBar.tsx), so its mere
    # presence is now the shell-level "is live" signal.
    expect(_tid(page, "pause-toggle")).to_be_visible()
    expect(_tid(page, "empty-review")).to_have_count(0)


def test_no_historical_badge_in_live_mode(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """Plan 5b final review, Finding C1: the review bar (and its HISTORICAL
    tag) must not render at all in live mode -- live mode gets its own
    "Live"/pause chrome in AppBar instead (ReviewBar.tsx)."""
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    # status-text is gone; the pause glyph (live-mode-only) is the AppBar's
    # live signal now.
    expect(_tid(page, "pause-toggle")).to_be_visible()
    expect(_tid(page, "review-bar")).to_have_count(0)
    expect(_tid(page, "historical-tag")).to_have_count(0)


def test_streamed_points_grow_the_chart(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    _push_tick(live_stream_dash, "r1", NOW, 10.0)
    page.goto(live_stream_dash.url)
    # "/" is the topology landing now (route swap); subject-link-* lives on
    # the grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
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
    # "/" is the topology landing now (route swap); subject-link-* lives on
    # the grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
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
    # "/" is the topology landing now (route swap); host-tile-* lives on the
    # grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
    expect(_tid(page, "host-tile-r1")).to_have_attribute("data-health", "down")


def test_a_silent_hosts_drillin_shows_a_growing_unreachable_banner(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """SubjectPage computed no health at all before this task (Task 5, 5b
    follow-ups) -- a dead host's drill-in rendered its charts normally with
    no indication it had stopped reporting. Pins: the banner appears, its
    outage GROWS on the next collection tick, and the chart itself keeps
    showing the host's last-known data throughout (frozen, not blanked, not
    stale-refreshed).

    Deterministic via Playwright's ``page.clock`` (1.61 -- Important 1, 5b
    follow-ups review), not a real wall-clock wait: the original spec waited
    on a REAL 5s ``setInterval`` tick with only a 10s (2x) timeout margin --
    the only wall-clock-dependent assertion in this whole file, run on
    chromium+firefox+webkit in CI, on a workstream that has already had a
    webkit timeout under load. ``page.clock`` fakes ``Date``/``setTimeout``/
    ``setInterval`` for the whole page; empirically (probed against a real
    browser), it auto-advances roughly with real time after ``install()`` --
    so navigation/hydration aren't starved of their own timers -- but
    ``fast_forward()`` still jumps it forward by an EXACT amount on top of
    wherever it currently sits, firing any due ``setInterval``
    (``data/clock.ts``'s ``useNow``, which backs the banner's health)
    exactly once per jump, however large. The banner's health math compares
    the pushed sample's timestamp against the browser's (now virtualized)
    ``Date.now()`` -- ``stale`` and the installed clock share the same
    Python ``ref`` instant so that math means what this test intends, not
    an artifact of whatever real time navigation happened to take. Neither
    tick below trusts a GUESSED "now": each reads the browser's own
    ``Date.now()`` immediately before its jump, so the expected banner text
    is computed exactly, never polled for.
    """
    ref = datetime.now(tz=timezone.utc)
    stale = ref - timedelta(seconds=20)
    stale_ms = stale.timestamp() * 1000
    _push_tick(live_stream_dash, "r1", stale, 10.0)

    page.clock.install(time=ref)
    page.goto(live_stream_dash.url)
    # "/" is the topology landing now (route swap); subject-link-* lives on
    # the grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()

    chart = _tid(page, "chart-CPU")  # chart-${chartKey}
    expect(chart).to_be_visible()
    point_count = chart.get_attribute("data-point-count")

    banner = _tid(page, "unreachable-banner")
    # 20s stale is already past HEALTH_K(3) x 5s = 15s the instant the page
    # loads, with margin for navigation/hydration -- to_be_visible() still
    # retries in case it isn't quite yet. The banner's actual TEXT at this
    # instant isn't asserted: it reflects data/clock.ts's `now` store, which
    # only advances when a setInterval tick fires -- not on every render --
    # so its exact value here is a function of whatever real time navigation
    # happened to take (same reason the ORIGINAL spec never pinned it
    # either). Both ticks below are deterministic instead: each is read via
    # the browser's own (virtualized) Date.now() immediately before forcing
    # it, so the expected text is computed, never guessed or waited for.
    expect(banner).to_be_visible()

    # One full collection tick (session.meta.interval == 5s, matching
    # live_stream_dash's FakeCollector(interval=5.0)) -- fast_forward fires
    # the due setInterval exactly once, deterministically, instead of
    # waiting on a real 5s wall-clock tick. Reading Date.now() immediately
    # before the jump (rather than assuming it) is what makes the expected
    # text exact regardless of how long navigation/hydration actually took.
    pre_tick1_ms = page.evaluate("() => Date.now()")
    page.clock.fast_forward(5_000)
    outage1 = _format_outage(pre_tick1_ms + 5_000 - stale_ms)
    expect(banner).to_have_text(f"Unreachable for {outage1} — showing last-known data")

    # A second tick, same recipe -- pins that the outage keeps GROWING (not
    # just changing once), the actual property this spec exists to cover.
    pre_tick2_ms = page.evaluate("() => Date.now()")
    page.clock.fast_forward(5_000)
    outage2 = _format_outage(pre_tick2_ms + 5_000 - stale_ms)
    expect(banner).to_have_text(f"Unreachable for {outage2} — showing last-known data")

    # The chart never lost or gained a point across all of this: it is
    # showing r1's last-known data, frozen -- not blanked, not stale-refreshed.
    expect(chart).to_have_attribute("data-point-count", point_count or "")

    # The fleet grid agrees -- this is the SAME derived health
    # (healthForHost/healthForHosts share one rule, data/health.ts), not a
    # parallel read that could drift from what the drill-in just showed.
    # (Route swap: the grid lives at #/hosts, not the topology landing.)
    page.goto(f"{live_stream_dash.url}#/hosts")
    expect(_tid(page, "host-tile-r1")).to_have_attribute("data-health", "down")


def test_choosing_a_wider_live_window_widens_the_chart_while_still_following(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """Task 6 (Plan 5b follow-ups): AppBar's live-window ButtonGroup
    (5m/15m/1h, beside Pause) drives reviewStore.ts's ``setWindow``. Choosing
    a wider preset must both (a) actually widen what the chart shows -- an
    older point that was outside the default 15m follow window comes into
    view -- and (b) leave the view FOLLOWING: the pause toggle must keep
    reading "Pause", never "Resume".

    (b) is the guard against the bug this task exists to prevent: `paused`
    is DERIVED from `range` (`useIsPaused`, reviewStore.ts), never a
    separately stored flag, specifically so a `setWindow` that also pinned
    `range` (the "following" case wrongly behaving like the "paused" case --
    see reviewStore.ts's doc comment on the two cases) could not go
    unnoticed: it would flip this same label.

    Fully deterministic without a wall clock: every timestamp below is an
    explicit historical ``ts=`` passed to ``push()``, so ``session.endMs``
    (and so the live-follow window, ``liveRange``) is pinned by the data
    itself, never by when this test happens to run.

    A review found a REAL bug here (reproduced by instrumenting ECharts'
    actual ``setOption()`` option object, not just this page's DOM): widening
    the window while following stretched the x-axis but never added the
    newly-in-window point to the drawn line -- ``ChartSection``'s option
    memo (SubjectPage.tsx) is keyed on ``revKey``/``range``/... , none of
    which ``setWindow`` moves while following (``range`` stays null, by
    design), so the expensive rebuild that actually bakes ``series`` data
    into the option never ran. ``data-point-count`` (below) could not catch
    it: it's computed in SubjectPage's render body straight off the
    ``series`` prop, which gets re-sliced (and so this attribute moves)
    every render regardless of whether ECharts was ever handed the new
    point. This spec now also asserts on ``chart-panel-CPU``'s
    ``data-echarts-point-count`` -- stamped from *inside* ChartPanel's own
    ``setOption()`` effect (see ChartPanel.tsx), off ``option.series[].data``
    itself -- which can only move when ECharts actually was.
    """
    _push_tick(live_stream_dash, "r1", NOW - timedelta(minutes=50), 1.0)
    _push_tick(live_stream_dash, "r1", NOW - timedelta(minutes=10), 2.0)
    _push_tick(live_stream_dash, "r1", NOW, 3.0)
    page.goto(live_stream_dash.url)
    # "/" is the topology landing now (route swap); subject-link-* lives on
    # the grid (#/hosts). live-window-* itself lives in SubjectPage's title
    # row (spec 2026-07-17 decision 10), reached once we're there.
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()

    chart = _tid(page, "chart-CPU")  # chart-${chartKey}
    panel = _tid(page, "chart-panel-CPU")  # chart-panel-${chartKey}: ChartPanel's own div
    expect(chart).to_be_visible()
    # Default window is 15m (windowMs's own store default): the point from
    # 50 minutes ago falls outside [now - 15m, now] and is not shown.
    expect(chart).to_have_attribute("data-point-count", "2")
    # ...and ECharts itself was actually handed those same 2 points, not
    # just the render-body prop that claims so.
    expect(panel).to_have_attribute("data-echarts-point-count", "2")
    # pause-toggle is a glyph now (AppBar rework); its aria-label carries
    # the Pause/Resume state, not its text content.
    expect(_tid(page, "pause-toggle")).to_have_attribute("aria-label", "Pause")

    _tid(page, "live-window-1h").click()

    # Widening to 1h pulls the 50-minute-old point into view in the
    # render-body prop...
    expect(chart).to_have_attribute("data-point-count", "3")
    # ...AND -- the point this spec exists to pin -- in what ECharts' own
    # setOption() call actually drew. Under the bug above, this assertion is
    # the one that fails (stuck at "2") while the one above still passes.
    expect(panel).to_have_attribute("data-echarts-point-count", "3")
    # ...while the view keeps following -- never pinned to a range.
    expect(_tid(page, "pause-toggle")).to_have_attribute("aria-label", "Pause")


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
    # "/" is the topology landing now (route swap); subject-link-* lives on
    # the grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
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
