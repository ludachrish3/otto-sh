"""Marking + chart gestures in a real browser (Monitor Plan 5c, Task 13).

Everything up to here (event CRUD routes, flocked archive writes, eventApi's
synthetic-fragment client, MarkControl/EventsPanel/EventEditor, the sweep/
drag/wheel/zoom-button chart gestures) is proven only in unit tests against a
mocked ECharts/fetch. This module is the first place any of it is driven
end-to-end: a real browser, a real ``MonitorServer``, real SSE, real ECharts.

Contract: ``data-testid``/``data-echarts-*`` attributes only (Tasks 8-12) --
styling and DOM structure are free to change. Every wait is an expectation
poll, never a flat sleep (5b soak lesson).

Fixture-timestamp trap (found writing this module, before any test ran):
``markNow()``/``startSpan()`` omit ``timestamp``, so the server stamps the
event at REAL wall-clock now (``event_ops.resolve_create``) -- independent of
whatever historical instant a test pushes metrics at. A live session's
follow window tracks ``session.endMs``, which only METRIC fragments extend
(``fragment.ts``), never events -- so a mark-now event can be real, recorded,
and permanently outside the visible window if the last pushed metric predates
it. Every spec below that creates a mark-now/start-span event and then checks
the chart's marker count pushes a follow-up metric point at a fresh
``datetime.now(tz=timezone.utc)`` afterward specifically to pull the window's
``to`` bound past the new event.
"""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

_FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _push_tick(dash, host: str, ts: datetime, value: float) -> None:
    """Push one CPU-chart point for *host* at *ts* -- ``push()``'s default
    ``chart="cpu"`` resolves to ``TopCpuParser``, whose ``chart`` attribute is
    ``"CPU"`` (capitalized) -- that's the ``chartKey`` the ``chart-${chartKey}``/
    ``chart-panel-${chartKey}`` testids carry (see test_live_shell.py's own
    ``_push_tick`` docstring for the same note)."""
    dash.run(dash.collector.push(host, "cpu", value, ts=ts))


def _post_json(url: str, body: dict) -> tuple[int, dict]:
    """POST *body* as JSON to *url*, returning (status, decoded body).

    Used only by ``test_refused_jump_keeps_panel_open`` to plant an
    out-of-bounds event directly through the HTTP API (bypassing the UI --
    there is no in-UI flow to create an event far outside the session).
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # local test server
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def _drag(page: Page, box: dict, from_frac: float, to_frac: float, *, ctrl: bool = False) -> None:
    """Horizontal mouse drag across *box* (a Playwright bounding_box()) from
    ``from_frac``..``to_frac`` of its width, at vertical center. Optionally
    holds Control for the whole gesture (the pan modifier, options.ts's
    ``moveOnMouseMove: "ctrl"``)."""
    y = box["y"] + box["height"] / 2
    x0 = box["x"] + box["width"] * from_frac
    x1 = box["x"] + box["width"] * to_frac
    if ctrl:
        page.keyboard.down("Control")
    try:
        page.mouse.move(x0, y)
        page.mouse.down()
        page.mouse.move((x0 + x1) / 2, y, steps=5)
        page.mouse.move(x1, y, steps=5)
        page.mouse.up()
    finally:
        if ctrl:
            page.keyboard.up("Control")


def _open_palette_command(page: Page, query: str, command_id: str) -> None:
    """Open the command palette, filter to *query*, click *command_id*'s row."""
    page.keyboard.press("Control+KeyK")
    expect(_tid(page, "command-menu")).to_be_visible()
    _tid(page, "command-input").fill(query)
    row = _tid(page, f"command-item-{command_id}")
    expect(row).to_be_visible()
    row.click()


def test_mark_now_appears_without_reload(page: Page, live_stream_dash) -> None:
    """``mark-button`` -> label -> Enter: the full SSE-echo round trip, no
    reload -- ``events-count`` increments and the subject chart's own
    ``data-echarts-marker-count`` (stamped from ChartPanel's imperative patch
    effect, not a render-body echo) increments with it."""
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-marker-count", re.compile(r"\d+"))
    before_markers = panel.get_attribute("data-echarts-marker-count")
    before_count = int(_tid(page, "events-count").inner_text())

    _tid(page, "mark-button").click()
    _tid(page, "mark-label-input").fill("checkpoint")
    page.keyboard.press("Enter")
    expect(_tid(page, "mark-popover")).to_have_count(0)

    # See the module docstring's "fixture-timestamp trap": pull the live
    # window's `to` bound past the just-created (real-now) event.
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 11.0)

    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"events-count\"]').innerText !== prev",
        arg=str(before_count),
    )
    assert int(_tid(page, "events-count").inner_text()) == before_count + 1
    expect(panel).not_to_have_attribute("data-echarts-marker-count", before_markers or "")


