"""Editable ``.json`` sources in a real browser (#958).

A ``.json`` review source is editable: its edits live in the server's
memory, every tab on the URL sees them through the push stream, and an
unsaved marker counts them until an export saves them. Export asks for a
name; Import on a ``.json`` URL switches the URL's source, for every tab,
after a prompt. A live or ``.db`` URL never offers Import. The behaviour is
documented in ``docs/cli/monitor/dashboard.md``.

The ``.json`` tests serve ``web/fixtures/repeated-ids.json`` through
``review_dash`` (conftest.py's ``EDITABLE_BOOT``): event ids ``1, 3, 3,
(none), 9``, event 1 with the legacy colour ``"red"``. The repeated and
id-less rows are view-only and must come out of an export unchanged.

Contract: ``data-testid`` attributes only. Every wait is an expectation
poll, never a flat sleep.
"""

import json
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Locator, Page, Request, expect

from otto.models import EventRecord, MonitorExport
from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.e2e.monitor.dashboard._date_fields import (
    DATE_TIME_INPUT,
    read_date_field,
    set_date_field,
)
from tests.e2e.monitor.dashboard._file_import import (
    drop_file,
    settle_blob_reads,
    settle_frames,
    track_blob_reads,
)
from tests.e2e.monitor.dashboard.conftest import EDITABLE_BOOT, EDITABLE_HOST, DbReviewHandle
from tests.e2e.monitor.dashboard.test_live_shell import _boot_live_with_r1

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

_FIXTURES = CHECKOUT_ROOT / "web" / "fixtures"
_KITCHEN_SINK = _FIXTURES / "kitchen-sink.json"
_MINIMAL = _FIXTURES / "minimal.json"

_SOURCE = "repeated-ids.json"
_EDITED = "repeated-ids-edited.json"
_SESSION = "2026-07-01T08-00-00-repeated"
_GREEN = "#2ca02c"
_EXPORT = "ControlOrMeta+KeyS"
_PALETTE = "ControlOrMeta+KeyK"
_SAVED_PATH = "/api/source/saved"


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _row(page: Page, label: str) -> Locator:
    return page.locator('[data-testid^="event-row-"]', has_text=label)


def _open_events(page: Page, dash: DashboardHarness, host: str = EDITABLE_HOST) -> None:
    """Load *dash* on *host*'s subject page and open its events panel."""
    page.goto(f"{dash.url}#/host/{host}")
    _tid(page, "subject-page").wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()


def _close_events(page: Page) -> None:
    """Close the events panel by its close button (focus may sit outside it)."""
    panel = _tid(page, "events-panel")
    panel.get_by_role("button", name="Close").click()
    expect(panel).to_have_count(0)


def _add_event(page: Page, label: str) -> int:
    """Add a point event from the open events panel; returns the server's id."""
    _tid(page, "events-compose-add").click()
    editor = _tid(page, "event-editor")
    editor.wait_for()
    _tid(page, "editor-label").fill(label)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith("/event")
    ) as created:
        _tid(page, "editor-save").click()
    editor.wait_for(state="detached")
    response = created.value
    assert response.status == 201, response.text()
    return int(response.json()["id"])


def _open_editor(page: Page, event_id: int) -> None:
    _tid(page, f"event-edit-{event_id}").click()
    _tid(page, "event-editor").wait_for()


def _save(page: Page) -> None:
    _tid(page, "editor-save").click()
    expect(_tid(page, "event-editor")).to_have_count(0)


def _editor_field(page: Page, test_id: str) -> Locator:
    """The event editor's Start (``editor-start``) or End (``editor-end``) date field."""
    return _tid(page, test_id).locator(DATE_TIME_INPUT)


def _expect_marker(page: Page, unsaved: int) -> None:
    marker = _tid(page, "unsaved-marker")
    if unsaved == 0:
        expect(marker).to_have_count(0)
    else:
        noun = "change" if unsaved == 1 else "changes"
        expect(marker).to_have_text(f"{unsaved} {noun} since export")


def _expect_undo(page: Page, *, enabled: bool) -> None:
    """The palette's Undo row: enabled with a step to replay, else the bare
    verb, disabled."""
    page.keyboard.press(_PALETTE)
    expect(_tid(page, "command-menu")).to_be_visible()
    row = _tid(page, "command-item-edit-undo")
    if enabled:
        expect(row).not_to_have_attribute("aria-disabled", "true")
    else:
        expect(row).to_have_attribute("aria-disabled", "true")
        expect(row).to_have_accessible_name("Undo")
    page.keyboard.press("Escape")
    expect(_tid(page, "command-menu")).to_have_count(0)


