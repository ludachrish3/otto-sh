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
whatever historical instant a test pushes metrics at. The live follow window
once tracked only ``session.endMs``, which only METRIC fragments extend, so a
mark-now event could be real, recorded and outside the visible window until
the next metric arrived (#866). The window now ends at ``displayEndMs``
(``web/src/data/time.ts``): the later of ``endMs`` and the server's
``observed_at``, which every fragment, snapshot and event-mutation response
carries. A spec that marks "now" therefore needs no follow-up metric to see
its marker, and ``test_mark_now_appears_without_reload`` pins exactly that.
"""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

_FIXTURES = CHECKOUT_ROOT / "web" / "fixtures"


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _push_tick(dash, host: str, ts: datetime, value: float) -> None:
    """Push one CPU-chart point for *host* at *ts* -- ``push()``'s default
    ``chart="cpu"`` resolves to ``TopCpuParser``, whose ``chart`` attribute is
    ``"CPU"`` (capitalized) -- that's the ``chartKey`` the ``chart-${chartKey}``/
    ``chart-panel-${chartKey}`` testids carry (see test_live_shell.py's own
    ``_push_tick`` docstring for the same note)."""
    dash.run(dash.collector.push(host, "cpu", value, ts=ts))


def _send_json(url: str, body: dict, *, method: str = "POST") -> tuple[int, dict]:
    """Send *body* as JSON to *url* by *method*, returning (status, decoded body).

    Plants or edits an event directly through the HTTP API, bypassing the
    UI (e.g. an out-of-bounds event no in-UI flow can create, or another
    writer's rename).
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
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

    # No metric is pushed after the mark: the server's observed_at alone must
    # bring the real-now event into the window (#866, the module docstring's
    # "fixture-timestamp trap").

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


def test_incomplete_start_date_blocks_save(page: Page, live_stream_dash) -> None:
    """Clearing one Start segment leaves the date incomplete: react-aria never
    calls onChange for that, so the form still holds the old instant. Save
    must be disabled and say why, not post the old timestamp (#864)."""
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "mark-menu").click()
    _tid(page, "menu-add-event").click()
    editor = _tid(page, "event-editor")
    editor.wait_for()
    _tid(page, "editor-label").fill("half-typed")
    expect(_tid(page, "editor-save")).to_be_enabled()

    day = _tid(page, "editor-start").locator('[data-type="day"]')
    day.click()
    # Each Backspace drops one digit, so two clear a two-digit day; on a
    # one-digit day the second lands on the blank segment and only moves
    # focus back, leaving the other segments untouched.
    page.keyboard.press("Backspace")
    page.keyboard.press("Backspace")
    expect(day).to_have_attribute("data-placeholder", "true")
    expect(_tid(page, "editor-save")).to_be_disabled()
    expect(editor.get_by_text("Enter a complete start time.")).to_be_visible()


def test_end_clear_button_aligns_with_end_input(page: Page, live_stream_dash) -> None:
    """End's ✕ sits level with End's input row, with or without the message
    under the field: it must not drop to the hint line (#864)."""
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "mark-menu").click()
    _tid(page, "menu-add-event").click()
    editor = _tid(page, "event-editor")
    editor.wait_for()
    end_input = _tid(page, "editor-end").locator('[role="group"]').first
    clear = _tid(page, "editor-end-clear")

    def assert_level() -> None:
        box = end_input.bounding_box()
        button = clear.bounding_box()
        assert box is not None
        assert button is not None
        centre = button["y"] + button["height"] / 2
        assert box["y"] <= centre <= box["y"] + box["height"], (box, button)

    assert_level()
    # A partial End shows its message under the field.
    _tid(page, "editor-end").locator('[data-type="month"]').click()
    page.keyboard.type("7")
    expect(editor.get_by_text("Enter a complete end time, or clear it with ✕.")).to_be_visible()
    assert_level()


