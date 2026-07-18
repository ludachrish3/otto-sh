"""Behavior specs for the redesigned review shell (plan 2026-07-11).

Contract: data-testid attributes only — styling and DOM structure are
free to change. Fixtures are the committed Plan-1 dummy-data documents
(web/fixtures/), imported through the client-side Import front door, so
every test here runs with zero backend data and zero external network —
except the two Task-7 boot specs at the bottom, which exist specifically to
prove the *other* path: a review-mode server hydrating the shell itself via
``/api/mode``/``/api/monitor_sessions`` (Plan 5a Task 6; endpoint renamed in
Plan 5b Task 3), with no Import interaction.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


# --- RangePicker (Task 7) driving helpers -----------------------------
#
# The old preset ButtonGroup + two datetime-local <input>s + Apply + Reset
# (`range-presets` / `range-from` / `range-to` / `range-apply` /
# `range-reset`) are retired. The replacement is ONE popover card
# (`data-testid="range-picker"`): a trigger button showing the current
# range, a preset rail (Full / Last 15m / Last 1h), and two minute-
# granularity date fields with Cancel/Apply. "Full" subsumes the old Reset.


def _open_range_picker(page) -> None:
    """Open the range picker's popover via its trigger button.

    `data-testid="range-picker"` lands on the wrapper `AriaDateRangePicker`
    itself renders (react-aria-components puts caller `data-*` props on its
    own root, not on the inner trigger `<button>`) — same "wrapper, not
    leaf" situation as the session picker documents elsewhere in this file.
    Playwright hit-tests by pixel, so scoping to the wrapper and clicking
    the button inside it is unaffected either way.
    """
    page.locator('[data-testid="range-picker"]').get_by_role("button").first.click()


def _click_range_preset(page, label: str) -> None:
    page.get_by_role("button", name=label, exact=True).click()


def _apply_range_picker(page) -> None:
    page.get_by_role("button", name="Apply", exact=True).click()


def _range_picker_label(page) -> str:
    """The trigger button's own displayed text — "Full range" or the
    formatted from/to — which is derived straight from the committed
    `range` value, not from whatever is mid-edit inside an open popover."""
    return page.locator('[data-testid="range-picker"]').inner_text()


def _read_range_field(page, field_index: int) -> datetime:
    """Read one of the open popover's two date fields (0=from, 1=to) back
    into a naive local-wall-clock `datetime` — the segment-typing analogue
    of the old idiom of reading `range-from`'s pre-populated value instead
    of hardcoding a window against the fixture's UTC timestamps (a
    UTC-derived window would land outside the session on a non-UTC host).
    """

    def seg(kind: str) -> str:
        return page.locator(f'[data-type="{kind}"]').nth(field_index).inner_text()

    month, day, year = int(seg("month")), int(seg("day")), int(seg("year"))
    hour12, minute = int(seg("hour")), int(seg("minute"))
    is_pm = seg("dayPeriod").strip().upper() == "PM"
    hour = (hour12 % 12) + (12 if is_pm else 0)
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 — naive local wall-clock by design


def _set_range_field(page, field_index: int, dt: datetime) -> None:
    """Type an exact local wall-clock instant into one of the popover's two
    date fields (0=from, 1=to).

    These are react-aria date SEGMENTS (`<span role="spinbutton">`, one per
    `data-type` — month/day/year/hour/minute/dayPeriod), not `<input>`
    elements, so `.fill()` doesn't apply. react-aria segments DO auto-advance
    focus when typed into continuously (like a native `<input type="date">`'s
    constituent parts), but that auto-advance is driven by a React state
    update between keystrokes — `page.keyboard.type()`'s keypresses land
    faster than that round-trip, so later digits land on whichever segment
    was focused when they were sent, not on the segment the earlier digits'
    auto-advance was heading for. Targeting each segment by its own
    `data-type` selector sidesteps the timing question entirely: every
    keystroke goes exactly where intended regardless of auto-advance.
    """
    hour12 = dt.hour % 12 or 12
    for kind, value, width in (
        ("month", dt.month, 2),
        ("day", dt.day, 2),
        ("year", dt.year, 4),
        ("hour", hour12, 2),
        ("minute", dt.minute, 2),
    ):
        page.locator(f'[data-type="{kind}"]').nth(field_index).click()
        page.keyboard.type(str(value).zfill(width))
    period_seg = page.locator('[data-type="dayPeriod"]').nth(field_index)
    want = "PM" if dt.hour >= 12 else "AM"
    if period_seg.inner_text().strip().upper() != want:
        period_seg.click()
        page.keyboard.press("ArrowUp")


def _point_on_edge(page, edge_id: str) -> dict:
    """Find a point on a topology edge's actual rendered stroke, not its
    naive bounding-box center.

    React Flow's parallel/diagonal edges can lay node cards — or just empty
    pane — directly over a curved edge's bbox-center point (verified
    empirically against kitchen-sink's layout: the impaired ``metrics-udp``
    edge's bbox center sits under the unrelated ``mgmt-01`` node card, and
    the declared ``app-db`` edge's bbox center sits over bare pane) — a plain
    ``locator.click()``/``locator.hover()``, even with ``force=True``, lands
    on whatever element the browser's real hit-test resolves to at that
    pixel (``force`` only skips Playwright's own actionability checks, not
    the browser's actual hit-testing). For a click this was observed to
    navigate to ``#/host/mgmt-01`` instead of opening the inspector; for a
    hover it silently fires no ``mouseenter`` at all, since the pane isn't
    the edge. React Flow renders a wide invisible
    ``react-flow__edge-interaction`` path specifically to be the edge's
    pointer target; this samples points along its actual curve and returns
    the first one that the browser's own ``elementFromPoint`` still resolves
    back to this edge.
    """
    path = page.locator(f'[data-testid="topo-link-{edge_id}"] path.react-flow__edge-interaction')
    point = path.evaluate(
        """(el) => {
            const total = el.getTotalLength();
            for (let i = 1; i < 20; i++) {
                const pt = el.getPointAtLength(total * (i / 20));
                const ctm = el.getScreenCTM();
                const x = ctm.a * pt.x + ctm.c * pt.y + ctm.e;
                const y = ctm.b * pt.x + ctm.d * pt.y + ctm.f;
                const top = document.elementFromPoint(x, y);
                if (top === el || el.parentElement.contains(top)) return { x, y };
            }
            return null;
        }"""
    )
    assert point is not None, f"no pointer-target point found along edge {edge_id}'s stroke"
    return point


def _click_edge(page, edge_id: str) -> None:
    """Click a topology edge's actual rendered stroke (see ``_point_on_edge``)."""
    point = _point_on_edge(page, edge_id)
    page.mouse.click(point["x"], point["y"])


def _wait_for_links(page, at_least: int) -> None:
    """Wait until the topology canvas has actually rendered its edges.

    React Flow renders an edge only once BOTH of its endpoint nodes have been
    measured — ``getEdgePosition`` returns null while either is uninitialized,
    and the edge wrapper then renders nothing — so ``topology-page`` (and even
    the node cards) sit in the DOM a beat before any ``topo-link-*`` does.
    ``locator.count()`` does not retry, so counting edges right after the shell
    mounts samples that window: it read 0 on ~2 of 10 WebKit runs locally,
    which is exactly how it failed CI on main (issue #130 — ``assert 0 >= 6``).

    Every edge assertion has to let the canvas settle first — *including* ones
    that assert an edge is ABSENT, which would otherwise pass vacuously inside
    the window, for the wrong reason.
    """
    page.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"topo-link-\"]').length >= n",
        arg=at_least,
    )


