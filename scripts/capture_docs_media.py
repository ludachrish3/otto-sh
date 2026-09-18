#!/usr/bin/env python3
"""Generate the docs' GUI media — screenshots — at build time.

Nothing under ``docs/_static/generated/`` is committed: docs/conf.py invokes
this script on every HTML build (dev VM, CI, Read the Docs), so the media is
produced from the *live* frontend code and can never drift from what otto
actually looks like — the same zero-rot property the architecture tree gets
from ``inheritance_diagram``.

The pipeline reuses the browser-e2e harness (``DashboardHarness`` +
``FakeCollector`` from ``tests/_fixtures``): the real ``MonitorServer`` serves
the real review shell (the built ``web/`` dist) with an EMPTY collector — no
server-seeded data, no boot-time API calls, exactly the production shell's
own boot behavior — and headless Chromium feeds it client-side through the
Import front door with a committed ``web/fixtures/`` document, the same way
``tests/e2e/monitor/dashboard/test_review_shell.py`` does. When the frontend
(or the fixture) changes, the media regenerates on the next build.

Live monitoring has no capture of its own: the review-first shell has no live
page to photograph, and that mode returns at a later phase (see
``docs/cli/monitor/live.md``). The coverage-report capture is unrelated to any
of this and always runs for real.

Modes — ``--mode`` flag, or the ``OTTO_DOCS_MEDIA`` env var:

- ``auto`` (default): regenerate only when the stamp says the inputs changed
  (this script, the fixtures, ``src/otto/monitor``, or the coverage code
  the report fixture renders through).
- ``force``: always regenerate (``make docs-media``).
- ``placeholder``: write tiny placeholder assets without launching a browser.
  Emergency escape hatch only — e.g. a broken Chromium install on the docs
  host — so a docs deploy is degraded, not blocked.

Chromium comes from ``make browsers`` (installed by ``make dev``); a missing
browser is a loud error naming that target, per the dev-environment contract.
"""

import argparse
import base64
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # for the shared tests/_fixtures harness

OUT_DIR = REPO_ROOT / "docs" / "_static" / "generated"
STAMP = OUT_DIR / ".stamp"

# Inputs whose change invalidates the media: this script, the harness
# fixtures it drives, the fixture documents it imports, the whole monitor
# subsystem (server, collector), the coverage renderer plus the store and
# exclusion filter the report fixture builds through, and the built
# frontend artifacts themselves (src/otto/_webassets/ — the actual bundles
# the real MonitorServer serves; they live outside the two package trees
# above, so without this entry a stale built bundle wouldn't invalidate the
# stamp and the docs media would silently keep showing the previous build).
_STAMP_INPUTS = [
    Path(__file__).resolve(),
    REPO_ROOT / "tests" / "_fixtures" / "_dashboard_harness.py",
    REPO_ROOT / "tests" / "_fixtures" / "_fake_collector.py",
    REPO_ROOT / "tests" / "_fixtures" / "_report_fixture.py",
    REPO_ROOT / "web" / "fixtures" / "kitchen-sink.json",
    REPO_ROOT / "web" / "fixtures" / "isp-core.json",
    REPO_ROOT / "src" / "otto" / "monitor",
    REPO_ROOT / "src" / "otto" / "coverage" / "renderer",
    REPO_ROOT / "src" / "otto" / "coverage" / "store",
    REPO_ROOT / "src" / "otto" / "coverage" / "exclusions",
    REPO_ROOT / "src" / "otto" / "_webassets",
]

# The files this script promises to produce (docs pages reference them).
ARTIFACTS = [
    "dashboard-topology.png",
    "dashboard-review.png",
    "dashboard-metrics.png",
    "dashboard-review-charts.png",
    "dashboard-element.png",
    "dashboard-events.png",
    "coverage-report.png",
    "coverage-tiers.png",
    "coverage-file.png",
    "coverage-runs.png",
    "coverage-context-focus.png",
    "coverage-product-focus.png",
    "coverage-legend.png",
    "coverage-asserted.png",
    "coverage-tickets.png",
    "coverage-ticket-context.png",
    "coverage-search.png",
]

_VIEWPORT = {"width": 1280, "height": 720}

