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


def _set_theme(page, *, dark: bool) -> None:
    """Drive the overflow menu's two-state toggle to a known theme."""
    if page.evaluate("document.documentElement.classList.contains('dark')") is dark:
        return
    page.locator('[data-testid="overflow-menu"]').click()
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "(want) => document.documentElement.classList.contains('dark') === want", arg=dark
    )


def _mean_channel(color: str) -> float:
    """Mean RGB channel of a computed ``rgb(...)``/``rgba(...)`` color."""
    channels = [float(v) for v in re.findall(r"[\d.]+", color)[:3]]
    assert len(channels) == 3, f"unparseable color {color!r}"
    return sum(channels) / 3


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


def test_grid_health_tiles_and_headline(shell_dash, page):
    """Fleet grid (UX §8): labeled headline at full range; down · duration
    when the selected range ends inside the outage window (health is
    last-known-within-range, so narrowing re-evaluates it)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    tile = page.locator('[data-testid="host-tile-chassis-a_lc1"]')
    tile.wait_for()
    assert re.search(r"% cpu", page.locator('[data-testid="headline-chassis-a_lc1"]').inner_text())
    w2 = page.locator('[data-testid="host-tile-workers_w2"]')
    assert "down ·" not in w2.inner_text()
    # Rollup bar: one segment per chassis member.
    assert page.locator('[data-testid="health-rollup-chassis-a"] > *').count() == 3

    # End the range inside workers_w2's 60-80min outage: derive +70min from
    # the pre-populated LOCAL from-input (same derivation the custom-range
    # spec uses — datetime-local is local wall-clock).
    start_raw = page.locator('[data-testid="range-from"]').input_value()
    start = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 — naive local wall-clock by design
    page.locator('[data-testid="range-to"]').fill(
        (start + timedelta(minutes=70)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-apply"]').click()
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
    re-scopes the shared range (review-bar inputs follow)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    assert page.locator('[data-testid="events-count"]').inner_text() == "4"
    before = page.locator('[data-testid="range-from"]').input_value()
    page.locator('[data-testid="events-button"]').click()
    page.locator('[data-testid="events-panel"]').wait_for()
    rows = page.locator('[data-testid^="event-row-"]')
    assert rows.count() == 4
    assert "log capture" in rows.nth(0).inner_text()  # newest first
    page.locator('[data-testid="event-row-2"]').click()  # stress-run span
    page.locator('[data-testid="events-panel"]').wait_for(state="detached")
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"range-from\"]').value !== prev",
        arg=before,
    )


def test_log_table_renders_and_filters(shell_dash, page):
    """Table tabs: kernel log rows render for db-01 and filter down."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
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
    dark class lands).

    The overflow menu's theme toggle is a strict two-state light<->dark
    flip (web/src/theme.ts: ``Theme = "light" | "dark"``, no "system"
    state) applied synchronously by AppBar's toggleTheme -> saveTheme ->
    applyTheme (theme.ts:16-18) — one click always flips the `dark` class,
    so waiting for it to differ from its pre-click value is a genuinely
    discriminating wait (unlike a `!== undefined` check against a boolean,
    which is always true and asserts nothing)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.locator('[data-testid="overflow-menu"]').click()
    before_dark = page.evaluate("document.documentElement.classList.contains('dark')")
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "(prev) => document.documentElement.classList.contains('dark') !== prev", arg=before_dark
    )
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()


def test_topology_toggle_and_map(shell_dash, page):
    """Grid <-> Topology toggle (UX §6); map renders elements at hop depths
    rooted at local (UX §10)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.get_by_text("Topology", exact=True).click()
    page.locator('[data-testid="topology-page"]').wait_for()
    for node in ("local", "edge-gw", "chassis-a", "workers", "db-01", "mgmt-01"):
        assert page.locator(f'[data-testid="topo-node-{node}"]').count() == 1
    # Rollup segments on the chassis element node (3 members: lc1, lc2, sup).
    assert page.locator('[data-testid="topo-node-chassis-a"] [data-status-segment]').count() == 3
    page.get_by_text("Grid", exact=True).click()
    page.locator('[data-testid="overview-page"]').wait_for()


def test_topology_zoom_controls_follow_theme(shell_dash, page):
    """React Flow's stock chrome (the zoom controls) is themed by the library's
    own ``.react-flow.dark`` token block, which keys off a `dark` class on the
    React Flow CONTAINER — the `dark` class theme.ts toggles on <html> cannot
    reach that selector. The page must hand React Flow an explicit colorMode;
    without it the zoom buttons keep their light-mode white background while
    the rest of the app is dark.
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


def test_link_inspector_survives_range_change(shell_dash, page):
    """Inspector selection is scoped to the view identity: it survives a
    review-bar range apply (a selected link is static config) and closes
    on navigation to another view.

    Desktop-width viewport: the non-modal inspector is a fixed full-height
    right aside, 384px wide (LinkInspector.tsx). At Playwright's default
    1280px width the panel's span (x >= 896) physically covers the review
    bar's right end where Apply sits (center x ~1016) — plain visual
    occlusion, not a focus trap or backdrop; everything left of the panel
    is fully interactive. Real desktop widths put the whole review bar left
    of the panel edge; 1600px reproduces that."""
    page.set_viewport_size({"width": 1600, "height": 900})
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

    # Custom range apply — same from-input-derived idiom as
    # test_custom_range_apply_and_reset: narrow +10..+20min off the
    # pre-populated LOCAL from-input (datetime-local is local wall-clock).
    start_local = page.locator('[data-testid="range-from"]').input_value()
    t0 = datetime.strptime(start_local, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 — naive local wall-clock by design
    page.locator('[data-testid="range-from"]').fill(
        (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-to"]').fill(
        (t0 + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
    )
    # Before Apply: the default "Full" range preset is still selected
    # (react-aria's Radio marks the active one with a `data-selected` attr).
    assert page.locator('[data-testid="range-presets"] [data-selected]').count() == 1
    page.locator('[data-testid="range-apply"]').click()
    # Discriminating proof the apply actually landed (not just that the
    # inputs hold the typed text): a custom window matches none of the
    # presets, so the "Full" pill loses its selected state once the store's
    # range is genuinely applied.
    page.wait_for_function(
        "() => document.querySelectorAll("
        "'[data-testid=\"range-presets\"] [data-selected]').length === 0"
    )
    assert panel.is_visible()

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
    for provenance in ("declared", "implicit", "dynamic", "reports-for", "local"):
        assert legend.locator(f'[data-testid="topo-legend-link-{provenance}"]').count() == 1
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
    tunnel = page.locator(
        '[data-testid="topo-link-tun-demo"] path[stroke-opacity]'
        ":not(.react-flow__edge-interaction)"
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