def _assert_reachable(page, testid: str) -> None:
    """Assert a real pointer at this element's centre actually lands ON it.

    Deliberately NOT expressed as ``locator.click()``. Playwright's hit-target
    check is engine-dependent: when the link inspector covered the topology's
    rightmost node (issue #134), firefox and webkit refused the click with
    "subtree intercepts pointer events" while CHROMIUM CLICKED IT ANYWAY and the
    lane went green — even though ``elementFromPoint`` proved the panel was on
    top and a raw ``mouse.click()`` at that point failed to navigate in chromium
    too. A click is therefore not a trustworthy occlusion check on its own.

    ``elementFromPoint`` is: it asks the browser what the USER would hit, and it
    answers identically in all three engines.

    Polls rather than sampling once. Opening the inspector narrows the canvas,
    and React Flow re-fits the graph only after its ResizeObserver reports the
    new box — a frame or two later. Sampling the instant the panel appears reads
    the pre-fit layout, which is a race, not a finding (it caught webkit and not
    chromium purely on scheduling luck). The claim under test is that the map
    SETTLES with nothing of it hidden, so the assertion has to let it settle.
    """
    hit_expr = f"""() => {{
      const el = document.querySelector('[data-testid="{testid}"]');
      if (el === null) return null;
      const b = el.getBoundingClientRect();
      const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
      return hit?.closest('[data-testid]')?.getAttribute('data-testid') ?? null;
    }}"""
    try:
        page.wait_for_function(f"({hit_expr})() === {testid!r}", timeout=5000)
    except PlaywrightTimeoutError:
        hit = page.evaluate(hit_expr)
        msg = f"{testid} is occluded: a click at its centre lands on {hit!r}"
        raise AssertionError(msg) from None


def _set_theme(page, *, dark: bool) -> None:
    """Drive the overflow menu's two-state toggle to a known theme."""
    if page.evaluate("document.documentElement.classList.contains('dark-mode')") is dark:
        return
    page.locator('[data-testid="overflow-menu"]').click()
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "(want) => document.documentElement.classList.contains('dark-mode') === want", arg=dark
    )


def _mean_channel(color: str) -> float:
    """Mean RGB channel of a computed ``rgb(...)``/``rgba(...)`` color."""
    channels = [float(v) for v in re.findall(r"[\d.]+", color)[:3]]
    assert len(channels) == 3, f"unparseable color {color!r}"
    return sum(channels) / 3


def test_empty_state_then_import(page, shell_dash):
    """Before/after of an import: empty-review -> review-bar (the "No
    data"/"Historical" status-text pair was deleted with the status cluster
    -- AppBar.tsx's header comment -- the empty-state -> review-bar
    transition already proves the same before/after without it, and both
    are already asserted below: the ``wait_for`` on line 2 for "before",
    ``_import_fixture``'s own ``review-bar`` wait for "after"). Route swap
    (spec 2026-07-17 topology-default-view): "/" is now the topology
    landing, so the grid-only ``element-section-*`` proof needs an explicit
    ``#/hosts`` hop -- it no longer follows for free from a bare import.
    """
    page.goto(shell_dash.url)
    page.locator('[data-testid="empty-review"]').wait_for()
    _import_fixture(page, "kitchen-sink.json")
    assert page.locator('[data-testid="historical-tag"]').inner_text() == "HISTORICAL"
    page.goto(f"{shell_dash.url}#/hosts")
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
    # "/" is the topology landing now (route swap); the grid lives at
    # #/hosts -- a same-document hash hop, so this stays a zero-network proof.
    page.goto(f"{shell_dash.url}#/hosts")
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').wait_for()
    assert blocked == []


