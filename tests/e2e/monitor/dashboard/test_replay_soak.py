"""Tier-3: stress the BROWSER with a full run's worth of real points.

The load generator is deliberately NOT the real collector. The >=1s interval
floor exists precisely to stop us hammering real hosts, so a `--interval 0.1`
soak would violate its own rationale. Instead we replay a run's worth of
points through the fake producer at maximum rate -- exercising
server -> SSE -> browser -> ECharts under load without touching a VM (the
push side alone runs ~180k points in ~1s in-process; the point is whether
the BROWSER keeps up with that firehose, not whether Python does).

Marker-gated (`soak`, registered in pyproject.toml's markers list and
excluded from `make dashboard`'s default `-m "browser and not soak"`
selection, and from noxfile.py's `dashboard` session — see that session's
`DASHBOARD_MARKER_EXPR` comment): this is minutes of pushing, not a
per-push test.

Chromium only. Measured directly (`--browser <engine>`, this file alone,
`-m browser --no-cov`): Chromium replays all ~180k points and finishes the
whole test in ~15s. WebKit never gets that far -- the very first
`chart.get_attribute("data-point-count")` read inside
`_wait_for_ingestion_to_settle` blows Playwright's own 60s action timeout
(`page.set_default_timeout`, conftest.py), i.e. WebKit's main thread is so
backed up applying ~180k individually-fragmented SSE updates that it can't
even answer a single DOM read within 60s. Raising `_SETTLE_TIMEOUT` alone
does not help -- that per-call ceiling is a Playwright default, not this
module's polling budget -- and open-endedly raising the *action* timeout to
"however long WebKit needs" would turn a fast, trusted soak into an
unbounded one for a browser this suite already exercises for correctness
elsewhere (test_live_shell.py etc.). `make dashboard-soak` already pinned
`--browser chromium` for this reason; the guard below makes that binding
instead of conventional, so a `-m soak` invocation on another engine skips
loudly instead of hanging.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.soak,
    pytest.mark.xdist_group("dashboard"),
]

# live_stream_dash (conftest.py) declares exactly these host ids in its lab
# snapshot, so h0/h1 both have a clickable subject-link.
HOSTS = [f"h{i}" for i in range(7)]
LABELS = [f"m{i}" for i in range(13)]  # ~90 series, the live bed's shape
TICKS = 2000  # ~2.6h of 5s-spaced samples; raise locally to reach 12h

_SETTLE_QUIET_FOR = 1.0  # seconds a signal must hold still to count as "settled"
_SETTLE_TIMEOUT = 60.0


def _wait_for_ingestion_to_settle(chart: Locator) -> None:
    """Block until *chart*'s ``data-point-count`` stops changing.

    ``live_stream_dash.run(_replay())`` returning only means the SERVER has
    queued every fragment -- delivering ~180k individually-fragmented SSE
    messages over one HTTP connection to the browser (this replay pushes one
    fragment per point, same as a real tick) is a separate, still-ongoing
    transfer at that point. Sampling a CPU profile of the gap below showed
    the main thread ~80% IDLE, not pegged -- the browser is waiting on the
    network, not stuck computing. That is a real, expected cost of this
    volume of individually-fragmented SSE traffic, not a UI freeze, so it
    does not belong inside the "does a click resolve promptly" budget below:
    conflating the two would make this test flake on nothing more than
    machine-to-machine variance in SSE transport speed. Once ingestion
    stops moving the (currently-viewed, still-ticking) chart's point count,
    every fragment has been applied and the responsiveness measurement can
    start from a clean, idle baseline.
    """
    last: str | None = None
    stable_since = time.monotonic()
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    while True:
        current = chart.get_attribute("data-point-count")
        now = time.monotonic()
        if current != last:
            last = current
            stable_since = now
        elif now - stable_since >= _SETTLE_QUIET_FOR:
            return
        if now > deadline:
            raise AssertionError(
                f"SSE ingestion never settled within {_SETTLE_TIMEOUT}s "
                f"(data-point-count last seen: {current!r})"
            )
        time.sleep(0.1)


def test_browser_stays_responsive_under_a_full_runs_data(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector], browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip(
            f"soak is chromium-only ({browser_name} main-thread stalls past "
            "Playwright's 60s action timeout under ~180k rapid SSE updates -- "
            "see module docstring); invoke via `make dashboard-soak`"
        )
    page.goto(live_stream_dash.url)
    # "/" is the topology landing now (route swap, spec 2026-07-17); subject-link-*
    # lives on the grid (#/hosts).
    page.goto(f"{live_stream_dash.url}#/hosts")
    page.locator('[data-testid="subject-link-h0"]').click()
    h0_chart = page.locator('[data-testid="chart-CPU"]')  # chart-${chartKey}; no data pushed yet

    # Anchored so the LAST tick lands at ~now: the live-follow window
    # (liveRange(session.endMs, windowMs), data/time.ts) only ever covers the
    # most recent windowMs (15min) ending at the latest ingested sample. A
    # fixed calendar constant here would stop overlapping that window the
    # day after it was written (session.endMs — seeded from the harness's
    # real-wall-clock frame.start — would sit chronologically AFTER every
    # point the replay pushes, freezing the window on empty history and
    # every chart's points-in-window filter to zero forever) — exactly the
    # trap a `datetime(2026, 7, 12, ...)` literal fell into one day later.
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=5 * TICKS)

    async def _replay() -> None:
        for t in range(TICKS):
            ts = t0 + timedelta(seconds=5 * t)
            for h in HOSTS:
                for label in LABELS:
                    await live_stream_dash.collector.push(h, label, float(t), ts=ts)

    live_stream_dash.run(_replay())
    _wait_for_ingestion_to_settle(h0_chart)

    # NOW measure responsiveness, from a settled baseline: the page must
    # still answer a click promptly after ~180k points have been applied.
    # Route back to the fleet grid first -- subject-link-* only exists
    # there, not on the SubjectPage we navigated to above -- via the in-app
    # breadcrumb (not a reload: a fresh `page.goto()` here would hydrate a
    # brand-new session tree from /api/monitor_sessions in one shot instead
    # of measuring whether the ALREADY-streamed-into app stays responsive).
    started = time.monotonic()
    page.get_by_role("link", name="Fleet").click()
    page.locator('[data-testid="subject-link-h1"]').click()
    expect(page.locator('[data-testid="chart-CPU"]')).to_be_visible()  # chart-${chartKey}
    assert time.monotonic() - started < 5.0, "the shell became unresponsive under load"
