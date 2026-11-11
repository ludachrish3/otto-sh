"""The event editor writes only the fields you changed (#993), in a real browser.

A review tab holds the event as it loaded it; another writer of the same
``.db`` archive (a second ``otto monitor`` review server) can change it
meanwhile. The editor sends only the fields the user touched, so a recolour
from the stale tab must not write the stale label back, and a legacy event
with a blank label (which the editor would otherwise refuse to resend) must
still be recolourable. The behaviour is documented in
``docs/cli/monitor/dashboard.md``.

Every archive row is seeded BEFORE a server boots: boot captures the document.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from otto.monitor import archive_edit
from tests._fixtures._dashboard_harness import DashboardHarness
from tests.e2e.monitor.dashboard.conftest import _boot_db_review, _make_marking_archive
from tests.e2e.monitor.dashboard.test_marking import _send_json, _tid

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

_T0 = datetime(2026, 7, 18, 12, 1, tzinfo=timezone.utc)
_GREEN = "#2ca02c"


def _session_id(path: str) -> str:
    with closing(sqlite3.connect(path)) as conn, conn:
        return str(conn.execute("SELECT id FROM sessions").fetchone()[0])


def _seed_event(path: str, label: str) -> tuple[str, int]:
    """One event row through the archive editor; returns (session id, event id)."""
    sid = _session_id(path)
    event_id = archive_edit.insert_event(
        path,
        sid,
        timestamp=_T0,
        end_timestamp=None,
        label=label,
        source="manual",
        color="#888888",
        dash="dash",
    )
    return sid, event_id


def _open_editor(page: Page, harness: DashboardHarness, event_id: int) -> None:
    page.goto(f"{harness.url}#/host/dbhost")
    page.locator('[data-testid="subject-page"]').wait_for()
    _tid(page, "events-button").click()
    _tid(page, "events-panel").wait_for()
    _tid(page, f"event-edit-{event_id}").click()
    _tid(page, "event-editor").wait_for()


def _recolour_green_and_save(page: Page) -> None:
    page.get_by_role("option", name="Green").click()
    _tid(page, "editor-save").click()
    expect(_tid(page, "event-editor")).to_have_count(0)


@pytest.fixture
def archive(tmp_path: Path) -> str:
    return _make_marking_archive(tmp_path)


@pytest.fixture
def harnesses() -> Iterator[list[DashboardHarness]]:
    """Every harness a test boots, stopped on teardown whatever the test did."""
    booted: list[DashboardHarness] = []
    yield booted
    for harness in booted:
        harness.stop()


def test_stale_tab_recolour_keeps_other_writers_label(
    page: Page, archive: str, harnesses: list[DashboardHarness]
) -> None:
    """Tab B opened the event as "original"; server A renames it; B, never
    reloaded, changes only the colour: the archive keeps A's label."""
    sid, event_id = _seed_event(archive, "original")
    server_a = _boot_db_review(archive)
    harnesses.append(server_a)
    server_b = _boot_db_review(archive)
    harnesses.append(server_b)

    _open_editor(page, server_b, event_id)
    status, body = _send_json(
        server_a.api_url(f"/api/session/{sid}/event/{event_id}"),
        {"label": "renamed by A"},
        method="PATCH",
    )
    assert status == 200, body

    _recolour_green_and_save(page)

    row = archive_edit.read_event(archive, sid, event_id)
    assert row is not None
    assert row.label == "renamed by A"
    assert row.color == _GREEN
    expect(page.locator('[data-testid^="event-row-"]', has_text="renamed by A")).to_be_visible()


def test_legacy_blank_label_event_recolours(
    page: Page, archive: str, harnesses: list[DashboardHarness]
) -> None:
    """A stored blank label (a legacy row) is recoloured without being
    resent: the row ends with the new colour and the blank label intact."""
    sid, event_id = _seed_event(archive, "placeholder")
    with closing(sqlite3.connect(archive)) as conn, conn:
        conn.execute("UPDATE events SET label = '' WHERE id = ?", (event_id,))
    server = _boot_db_review(archive)
    harnesses.append(server)

    _open_editor(page, server, event_id)
    _recolour_green_and_save(page)

    row = archive_edit.read_event(archive, sid, event_id)
    assert row is not None
    assert row.label == ""
    assert row.color == _GREEN