def test_drift_session_picker_rerenders_lab(page, shell_dash):
    """The config-drift acceptance path (spec 2026-07-10 §1): each session
    renders under the lab config as it was at run time."""
    page.goto(shell_dash.url)
    _import_fixture(page, "drift.json")
    # session-picker/RangePicker are shell-level (ReviewBar), but
    # subject-link-* is grid-only -- route to #/hosts so the below is a real
    # claim about presence/absence, not a page that can never show it either way.
    page.goto(f"{shell_dash.url}#/hosts")
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
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    _open_range_picker(page)
    _click_range_preset(page, "Last 15m")
    _apply_range_picker(page)
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText !== prev",
        arg=full,
    )

    # "Full" subsumes the old Reset — returns to the overview state: first
    # session + full range.
    _open_range_picker(page)
    _click_range_preset(page, "Full")
    _apply_range_picker(page)
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]') === null"
        " || document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_custom_range_apply_and_reset(page, shell_dash):
    """Custom from/to window (UX spec §12): Apply narrows the subject's
    range-scoped selection; the Full preset restores the full range."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    # The popover opens pre-seeded with the session's own full bounds (see
    # RangePicker.tsx), so read the "from" field back rather than hardcoding
    # a window off the fixture's UTC timestamps — on a non-UTC host a
    # UTC-derived window would land outside the session entirely.
    _open_range_picker(page)
    t0 = _read_range_field(page, 0)
    _set_range_field(page, 0, t0 + timedelta(minutes=10))
    _set_range_field(page, 1, t0 + timedelta(minutes=20))
    _apply_range_picker(page)
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

    _open_range_picker(page)
    _click_range_preset(page, "Full")
    _apply_range_picker(page)
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_deep_link_and_reload(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
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
    # "/" is the topology landing now; hop to #/hosts first so it's a real
    # history entry for go_back() to return to (route swap fallout).
    page.goto(f"{shell_dash.url}#/hosts")
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
    shell chrome. Same-document hash navigations, so the import survives.

    ``shell_dash.url`` now carries ``?key=…`` (access-key gate, spec
    2026-07-16); appending a *leading*-slash fragment (``"/#/bogus"``, the
    original form here) lands that slash inside the query value itself
    (``?key=XXXX/``), which differs from the just-navigated URL and so
    defeats the browser's same-document hash optimization — the page does a
    full reload instead, wiping the just-imported client-side state, and
    neither locator below ever appears. The fragment-only form used
    everywhere else in this file (``f"{shell_dash.url}#/topology"``) is the
    correct idiom: no characters between the query and ``#``.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")

    # Site 1: no route matches at all -> the router-level fallback
    # (App.tsx's Switch catch-all Route). The review bar staying visible
    # proves this render site keeps the chrome too.
    page.goto(f"{shell_dash.url}#/bogus")
    page.locator('[data-testid="not-found"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()

    # Site 2: /host/:id matches but the id is unknown in this session ->
    # SubjectPage's own unknown-subject branch. The review bar staying
    # visible proves this render site keeps the chrome too.
    page.goto(f"{shell_dash.url}#/host/ghost")
    page.locator('[data-testid="not-found"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()


def test_import_error_banner_in_loaded_state(page, shell_dash, tmp_path):
    """A failed re-import after data is already loaded (⋯ menu or drag-drop,
    UX spec §12) must surface visibly, not just set store state invisibly —
    EmptyState's import-error surface has a loaded-shell counterpart too."""
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    page.goto(f"{shell_dash.url}#/hosts")  # overview-page is grid-only, not the topology landing
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