def _is_acknowledgement(request: Request) -> bool:
    return request.url.split("?")[0].endswith(_SAVED_PATH)


def _export(page: Page, expected_name: str, tmp_path: Path, *, ack_fails: bool = False) -> Path:
    """Ctrl+S, keep the offered name, Enter; returns the saved download.

    With *ack_fails*, the export's acknowledgement to the server must also be
    tried and fail (the server is gone): the confirm waits for that request's
    ``requestfailed``, so the caller's checks run after it.
    """
    page.keyboard.press(_EXPORT)
    name = _tid(page, "export-name")
    expect(name).to_be_focused()
    expect(name).to_have_value(expected_name)
    failed = page.expect_event("requestfailed", _is_acknowledgement) if ack_fails else nullcontext()
    with failed, page.expect_download() as downloaded:
        page.keyboard.press("Enter")
    download = downloaded.value
    assert download.suggested_filename == expected_name
    saved = tmp_path / download.suggested_filename
    download.save_as(saved)
    expect(_tid(page, "export-dialog")).to_have_count(0)
    return saved


def _served_sessions(dash: DashboardHarness) -> list[dict]:
    """The sessions of the document *dash* serves now, as its body spells them."""
    source = dash.server.source_slot.current
    assert source is not None
    return json.loads(source.body())["sessions"]


def _served_events(dash: DashboardHarness, session_id: str = _SESSION) -> list[dict]:
    """The events *session_id* holds in the document *dash* serves now."""
    return next(s for s in _served_sessions(dash) if s["id"] == session_id)["events"]


def _bounds(sessions: list[dict]) -> list[tuple[str, str, str | None]]:
    """Each session's id, ``start`` and ``end`` exactly as spelled (``None``: no ``end``)."""
    return [(s["id"], s["start"], s.get("end")) for s in sessions]


def _events_of(doc: MonitorExport, session_id: str) -> list[EventRecord]:
    return next(s for s in doc.sessions if s.id == session_id).events


@pytest.fixture
def second_tab(context: BrowserContext) -> Iterator[Page]:
    """Tab B: another tab in the same browser, on the same URL as ``page``."""
    tab = context.new_page()
    yield tab
    tab.close()