def test_span_start_stop_flow(page: Page, live_stream_dash) -> None:
    """Start via the menu -> ``menu-end-span`` becomes enabled -> End -> the
    events row shows a duration (the " · <span>" suffix EventsPanel only
    appends once ``end_timestamp`` is set)."""
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "mark-menu").click()
    end_item = _tid(page, "menu-end-span")
    expect(end_item).to_have_attribute("aria-disabled", "true")
    page.keyboard.press("Escape")
    expect(end_item).to_have_count(0)

    _tid(page, "mark-menu").click()
    _tid(page, "menu-start-span").click()
    _tid(page, "mark-label-input").fill("soak")
    page.keyboard.press("Enter")
    expect(_tid(page, "mark-popover")).to_have_count(0)

    _tid(page, "mark-menu").click()
    end_item = _tid(page, "menu-end-span")
    expect(end_item).not_to_have_attribute("aria-disabled", "true")
    end_item.click()

    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    row = page.locator('[data-testid^="event-row-"]').first
    # Before ending, a span-in-progress row shows only its start time (no "
    # · <duration>" suffix -- EventsPanel.tsx only appends that once
    # end_timestamp is set); after End it must.
    page.wait_for_function(
        "() => document.querySelector('[data-testid^=\"event-row-\"]').innerText.includes('·')"
    )
    assert "soak" in row.inner_text()


