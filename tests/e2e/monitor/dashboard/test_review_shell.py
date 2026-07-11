"""Behavior specs for the redesigned review shell (plan 2026-07-11).

Contract: data-testid attributes only — styling and DOM structure are
free to change. Fixtures are the committed Plan-1 dummy-data documents
(web/fixtures/), imported through the client-side Import front door, so
every test here runs with zero backend data and zero external network.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def test_empty_state_then_import(page, shell_dash):
    page.goto(shell_dash.url)
    page.locator('[data-testid="empty-review"]').wait_for()
    assert page.locator('[data-testid="status-text"]').inner_text() == "No data"
    _import_fixture(page, "kitchen-sink.json")
    assert page.locator('[data-testid="status-text"]').inner_text() == "Historical"
    assert page.locator('[data-testid="historical-tag"]').inner_text() == "HISTORICAL"
    page.locator('[data-testid="element-section-chassis-a"]').wait_for()
    page.locator('[data-testid="element-section-spare-chassis"]').wait_for()


def test_renders_fully_offline(page, shell_dash):
    """Air-gap runtime pin (successor of the deleted regression test):
    the shell + import + overview render with every non-local request and
    every WebSocket blocked."""
    blocked: list[str] = []

    def block(route):
        url = route.request.url
        if "127.0.0.1" in url or "localhost" in url:
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    page.route("**/*", block)
    page.route_web_socket("**/*", lambda ws: blocked.append(ws.url))
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').wait_for()
    assert blocked == []


def test_drift_session_picker_rerenders_lab(page, shell_dash):
    """The config-drift acceptance path (spec 2026-07-10 §1): each session
    renders under the lab config as it was at run time."""
    page.goto(shell_dash.url)
    _import_fixture(page, "drift.json")
    picker = page.locator('[data-testid="session-picker"]')
    picker.wait_for()
    assert page.locator('[data-testid="subject-link-workers_w2"]').count() == 0

    picker.click()
    # react-aria's Select keeps a visually-hidden native <select> (its
    # <option>s carry the same text) alongside the open popover's listbox
    # — get_by_text sees both; role="option" only resolves the one popover
    # item that's actually in the accessibility tree.
    page.get_by_role("option", name="expanded", exact=True).click()
    page.locator('[data-testid="subject-link-workers_w2"]').wait_for()

    picker.click()
    page.get_by_role("option", name="rewired", exact=True).click()
    page.locator('[data-testid="subject-link-edge-gw"]').wait_for()
    assert page.locator('[data-testid="subject-link-workers_w2"]').count() == 0


def test_single_session_hides_picker(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    assert page.locator('[data-testid="session-picker"]').count() == 0


def test_range_presets_change_subject_summary(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    # react-aria's Radio wraps a visually-hidden (clip-rect'd) native
    # <input> inside its <label>; the input's own tiny clipped box makes
    # Playwright's role-based click land on the label instead and time out
    # on "element intercepts pointer events". The label text is the real
    # click target users see, so target it directly, scoped to the presets
    # group in case the label text ever appears elsewhere on the page.
    page.locator('[data-testid="range-presets"]').get_by_text("Last 15m", exact=True).click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText !== prev",
        arg=full,
    )
    page.locator('[data-testid="range-reset"]').click()
    # Reset returns to the overview state: first session + full range.
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]') === null"
        " || document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_custom_range_apply_and_reset(page, shell_dash):
    """Custom from/to window (UX spec §12): Apply narrows the subject's
    range-scoped selection; Reset restores the full range."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    # datetime-local inputs hold LOCAL time (msToLocalInput), so derive the
    # narrow window from the pre-populated session-start value instead of
    # hardcoding strings off the fixture's UTC timestamps — on a non-UTC
    # host a UTC-derived window would land outside the session entirely.
    start_local = page.locator('[data-testid="range-from"]').input_value()
    # DTZ007 suppressed: deliberately naive — a datetime-local value is
    # wall-clock text with no timezone; it round-trips into the same input.
    t0 = datetime.strptime(start_local, "%Y-%m-%dT%H:%M")  # noqa: DTZ007
    page.locator('[data-testid="range-from"]').fill(
        (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-to"]').fill(
        (t0 + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-apply"]').click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText !== prev",
        arg=full,
    )
    # The narrowed window (+10..+20min) must show a strictly smaller,
    # nonzero sample count than the full range — not just "different text",
    # which a "0 samples in range" summary would also satisfy. Match the
    # "samples in range" count specifically (not the leading "N series"
    # count earlier in the same string).
    narrowed = page.locator('[data-testid="series-summary"]').inner_text()
    full_count = int(re.search(r"(\d+) samples in range", full).group(1))
    narrowed_count = int(re.search(r"(\d+) samples in range", narrowed).group(1))
    assert 0 < narrowed_count < full_count

    page.locator('[data-testid="range-reset"]').click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_deep_link_and_reload(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert page.url.endswith("#/host/db-01")
    # Imported data is in-memory only: reload keeps the hash but shows the
    # empty state (honest current behavior — persistence is a later call).
    page.reload()
    page.locator('[data-testid="empty-review"]').wait_for()
    assert page.url.endswith("#/host/db-01")


def test_deep_link_back_forward(page, shell_dash):
    """Browser back/forward walk the hash history (UX spec: back/forward
    must work) — wouter's useHashLocation reacts to popstate/hashchange,
    and the in-memory import survives since these are same-document navs."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert "db-01" in page.locator('[data-testid="subject-title"]').inner_text()

    page.go_back()
    page.locator('[data-testid="overview-page"]').wait_for()

    page.go_forward()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert "db-01" in page.locator('[data-testid="subject-title"]').inner_text()


def test_not_found_routes(page, shell_dash):
    """Both not-found render sites (Task 5 ledger: there are two) keep the
    shell chrome. Same-document hash navigations, so the import survives."""
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")

    # Site 1: no route matches at all -> the router-level fallback
    # (App.tsx's Switch catch-all Route). The review bar staying visible
    # proves this render site keeps the chrome too.
    page.goto(shell_dash.url + "/#/bogus")
    page.locator('[data-testid="not-found"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()

    # Site 2: /host/:id matches but the id is unknown in this session ->
    # SubjectPage's own unknown-subject branch. The review bar staying
    # visible proves this render site keeps the chrome too.
    page.goto(shell_dash.url + "/#/host/ghost")
    page.locator('[data-testid="not-found"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()


def test_import_error_banner_in_loaded_state(page, shell_dash, tmp_path):
    """A failed re-import after data is already loaded (⋯ menu or drag-drop,
    UX spec §12) must surface visibly, not just set store state invisibly —
    EmptyState's import-error surface has a loaded-shell counterpart too."""
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    page.locator('[data-testid="overview-page"]').wait_for()

    bogus = tmp_path / "bogus.json"
    bogus.write_text("{}", encoding="utf-8")
    page.locator('[data-testid="import-input"]').set_input_files(bogus)

    page.locator('[data-testid="import-error"]').wait_for()
    assert page.locator('[data-testid="import-error"]').is_visible()
    # Prior data survives a failed re-import.
    assert page.locator('[data-testid="overview-page"]').is_visible()

    page.locator('[data-testid="import-error-dismiss"]').click()
    assert page.locator('[data-testid="import-error"]').count() == 0


def test_theme_toggle_persists_across_reload(page, shell_dash):
    page.goto(shell_dash.url)
    page.locator('[data-testid="overflow-menu"]').click()
    before = page.evaluate("document.documentElement.classList.contains('dark')")
    page.locator('[data-testid="menu-theme"]').click()
    assert page.evaluate("document.documentElement.classList.contains('dark')") is not before
    page.reload()
    assert page.evaluate("document.documentElement.classList.contains('dark')") is not before


def test_export_downloads_loaded_set(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    page.locator('[data-testid="overflow-menu"]').click()
    with page.expect_download() as download_info:
        page.locator('[data-testid="menu-export"]').click()
    path = download_info.value.path()
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["format"] == 1
    assert len(doc["sessions"]) == 1