@pytest.mark.parametrize("review_dash", [EDITABLE_BOOT], indirect=True)
class TestJsonSource:
    """Two tabs on one editable ``.json`` source."""

    def test_json_edit_shows_in_every_tab_with_the_marker(
        self, page: Page, second_tab: Page, review_dash: DashboardHarness[MetricCollector]
    ) -> None:
        """An Add event in tab A shows in tab B, and both count it as unsaved
        without a reload; a reload keeps the event and the marker."""
        _open_events(page, review_dash)
        _open_events(second_tab, review_dash)
        _expect_marker(page, 0)

        _add_event(page, "from tab A")

        for tab in (page, second_tab):
            expect(_row(tab, "from tab A")).to_be_visible()
            _expect_marker(tab, 1)

        for tab in (page, second_tab):
            tab.reload()
            _tid(tab, "subject-page").wait_for()
            _expect_marker(tab, 1)
            expect(_tid(tab, "events-count")).to_have_text("6")

    def test_export_round_trips_every_edit(
        self,
        page: Page,
        second_tab: Page,
        review_dash: DashboardHarness[MetricCollector],
        tmp_path: Path,
    ) -> None:
        """Create, relabel, recolour, re-dash, retime, end a span and delete;
        the export carries every edit under the server's ids, leaves the
        view-only rows and the legacy colour alone, and clears the marker in
        every tab."""
        _open_events(page, review_dash)
        _open_events(second_tab, review_dash)

        added = _add_event(page, "added")
        assert added > 9, "a new id must sit above every id the file holds"
        doomed = _add_event(page, "doomed")

        _open_editor(page, 9)
        _tid(page, "editor-label").fill("ninth renamed")
        page.get_by_role("option", name="Green").click()
        _tid(page, "editor-dash").click()
        page.get_by_role("option", name="dot", exact=True).click()
        start = _editor_field(page, "editor-start")
        set_date_field(start, read_date_field(start) + timedelta(minutes=1))
        _save(page)

        _open_editor(page, 9)
        end_at = read_date_field(_editor_field(page, "editor-start")) + timedelta(minutes=1)
        set_date_field(_editor_field(page, "editor-end"), end_at)
        _save(page)

        _open_editor(page, doomed)
        _tid(page, "editor-delete").click()
        _tid(page, "confirm-delete-confirm").click()
        expect(_tid(page, "event-editor")).to_have_count(0)
        expect(_row(page, "doomed")).to_have_count(0)

        for tab in (page, second_tab):
            _expect_marker(tab, 5)
        _close_events(page)

        served = _served_events(review_dash)
        saved = _export(page, _EDITED, tmp_path)
        doc = MonitorExport.model_validate_json(saved.read_text(encoding="utf-8"))
        # Each session's start and end come back exactly as served, and an
        # end the server sent none for stays absent.
        exported_sessions = json.loads(saved.read_text(encoding="utf-8"))["sessions"]
        assert _bounds(exported_sessions) == _bounds(_served_sessions(review_dash))

        events = _events_of(doc, _SESSION)
        by_label = {e.label: e for e in events}
        assert sorted(by_label) == sorted(
            ["first", "third", "third again", "no id", "ninth renamed", "added"]
        )
        ninth = by_label["ninth renamed"]
        assert ninth.id == 9
        assert ninth.color == _GREEN
        assert ninth.dash == "dot"
        assert ninth.timestamp == datetime(2026, 7, 1, 8, 6, tzinfo=timezone.utc)
        assert ninth.end_timestamp == datetime(2026, 7, 1, 8, 7, tzinfo=timezone.utc)
        assert by_label["added"].id == added
        assert by_label["first"].id == 1
        assert by_label["first"].color == "red"
        assert [e.id for e in events if e.label.startswith("third")] == [3, 3]
        assert by_label["no id"].id is None

        assert [e.id for e in events] == [e.get("id") for e in served]
        # The view-only rows (both id-3 rows and the id-less one) come out
        # exactly as the server holds them.
        view_only = [i for i, e in enumerate(events) if e.id in (3, None)]
        assert len(view_only) == 3
        for i in view_only:
            assert events[i].model_dump(mode="json") == EventRecord.model_validate(
                served[i]
            ).model_dump(mode="json")
        untouched = _events_of(doc, f"{_SESSION}-2")
        assert [(e.id, e.label) for e in untouched] == [
            (1, "first"),
            (3, "third"),
            (3, "third again"),
            (None, "no id"),
            (9, "ninth"),
        ]

        for tab in (page, second_tab):
            _expect_marker(tab, 0)

    def test_import_with_unsaved_prompts_then_switches_every_tab(
        self, page: Page, second_tab: Page, review_dash: DashboardHarness[MetricCollector]
    ) -> None:
        """Import over two unsaved changes, one from each tab, names them in
        the prompt; confirming moves every tab to the new file, tab B is told,
        and neither tab keeps its undo step from the old source."""
        _open_events(page, review_dash)
        _open_events(second_tab, review_dash)
        _add_event(page, "unsaved in A")
        _add_event(second_tab, "unsaved in B")
        for tab in (page, second_tab):
            _expect_marker(tab, 2)
            _close_events(tab)
            _expect_undo(tab, enabled=True)

        _tid(page, "import-input").set_input_files(_KITCHEN_SINK)
        prompt = _tid(page, "confirm-switch")
        expect(prompt).to_contain_text(
            "Opening kitchen-sink.json discards 2 unsaved changes to repeated-ids.json."
            " Export first to keep them."
        )
        _tid(page, "confirm-switch-confirm").click()
        expect(prompt).to_have_count(0)

        expect(_tid(second_tab, "toast")).to_contain_text("Source changed to kitchen-sink.json")
        for tab in (page, second_tab):
            expect(_tid(tab, "source-name")).to_have_text("kitchen-sink.json")
            _expect_marker(tab, 0)
        for tab in (page, second_tab):
            _expect_undo(tab, enabled=False)

    def test_reimport_of_the_export_is_a_new_source(
        self,
        page: Page,
        second_tab: Page,
        review_dash: DashboardHarness[MetricCollector],
        tmp_path: Path,
    ) -> None:
        """Re-importing the edited copy just exported (same session and event
        ids) is a new source: every tab moves to its token, with no undo step,
        no open editor and no events from the old source; a write from either
        tab then lands on the new source."""
        _open_events(page, review_dash)
        _open_events(second_tab, review_dash)
        _add_event(page, "kept")
        _add_event(second_tab, "kept too")
        for tab in (page, second_tab):
            _expect_marker(tab, 2)
            _close_events(tab)
        saved = _export(page, _EDITED, tmp_path)
        for tab in (page, second_tab):
            _expect_marker(tab, 0)
            _expect_undo(tab, enabled=True)
        _tid(second_tab, "events-button").click()
        _open_editor(second_tab, 9)
        old_token = review_dash.source_token()

        assert drop_file(page, saved) == 1
        prompt = _tid(page, "confirm-switch")
        expect(prompt).to_contain_text(f"Opening {_EDITED} closes {_SOURCE}.")
        _tid(page, "confirm-switch-confirm").click()
        expect(prompt).to_have_count(0)

        expect(_tid(second_tab, "toast")).to_contain_text(f"Source changed to {_EDITED}")
        expect(_tid(second_tab, "event-editor")).to_have_count(0)
        assert review_dash.source_token() != old_token
        for tab in (page, second_tab):
            expect(_tid(tab, "source-name")).to_have_text(_EDITED)
            expect(_tid(tab, "events-count")).to_have_text("7")
            _expect_marker(tab, 0)
        expect(_tid(second_tab, "events-panel")).to_be_visible()
        _close_events(second_tab)
        for tab in (page, second_tab):
            _expect_undo(tab, enabled=False)

        _tid(second_tab, "events-button").click()
        after = _add_event(second_tab, "after the switch")
        for tab in (page, second_tab):
            expect(_tid(tab, "events-count")).to_have_text("8")
            _expect_marker(tab, 1)
        labels = [e["label"] for e in _served_events(review_dash)]
        assert labels.count("kept") == 1
        assert labels.count("kept too") == 1
        assert "after the switch" in labels
        assert after > 9

    def test_export_after_the_server_stops(
        self, page: Page, review_dash: DashboardHarness[MetricCollector], tmp_path: Path
    ) -> None:
        """With ``otto monitor`` gone, Export still downloads under the
        confirmed name; the acknowledgement is tried and fails silently (no
        toast), so the marker stays, beside the reconnecting banner."""
        _open_events(page, review_dash)
        _add_event(page, "before the stop")
        _close_events(page)
        _expect_marker(page, 1)

        review_dash.stop()
        expect(_tid(page, "reconnecting-banner")).to_be_visible()

        saved = _export(page, _EDITED, tmp_path, ack_fails=True)
        settle_frames(page)
        expect(_tid(page, "toast")).to_have_count(0)
        doc = MonitorExport.model_validate_json(saved.read_text(encoding="utf-8"))
        assert "before the stop" in [e.label for e in _events_of(doc, _SESSION)]
        _expect_marker(page, 1)
        expect(_tid(page, "reconnecting-banner")).to_be_visible()