def test_drag_zoom_select(page: Page, live_stream_dash) -> None:
    """A plain drag across the chart canvas (no modifier) zoom-selects: the
    window shrinks toward the dragged sub-range and, in live mode, pause
    derives to "paused" (pause-toggle reads Resume) -- `range` going non-null
    is the ONE thing `useIsPaused` derives from."""
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(live_stream_dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-window-to", re.compile(r"\d+"))
    before_to = panel.get_attribute("data-echarts-window-to")
    expect(_tid(page, "pause-toggle")).to_have_attribute("aria-label", "Pause")

    box = panel.bounding_box()
    assert box is not None
    _drag(page, box, 0.25, 0.7)

    expect(panel).not_to_have_attribute("data-echarts-window-to", before_to or "")
    after_to = int(panel.get_attribute("data-echarts-window-to") or "0")
    # The drag's right edge (70% across the panel) sits short of the full
    # window's right edge -- a genuine zoom-select must shrink the window's
    # `to` bound down to somewhere inside the original span, not merely
    # relocate it.
    assert after_to < int(before_to or "0")
    expect(_tid(page, "pause-toggle")).to_have_attribute("aria-label", "Resume")


def test_ctrl_drag_pans(page: Page, live_stream_dash) -> None:
    """Ctrl-drag pans: the window's bounds shift, but its WIDTH is unchanged
    -- the spec's no-op risk #2 probe. Two independent defects found only by
    driving a real browser (see ChartPanel.tsx's comment on the manual pan
    handler that replaces dataZoom's for this gesture): (1) an armed global
    brush cursor (Task 12's `takeGlobalCursor`) captures every plain drag,
    Ctrl-held or not; (2) even with the brush out of the way, ECharts'
    OWN "inside" dataZoom pan is structurally a no-op here, because
    `xAxis.min`/`max` always equal the currently-shown window -- its
    percent range is permanently `[0, 100]`, its own full extent, so there
    is never room within it to shift. ChartPanel now owns this gesture by
    hand instead.

    Dragged toward EARLIER times (right-to-left on screen, i.e. increasing
    pixel position -- see `_drag`'s direction), not later: the live-follow
    window's `to` bound already sits at the session's latest sample
    (`live_stream_dash`'s `bounds.to`), so panning toward LATER times would
    immediately hit that wall and clamp asymmetrically (verified empirically
    -- that clamping is `clampRange` doing its job, not a pan bug, but it
    would make this assertion fail for the wrong reason). Earlier has an
    hour of headroom (`_FRAME_BACKDATE`, conftest.py).
    """
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(live_stream_dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-window-to", re.compile(r"\d+"))
    expect(panel).to_have_attribute("data-echarts-window-from", re.compile(r"\d+"))
    before_to = int(panel.get_attribute("data-echarts-window-to") or "0")
    before_from = int(panel.get_attribute("data-echarts-window-from") or "0")
    before_width = before_to - before_from

    box = panel.bounding_box()
    assert box is not None
    _drag(page, box, 0.3, 0.6, ctrl=True)  # left-to-right: pans toward earlier times

    expect(panel).not_to_have_attribute("data-echarts-window-to", str(before_to))
    after_to = int(panel.get_attribute("data-echarts-window-to") or "0")
    after_from = int(panel.get_attribute("data-echarts-window-from") or "0")
    after_width = after_to - after_from
    # Bounds actually moved...
    assert after_from != before_from
    # ...but the WIDTH is preserved (a pan, not a zoom) -- each bound is
    # rounded independently, so allow a 1ms slop.
    assert abs(after_width - before_width) <= 1


def test_wheel_scrolls_page_not_chart(page: Page, live_stream_dash) -> None:
    """Wheeling over a chart scrolls the PAGE (Task 11: the wheel was freed
    from ECharts entirely -- `zoomOnMouseWheel`/`moveOnMouseWheel` both
    false) -- the chart's own window must not move."""
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    page.set_viewport_size({"width": 1280, "height": 400})
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()
    # `live_stream_dash`'s lab declares 9 hosts (_LIVE_STREAM_HOSTS) -- the
    # grid this just navigated from is tall enough that clicking a
    # lower tile auto-scrolled it into view, and that scroll position
    # survives the hash-only navigation to this page (no full reload
    # resets it). Reset explicitly so "did the wheel scroll it" has
    # somewhere to go and isn't measuring an already-maxed-out page.
    page.evaluate("() => window.scrollTo(0, 0)")

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-window-to", re.compile(r"\d+"))
    before_to = panel.get_attribute("data-echarts-window-to")
    before_scroll = page.evaluate("() => window.scrollY")

    box = panel.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 600)

    page.wait_for_function(
        "(prev) => window.scrollY !== prev",
        arg=before_scroll,
    )
    expect(panel).to_have_attribute("data-echarts-window-to", before_to or "")


def test_sweep_creates_span_via_editor(page: Page, live_stream_dash) -> None:
    """Palette "Sweep span on chart" -> `sweep-chip` -> drag across a chart
    -> the editor opens with BOTH time fields populated (a span, not a
    point draft -- `editor-end-clear` is only enabled once `endTimestampMs`
    is set) -> label + Save -> the marker count increments and the events
    row lists the new span."""
    t0 = datetime.now(tz=timezone.utc)
    for mins in (10, 5, 0):
        _push_tick(live_stream_dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-marker-count", re.compile(r"\d+"))
    before_markers = panel.get_attribute("data-echarts-marker-count")

    _open_palette_command(page, "Sweep", "action-sweep-span")
    expect(_tid(page, "command-menu")).to_have_count(0)
    expect(_tid(page, "sweep-chip")).to_be_visible()

    box = panel.bounding_box()
    assert box is not None
    _drag(page, box, 0.25, 0.7)

    editor = _tid(page, "event-editor")
    editor.wait_for()
    # A plain "Add event…" draft has no end (editor-end-clear disabled); a
    # sweep-created draft always has both bounds, so this control being
    # enabled is the discriminating proof it's a SPAN draft, not a point.
    expect(_tid(page, "editor-end-clear")).to_be_enabled()

    _tid(page, "editor-label").fill("swept span")
    _tid(page, "editor-save").click()
    editor.wait_for(state="detached")

    expect(panel).not_to_have_attribute("data-echarts-marker-count", before_markers or "")

    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    row = page.locator('[data-testid^="event-row-"]', has_text="swept span")
    expect(row).to_be_visible()
    assert "·" in row.inner_text()  # a span shows its duration


def test_zoom_buttons(page: Page, live_stream_dash) -> None:
    """Clicking `zoom-in-*` halves the echarts window -- proven by the point
    count ECharts was actually handed (`data-echarts-point-count`, stamped
    from ChartPanel's own `setOption()` effect) dropping from all 4 pushed
    points to exactly the 2 that fall inside the halved-about-center window,
    not merely by the window's bounds moving."""
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(live_stream_dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-point-count", "4")

    _tid(page, "zoom-in-CPU").click()

    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"chart-panel-CPU\"]')"
        ".getAttribute('data-echarts-point-count') !== '4'"
    )
    expect(panel).to_have_attribute("data-echarts-point-count", "2")


def test_db_review_edit_persists_across_restart(page: Page, db_review_dash) -> None:
    """A ``.db`` archive is event-editable in review mode (Plan 5c Task 5):
    add an event via ``events-compose-add`` -> editor -> Save; restart the
    harness server against the SAME archive file (a fresh
    ``MonitorServer`` re-reading it from disk, not the same in-memory
    ``document`` object -- see ``DbReviewHandle.restart()``); reload; the
    event is still listed."""
    page.goto(f"{db_review_dash.harness.url}#/host/dbhost")
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    _tid(page, "events-compose-add").click()

    editor = _tid(page, "event-editor")
    editor.wait_for()
    _tid(page, "editor-label").fill("post-hoc note")
    _tid(page, "editor-save").click()
    editor.wait_for(state="detached")

    row = page.locator('[data-testid^="event-row-"]', has_text="post-hoc note")
    expect(row).to_be_visible()

    new_harness = db_review_dash.restart()
    page.goto(f"{new_harness.url}#/host/dbhost")
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    row = page.locator('[data-testid^="event-row-"]', has_text="post-hoc note")
    expect(row).to_be_visible()


def test_json_review_has_no_marking_chrome(page: Page, review_dash) -> None:
    """A ``.json`` review (``review_dash``: ``archive_path=None``, so
    ``editable`` is false) has NO reachable marking chrome at all.

    ``review_dash``'s document (both sessions built from ``minimal.json``,
    which carries no events -- see conftest.py's ``_review_boot_document``)
    has zero events, so SubjectPage's own gate
    (``session.events.length > 0 || editable``) means ``events-button``
    itself never renders -- the strongest form of "no marking chrome": there
    is nothing here for a user to even find, let alone use. ``mark-button``
    is gone too (AppBar only mounts ``MarkControl`` in live mode). The
    events-panel/compose/row-edit testids are asserted absent as well, for
    the exact claim the spec names, even though their absence follows
    structurally from the button that would reveal them being gone.
    """
    page.goto(review_dash.url)
    page.locator('[data-testid="review-bar"]').wait_for()
    page.goto(f"{review_dash.url}#/hosts")
    page.locator('[data-testid^="subject-link-"]').first.click()
    page.locator('[data-testid="subject-page"]').wait_for()

    assert _tid(page, "mark-button").count() == 0
    assert _tid(page, "events-button").count() == 0
    assert _tid(page, "events-panel").count() == 0
    assert _tid(page, "events-compose").count() == 0
    assert page.locator('[data-testid^="event-edit-"]').count() == 0
    assert page.locator('[data-testid^="event-endnow-"]').count() == 0


def test_refused_jump_keeps_panel_open(page: Page, live_stream_dash) -> None:
    """A row whose jump target falls entirely outside the session's padded
    bounds (``EventsPanel.jump``'s ``JUMP_PAD_MS`` window) shows
    ``jump-notice`` instead of silently closing the panel."""
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    session_id = live_stream_dash.collector.session_id
    far_ts = t0 - timedelta(days=2)
    status, body = _post_json(
        live_stream_dash.api_url(f"/api/session/{session_id}/event"),
        {"label": "faraway", "timestamp": far_ts.isoformat()},
    )
    assert status == 201, body

    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    row = page.locator('[data-testid^="event-row-"]', has_text="faraway")
    row.click()

    expect(_tid(page, "jump-notice")).to_be_visible()
    expect(_tid(page, "events-panel")).to_be_visible()


# --- Visual gate (Task 13 Step 4): span-label legibility, both themes -----
#
# Not one of the ten behavior specs above -- legibility can only be judged by
# looking at the rendered pixels, so this test's job is to produce the two
# PNGs a human (or an agent with the Read tool) then actually looks at; see
# task-13-report.md for what was seen. Named with "zz_shot" (not one of the
# ten spec names) specifically so `pytest ... -k zz_shot` selects only this.


def _set_theme(page: Page, *, dark: bool) -> None:
    if page.evaluate("document.documentElement.classList.contains('dark-mode')") is dark:
        return
    page.locator('[data-testid="overflow-menu"]').click()
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "(want) => document.documentElement.classList.contains('dark-mode') === want", arg=dark
    )


def test_zz_shot_span_labels_both_themes(page: Page, shell_dash, tmp_path) -> None:
    """Screenshot the overlapping-span markArea labels (kitchen-sink.json's
    "stress run" 09:25-09:35 / "log capture" 09:30-09:40, Task 11's
    dark-mode label fix -- `eventOverlay`'s markArea label now carries
    `color: theme.ink`) in both themes, to ``reports/monitor-e2e-shots/``.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(shell_dash.url)
    page.locator('[data-testid="import-input"]').set_input_files(_FIXTURES / "kitchen-sink.json")
    page.locator('[data-testid="review-bar"]').wait_for()
    page.goto(f"{shell_dash.url}#/host/chassis-a_lc1")
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()

    out_dir = Path(__file__).resolve().parents[4] / "reports" / "monitor-e2e-shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    for theme_name, dark in (("light", False), ("dark", True)):
        _set_theme(page, dark=dark)
        out_path = out_dir / f"span-labels-{theme_name}.png"
        page.locator('[data-testid="subject-page"]').screenshot(path=str(out_path))
        assert out_path.stat().st_size > 0
