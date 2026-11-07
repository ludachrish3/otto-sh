"""Event undo and redo in a real browser (#929).

The unit suites drive the history against mocked fetch; this module is the
end-to-end proof: a real ``MonitorServer``, the real revert route, the real
keyboard chords. The wording asserted here is ``web/src/data/undoRedo.ts``'s;
the behaviour is documented under "Undo and redo" in
``docs/cli/monitor/dashboard.md``.

Every wait is an expectation poll. The tab runs one event mutation at a time,
so each chord waits for its toast before the next is pressed.
"""

import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.monitor.dashboard.test_marking import (
    _open_editor_for_seeded_event,
    _push_tick,
    _tid,
)

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

_UNDO = "Control+KeyZ"
_REDO = "Control+KeyY"


def _expect_toast(page: Page, text: str) -> None:
    expect(_tid(page, "toast")).to_contain_text(text)


def _events(page: Page):
    return page.locator('[data-testid^="event-edit-"]')


def _row_for(page: Page, event_id: int):
    """The events-panel ``<li>`` of wire id *event_id*."""
    return page.locator("li", has=_tid(page, f"event-edit-{event_id}"))


def test_undo_redo_round_trip(page: Page, live_stream_dash) -> None:
    """Mark, edit the label, delete; Ctrl+Z three times restores each step in
    reverse with the SAME id, and Ctrl+Y three times re-applies them."""
    _push_tick(live_stream_dash, "r1", datetime.now(tz=timezone.utc), 10.0)
    page.goto(live_stream_dash.url)
    page.goto(f"{live_stream_dash.url}#/hosts")
    _tid(page, "subject-link-r1").click()
    page.locator('[data-testid="subject-page"]').wait_for()

    _tid(page, "mark-button").click()
    _tid(page, "mark-label-input").fill("first")
    page.keyboard.press("Enter")
    expect(_tid(page, "mark-popover")).to_have_count(0)

    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    expect(_events(page)).to_have_count(1)
    test_id = _events(page).first.get_attribute("data-testid")
    assert test_id is not None
    event_id = int(test_id.removeprefix("event-edit-"))

    _tid(page, f"event-edit-{event_id}").click()
    _tid(page, "editor-label").fill("second")
    _tid(page, "editor-save").click()
    expect(_tid(page, "event-editor")).to_have_count(0)
    expect(_row_for(page, event_id)).to_contain_text("second")

    _tid(page, f"event-edit-{event_id}").click()
    _tid(page, "editor-delete").click()
    _tid(page, "confirm-delete-confirm").click()
    expect(_tid(page, "event-editor")).to_have_count(0)
    expect(_events(page)).to_have_count(0)

    page.keyboard.press(_UNDO)
    _expect_toast(page, "Undid: delete “second”")
    expect(_row_for(page, event_id)).to_contain_text("second")

    page.keyboard.press(_UNDO)
    _expect_toast(page, "Undid: edit “second”")
    expect(_row_for(page, event_id)).to_contain_text("first")

    page.keyboard.press(_UNDO)
    _expect_toast(page, "Undid: add “first”")
    expect(_events(page)).to_have_count(0)

    page.keyboard.press(_REDO)
    _expect_toast(page, "Redid: add “first”")
    expect(_row_for(page, event_id)).to_contain_text("first")

    page.keyboard.press(_REDO)
    _expect_toast(page, "Redid: edit “second”")
    expect(_row_for(page, event_id)).to_contain_text("second")

    page.keyboard.press(_REDO)
    _expect_toast(page, "Redid: delete “second”")
    expect(_events(page)).to_have_count(0)