def test_db_edit_shows_in_a_second_tab(
    page: Page, second_tab: Page, db_review_dash: DbReviewHandle
) -> None:
    """On one ``.db`` server, an event tab A adds shows in tab B without a reload."""
    harness = db_review_dash.harness
    _open_events(page, harness, "dbhost")
    _open_events(second_tab, harness, "dbhost")
    expect(_tid(second_tab, "events-count")).to_have_text("0")

    _add_event(page, "db note")

    expect(_row(second_tab, "db note")).to_be_visible()
    expect(_tid(second_tab, "events-count")).to_have_text("1")


def _expect_ignored_drop(page: Page) -> None:
    """No Import item, and a file dropped anyway is ignored: no prompt, and
    the tab keeps the source it shows.

    The drop must carry its file to the window, and the tab must not start
    reading it: Import is refused before the file is read.
    """
    _tid(page, "overflow-menu").click()
    expect(_tid(page, "menu-theme")).to_be_visible()
    expect(_tid(page, "menu-import")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(_tid(page, "menu-theme")).to_have_count(0)

    track_blob_reads(page)
    assert drop_file(page, _MINIMAL) == 1
    assert settle_blob_reads(page) == 0
    expect(_tid(page, "confirm-switch")).to_have_count(0)


def test_import_not_offered_on_live_or_db(
    page: Page,
    live_stream_dash: DashboardHarness[FakeCollector],
    db_review_dash: DbReviewHandle,
) -> None:
    """A live URL and a ``.db`` URL offer no Import, and ignore a dropped file."""
    _boot_live_with_r1(page, live_stream_dash)
    _expect_ignored_drop(page)
    expect(_tid(page, "mark-button")).to_be_visible()
    expect(_tid(page, "review-bar")).to_have_count(0)

    harness = db_review_dash.harness
    page.goto(f"{harness.url}#/host/dbhost")
    _tid(page, "subject-page").wait_for()
    _expect_ignored_drop(page)
    expect(_tid(page, "source-name")).to_have_text(db_review_dash.archive_path)
    expect(_tid(page, "subject-page")).to_be_visible()
