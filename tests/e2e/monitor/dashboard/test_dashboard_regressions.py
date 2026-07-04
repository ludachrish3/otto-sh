"""Pins the by-design fixes for the legacy dashboard's known frontend bugs
(`todo/TODO.md`): Task 10 fixed #1 (chart divs growing forever — one
legend/trace per PID ever seen, accumulated without bound); Task 11 fixes #2
(plots never resized with the window) and #3 (Safari's hovering modebar
overdraws past the plot, a symptom of #2 — see
`test_safari_modebar_contained_after_resize`'s docstring). Task 12 (this
addition) pins the plan's air-gap hard requirement: otto's labs have no
network access, so the dashboard must never fetch anything at runtime but
itself — `test_dashboard_renders_fully_offline` below. The Phase 2 plan's
sanctioned pin-evolution list lets Tasks 10-12 ADD pins to this file; no
existing assertion in this suite changes.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, WebSocketRoute, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

# 2 legend rows * 6 items/row (web/src/plotly.ts's ITEMS_PER_ROW) — the
# Task 10 legend cap (web/src/retirement.ts's LEGEND_CAP_ROWS).
LEGEND_CAP_ENTRIES = 12

# web/src/retirement.ts's RETIREMENT_K: a proc/* trace retires from the chart
# once its PID is absent from the latest K distinct collection ticks.
RETIREMENT_K = 3

CHURN_PID_COUNT = 100

# `document.querySelector` (not a locator) matches dashboard_live.py's own
# `_overall_cpu_len` convention: '#tab-cpu .metric-plot' has four divs (CPU,
# Load, Per-core CPU, and Processes charts) and querySelector always resolves
# the FIRST — the CPU/proc chart, the one under test here.
_CPU_CHART_JS = "document.querySelector('#tab-cpu .metric-plot')"


def _open_host(page: Page, dash: DashboardHarness[FakeCollector], host: str = "host1") -> None:
    page.goto(dash.url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", host)
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()


def _resize_and_settle(page: Page, width: int, height: int = 800) -> None:
    """Shared by both Task 11 resize pins below: `set_viewport_size` then wait
    for the CPU chart's Plotly-internal width to actually follow it.
    `clientWidth` updates the instant the CSS layout reflows; `_fullLayout.width`
    (Plotly's own internal record) only catches up once the ResizeObserver's
    callback fires and its `plotly.resize()` promise resolves — waiting on the
    LATTER is the point of both pins, so it's what this helper waits on.
    """
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_function(f"() => {_CPU_CHART_JS}._fullLayout.width === {width}")


def test_constant_height_and_capped_legend_under_pid_churn(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """Pump 100 churning fake PIDs onto the CPU chart (host1 already carries
    two preloaded procs — 101/202) and assert the symptoms of the legacy bug
    are gone: the chart div's height never changes, the legend never exceeds
    its budget, and the drawn trace count stays bounded at the retirement
    window (not one trace per PID ever seen).
    """
    _open_host(page, live_dash)

    before_height = page.evaluate(f"() => {_CPU_CHART_JS}.clientHeight")
    assert before_height > 0

    t0 = datetime.now(tz=timezone.utc)
    for i in range(CHURN_PID_COUNT):
        live_dash.run(
            live_dash.collector.push(
                "host1",
                f"proc/{20000 + i}",
                float(i),
                ts=t0 + timedelta(seconds=i),
            )
        )

    # Settle: wait for the newest PID's trace to actually land before reading height/legend.
    page.wait_for_function(
        f"() => ({_CPU_CHART_JS}?.data || []).some(t => t.name === '{20000 + CHURN_PID_COUNT - 1}')"
    )

    after_height = page.evaluate(f"() => {_CPU_CHART_JS}.clientHeight")
    assert after_height == before_height, "chart div height must stay CONSTANT under PID churn"

    legend_entry_count = page.evaluate(
        f"() => ({_CPU_CHART_JS}?.data || []).filter(t => t.showlegend !== false && t.name).length"
    )
    assert legend_entry_count <= LEGEND_CAP_ENTRIES

    # Retirement itself (not just the legend cap): the DRAWN trace count must
    # stay bounded — unbounded gd.data growth was the original bug's
    # memory/perf symptom, and the two assertions above would both stay green
    # even if retirement were a no-op (height is unconditionally constant now,
    # and the cap independently bounds the legend). Every churned PID gets its
    # own distinct tick timestamp here, so exactly RETIREMENT_K proc traces
    # survive (the last K PIDs; the preloaded procs 101/202 ticked 15s before
    # the churn and are long since outside the window), plus the one non-proc
    # trace that never retires: 'Overall CPU'.
    trace_count = page.evaluate(f"() => ({_CPU_CHART_JS}?.data || []).length")
    assert trace_count == RETIREMENT_K + 1, (
        f"expected 1 non-proc + {RETIREMENT_K} live proc traces, got {trace_count} "
        f"(is PID-trace retirement no longer engaging?)"
    )

    # The churned PIDs actually left something behind in the store (export
    # unaffected by retirement) even though the chart itself dropped them —
    # this is the "store data retained, chart retires" half of the policy.
    series = live_dash.collector.get_series()
    assert f"host1/proc/{20000}" in series
    assert f"host1/proc/{20000 + CHURN_PID_COUNT - 1}" in series


def test_resize_relayouts_plot_width(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """Task 11, legacy bug #2 (`todo/TODO.md`'s "Plots do not dynamically
    resize when the window changes size"): `PLOT_CONFIG.responsive` was
    `false` (web/src/plotly.ts) and nothing else ever told Plotly to
    re-measure, so a plot drawn at one viewport width stayed frozen at that
    width forever — Task 11's diagnosis confirmed this left Plotly's
    internal `.svg-container` sized for the OLD box after a real resize, in
    EVERY engine, not just the one the modebar pin below happens to run
    under. `ChartPanel`'s ResizeObserver (`plotly.resize`, plotly.ts) is the
    fix: assert the CPU chart's Plotly-internal width tracks a real
    `set_viewport_size` shrink from 1200 down to 800.
    """
    _open_host(page, live_dash)
    _resize_and_settle(page, 1200)
    before_width = page.evaluate(f"() => {_CPU_CHART_JS}._fullLayout.width")
    assert before_width == 1200

    _resize_and_settle(page, 800)
    after_width = page.evaluate(f"() => {_CPU_CHART_JS}._fullLayout.width")
    after_client_width = page.evaluate(f"() => {_CPU_CHART_JS}.clientWidth")

    assert after_width < before_width, (
        "plot width must shrink when its container narrows (no resize handling = frozen width)"
    )
    assert after_width == after_client_width == 800, (
        "plot width must match the container's ACTUAL current width, not a stale one"
    )


@pytest.mark.only_browser("webkit")
def test_safari_modebar_contained_after_resize(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """Task 11, legacy bug #3 (`todo/TODO.md`: "Safari overdraws to the right
    side of the screen. It's the hovering toolbar that overhangs."). Root
    cause, confirmed empirically (also written up in web/src/dashboard.css's
    `.metric-plot` comment): a symptom of bug #2
    above, in ANY engine — Plotly positions its hovering modebar `position:
    absolute; right: 2px` inside `.svg-container`, a child it sizes in raw
    pixels at draw time. Without a live resize handler, that child stays
    sized for whatever box the plot was FIRST drawn into; once the actual
    container resizes narrower, the modebar (still anchored to the stale,
    wider box's right edge) renders past the ACTUAL plot's right edge —
    reaching past the page's own right edge entirely in the reproduction.
    Task 11 fixes the root cause (`ChartPanel`'s ResizeObserver, pinned
    engine-agnostically by `test_resize_relayouts_plot_width` above) and adds
    `overflow: hidden` containment (dashboard.css) as defense in depth. This
    test is the WebKit-specific half of that pin, matching the bug report's
    own browser — see this file's module docstring and the Makefile's
    `dashboard-webkit` target/comment for why `only_browser` gates it to a
    dedicated invocation instead of folding it into the default (chromium)
    `make dashboard` run.
    """
    _open_host(page, live_dash)
    _resize_and_settle(page, 1200)
    _resize_and_settle(page, 800)

    chart = page.locator("#tab-cpu .metric-plot").first
    chart.hover()
    modebar = chart.locator(".modebar").first
    expect(modebar).to_be_visible()

    chart_box = chart.bounding_box()
    modebar_box = modebar.bounding_box()
    assert chart_box is not None
    assert modebar_box is not None
    assert modebar_box["x"] >= chart_box["x"] - 0.5, (
        "modebar must not overhang the plot's LEFT edge"
    )
    assert modebar_box["x"] + modebar_box["width"] <= chart_box["x"] + chart_box["width"] + 0.5, (
        "modebar must stay within the plot's RIGHT edge — the Safari overdraw bug"
    )


def test_dashboard_renders_fully_offline(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """Task 12: pins the plan's air-gap hard requirement at runtime, not just
    statically. `scripts/check_airgap.sh` (Task 3) greps the BUILT bundle for
    absolute URLs; this pin instead drives a real browser against the served
    dashboard with every non-local request blocked, so a regression that
    somehow evades the static grep (e.g. a URL assembled at runtime from
    string parts, or fetched by a dependency's code path the grep's file
    globs miss) still fails loudly here. otto's labs have no network access —
    the dashboard may only ever talk to the server that served it.

    Routing hooks the harness at the ``page`` level (pytest-playwright's
    built-in per-test fixture, not a custom context), on BOTH transports a
    page can reach the network through: `page.route` intercepts every
    HTTP-shaped request (document, assets, fetch/XHR, EventSource) —
    same-origin (127.0.0.1, the ``live_dash`` harness's own bind host)
    requests are allowed through via ``route.continue_()``, anything else is
    aborted and its URL recorded; `page.route_web_socket` covers the
    transport `page.route` deliberately does NOT see, WebSocket handshakes —
    a routed WS never reaches its server unless the handler opts in via
    ``connect_to_server()``, so local ones are connected through (nothing
    uses WS today — the dashboard streams over SSE — but a future WS port
    stays covered either way) and non-local ones are recorded and simply
    never connected (see the handler's comment for why not ``close()``).
    Both feed the same ``blocked`` list. Two assertions: (1) the CPU chart's
    'Overall CPU' trace still carries all 3 preloaded ticks — proof the
    block didn't eat a legitimate same-origin request and the app rendered
    for real, not just failed quietly; (2) after a short settle (see the
    comment there for why not ``networkidle``), the blocked list is empty —
    proof nothing non-local was ever attempted. A CDN/font/analytics/WS
    regression fails with the offending URL(s) in the message either way.
    """
    blocked: list[str] = []

    def _is_local(url: str) -> bool:
        return urlparse(url).hostname in ("127.0.0.1", "localhost")

    def _block_non_local(route: Route) -> None:
        if _is_local(route.request.url):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    def _block_non_local_ws(ws: WebSocketRoute) -> None:
        # Recording WITHOUT connect_to_server() is the whole block: a routed
        # WebSocket never reaches any server unless the handler opts in
        # (documented Playwright default), so the non-local branch needs no
        # close() call — which matters, because sync-API WS route handlers
        # run inline on Playwright's dispatcher fiber, where a blocking sync
        # call like ws.close() deadlocks until pytest-timeout kills the test
        # (verified empirically). connect_to_server() is safe here: it is
        # dispatch-only (not wrapped in a blocking round-trip).
        if _is_local(ws.url):
            ws.connect_to_server()
        else:
            blocked.append(ws.url)

    page.route("**/*", _block_non_local)
    page.route_web_socket("**/*", _block_non_local_ws)

    _open_host(page, live_dash)

    overall_cpu_len = page.evaluate(
        f"() => {{"
        f"  const gd = {_CPU_CHART_JS};"
        f"  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        f"  return tr ? tr.x.length : -1;"
        f"}}"
    )
    assert overall_cpu_len == 3, (
        "the CPU chart must render real trace data (3 preloaded ticks) while fully "
        f"offline, got trace length {overall_cpu_len} "
        "(did the offline route block a legitimate same-origin request? "
        f"blocked so far: {blocked})"
    )

    # Settle so delayed/idle-time external fetches (e.g. fired off a timer
    # just after load) are observed before the negative assertion below.
    # `wait_for_load_state("networkidle")` is the obvious tool but can NEVER
    # fire here: the dashboard holds its /api/stream SSE connection open for
    # the page's whole life, so the network is never idle (verified
    # empirically — it times out). A fixed short settle is the equivalent.
    page.wait_for_timeout(500)

    assert blocked == [], (
        f"dashboard attempted non-local request(s) at runtime — air-gap violation: {blocked}"
    )