def test_color_swatches_are_named(page: Page, live_stream_dash) -> None:
    """The colour swatches are named options (#858): hovering Violet shows
    its name as a tooltip, the arrow keys move along the row, Space picks
    Green, the persistent name follows, and the POST carries Green's hex."""
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "mark-menu").click()
    _tid(page, "menu-add-event").click()
    editor = _tid(page, "event-editor")
    editor.wait_for()
    _tid(page, "editor-label").fill("named swatch")
    expect(_tid(page, "editor-color-name")).to_have_text("· Grey")

    violet = editor.get_by_role("option", name="Violet")
    violet.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Violet")

    # The selected swatch is the row's one tab stop; the arrows move on.
    grey = editor.get_by_role("option", name="Grey")
    grey.focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    expect(editor.get_by_role("option", name="Green")).to_be_focused()
    page.keyboard.press("Space")
    expect(_tid(page, "editor-color-success")).to_have_attribute("aria-selected", "true")
    expect(_tid(page, "editor-color-name")).to_have_text("· Green")
    # One tab stop for the whole row: Tab leaves the options (for Custom…).
    page.keyboard.press("Tab")
    expect(editor.locator('[role="option"]:focus')).to_have_count(0)
    expect(_tid(page, "editor-color-custom-open")).to_be_focused()

    with page.expect_request(
        lambda r: r.method == "POST" and r.url.endswith("/event")
    ) as request_info:
        _tid(page, "editor-save").click()
    assert request_info.value.post_data_json["color"] == "#2ca02c"
    editor.wait_for(state="detached")


_HEX_ERROR = "Enter six hexadecimal digits, for example #00abcd"