# Per-operation Playwright ceiling for the capture. Playwright's 30s default is
# tight for a full-page screenshot of the dashboard with its ECharts canvases
# rendered — the heaviest step in the docs build. On a busy host (e.g. a dev
# VM also running the test suite) the render can be starved past 30s and fail
# the whole `make docs`/`make release` on transient CPU load, not a real fault
# (an idle run finishes in ~15s). This generous ceiling keeps the build
# tolerant of a loaded box while still failing eventually if a capture
# genuinely wedges.
_CAPTURE_TIMEOUT_MS = 90_000

# 1x1 transparent PNG for placeholder mode.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _input_digest() -> str:
    """Content hash of everything that should invalidate the media."""
    h = hashlib.sha256()
    for root in _STAMP_INPUTS:
        files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            h.update(str(f.relative_to(REPO_ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _is_fresh(digest: str) -> bool:
    if not all((OUT_DIR / name).exists() for name in ARTIFACTS):
        return False
    return STAMP.exists() and STAMP.read_text().strip() == digest


def _write_placeholders() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        (OUT_DIR / name).write_bytes(_PLACEHOLDER_PNG)
    STAMP.unlink(missing_ok=True)  # placeholders are never "fresh"
    print("docs media: wrote PLACEHOLDERS (no browser run) — media is degraded", flush=True)


def _clip_shot(
    page: Any,
    name: str,
    *locators: Any,
    pad: int = 12,
    bottom: float | None = None,
    right: float | None = None,
) -> None:
    """Screenshot the union of *locators*' boxes (plus *pad*), nothing more.

    Every still is clipped to the feature it illustrates rather than taken
    as a full page: a full-page capture of a subject page is thousands of
    pixels of log table, and a viewport capture of a short page is mostly
    empty canvas. ``bottom`` (page coordinates) cuts the union short — for
    a region that starts at one element and should stop partway down
    another (e.g. "the app bar down to the tenth code row"); the cut is
    exact, with no pad below it. ``right`` (page coordinates too, see
    ``_page_right``) does the same for the right edge, plus *pad* (a
    full-width row whose content sits on its left).

    Boxes come back in viewport coordinates; the clip of a ``full_page``
    screenshot is in page coordinates, so the current scroll offset is
    added back before clipping. Only the *pad* is trimmed at the document's
    edges; a box that itself runs off the document raises, since clamping
    it would silently crop the feature the shot is meant to show.
    """
    scroll_x, scroll_y, doc_w, doc_h = page.evaluate(
        "[scrollX, scrollY, document.documentElement.scrollWidth,"
        " document.documentElement.scrollHeight]"
    )
    boxes = []
    for loc in locators:
        box = loc.bounding_box()
        if box is None:
            raise RuntimeError(f"docs media: {name}: {loc} has no box (not rendered)")
        boxes.append(box)
    left = min(b["x"] for b in boxes) + scroll_x
    top = min(b["y"] for b in boxes) + scroll_y
    right_edge = max(b["x"] + b["width"] for b in boxes) + scroll_x
    bottom_edge = max(b["y"] + b["height"] for b in boxes) + scroll_y
    if left < 0 or top < 0 or right_edge > doc_w or bottom_edge > doc_h:
        raise RuntimeError(
            f"docs media: {name}: the clip ({left:.0f},{top:.0f})-"
            f"({right_edge:.0f},{bottom_edge:.0f}) runs off the {doc_w}x{doc_h} document"
        )
    x0 = max(0, left - pad)
    y0 = max(0, top - pad)
    x1 = min(doc_w, right_edge + pad)
    y1 = min(doc_h, bottom_edge + pad)
    if bottom is not None:
        y1 = min(y1, bottom)
    if right is not None:
        x1 = min(x1, right + pad)
    page.mouse.move(0, 0)  # no hover state left over from the last click
    page.screenshot(
        path=OUT_DIR / name,
        full_page=True,
        clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
    )


def _page_bottom(loc: Any) -> float:
    """*loc*'s bottom edge in page coordinates (for ``_clip_shot(bottom=)``)."""
    box = loc.bounding_box()
    if box is None:
        raise RuntimeError(f"docs media: {loc} has no box (not rendered)")
    return box["y"] + box["height"] + loc.page.evaluate("scrollY")


def _page_right(loc: Any) -> float:
    """*loc*'s right edge in page coordinates (for ``_clip_shot(right=)``)."""
    box = loc.bounding_box()
    if box is None:
        raise RuntimeError(f"docs media: {loc} has no box (not rendered)")
    return box["x"] + box["width"] + loc.page.evaluate("scrollX")


def _tid(page: Any, testid: str) -> Any:
    return page.locator(f'[data-testid="{testid}"]')


def _wait_toasts_gone(page: Any) -> None:
    """Let any toast (pin/clear notices, Toast.tsx) finish before a shot."""
    toast = _tid(page, "toast")
    if toast.count():
        toast.first.wait_for(state="detached")


def _capture_coverage_report(browser) -> None:  # noqa: ANN001 — playwright import is deferred
    """Photograph the coverage report's features against ONE rendered report.

    The fixture is shared with ``tests/e2e/cov/report_browser/`` (see
    ``build_fixture_report``'s docstring), so the routes, testids and
    fixture facts below are the ones that suite already pins — e.g.
    ``PROJ-204`` owns lines in ``product/main.c`` but nothing in
    ``product/utils.c`` (so pinning it hides a real row), ``nightly-full``
    is the three-run, two-host, two-product context, and ``agent`` is the
    only product with evidence under ``lib/``.

    Each shot opens its route in a fresh browser context (``_open``) so no
    pin, focus or expanded row leaks from one shot into the next — a plain
    navigation that drops a pinned ticket or context from the hash
    REASSERTS it (focus.tsx's ``onHashChange``), and the pins also persist
    per report in ``localStorage``, which a new context starts without.
    """
    from tests._fixtures._report_fixture import build_fixture_report

    with tempfile.TemporaryDirectory(prefix="otto-docs-cov-") as tmp:
        report_dir = build_fixture_report(Path(tmp))
        base_uri = (report_dir / "index.html").as_uri()
        contexts: list[Any] = []

        def _open(route: str, ready: str, *, height: int = _VIEWPORT["height"]):  # noqa: ANN202
            # A new context per shot: empty localStorage, so no pin a
            # previous shot set survives; the previous one is closed here.
            while contexts:
                contexts.pop().close()
            context = browser.new_context(viewport={"width": _VIEWPORT["width"], "height": height})
            contexts.append(context)
            page = context.new_page()
            page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
            page.goto(base_uri + "#" + route)
            _tid(page, ready).first.wait_for()
            return page

        # Directory page — the SPA is a hash-router (covapp) — with no hash
        # it falls through to the NotFoundPlaceholder route, so every route
        # below spells "#/coverage..." explicitly. The overview: stats card
        # and the tree of both source dirs.
        page = _open("/coverage", "tree-row-dir:product")
        _clip_shot(page, "coverage-report.png", _tid(page, "app-bar"), _tid(page, "directory-tree"))

        # Tiers — the tree card alone: one coverage column per tier beside
        # the combined line/branch percentages, for every dir and file.
        _clip_shot(page, "coverage-tiers.png", _tid(page, "directory-tree"))

        # Legend — the ⋮ menu's coverage key (tiers, line states, branch
        # pills). The menu is taller than the default viewport, so this page
        # gets a tall one; the clip starts at the key's first header.
        page = _open("/coverage", "tree-row-dir:product", height=1600)
        _tid(page, "appbar-menu").click()
        key_header = page.get_by_text("Coverage key — tiers")
        key_header.wait_for()
        last_key = page.get_by_role("menuitem").filter(has_text="unreachable")
        _clip_shot(page, "coverage-legend.png", key_header, last_key, pad=4)
        page.keyboard.press("Escape")

        # File page — main.c's annotated source with two lines' run
        # drilldowns open: line 3 (a system run on router-a and the unit
        # harvest, each with its hit count) and line 6 (the revoked
        # smoke-old run, its "revoked" tag struck through).
        page = _open("/coverage/product/main.c", "code-row-1")
        _tid(page, "code-expander-3").click()
        _tid(page, "code-expander-6").click()
        _tid(page, "run-chip").first.wait_for()
        _clip_shot(
            page,
            "coverage-file.png",
            _tid(page, "code-card"),
            bottom=_page_bottom(_tid(page, "code-row-9")),
        )

        # Asserted coverage — main.c lines 1-3: line 1's solid bench count
        # beside line 2's hollow asserted marker, with line 2's drilldown
        # open on the override entry and its reason.
        page = _open("/coverage/product/main.c", "code-row-1")
        _tid(page, "code-expander-2").click()
        _tid(page, "asserted-chip").wait_for()
        _clip_shot(
            page,
            "coverage-asserted.png",
            _tid(page, "code-card"),
            bottom=_page_bottom(_tid(page, "code-row-4")),
        )

        # Runs page — tier and product chips, one row per context, and
        # nightly-full's detail open on its per-host lines: three member
        # runs across two hosts, one of them carrying two products.
        page = _open("/runs", "run-row-nightly-full", height=1200)
        _tid(page, "run-row-nightly-full").click()
        _tid(page, "run-detail-nightly-full").wait_for()
        _clip_shot(page, "coverage-runs.png", _tid(page, "tier-chip-all"), _tid(page, "runs-card"))

        # Context focus — nightly-full pinned from its own detail row, then
        # main.c: the app bar's focus chip, the stats card scoped to that
        # context, and every instrumented line no nightly-full run hit
        # reading uncovered.
        _tid(page, "focus-context-btn").click()
        _tid(page, "focus-chip").wait_for()
        page.evaluate("location.hash = '#/coverage/product/main.c?ctx=nightly-full'")
        _tid(page, "code-row-12").wait_for()
        _wait_toasts_gone(page)
        _clip_shot(
            page,
            "coverage-context-focus.png",
            _tid(page, "app-bar"),
            _tid(page, "code-card"),
            bottom=_page_bottom(_tid(page, "code-row-13")),
        )

        # Product focus — "agent" pinned from the runs page's product chips,
        # then the root directory page: lib/ carries all of agent's evidence
        # and product/ none, tier columns read "—" (a product spans tiers).
        page = _open("/runs", "product-chip-agent")
        _tid(page, "product-chip-agent").click()
        _tid(page, "product-chip").wait_for()
        page.evaluate("location.hash = '#/coverage?product=agent'")
        _tid(page, "tree-row-dir:lib").wait_for()
        _wait_toasts_gone(page)
        _clip_shot(
            page, "coverage-product-focus.png", _tid(page, "app-bar"), _tid(page, "directory-tree")
        )

        # Tickets page — five tickets from 0% to fully covered, sorted
        # worst-uncovered-first, with PROJ-311's row open on its missing
        # range (ring_drain(), never reached) and the attributed-lines card.
        page = _open("/tickets", "ticket-row")
        _tid(page, "ticket-toggle-PROJ-311").click()
        _tid(page, "ticket-detail").wait_for()
        _clip_shot(
            page, "coverage-tickets.png", _tid(page, "stats-card"), _tid(page, "tickets-card")
        )

        # Pinned ticket context — pinning PROJ-204 at "product/" hides
        # utils.c's row (PROJ-204 owns nothing there) and shows the
        # hidden-count banner under the tree. Pinning lives in the app
        # bar's own search box.
        page = _open("/coverage/product", "tree-row-file:product/utils.c")
        _tid(page, "ticket-search").locator("input").fill("PROJ-204")
        _tid(page, "ticket-search-option-PROJ-204").click()
        _tid(page, "ticket-scope-banner").wait_for()
        # The option list closes itself on select; Escape dismisses any
        # popover the click may have left open.
        page.keyboard.press("Escape")
        _tid(page, "ticket-search-options").wait_for(state="hidden")
        _wait_toasts_gone(page)
        _clip_shot(
            page,
            "coverage-ticket-context.png",
            _tid(page, "app-bar"),
            _tid(page, "ticket-scope-banner"),
        )

        # Search palette — opened from the directory page, mid-query, so the
        # shot shows grouped results, both chips, and the total-count footer.
        page = _open("/coverage/product", "tree-row-file:product/utils.c")
        page.keyboard.press("Control+K")
        page.get_by_role("searchbox").fill("checked_add")
        _tid(page, "search-result-0").wait_for()
        _clip_shot(page, "coverage-search.png", _tid(page, "search-palette"), pad=0)

        while contexts:
            contexts.pop().close()


def _wait_for_edges_settled(
    page,  # noqa: ANN001 — playwright import is deferred
    selector: str,
    *,
    interval_s: float = 0.1,
    stable_reads: int = 3,
    timeout_s: float = 10.0,
) -> None:
    """Poll ``selector``'s match count until it holds steady, not a flat sleep.

    React Flow commits an edge batch in more than one paint (issue #130), so
    "at least one edge exists" (the caller's own prior wait) is not "the
    canvas is done growing" — a fixed sleep either races a slow/loaded host
    (screenshots an incomplete batch) or wastes time on a fast one. Polling
    for the count to stop changing is a real completion signal either way,
    the same idea ``tests/e2e/monitor/dashboard/test_topology_budget.py``'s
    ``_wait_for_links`` uses (there, waiting for a known EXACT total derived
    from the fixture; here, no such total is threaded through, so waiting for
    the count to settle is the equivalent bounded-poll signal).
    """
    deadline = time.monotonic() + timeout_s
    last = -1
    steady = 0
    while time.monotonic() < deadline:
        count = page.locator(selector).count()
        steady = steady + 1 if count == last else 0
        if steady >= stable_reads:
            return
        last = count
        time.sleep(interval_s)
    raise TimeoutError(f"{selector!r} count never settled (last read: {last})")


def _capture_topology(browser, harness) -> None:  # noqa: ANN001 — deferred imports
    """Photograph the topology map — the dashboard's landing view.

    Fed through the Import front door with the densest fixture.
    ``web/fixtures/isp-core.json`` (25 hosts / 44 links) carries, since the
    fixture touch-up landed for this shot (spec 2026-07-17
    topology-default-view) and the addendum review's honesty fix, a degraded
    AND an ok tunnel — each actually riding declared links — alongside its
    existing uncertain one, plus two chassis ``elements`` — one frame shows
    the whole tri-state health story (ok / degraded / uncertain) and element
    grouping. ``/`` is the current landing route, so importing there needs no
    follow-up navigation, unlike the grid capture below.

    React Flow withholds an edge until BOTH its endpoint nodes have been
    measured (see ``tests/e2e/monitor/dashboard/test_review_shell.py``'s
    ``_wait_for_links`` docstring, issue #130): ``topology-page`` — and even
    the node cards — mount a beat before any edge path exists, so waiting on
    node presence alone would screenshot an edgeless canvas. Wait on an
    actually-rendered edge path element, then poll until the rendered count
    stops growing (see ``_wait_for_edges_settled``) instead of guessing a
    flat delay, before the shot.

    Legibility: the fit reserves a fixed 256px under the graph for the key
    (``FIT_PADDING`` in TopologyPage.tsx), so at the default viewport the
    fitted graph is a thumbnail. A taller viewport lets the fit grow until
    the graph's width binds, which brings node labels up to reading size,
    and the clip is the nodes plus the key — not the empty canvas around
    them.
    """
    fixture = REPO_ROOT / "web" / "fixtures" / "isp-core.json"
    edge_selector = '[data-testid^="topo-link-"] path.react-flow__edge-path'
    page = browser.new_page(viewport={"width": _VIEWPORT["width"], "height": 1100})
    page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
    page.goto(harness.url)
    page.locator('[data-testid="import-input"]').set_input_files(fixture)
    page.locator('[data-testid="topology-page"]').wait_for()
    page.locator(edge_selector).first.wait_for()
    _wait_for_edges_settled(page, edge_selector)
    nodes = page.locator(".react-flow__node")
    _clip_shot(
        page,
        "dashboard-topology.png",
        *[nodes.nth(i) for i in range(nodes.count())],
        _tid(page, "topo-legend"),
        pad=16,
    )
    page.close()


def _capture_dashboard(browser, harness) -> None:  # noqa: ANN001 — deferred imports
    """Photograph the review shell fed through the Import front door.

    The review shell has no server-seeded page to open (see the module
    docstring): it boots to an empty Import front door and is fed
    client-side, exactly the way the browser e2e suite does — see
    ``tests/e2e/monitor/dashboard/test_review_shell.py::_import_fixture``.
    The harness's collector stays empty; ``web/fixtures/kitchen-sink.json``
    supplies every session, host, element, event and metric these captures
    show. Every later route is a same-document hash navigation, so the
    imported data survives it. ``/`` is the topology landing (spec
    2026-07-17 topology-default-view), so the grid needs an explicit
    ``#/hosts`` hop.

    Stills, each clipped to its feature (a subject page runs on into
    thousands of pixels of log table below the charts):

    - the fleet grid overview;
    - ``dashboard-metrics``: one host's first three synced charts, event
      markers and spans across all of them;
    - ``dashboard-review-charts``: that host's subject page — series tree
      and chip filters beside the top of its chart stack;
    - ``dashboard-element``: an element subject (chassis-a) narrowed to its
      CPU chart, one line per member host;
    - ``dashboard-events``: the events slide-over's list.
    """
    fixture = REPO_ROOT / "web" / "fixtures" / "kitchen-sink.json"
    page = browser.new_page(viewport=_VIEWPORT)
    page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
    page.goto(harness.url)
    _tid(page, "import-input").set_input_files(fixture)
    _tid(page, "review-bar").wait_for()
    page.goto(f"{harness.url}#/hosts")
    _tid(page, "host-tile-chassis-a_lc1").wait_for()
    # The first three element groups — a three-host chassis and two
    # single-host elements — cut at the tiles' right edge (each group's
    # health-rollup bar and header run the full page width).
    tiles = _tid(page, "element-section-chassis-a").locator('[data-testid^="host-tile-"]')
    _clip_shot(
        page,
        "dashboard-review.png",
        _tid(page, "element-section-chassis-a"),
        _tid(page, "element-section-edge-gw"),
        right=_page_right(tiles.last),
    )

    page.goto(f"{harness.url}#/host/chassis-a_lc1")
    _tid(page, "chart-panel-cpu").locator("canvas").wait_for()
    _tid(page, "chart-panel-net").locator("canvas").wait_for()
    page.wait_for_timeout(400)  # let ECharts finish its initial render pass
    sections = _tid(page, "chart-stack").locator(":scope > section")
    _clip_shot(page, "dashboard-metrics.png", sections.nth(0), sections.nth(2))
    # The series panel is a full-height column beside the stack, so the
    # clip is cut at the second chart's bottom edge.
    _clip_shot(
        page,
        "dashboard-review-charts.png",
        _tid(page, "subject-title"),
        _tid(page, "series-panel"),
        sections.nth(1),
        bottom=_page_bottom(sections.nth(1)),
    )

    page.goto(f"{harness.url}#/host/chassis-a")
    _tid(page, "subject-title").filter(has_text="chassis-a").wait_for()
    _tid(page, "chart-chips").get_by_text("CPU %").click()
    _tid(page, "chart-cpu").wait_for()
    _tid(page, "chart-mem").wait_for(state="detached")
    page.wait_for_timeout(400)  # the stack re-renders to one chart
    _clip_shot(
        page,
        "dashboard-element.png",
        _tid(page, "subject-title"),
        _tid(page, "series-panel"),
        _tid(page, "chart-stack"),
    )

    _tid(page, "events-button").click()
    panel = _tid(page, "events-panel")
    panel.locator("ul").wait_for()
    page.wait_for_timeout(400)  # the slide-over's enter transition
    # The panel spans the viewport's full height; clip to its content, with
    # no pad sideways (that would pull in the dimmed page behind it).
    _clip_shot(
        page,
        "dashboard-events.png",
        panel,
        pad=0,
        bottom=_page_bottom(panel.locator("ul")) + 20,
    )
    page.close()


def _capture(harness) -> None:  # noqa: ANN001 — DashboardHarness import is deferred
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError as e:
            raise SystemExit(
                f"docs media: Chromium is not installed ({e}).\n"
                "The dev environment provides it — run `make browsers` "
                "(or `make dev`). To ship degraded docs in an emergency, "
                "set OTTO_DOCS_MEDIA=placeholder."
            ) from e
        try:
            _capture_topology(browser, harness)
            _capture_dashboard(browser, harness)

            # Still shot of the coverage HTML report (same fixture the
            # report_browser Playwright suite pins).
            _capture_coverage_report(browser)
        finally:
            browser.close()


def main() -> None:
    """Resolve the mode, then capture (or skip, or write placeholders)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["auto", "force", "placeholder"],
        default=os.environ.get("OTTO_DOCS_MEDIA", "auto"),
        help="auto: regenerate when stale; force: always; placeholder: no browser",
    )
    mode = parser.parse_args().mode

    if mode == "placeholder":
        _write_placeholders()
        return

    digest = _input_digest()
    if mode == "auto" and _is_fresh(digest):
        print("docs media: up to date (stamp matches) — skipping capture", flush=True)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    from tests._fixtures._dashboard_harness import DashboardHarness
    from tests._fixtures._fake_collector import FakeCollector

    harness = DashboardHarness(FakeCollector()).start()
    try:
        _capture(harness)
    finally:
        harness.stop()

    STAMP.write_text(digest + "\n")
    names = ", ".join(ARTIFACTS)
    print(f"docs media: captured {names} in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