def test_dropped_row_surfaces_data_warnings_banner(page, shell_dash, tmp_path):
    """Plan 5b final-review Finding [1]: `state.warnings` (rows dropped for an
    invalid timestamp — data/exportDoc.ts's dropInvalidTimestamps) had no
    render site at all — "drop and warn" was, in practice, "drop silently."
    Imports a one-off document (same tmp_path + set_input_files pattern as
    test_import_error_banner_in_loaded_state above) carrying one good and one
    malformed-timestamp metric row and asserts the user actually SEES the
    resulting warning, can dismiss it, and a later fresh warning re-shows it.
    """
    page.goto(shell_dash.url)

    with_bad_row = tmp_path / "warn-fixture.json"
    with_bad_row.write_text(
        json.dumps(
            {
                "format": 1,
                "sessions": [
                    {
                        "id": "warn-fixture",
                        "start": "2026-07-01T08:00:00Z",
                        "lab": {"hosts": [{"id": "solo", "element": "solo"}]},
                        "metrics": [
                            {
                                "timestamp": "2026-07-01T08:00:00Z",
                                "host": "solo",
                                "label": "CPU %",
                                "value": 1,
                            },
                            {
                                "timestamp": "not-a-timestamp",
                                "host": "solo",
                                "label": "CPU %",
                                "value": 2,
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    page.locator('[data-testid="import-input"]').set_input_files(with_bad_row)
    page.locator('[data-testid="review-bar"]').wait_for()

    banner = page.locator('[data-testid="data-warnings-banner"]')
    banner.wait_for()
    assert "dropped 1 metric with invalid timestamp" in banner.inner_text()

    page.locator('[data-testid="data-warnings-dismiss"]').click()
    assert page.locator('[data-testid="data-warnings-banner"]').count() == 0

    # A fresh warning (a second, unrelated dropped row) re-shows the banner —
    # dismissal must not permanently silence the channel.
    second_bad_row = tmp_path / "warn-fixture-2.json"
    second_bad_row.write_text(
        json.dumps(
            {
                "format": 1,
                "sessions": [
                    {
                        "id": "warn-fixture-2",
                        "start": "2026-07-01T09:00:00Z",
                        "lab": {"hosts": [{"id": "solo", "element": "solo"}]},
                        "metrics": [
                            {
                                "timestamp": "still-not-a-timestamp",
                                "host": "solo",
                                "label": "CPU %",
                                "value": 3,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    page.locator('[data-testid="import-input"]').set_input_files(second_bad_row)
    banner.wait_for()
    assert "dropped 1 metric with invalid timestamp" in banner.inner_text()


def test_theme_toggle_persists_across_reload(page, shell_dash):
    page.goto(shell_dash.url)
    page.locator('[data-testid="overflow-menu"]').click()
    before = page.evaluate("document.documentElement.classList.contains('dark-mode')")
    page.locator('[data-testid="menu-theme"]').click()
    assert page.evaluate("document.documentElement.classList.contains('dark-mode')") is not before
    page.reload()
    assert page.evaluate("document.documentElement.classList.contains('dark-mode')") is not before


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


def test_grid_health_tiles_and_headline(shell_dash, page):
    """Fleet grid (UX §8): labeled headline at full range; down · duration
    when the selected range ends inside the outage window (health is
    last-known-within-range, so narrowing re-evaluates it)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # host-tile-* is grid-only, not the topology landing
    tile = page.locator('[data-testid="host-tile-chassis-a_lc1"]')
    tile.wait_for()
    assert re.search(r"% cpu", page.locator('[data-testid="headline-chassis-a_lc1"]').inner_text())
    w2 = page.locator('[data-testid="host-tile-workers_w2"]')
    assert "down ·" not in w2.inner_text()
    # Rollup bar: one segment per chassis member.
    assert page.locator('[data-testid="health-rollup-chassis-a"] > *').count() == 3

    # End the range inside workers_w2's 60-80min outage: derive +70min from
    # the popover's pre-seeded "from" field (same derivation the custom-range
    # spec uses).
    _open_range_picker(page)
    start = _read_range_field(page, 0)
    _set_range_field(page, 1, start + timedelta(minutes=70))
    _apply_range_picker(page)
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"host-tile-workers_w2\"]')"
        ".innerText.includes('down ·')"
    )
    assert "down · 10m" in w2.inner_text()


def test_subject_charts_render_and_filter(shell_dash, page):
    """Per-subject stack (UX §9): canvases render per chart group; the
    series tree checkbox and chip filters narrow the stack."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').click()
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    assert page.locator('[data-testid="chart-stack"] canvas').count() >= 4
    # Uncheck the CPU series -> its (single-series) panel unmounts.
    page.locator('[data-testid="series-node-CPU %"]').click()
    page.locator('[data-testid="chart-panel-cpu"]').wait_for(state="detached")
    # Chip filter narrows to one group.
    page.locator('[data-testid="chip-mem"]').click()
    page.locator('[data-testid="chart-panel-psu-temp"]').wait_for(state="detached")
    assert page.locator('[data-testid="chart-stack"] canvas').count() == 1


def test_source_badges_and_source_filter(shell_dash, page):
    """Provenance (UX §9): mgmt-sourced series wear a badge; the source
    chip filters the tree to externally-sourced series only."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').click()
    panel = page.locator('[data-testid="series-panel"]')
    panel.wait_for()
    assert "mgmt-01" in panel.inner_text()
    before = page.locator('[data-testid^="series-node-"]').count()
    page.locator('[data-testid="chip-source-mgmt-01"]').click()
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-testid^=\"series-node-\"]').length < {before}"
    )
    # Only the two mgmt-sourced charts remain for this host.
    assert page.locator('[data-testid^="chart-panel-"]').count() == 2


def test_events_slide_over_jumps_range(shell_dash, page):
    """Events (UX §11 review subset): reverse-chron slide-over; a row jump
    re-scopes the shared range (the range picker's trigger label follows).

    ``events-button``/``events-count`` moved to SubjectPage's title row
    (spec 2026-07-17 decision 11) -- events are session-scoped, not
    per-host, so any subject page carries the same badge; the entry point
    below hops through the grid to reach one, but every assertion past that
    is unchanged.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")
    page.locator('[data-testid^="subject-link-"]').first.click()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert page.locator('[data-testid="events-count"]').inner_text() == "4"
    before = _range_picker_label(page)
    page.locator('[data-testid="events-button"]').click()
    page.locator('[data-testid="events-panel"]').wait_for()
    rows = page.locator('[data-testid^="event-row-"]')
    assert rows.count() == 4
    assert "log capture" in rows.nth(0).inner_text()  # newest first
    page.locator('[data-testid="event-row-2"]').click()  # stress-run span
    page.locator('[data-testid="events-panel"]').wait_for(state="detached")
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"range-picker\"]').innerText !== prev",
        arg=before,
    )


def test_log_table_renders_and_filters(shell_dash, page):
    """Table tabs: kernel log rows render for db-01 and filter down."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-db-01"]').click()
    table = page.locator('[data-testid="log-table-kernel"]')
    table.wait_for()
    rows_before = table.locator("tbody tr").count()
    assert rows_before > 0
    page.locator('[data-testid="log-filter-kernel"] input').fill("no-such-message-xyz")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"log-table-kernel\"]')"
        ".querySelectorAll('tbody tr').length === 0"
    )


def test_element_subject_renders_member_series(shell_dash, page):
    """Element drill-in: /host/chassis-a stacks member + element-targeted
    series (ambient) as charts."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/host/chassis-a")
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.locator('[data-testid="chart-panel-ambient"] canvas').wait_for()


def test_theme_toggle_with_charts_open(shell_dash, page):
    """Theme flip re-renders open charts without error (canvas persists,
    dark-mode class lands).

    The overflow menu's theme toggle is a strict two-state light<->dark
    flip (web/src/theme.ts: ``Theme = "light" | "dark"``, no "system"
    state) applied synchronously by AppBar's toggleTheme -> saveTheme ->
    applyTheme (theme.ts) — one click always flips the single `dark-mode`
    class (Untitled UI's vendored theme.css gates its dark tokens on it;
    app.css's `@custom-variant dark` reads the same class), so waiting for
    `dark-mode` to differ from its pre-click value is a genuinely
    discriminating wait (unlike a `!== undefined` check against a boolean,
    which is always true and asserts nothing)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")  # subject-link-* is grid-only, not the topology landing
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.locator('[data-testid="overflow-menu"]').click()
    before_dark = page.evaluate("document.documentElement.classList.contains('dark-mode')")
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "(prev) => document.documentElement.classList.contains('dark-mode') !== prev",
        arg=before_dark,
    )
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()


def test_topology_toggle_and_map(shell_dash, page):
    """Topology <-> Hosts toggle (UX §6, ViewSwitcher tab labels per the
    button-border tabs rework); map renders elements at hop depths rooted at
    local (UX §10).

    "/" is the topology landing now (route swap, spec 2026-07-17), so the
    import already lands here -- the round trip below still proves the
    switcher works both ways, just starting from the other end. The
    switcher's second tab is labeled "Hosts" (ViewSwitcher.tsx), not the
    retired "Grid" label.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="topology-page"]').wait_for()
    for node in ("local", "edge-gw", "chassis-a", "workers", "db-01", "mgmt-01"):
        assert page.locator(f'[data-testid="topo-node-{node}"]').count() == 1
    # Rollup segments on the chassis element node (3 members: lc1, lc2, sup).
    assert page.locator('[data-testid="topo-node-chassis-a"] [data-status-segment]').count() == 3
    page.get_by_text("Hosts", exact=True).click()
    page.locator('[data-testid="overview-page"]').wait_for()
    page.get_by_text("Topology", exact=True).click()
    page.locator('[data-testid="topology-page"]').wait_for()


def test_topology_zoom_controls_follow_theme(shell_dash, page):
    """React Flow's stock chrome (the zoom controls) is themed by the library's
    own ``.react-flow.dark`` token block, which keys off a `dark` class on the
    React Flow CONTAINER — the `dark-mode` class theme.ts toggles on <html>
    cannot reach that selector (different class, different element). The page
    must hand React Flow an explicit colorMode; without it the zoom buttons
    keep their light-mode white background while the rest of the app is dark.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    zoom_in = page.locator("button.react-flow__controls-zoomin")
    zoom_in.wait_for()

    for want_dark, bound in ((True, 128), (False, 200)):
        _set_theme(page, dark=want_dark)
        mean = _mean_channel(zoom_in.evaluate("(el) => getComputedStyle(el).backgroundColor"))
        if want_dark:
            assert mean < bound, f"zoom button stayed light in dark mode (mean channel {mean})"
        else:
            assert mean > bound, f"zoom button is not light in light mode (mean channel {mean})"


def test_topology_drill_in_and_singleton(shell_dash, page):
    """Element enter -> intra view with slot badges; singleton goes straight
    to the host page."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-chassis-a"]').click()
    page.locator('[data-testid="topo-breadcrumb"]').wait_for()
    lc1 = page.locator('[data-testid="topo-node-chassis-a_lc1"]')
    lc1.wait_for()
    assert "slot 1" in lc1.inner_text()
    lc1.click()
    page.locator('[data-testid="subject-page"]').wait_for()
    # Singleton: db-01's element node lands on the host page directly.
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()


def test_link_inspector_and_parallel_edges(shell_dash, page):
    """Links are first-class: declared edges select into the inspector; the
    workers~db pair fans out as two parallel edges; impair pill shows."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)
    impair = page.locator('[data-testid^="topo-impair-"]')
    assert impair.count() == 1
    assert "edge-gw" in impair.inner_text()
    # Click the impaired edge's stroke and inspect (see _click_edge's
    # docstring: a naive bbox-center click lands on an unrelated node here).
    marker_testid = impair.get_attribute("data-testid")
    edge_id = marker_testid.removeprefix("topo-impair-")
    _click_edge(page, edge_id)
    panel = page.locator('[data-testid="link-inspector"]')
    panel.wait_for()
    assert "udp" in page.locator('[data-testid="inspector-protocol"]').inner_text()
    assert "coming soon" in page.locator('[data-testid="inspector-netem"]').inner_text()
    page.get_by_label("Close").click()
    panel.wait_for(state="detached")


def test_link_less_edges_do_not_open_the_inspector(shell_dash, page):
    """A reports-for edge carries no LinkSnapshot — there is nothing to inspect,
    so clicking it is inert and the hover card is its whole story. Before the
    edge-class collapse this opened a degenerate panel: raw edge id as the
    title, no fact rows, and a NetEm section for a relation that has no link
    object to configure."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 1"
    )

    _click_edge(page, "reports:mgmt-01~chassis-a")

    # A negative assertion needs a barrier, or it passes trivially by running
    # before the click is even processed. NOT a sleep (this repo has no
    # wait_for_timeout anywhere, and an arbitrary budget is a flake waiting to
    # happen). The barrier is sound because Playwright's `page.mouse.click()`
    # blocks through its CDP round-trips (move → down → up); Chromium
    # synthesizes and dispatches the native `click` event synchronously inside
    # the `mouseReleased` call, and React flushes discrete-event state updates
    # synchronously within that same dispatch. By the time `_click_edge()`
    # returns to the test script, the click's full effect is already committed
    # to the DOM — no window is left for the assertion to race. This suite
    # (noxfile.py `dashboard`) also runs on Firefox and WebKit: both dispatch
    # their synthesized click synchronously within the same input-processing
    # task and flush React's resulting state update before yielding back to
    # Playwright, so the same no-window argument holds on all three engines.
    # The hover-card wait is a belt-and-braces check that the pointer genuinely
    # landed on the edge (the thing `_point_on_edge` exists to guarantee), not
    # the thing that makes the assertion safe.
    page.locator('[data-testid="topo-hover-reports:mgmt-01~chassis-a"]').wait_for()
    assert page.locator('[data-testid="link-inspector"]').count() == 0


def test_minimap_toggles_and_does_not_occlude_the_map(shell_dash, page):
    """The minimap is off by default, toggles on, and — being an overlay panel in
    the canvas's bottom-right — must not cover a node.

    The occlusion assertion is elementFromPoint, NOT locator.click(): click()
    auto-scrolls and retries, so it can manufacture a click on an element a real
    user could never reach. That is precisely how chromium false-passed #134 while
    the panel was covering chassis-a the whole time.
    """
    # Matches the layout guard's viewport (test_topology_page_does_not_scroll):
    # below ~1150px wide, ReviewBar wraps to a second row, the canvas is
    # shorter, and the graph re-fits — the default 1280x720 viewport never
    # exercises that wrapped-chrome regime, so it can't tell us whether a node
    # then lands under the minimap.
    page.set_viewport_size({"width": 1100, "height": 720})
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)

    minimap = page.locator('[data-testid="topo-minimap"]')
    assert minimap.count() == 0
    page.locator('[data-testid="minimap-toggle"]').click()
    # MiniMap doesn't forward data-testid to its rendered Panel (it hardcodes its
    # own "rf__minimap"), so a wrapper carries our testid instead, and that
    # wrapper is `display: contents` so it doesn't disturb the Panel's absolute
    # positioning — which also means the wrapper itself always has an empty
    # bounding box, so Playwright's default visible-state wait never resolves on
    # it. `attached` is the correct state for the wrapper; the panel's actual
    # on-screen presence is confirmed via its real rendered class below.
    minimap.wait_for(state="attached")
    panel = page.locator(".react-flow__minimap")
    panel.wait_for()
    assert panel.bounding_box() is not None, "minimap toggled on but nothing rendered visibly"

    # Every node must still be the top element at its own centre.
    nodes = page.locator('[data-testid^="topo-node-"]').all()
    assert nodes, "no topo-node-* elements found — testid prefix may have drifted"
    for node in nodes:
        testid = node.get_attribute("data-testid")
        box = node.bounding_box()
        assert box is not None, f"{testid} has no box"
        hit = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.closest('[data-testid]')"
            "?.getAttribute('data-testid')",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        )
        assert hit == testid, f"{testid} is covered at its centre by {hit}"


def test_topology_page_does_not_scroll(shell_dash, page):
    """Layout regression guard for the flex-based canvas sizing (replacing a
    hardcoded ``h-[calc(100vh-6.5rem)]`` chrome-height constant, e3116a0).

    The vitest for this (``pages.test.tsx``'s "sizes the topology canvas by
    flex, not by a guessed chrome height") only pins the CSS class strings —
    jsdom does no box layout, so it would stay green even if the real layout
    genuinely overflowed. This is the only committed check that actually
    measures the box: the canvas is supposed to fit exactly in the viewport
    below the chrome, so the document must not scroll.

    Forces a narrower-than-default viewport on purpose. ReviewBar is
    flex-wrap, but empirically it only wraps to a second row below ~1150px
    wide — Playwright's default 1280x720 viewport never triggers it, on
    either fixture, so a test at the default size would pass vacuously
    whether or not the fix is present (verified: it stayed green against the
    old hardcoded height too). At 1100px the old constant measurably
    overflowed the document by ~40px; this width is what actually reproduces
    the bug the fix addresses.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    page.set_viewport_size({"width": 1100, "height": 720})
    overflow = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    assert overflow <= 1, f"topology page scrolls by {overflow}px — the canvas is overtall"


def test_sources_overlay_toggles_reports_edges(shell_dash, page):
    """Sources overlay (UX §10): default off; toggling reveals the mgmt
    reports-for edge and toggling again removes it."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    # Default-off is only a real claim once the canvas has drawn its other
    # edges; asserting it against a still-empty canvas proves nothing.
    _wait_for_links(page, 6)
    reports = page.locator('[data-testid^="topo-link-reports:"]')
    assert reports.count() == 0
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 1"
    )
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 0"
    )


def test_cascade_unreachable_vs_down(shell_dash, page):
    """Reachability cascade (spec headline): dead gateway renders down; the
    silent hosts behind it render unreachable, and the rack element node's
    worst-status rollup reports unreachable (not down — the whole point of
    the cascade is to distinguish "genuinely down" from "silent because an
    ancestor is down"); the intra view's member nodes carry unreachable
    themselves, and both declared parallel links between them still render."""
    page.goto(shell_dash.url)
    _import_fixture(page, "cascade.json")
    page.goto(f"{shell_dash.url}#/topology")
    gw = page.locator('[data-testid="topo-node-gw-a"]')
    gw.wait_for()
    assert gw.get_attribute("data-status") == "down"
    rack = page.locator('[data-testid="topo-node-rack-a"]')
    # Verified against the running DOM: rack-a's two members (n1, n2) are
    # both "unreachable" (silent + dead ancestor gw-a), and worst() only
    # promotes to "down" if a member is itself down — neither is, so the
    # rollup's worst status is "unreachable", not "down".
    assert rack.get_attribute("data-status") == "unreachable"
    assert rack.locator('[data-status-segment="unreachable"]').count() == 2
    # Intra view: the member nodes carry unreachable themselves.
    rack.click()
    n1 = page.locator('[data-testid="topo-node-rack-a_n1"]')
    n1.wait_for()
    assert n1.get_attribute("data-status") == "unreachable"
    # Parallel rack pair (cascade.json's "pair-a"/"pair-b" declared links
    # between rack-a_n1 and rack-a_n2) fans out as two distinct declared
    # edges — link ids pass through as edge ids verbatim (data/topology.ts).
    # The intra view mounts a fresh canvas, so its edges lag its nodes too.
    _wait_for_links(page, 2)
    assert page.locator('[data-testid="topo-link-pair-a"]').count() == 1
    assert page.locator('[data-testid="topo-link-pair-b"]').count() == 1


def test_topology_pan_zoom_fit(shell_dash, page):
    """Pan/zoom smoke: dragging the pane moves the viewport; Fit restores."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-local"]').wait_for()
    viewport = page.locator(".react-flow__viewport")
    before = viewport.get_attribute("style")
    pane = page.locator(".react-flow__pane")
    box = pane.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 180, box["y"] + box["height"] / 2 + 60)
    page.mouse.up()
    page.wait_for_function(
        "(prev) => document.querySelector('.react-flow__viewport').getAttribute('style') !== prev",
        arg=before,
    )
    page.locator('[data-testid="topo-fit"]').click()
    page.locator('[data-testid="topo-node-local"]').wait_for()


def test_fit_padding_bottom_is_an_absolute_reserve_at_a_tall_viewport(shell_dash, page):
    """``TopologyPage``'s ``FIT_PADDING.bottom`` must be the STRING ``"256px"``,
    not a bare number.

    React Flow's ``parsePadding`` (``@xyflow/system``) treats a bare number as
    a FRACTION of the viewport dimension, even inside the per-side padding
    object — only the ``"NNpx"`` string form is an absolute reserve. At
    Playwright's default 1280x720 viewport the fraction happens to evaluate
    close enough to the intended reserve that every existing gate (vitest,
    tsc, Biome, all three browser engines) stayed green with the bug in
    place, the one time it shipped. At a genuinely tall canvas the fraction
    saturates toward HALF the canvas height, so this only shows up away from
    the default size — which is the whole reason it shipped unnoticed.

    ``sprawl.json`` is deliberately tall relative to its width in MODEL space
    (~1168 x ~1887, verified via ``layoutTopo`` — its longest chain is
    isp-core's twin, but sprawl's leaf-heavy shape stacks taller), so
    ``fitView``'s ``zoom = min(xZoom, yZoom)`` is HEIGHT-bound here: the
    bottom reserve is the actual binding constraint, not slack a
    wide/short fixture would hide.

    Hand-computed at width=1280/height=1400 (this test's viewport):
    a buggy bare-number 256 reserves well over half of the 1400px canvas,
    leaving the fitted graph's lowest edge at almost exactly the vertical
    midpoint (~0.50); the fixed ``"256px"`` reserves a flat 256px regardless
    of canvas height, leaving the graph extending to ~0.80. 0.65 sits with a
    wide margin on both sides of that split.
    """
    page.set_viewport_size({"width": 1280, "height": 1400})
    page.goto(shell_dash.url)
    _import_fixture(page, "sprawl.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 30)

    canvas = page.locator(".react-flow__pane").bounding_box()
    assert canvas is not None, "no react-flow pane rendered"
    nodes = page.locator(".react-flow__node").all()
    assert nodes, "no react-flow nodes rendered"
    boxes = [n.bounding_box() for n in nodes]
    assert all(boxes), "a react-flow node had no bounding box"
    lowest_bottom = max(b["y"] + b["height"] for b in boxes)
    lowest_frac = (lowest_bottom - canvas["y"]) / canvas["height"]

    assert lowest_frac > 0.65, (
        f"the fitted map's lowest node bottom sits at only {lowest_frac:.2f} of "
        "the canvas height down -- the graph looks squeezed into the top half. "
        'FIT_PADDING.bottom likely regressed from "256px" to a bare number '
        "256, which React Flow's parsePadding treats as a FRACTION, not an "
        "absolute px reserve (see TopologyPage.tsx)."
    )


def test_link_inspector_survives_range_change(shell_dash, page):
    """Inspector selection is scoped to the view identity: it survives a
    review-bar range apply (a selected link is static config) and closes on
    navigation to another view.

    Runs at Playwright's DEFAULT 1280px width, and that is the point. The
    inspector used to be a viewport-fixed full-height right aside, so at 1280px
    its span (x >= 896) covered the review bar's right end where Apply sits
    (center x ~1016) — this test had to force a 1600px viewport to reach the
    button at all. The inspector no longer overlays ANYTHING: it is a layout
    sibling of the canvas that reserves its own 384px column, so it can reach
    neither the review bar nor the map. Both claims are proved below — the
    range picker's Apply click (Playwright fails it if anything overlays the
    button) and `_assert_reachable` on the map's rightmost node.

    That node is the second proof for a reason. The 1600px override was hiding
    TWO occlusions, not one: while it was in force the canvas was wide enough
    that the panel missed the map's deepest column too. Once the override came
    off, the panel — then an overlay pinned to the canvas's right edge — landed
    squarely on `chassis-a` (the only depth-2 element, laid out at x = 2*COL_W,
    hard against the right edge that fitView fits it to). That is issue #134."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    impair = page.locator('[data-testid^="topo-impair-"]')
    marker_testid = impair.get_attribute("data-testid")
    edge_id = marker_testid.removeprefix("topo-impair-")
    _click_edge(page, edge_id)
    panel = page.locator('[data-testid="link-inspector"]')
    panel.wait_for()

    # Custom range apply — same segment-typing idiom as
    # test_custom_range_apply_and_reset: narrow +10..+20min off the
    # popover's pre-seeded "from" field.
    before_label = _range_picker_label(page)
    _open_range_picker(page)
    t0 = _read_range_field(page, 0)
    _set_range_field(page, 0, t0 + timedelta(minutes=10))
    _set_range_field(page, 1, t0 + timedelta(minutes=20))
    _apply_range_picker(page)
    # Discriminating proof the apply actually landed (not just that the
    # fields hold the typed text): the trigger label is derived straight
    # from the committed `range` value, so it only changes once the store's
    # range is genuinely applied.
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"range-picker\"]').innerText !== prev",
        arg=before_label,
    )
    assert panel.is_visible()

    # The open panel must not have eaten any of the map (issue #134). Asserted
    # BEFORE the click, and geometrically rather than by clicking, because
    # chromium's hit-target check would let the click through even if it had.
    _assert_reachable(page, "topo-node-chassis-a")

    # Navigating into an element view is a different view identity ->
    # the inspector detaches.
    page.locator('[data-testid="topo-node-chassis-a"]').click()
    page.locator('[data-testid="topo-breadcrumb"]').wait_for()
    panel.wait_for(state="detached")


def test_topology_legend_hover_and_tunnel_casing(shell_dash, page):
    """The canvas explains itself: an anchored key decodes every line style and
    status colour, hovering an edge names the link under the cursor without
    opening the inspector, and a tunnel carries its casing."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)

    legend = page.locator('[data-testid="topo-legend"]')
    legend.wait_for()
    for edge_class in ("static", "tunnel", "reports-for"):
        assert legend.locator(f'[data-testid="topo-legend-link-{edge_class}"]').count() == 1
    for status in ("ok", "down", "unreachable", "no-data", "unknown"):
        assert legend.locator(f'[data-testid="topo-legend-status-{status}"]').count() == 1

    toggle = page.locator('[data-testid="topo-legend-toggle"]')
    assert toggle.get_attribute("aria-expanded") == "true"
    toggle.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"topo-legend-toggle\"]')"
        ".getAttribute('aria-expanded') === 'false'"
    )

    # The tunnel's casing: a second, wider, translucent stroke on the same
    # path. No other provenance draws one. React Flow's BaseEdge always adds
    # its own invisible `react-flow__edge-interaction` hit-target path, which
    # (verified against the live DOM) *also* carries a `stroke-opacity`
    # attribute (`"0"`, to stay invisible) on every edge, tunnel or not — a
    # bare `path[stroke-opacity]` selector matches that library boilerplate
    # too and overcounts by one, so the casing has to be picked out by name.
    #
    # kitchen-sink's bare 2-hop tunnel is `tun-00000000demo-15001` (edge-gw ->
    # db-01, one segment, index 0) — tunnels moved off `LinkSnapshot` (spec
    # 2026-07-16), so the segment testid is `topo-link-<tunnelId>:<i>`, not a
    # link id (the old `tun-demo` link no longer exists).
    tunnel = page.locator(
        '[data-testid="topo-link-tun-00000000demo-15001:0"] '
        "path[stroke-opacity]:not(.react-flow__edge-interaction)"
    )
    assert tunnel.count() == 1
    other = page.locator(
        '[data-testid="topo-link-app-db"] path[stroke-opacity]:not(.react-flow__edge-interaction)'
    )
    assert other.count() == 0

    # Hovering an edge names it — and does NOT open the inspector. app-db's
    # curve bows well clear of its own bounding-box center (verified against
    # the live DOM: that point sits over bare pane), so the mouse has to
    # land on a real point along the rendered stroke (_point_on_edge) rather
    # than a naive hover — the same trap _click_edge documents for clicks.
    point = _point_on_edge(page, "app-db")
    page.mouse.move(point["x"], point["y"])
    card = page.locator('[data-testid="topo-hover-app-db"]')
    card.wait_for()
    assert "app-db" in card.inner_text()
    assert page.locator('[data-testid="link-inspector"]').count() == 0


def test_review_mode_boots_hydrated(review_dash, page):
    """Boot hydration (Plan 5a Task 6): a review-mode server opens straight
    into the dashboard from its loaded document, with no Import interaction
    — plus the session-note supporting-text's end-to-end proof. Task 3
    follow-up: the fork that hand-built the picker's trigger button (and
    smuggled the note through a ``title`` attribute react-aria-components'
    ``filterDOMProps`` would otherwise strip) is gone; the note now maps
    onto vendored Untitled UI Select's native ``supportingText`` field
    (select-shared.tsx's ``SelectItemType``), which select-item.tsx renders
    as a second, always-visible text node under the option's label — this
    asserts that text actually reaches the rendered DOM option."""
    page.goto(review_dash.url)
    page.locator('[data-testid="review-bar"]').wait_for()
    assert page.locator('[data-testid="source-name"]').inner_text() == "minimal.json"

    picker = page.locator('[data-testid="session-picker"]')
    picker.wait_for()
    picker.click()
    # The click opens a react-aria Popover; Locator.count() does NOT
    # auto-retry (unlike get_attribute/inner_text below), so counting the
    # options straight after the click races the listbox committing to the
    # DOM. Wait for the render transition first — the file's existing idiom.
    page.get_by_role("option").first.wait_for()
    assert page.get_by_role("option").count() == 2
    # The option's accessible name comes from the label slot alone
    # (select-item.tsx's `AriaText slot="label"`, wired to aria-labelledby)
    # — the supportingText slot is a separate, aria-describedby'd node, so
    # `name=` matching by label continues to work unscoped by the note.
    plain = page.get_by_role("option", name="minimal", exact=True)
    noted = page.get_by_role("option", name="second", exact=True)
    assert "second run" not in plain.inner_text()
    assert "second run" in noted.inner_text()
    page.keyboard.press("Escape")
    noted.wait_for(state="detached")  # popover closed

    # Smoke that the hydrated data drives the whole shell, not just the
    # review bar: the topology page renders off the same session, and the
    # chrome (review bar) survives the round trip back to the overview.
    #
    # Route swap (spec 2026-07-17 topology-default-view): "/" is now the
    # topology landing itself, so a bare boot no longer leaves an #/hosts
    # history entry for go_back() to land on -- visit it explicitly first.
    page.goto(f"{review_dash.url}#/hosts")
    page.locator('[data-testid="overview-page"]').wait_for()
    page.goto(f"{review_dash.url}#/topology")
    page.locator('[data-testid="topo-node-local"]').wait_for()
    page.go_back()
    page.locator('[data-testid="overview-page"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()


def test_live_mode_still_boots_empty(shell_dash, page):
    """Pin: in live mode (``/api/mode`` -> ``"live"``), Task 6's boot fetch
    is a no-op — the shell still opens to the Import front door exactly as
    it did before that fetch existed."""
    page.goto(shell_dash.url)
    page.locator('[data-testid="empty-review"]').wait_for()
    page.locator('[data-testid="import-input"]').wait_for(state="attached")
    assert page.locator('[data-testid="review-bar"]').count() == 0