def _open_subject(page: Page, dash) -> None:
    """Push a fresh point for r1 and open its subject page."""
    _push_tick(dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    page.goto(dash.url)
    page.goto(f"{dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()


def _open_new_event_editor_by_keyboard(page: Page) -> Locator:
    """Mark menu -> Add event, by keyboard only; returns the editor."""
    _tid(page, "mark-menu").focus()
    page.keyboard.press("Enter")
    expect(_tid(page, "menu-start-span")).to_be_visible()
    # Add event is the menu's last item.
    page.keyboard.press("End")
    expect(_tid(page, "menu-add-event")).to_be_focused()
    page.keyboard.press("Enter")
    editor = _tid(page, "event-editor")
    editor.wait_for()
    return editor


def _open_custom_by_keyboard(page: Page) -> Locator:
    """Focus Custom…, press Enter; the hex field takes focus."""
    _tid(page, "editor-color-custom-open").focus()
    page.keyboard.press("Enter")
    dialog = _tid(page, "custom-color-dialog")
    expect(dialog).to_be_visible()
    expect(_tid(page, "custom-color-hex")).to_be_focused()
    return dialog


def _replace_hex(page: Page, text: str) -> None:
    """Select the hex field's text and type *text* over it, keystroke by keystroke."""
    hex_input = _tid(page, "custom-color-hex")
    hex_input.press("ControlOrMeta+a")
    hex_input.press_sequentially(text)


def test_custom_color_exact_value(page: Page, live_stream_dash) -> None:
    """Custom… (#859), keyboard only: type an exact hex, Use colour, Save --
    the POST carries exactly that colour."""
    _open_subject(page, live_stream_dash)
    _open_new_event_editor_by_keyboard(page)
    _tid(page, "editor-label").fill("exact custom")
    _open_custom_by_keyboard(page)
    _replace_hex(page, "#00abcd")
    _tid(page, "custom-color-apply").focus()
    page.keyboard.press("Enter")
    expect(_tid(page, "custom-color-dialog")).to_have_count(0)
    expect(_tid(page, "editor-color-name")).to_have_text("· Custom — #00abcd")
    expect(_tid(page, "editor-color-custom")).to_have_attribute("aria-selected", "true")

    with page.expect_request(
        lambda r: r.method == "POST" and r.url.endswith("/event")
    ) as request_info:
        _tid(page, "editor-save").click()
    assert request_info.value.post_data_json["color"] == "#00abcd"


def test_custom_color_incomplete_hex_is_refused_without_blur(page: Page, live_stream_dash) -> None:
    """An incomplete hex refuses Use colour at once, with no blur (#859):
    react-stately's commit would revert the text to the old colour, so a
    stale value must never be applied. Enter and an explicit blur (both
    commits) leave it refused."""
    _open_subject(page, live_stream_dash)
    _open_new_event_editor_by_keyboard(page)
    dialog = _open_custom_by_keyboard(page)
    _replace_hex(page, "#00ab")
    apply = _tid(page, "custom-color-apply")
    expect(apply).to_be_disabled()
    expect(dialog.get_by_text(_HEX_ERROR)).to_be_visible()
    expect(_tid(page, "editor-color-name")).to_have_text("· Grey")

    page.keyboard.press("Enter")
    expect(apply).to_be_disabled()
    expect(dialog.get_by_text(_HEX_ERROR)).to_be_visible()
    _tid(page, "custom-color-hex").blur()
    # The field keeps the refused text beside the error, never the old colour.
    expect(_tid(page, "custom-color-hex")).to_have_value("#00ab")
    expect(apply).to_be_disabled()
    expect(dialog.get_by_text(_HEX_ERROR)).to_be_visible()
    expect(_tid(page, "editor-color-name")).to_have_text("· Grey")


def test_custom_color_nested_escape(page: Page, live_stream_dash) -> None:
    """Escape closes only the Custom… popover: focus returns to Custom… and
    the editor slideout stays open (#859)."""
    _open_subject(page, live_stream_dash)
    editor = _open_new_event_editor_by_keyboard(page)
    _open_custom_by_keyboard(page)
    page.keyboard.press("Escape")
    expect(_tid(page, "custom-color-dialog")).to_have_count(0)
    expect(_tid(page, "editor-color-custom-open")).to_be_focused()
    expect(editor).to_be_visible()


def test_custom_color_existing_event(page: Page, live_stream_dash) -> None:
    """An existing ``#00abcd`` event opens with its custom slot selected, and
    a label-only edit's PATCH carries no ``color`` key (#858/#859)."""
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    session_id = live_stream_dash.collector.session_id
    status, body = _send_json(
        live_stream_dash.api_url(f"/api/session/{session_id}/event"),
        {"label": "teal", "color": "#00abcd"},
    )
    assert status == 201, body
    _open_subject(page, live_stream_dash)
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    _tid(page, f"event-edit-{body['id']}").click()
    editor = _tid(page, "event-editor")
    editor.wait_for()
    expect(_tid(page, "editor-color-custom")).to_have_attribute("aria-selected", "true")
    expect(_tid(page, "editor-color-name")).to_have_text("· Custom — #00abcd")

    _tid(page, "editor-label").fill("teal v2")
    with page.expect_request(lambda r: r.method == "PATCH") as request_info:
        _tid(page, "editor-save").click()
    sent = request_info.value.post_data_json
    assert sent["label"] == "teal v2"
    assert "color" not in sent


def test_custom_color_previews_paint_the_plot_surface(page: Page, live_stream_dash) -> None:
    """Both previews' backgrounds equal the computed ``bg-primary`` of a probe
    under each app theme -- the plot's real surface -- whichever theme the
    app is in while the popover is open (#859)."""
    _open_subject(page, live_stream_dash)
    _open_new_event_editor_by_keyboard(page)
    _open_custom_by_keyboard(page)
    read = """(dark) => {
        document.documentElement.classList.toggle('dark-mode', dark);
        const probe = document.createElement('div');
        probe.className = 'bg-primary';
        document.body.append(probe);
        const surface = getComputedStyle(probe).backgroundColor;
        probe.remove();
        const bg = (id) => getComputedStyle(
            document.querySelector(`[data-testid="${id}"]`)).backgroundColor;
        return {
            surface,
            light: bg('custom-color-preview-light'),
            dark: bg('custom-color-preview-dark'),
        };
    }"""
    in_light = page.evaluate(read, False)
    in_dark = page.evaluate(read, True)
    assert in_light["surface"] != in_dark["surface"], (in_light, in_dark)
    for seen in (in_light, in_dark):
        assert seen["light"] == in_light["surface"], seen
        assert seen["dark"] == in_dark["surface"], seen


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


def test_zoom_buttons_clear_the_y_axis(page: Page, live_stream_dash) -> None:
    """The +/- zoom column sits top-LEFT, but in a gutter widened for it: the
    chart's grid.left (options.ts) is 88px, so the y-axis line — and its
    right-aligned tick labels, name and hover crosshair y-value label — sits far
    enough right of the ~32px-wide button column to clear it for realistic tick
    widths (TODO item 2). Guarded geometrically here: the button's right edge
    stays within the left gutter; the grid.left>=80 assertion in
    chartoptions.test.ts guards the other half — that the axis was pushed right.
    (A pathologically wide tick label, 6-7 digits, could still reach back under
    the column; realistic metric ranges don't, so it's left unbounded.)
    """
    # button column lives entirely within the left gutter, clear of the y-axis
    # line at 88px and its left-extending tick labels
    gutter_px = 56
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU").bounding_box()
    zoom = _tid(page, "zoom-in-CPU").bounding_box()
    assert panel is not None
    assert zoom is not None
    right_edge = zoom["x"] + zoom["width"] - panel["x"]
    assert right_edge <= gutter_px, (
        f"zoom column right edge is {right_edge:.0f}px into the chart — past the "
        f"{gutter_px}px gutter, so it reaches the y-axis label band it must clear"
    )


def test_slideout_scrim_outstacks_live_window_tabs(page: Page, live_stream_dash) -> None:
    """The event slide-out's scrim must out-stack the subject page's live-window
    (5m/15m/1h) tabs (TODO item 1).

    The vendored ``Tab`` paints its pill at ``z-10`` as a flex item whose
    stacking context hoists to the document root (nothing between it and
    ``<body>`` forms one). With the scrim at its original ``z-index: auto`` the
    tabs therefore painted THROUGH the open panel, on top of it — measured on
    the real bundle at ~2x the brightness of the dimmed background around them
    (tab centre ~151 vs surroundings ~83 on a 0-255 scale). Everything else on
    the page (the chart, the +/- zoom column trapped in the chart's own local
    stacking context, the AppBar) dims correctly under the same scrim; the tabs
    were the sole escapee.

    ``elementFromPoint`` cannot catch this: the tab paints above the panel but
    is NOT the pointer target there (the panel content is), so paint-order and
    hit-order disagree at that pixel. The regression is pinned on the stacking
    order itself — the full-viewport scrim geometrically covers the tabs, and it
    must carry a numeric z-index at least the tabs' so it paints over them. The
    scrim now uses ``z-50`` (matching the command palette's own scrim).
    """
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(live_stream_dash, "r1", t0, 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()

    stack = page.evaluate(
        """() => {
          const zval = (el) => {
            const z = parseInt(getComputedStyle(el).zIndex, 10);
            return Number.isNaN(z) ? null : z;
          };
          const tab = document.querySelector('[data-testid="live-window-5m"]');
          const scrim = [...document.querySelectorAll('div')].find((d) => {
            const s = getComputedStyle(d);
            return s.position === 'fixed' && d.className.includes('bg-overlay');
          });
          if (!tab || !scrim) return null;
          const tr = tab.getBoundingClientRect();
          const sr = scrim.getBoundingClientRect();
          const cx = tr.x + tr.width / 2;
          const cy = tr.y + tr.height / 2;
          return {
            tabZ: zval(tab),
            scrimZ: zval(scrim),
            covers: sr.x <= cx && cx <= sr.x + sr.width && sr.y <= cy && cy <= sr.y + sr.height,
          };
        }"""
    )
    assert stack is not None, "live-window tabs and/or the slide-out scrim were not found"
    assert stack["covers"], "the scrim does not geometrically cover the live-window tabs"
    # Premise guard: the tab escapes only because it carries a positive z-index.
    assert stack["tabZ"] is not None, (
        "premise changed: the live-window tab no longer carries a numeric z-index to escape"
    )
    assert stack["scrimZ"] is not None, (
        "scrim has z-index: auto — the live-window tabs paint on top of the open slide-out"
    )
    assert stack["scrimZ"] >= stack["tabZ"], (
        f"scrim (z={stack['scrimZ']}) does not out-stack the live-window tabs "
        f"(z={stack['tabZ']}): the tabs paint on top of the open slide-out"
    )


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
    status, body = _send_json(
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


def _label_rects(panel: Locator) -> list[list[float]]:
    """``data-echarts-label-rects``: each visible event label's
    ``[lane, leftPx, widthPx]``, plot-relative (ChartPanel's overlay effect)."""
    return json.loads(panel.get_attribute("data-echarts-label-rects") or "[]")


def test_clustered_and_edge_labels_stay_in_plot(page: Page, live_stream_dash) -> None:
    """Three point events 2 s apart (the test start/pass/fail case, #863)
    take three label lanes instead of overprinting, and a label at the
    window's right edge is shifted inside the plot rather than clipped."""
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(live_stream_dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    session_id = live_stream_dash.collector.session_id
    url = live_stream_dash.api_url(f"/api/session/{session_id}/event")
    cluster_at = t0 - timedelta(minutes=5)
    events = [
        ("test start", cluster_at),
        ("test pass", cluster_at + timedelta(seconds=2)),
        ("test fail", cluster_at + timedelta(seconds=4)),
        ("right window edge", t0),
    ]
    for label, ts in events:
        status, body = _send_json(url, {"label": label, "timestamp": ts.isoformat()})
        assert status == 201, body
    # No later tick: the snapshot's observed_at puts the window's `to` just
    # past the right-edge event (#866).

    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-marker-count", str(len(events)))
    page.wait_for_function(
        "(n) => JSON.parse(document.querySelector('[data-testid=\"chart-panel-CPU\"]')"
        ".dataset.echartsLabelRects || '[]').length === n",
        arg=len(events),
    )
    rects = _label_rects(panel)
    # The plot width ChartPanel laid those rects out across, stamped beside them.
    plot_width = float(panel.get_attribute("data-echarts-plot-width") or "nan")
    assert {lane for lane, _, _ in rects} == {0, 1, 2}, rects
    for lane, left, width in rects:
        assert left >= 0, (lane, left, width)
        assert left + width <= plot_width, (lane, left, width, plot_width)


def _seed_cluster(dash) -> dict[str, int]:
    """Five point events 1 ms apart (#920's coincident cluster), between CPU
    ticks; returns each label's wire id."""
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    url = dash.api_url(f"/api/session/{dash.collector.session_id}/event")
    cluster_at = t0 - timedelta(minutes=5)
    labels = ["test start", "collect logs and more", "test pass", "test fail", "teardown"]
    ids: dict[str, int] = {}
    for i, label in enumerate(labels):
        ts = cluster_at + timedelta(milliseconds=i)
        status, body = _send_json(url, {"label": label, "timestamp": ts.isoformat()})
        assert status == 201, body
        ids[label] = body["id"]
    return ids


def test_cluster_overflow_lists_hidden_events(page: Page, live_stream_dash) -> None:
    """A five-event cluster keeps the three earliest labels visible, and the
    "(+2)" label is a keyboard-reachable button listing the two hidden
    events, each of which opens the editor (#920)."""
    ids = _seed_cluster(live_stream_dash)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    panel = _tid(page, "chart-panel-CPU")
    expect(panel).to_have_attribute("data-echarts-marker-count", "5")
    # The "(+2)" host is the third event in time, not the widest label.
    overflow = _tid(page, f"cluster-overflow-{ids['test pass']}")
    expect(overflow).to_have_attribute(
        "aria-label", "test pass (+2): 2 more events: test fail, teardown"
    )

    # The overlay passes the pointer through everywhere but the button: the
    # plot's centre still hits the chart's own canvas.
    box = panel.bounding_box()
    assert box is not None
    hit = page.evaluate(
        "([x, y]) => document.elementFromPoint(x, y)?.closest('[data-testid=\"chart-panel-CPU\"]')"
        " !== null",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert hit is True

    # Reachable by Tab: the next stop after the chart's zoom-out button.
    _tid(page, "zoom-out-CPU").focus()
    page.keyboard.press("Tab")
    expect(overflow).to_be_focused()
    page.keyboard.press("Enter")
    listing = _tid(page, "cluster-overflow-list")
    expect(listing).to_be_visible()
    expect(listing).to_contain_text("test fail")
    expect(listing).to_contain_text("teardown")
    # Each row leads with a 24-hour HH:MM:SS clock time, as the x-axis reads.
    expect(listing).to_contain_text(re.compile(r"\b\d{2}:\d{2}:\d{2}test fail"))
    expect(listing).not_to_contain_text(re.compile(r"\b(AM|PM)\b"))

    _tid(page, f"cluster-overflow-item-{ids['test fail']}").click()
    expect(_tid(page, "event-editor")).to_be_visible()
    expect(_tid(page, "editor-label")).to_have_value("test fail")


def test_cluster_overflow_list_scrolls_at_a_short_viewport(page: Page, live_stream_dash) -> None:
    """A cluster with many hidden events lists them in a height-capped,
    scrollable popover: at a short viewport the list stays on screen and its
    last row scrolls into view and opens the editor (#920)."""
    dash = live_stream_dash
    t0 = datetime.now(tz=timezone.utc)
    for mins in (14, 10, 5, 0):
        _push_tick(dash, "r1", t0 - timedelta(minutes=mins), 10.0)
    url = dash.api_url(f"/api/session/{dash.collector.session_id}/event")
    cluster_at = t0 - timedelta(minutes=5)
    ids: list[int] = []
    for i in range(60):
        ts = cluster_at + timedelta(milliseconds=i)
        status, body = _send_json(url, {"label": f"step {i:02d}", "timestamp": ts.isoformat()})
        assert status == 201, body
        ids.append(body["id"])
    page.set_viewport_size({"width": 1280, "height": 600})
    page.goto(dash.url)
    page.goto(f"{dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()
    expect(_tid(page, "chart-panel-CPU")).to_have_attribute("data-echarts-marker-count", "60")

    # The earliest three label lanes stay visible; the third hosts the "(+57)".
    _tid(page, f"cluster-overflow-{ids[2]}").click()
    listing = _tid(page, "cluster-overflow-list")
    expect(listing).to_be_visible()
    box = listing.bounding_box()
    assert box is not None
    assert box["y"] >= 0, box
    assert box["y"] + box["height"] <= 600, box
    scrolls = listing.evaluate(
        "el => [...el.querySelectorAll('*')].concat(el)"
        ".some(n => n.scrollHeight > n.clientHeight && getComputedStyle(n).overflowY === 'auto')"
    )
    assert scrolls is True

    last = _tid(page, f"cluster-overflow-item-{ids[-1]}")
    last.scroll_into_view_if_needed()
    expect(last).to_be_in_viewport()
    last.click()
    expect(_tid(page, "event-editor")).to_be_visible()
    expect(_tid(page, "editor-label")).to_have_value("step 59")


def _open_editor_for_seeded_event(page: Page, dash, label: str) -> int:
    """Plant one point event through the API and open its editor from the
    events panel; returns its wire id."""
    t0 = datetime.now(tz=timezone.utc)
    _push_tick(dash, "r1", t0, 10.0)
    status, body = _send_json(
        dash.api_url(f"/api/session/{dash.collector.session_id}/event"),
        {"label": label, "timestamp": (t0 - timedelta(seconds=30)).isoformat()},
    )
    assert status == 201, body
    page.goto(dash.url)
    page.goto(f"{dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    _tid(page, f"event-edit-{body['id']}").click()
    _tid(page, "event-editor").wait_for()
    return body["id"]


def test_delete_asks_and_escape_keeps_the_editor(page: Page, live_stream_dash) -> None:
    """Delete asks in a dialog that names the event: a double-click deletes
    nothing, Escape closes only the dialog (focus back on Delete), and Confirm
    deletes the event and closes both (#869)."""
    event_id = _open_editor_for_seeded_event(page, live_stream_dash, "soak run")
    deletes: list[str] = []
    page.on("request", lambda r: deletes.append(r.url) if r.method == "DELETE" else None)

    _tid(page, "editor-delete").dblclick()
    dialog = _tid(page, "confirm-delete")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(
        "\u201csoak run\u201d will be removed. You can undo this with Ctrl Z."
    )
    expect(_tid(page, "confirm-delete-cancel")).to_be_focused()
    assert deletes == []

    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(_tid(page, "event-editor")).to_be_visible()
    expect(_tid(page, "editor-delete")).to_be_focused()
    assert deletes == []

    _tid(page, "editor-delete").click()
    _tid(page, "confirm-delete-confirm").click()
    expect(dialog).to_have_count(0)
    expect(_tid(page, "event-editor")).to_have_count(0)
    assert len(deletes) == 1
    assert deletes[0].endswith(f"/event/{event_id}")


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
    page.goto(f"{shell_dash.url}#/host/chassis-a-lc1-1")
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()

    out_dir = CHECKOUT_ROOT / "reports" / "monitor-e2e-shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    for theme_name, dark in (("light", False), ("dark", True)):
        _set_theme(page, dark=dark)
        out_path = out_dir / f"span-labels-{theme_name}.png"
        page.locator('[data-testid="subject-page"]').screenshot(path=str(out_path))
        assert out_path.stat().st_size > 0

    # #863: a dense point cluster (test lifecycle markers milliseconds-to-
    # seconds apart, collapsing into three lanes and a "(+N)" count) and
    # labels at both window edges (shifted inside the plot, clear of the
    # y-axis name). Each is kitchen-sink.json plus extra point events.
    scenarios = {
        "clustered-points": [
            ("test start", "08:40:00"),
            ("test pass", "08:40:00.004"),
            ("test fail", "08:40:00.009"),
            ("teardown", "08:40:02"),
            ("collect logs", "08:40:04"),
        ],
        "edge-labels": [
            ("LEFT WINDOW EDGE", "08:00:00"),
            ("RIGHT WINDOW EDGE", "10:00:00"),
            ("deploy v2.14.0-rc3 to the staging cluster", "09:58:00"),
        ],
    }
    base = json.loads((_FIXTURES / "kitchen-sink.json").read_text())
    for name, extra in scenarios.items():
        doc = json.loads(json.dumps(base))
        session = doc["sessions"][0]
        for n, (label, clock) in enumerate(extra):
            session["events"].append(
                {
                    "id": 100 + n,
                    "timestamp": f"2026-07-01T{clock}Z",
                    "label": label,
                    "source": "manual",
                    "color": "#7c5cff",
                    "dash": "dash",
                }
            )
        fixture = tmp_path / f"{name}.json"
        fixture.write_text(json.dumps(doc))
        page.goto(shell_dash.url)
        page.locator('[data-testid="import-input"]').set_input_files(fixture)
        page.locator('[data-testid="review-bar"]').wait_for()
        page.goto(f"{shell_dash.url}#/host/chassis-a-lc1-1")
        panel = page.locator('[data-testid="chart-panel-cpu"]')
        panel.locator("canvas").wait_for()
        expect(panel).to_have_attribute("data-echarts-label-rects", re.compile(r"^\[\["))
        for theme_name, dark in (("light", False), ("dark", True)):
            _set_theme(page, dark=dark)
            out_path = out_dir / f"{name}-{theme_name}.png"
            panel.screenshot(path=str(out_path))
            assert out_path.stat().st_size > 0