def test_ctrl_z_in_label_field_is_text_undo(
    page: Page, live_stream_dash, browser_name: str
) -> None:
    """Ctrl+Z in the editor's label field belongs to the field: no revert
    request is sent and the event list is unchanged, on every engine. The
    field's own text reverting is asserted on Chromium only; native text undo
    under Playwright is not reliable on Firefox and WebKit."""
    _open_editor_for_seeded_event(page, live_stream_dash, "soak run")
    # Count revert calls in the page, at the call: a handler that fired would
    # call fetch synchronously inside the Ctrl+Z keydown dispatch.
    page.evaluate(
        """() => {
            window.__reverts = 0;
            const send = window.fetch;
            window.fetch = (input, init) => {
                if (String(input).endsWith("/revert")) window.__reverts += 1;
                return send(input, init);
            };
        }"""
    )
    rows = page.locator('[data-testid^="event-row-"]')
    expect(rows.filter(has_text="soak run")).to_have_count(1)
    rows_before = rows.all_inner_texts()

    label = _tid(page, "editor-label")
    label.click()
    label.press("End")
    label.press_sequentially(" extra")
    expect(label).to_have_value("soak run extra")
    page.keyboard.press(_UNDO)
    if browser_name == "chromium":
        expect(label).not_to_have_value("soak run extra")
    # The synchronisation point: keys are dispatched in order, so once the
    # sentinel typed after Ctrl+Z is in the field, Ctrl+Z's own dispatch,
    # and any request a handler sent from it, has run.
    page.keyboard.type("!")
    expect(label).to_have_value(re.compile(r"!$"))

    assert page.evaluate("() => window.__reverts") == 0
    expect(_tid(page, "toast")).to_have_count(0)
    assert rows.all_inner_texts() == rows_before


def test_toast_is_reachable_with_the_events_panel_open(page: Page, live_stream_dash) -> None:
    """Undo from the Events panel (a modal): its toast is not hidden from
    assistive technology, F6 then Tab reaches its button by keyboard, and a
    click on it works without closing the panel."""
    event_id = _open_editor_for_seeded_event(page, live_stream_dash, "soak run")
    _tid(page, "editor-delete").click()
    _tid(page, "confirm-delete-confirm").click()
    expect(_tid(page, "event-editor")).to_have_count(0)
    expect(_events(page)).to_have_count(0)

    page.keyboard.press(_UNDO)
    _expect_toast(page, "Undid: delete “soak run”")
    expect(_tid(page, "events-panel")).to_be_visible()
    hidden_by = """(el) => {
        for (let n = el; n !== null; n = n.parentElement) {
            const hidden = n.inert || n.getAttribute("aria-hidden") === "true";
            if (hidden) return n.outerHTML.slice(0, 80);
        }
        return null;
    }"""
    assert _tid(page, "toast-action").evaluate(hidden_by) is None

    page.keyboard.press("F6")
    expect(_tid(page, "toast")).to_be_focused()
    page.keyboard.press("Tab")
    expect(_tid(page, "toast-action")).to_be_focused()
    page.keyboard.press("Enter")
    _expect_toast(page, "Redid: delete “soak run”")
    expect(_events(page)).to_have_count(0)

    _tid(page, "toast-action").click()
    _expect_toast(page, "Undid: delete “soak run”")
    expect(_row_for(page, event_id)).to_contain_text("soak run")
    expect(_tid(page, "events-panel")).to_be_visible()


def test_undo_in_db_review(page: Page, db_review_dash) -> None:
    """In a ``.db`` review: add an event, delete it, undo. The same id is
    back, and it is in the archive (a restarted server re-reads it)."""
    page.goto(f"{db_review_dash.harness.url}#/host/dbhost")
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    _tid(page, "events-compose-add").click()
    _tid(page, "editor-label").fill("post-hoc note")
    _tid(page, "editor-save").click()
    expect(_tid(page, "event-editor")).to_have_count(0)
    expect(_events(page)).to_have_count(1)
    test_id = _events(page).first.get_attribute("data-testid")
    assert test_id is not None
    event_id = int(test_id.removeprefix("event-edit-"))

    _tid(page, f"event-edit-{event_id}").click()
    _tid(page, "editor-delete").click()
    _tid(page, "confirm-delete-confirm").click()
    expect(_tid(page, "event-editor")).to_have_count(0)
    expect(_events(page)).to_have_count(0)

    page.keyboard.press(_UNDO)
    _expect_toast(page, "Undid: delete “post-hoc note”")
    expect(_row_for(page, event_id)).to_contain_text("post-hoc note")

    new_harness = db_review_dash.restart()
    page.goto(f"{new_harness.url}#/host/dbhost")
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    expect(_tid(page, f"event-edit-{event_id}")).to_have_count(1)
    expect(_row_for(page, event_id)).to_contain_text(re.compile("post-hoc note"))
