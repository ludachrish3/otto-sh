"""Pins the report header and the runs table at 820-1000 px (issue #888).

At these widths the fully populated header (search hit, context, ticket and
product pins, overrides badge) must wrap rather than overlap or leave the
viewport, and the runs table must scroll inside its own scrollport rather
than clipping cells or widening the page. Phones (< 820 px) and the
CodeView gutters are out of scope here.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.cov.report_browser._support import swap_payload_strings

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

# Source of truth: NARROW_MAX_WIDTH in web/src/covapp/sourceColumns.tsx.
NARROW_MAX_WIDTH = 820
WIDTHS = [NARROW_MAX_WIDTH, 960, 1000]
VIEWPORT_HEIGHT = 800

# Context / ticket / product / search values come from
# tests/_fixtures/_report_fixture.py (router-a/firmware, PROJ-204, firmware,
# product/main.c's checked_add).
FOCUS_CTX = "router-a/firmware"
POPULATED_ROUTE = (
    "/coverage/product/main.c?lines=3&q=checked_add"
    f"&ctx={quote(FOCUS_CTX, safe='')}&ticket=PROJ-204&product=firmware"
)
# The single-line header controls (chips and pills); ``brand``/``app-nav``
# are groups, checked for overlap and containment but not for height.
CHIP_TESTIDS = ["search-pill", "focus-chip", "ticket-chip", "product-chip", "overrides-badge"]
CONTROL_TESTIDS = [
    "brand",
    "app-nav",
    *CHIP_TESTIDS,
    "theme-toggle",
    "search-trigger",
    "ticket-search",
    "appbar-menu",
]
MAX_CHIP_HEIGHT_PX = 28

# Long values swapped into the report's payload (before the app reads it) so
# the long-name cases exercise truncation, not the fixture's short names.
LONG_TITLE = "otto example product with a deliberately very long report title " * 5
LONG_PRODUCT = "firmware-" + "p" * 70
LONG_TICKET = "PROJ-204-" + "t" * 70
LONG_QUERY = "checked_add|" + "q" * 70
LONG_BOARD = "router-a-" + "b" * 60
LONG_LAB = "lab1-" + "l" * 60
LONG_TIER = "hardwarequalification"
# The header swap rewrites "firmware" inside the run labels too, so the
# focused context's label is the long one.
LONG_CTX = FOCUS_CTX.replace("firmware", LONG_PRODUCT)
LONG_ROUTE = (
    f"/coverage/product/main.c?lines=3&re=1&q={quote(LONG_QUERY)}"
    f"&ctx={quote(LONG_CTX, safe='')}&ticket={LONG_TICKET}&product={LONG_PRODUCT}"
)
MAX_CHIP_WIDTH_PX = 192  # max-w-48

# Chip testid -> the full value its label span's `title` must carry.
CHIP_LABELS = {"ticket-chip": LONG_TICKET, "product-chip": LONG_PRODUCT}

SCROLLPORT = '[data-testid="runs-card"] > div.overflow-x-auto'


def _goto(page: Page, report_dir: Path, width: int, hash_route: str) -> None:
    page.set_viewport_size({"width": width, "height": VIEWPORT_HEIGHT})
    page.goto((report_dir / "index.html").as_uri() + f"#{hash_route}")


def _inject_long_values(
    page: Page, *, header: bool = False, cells: bool = False, tier: bool = False
) -> None:
    """Rewrite the payload as ``window.__OTTO_COV__`` is assigned."""
    swaps: list[tuple[str, str]] = []
    if header:
        swaps += [
            ("otto example product", LONG_TITLE),
            ("firmware", LONG_PRODUCT),
            ("PROJ-204", LONG_TICKET),
        ]
    if cells:
        swaps += [('"router-a"', json.dumps(LONG_BOARD)), ('"lab1"', json.dumps(LONG_LAB))]
    if tier:
        swaps += [('"Unit"', json.dumps(LONG_TIER))]
    swap_payload_strings(page, swaps)


def _boxes(page: Page, testids: list[str], *, required: list[str]) -> dict[str, dict]:
    """Every visible control's box, read in ONE frame.

    The ``?lines=`` route scrolls its row into view about when the chips
    appear, so boxes read one round trip apart can straddle that scroll and
    pair a pre-scroll box with a post-scroll one (#963). One ``evaluate``
    reads them all from the same layout, once every required control is
    up (the search pill waits on its search hit).
    """
    for testid in required:
        control = page.locator(f'[data-testid="app-bar"] [data-testid="{testid}"]').first
        expect(control).to_be_visible()
    found: dict[str, dict | None] = page.evaluate(
        """(testids) => Object.fromEntries(testids.map((id) => {
            const el = document.querySelector(
                `[data-testid="app-bar"] [data-testid="${id}"]`);
            if (!el || getComputedStyle(el).visibility === "hidden") return [id, null];
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return [id, null];
            return [id, {x: r.x, y: r.y, width: r.width, height: r.height}];
        }))""",
        testids,
    )
    for testid in required:
        assert found[testid] is not None, f"{testid} missing from the header"
    return {testid: box for testid, box in found.items() if box is not None}


def _intersect(a: dict, b: dict) -> bool:
    return (
        a["x"] < b["x"] + b["width"] - 0.5
        and b["x"] < a["x"] + a["width"] - 0.5
        and a["y"] < b["y"] + b["height"] - 0.5
        and b["y"] < a["y"] + a["height"] - 0.5
    )


def _scrollport(page: Page) -> Locator:
    return page.locator(SCROLLPORT)


@pytest.mark.parametrize("width", WIDTHS)
def test_header_controls_never_overlap(page: Page, report_dir: Path, width: int) -> None:
    _goto(page, report_dir, width, POPULATED_ROUTE)
    # The populated header really is populated.
    boxes = _boxes(page, CONTROL_TESTIDS, required=["brand", "app-nav", *CHIP_TESTIDS])
    _assert_header_sound(boxes, width)


def _assert_header_sound(boxes: dict[str, dict], width: int) -> None:
    names = list(boxes)
    for i, a in enumerate(names):
        box = boxes[a]
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= width, f"{a} leaves the viewport: {box}"
        for b in names[i + 1 :]:
            assert not _intersect(box, boxes[b]), f"{a} overlaps {b}: {box} {boxes[b]}"
    for testid in CHIP_TESTIDS:
        if testid in boxes:
            assert boxes[testid]["height"] <= MAX_CHIP_HEIGHT_PX, f"{testid} wraps: {boxes[testid]}"
            assert boxes[testid]["width"] <= MAX_CHIP_WIDTH_PX + 0.5, (
                f"{testid} wide: {boxes[testid]}"
            )


@pytest.mark.parametrize("width", WIDTHS)
def test_header_with_long_names_stays_sound(page: Page, report_dir: Path, width: int) -> None:
    _inject_long_values(page, header=True, cells=False)
    # The ticket's chunk is fetched by id; give the real PROJ-204 chunk a copy
    # under the long id so the renamed ticket resolves like the original. The
    # report is served from file://, which page.route cannot intercept in
    # WebKit, so the copy is written beside the original. report_dir is
    # session-scoped; one extra file in it is harmless to the other tests.
    tickets = report_dir / "cov_data" / "tickets"
    chunk = (tickets / "PROJ-204.js").read_text()
    (tickets / f"{LONG_TICKET}.js").write_text(chunk.replace("PROJ-204", LONG_TICKET))
    _goto(page, report_dir, width, LONG_ROUTE)
    boxes = _boxes(page, CONTROL_TESTIDS, required=["brand", "app-nav", *CHIP_TESTIDS])
    _assert_header_sound(boxes, width)
    for chip, full in CHIP_LABELS.items():
        label = page.locator(f'[data-testid="{chip}"] [title]').first
        assert label.get_attribute("title") == full
    pill_label = page.locator('[data-testid="search-pill"] [title]').first
    assert pill_label.get_attribute("title") == LONG_QUERY
    # The report title truncates (min-w-0 truncate) and keeps its full text
    # in `title`.
    title_span = page.locator('[data-testid="brand"] [title]')
    assert title_span.get_attribute("title") == LONG_TITLE
    assert title_span.evaluate("el => el.scrollWidth > el.clientWidth")


@pytest.mark.parametrize("width", [NARROW_MAX_WIDTH])
def test_runs_scrollport_fits_the_page(page: Page, report_dir: Path, width: int) -> None:
    _goto(page, report_dir, width, "/runs")
    port = _scrollport(page)
    expect(port).to_be_visible()
    sizes = port.evaluate(
        "el => ({w: el.getBoundingClientRect().width, sw: el.scrollWidth, cw: el.clientWidth})"
    )
    assert sizes["w"] <= width
    assert sizes["sw"] > sizes["cw"]


@pytest.mark.parametrize("width", WIDTHS)
def test_runs_scroll_reveals_last_column(page: Page, report_dir: Path, width: int) -> None:
    _goto(page, report_dir, width, "/runs")
    port = _scrollport(page)
    expect(port).to_be_visible()
    port.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    cells = page.evaluate(
        """() => {
            const port = document.querySelector('[data-testid="runs-card"] > div.overflow-x-auto');
            const head = port.firstElementChild.firstElementChild.lastElementChild;
            const row = document.querySelector('[data-testid^="run-row-"]');
            const rect = (el) => {
                const r = el.getBoundingClientRect();
                return {l: r.left, r: r.right};
            };
            return {head: rect(head), cell: rect(row.lastElementChild), vw: window.innerWidth};
        }"""
    )
    for key in ("head", "cell"):
        assert cells[key]["l"] >= 0, cells
        assert cells[key]["r"] <= cells["vw"] + 0.5, cells
    assert abs(cells["head"]["l"] - cells["cell"]["l"]) <= 1, cells


@pytest.mark.parametrize("width", [NARROW_MAX_WIDTH])
def test_runs_expanded_details_scroll_with_rows(page: Page, report_dir: Path, width: int) -> None:
    _goto(page, report_dir, width, "/runs")
    row = page.locator(f'[data-testid="run-row-{FOCUS_CTX}"]')
    row.click()
    detail = page.locator(f'[data-testid="run-detail-{FOCUS_CTX}"]')
    expect(detail).to_be_visible()

    def left_edge(loc: Locator) -> float:
        box = loc.bounding_box()
        assert box is not None
        return box["x"]

    detail_x0, row_x0 = left_edge(detail), left_edge(row)
    _scrollport(page).evaluate("el => { el.scrollLeft = 120; }")
    detail_dx = detail_x0 - left_edge(detail)
    row_dx = row_x0 - left_edge(row)
    assert row_dx > 0
    assert detail_dx == pytest.approx(row_dx, abs=1)


@pytest.mark.parametrize("long_values", [False, True], ids=["fixture", "long-board-labs"])
@pytest.mark.parametrize("width", WIDTHS)
def test_runs_cells_not_clipped(
    page: Page, report_dir: Path, width: int, long_values: bool
) -> None:
    if long_values:
        _inject_long_values(page, header=False, cells=True)
    _goto(page, report_dir, width, "/runs")
    expect(page.locator('[data-testid^="run-row-"]').first).to_be_visible()
    clipped = page.evaluate(
        """() => {
            const out = [];
            for (const row of document.querySelectorAll('[data-testid^="run-row-"]')) {
                [...row.children].slice(1).forEach((cell, i) => {
                    for (const el of [cell, ...cell.querySelectorAll('*')]) {
                        if (el.scrollWidth > el.clientWidth + 1) {
                            out.push(
                                `${row.dataset.testid} col${i + 1} <${el.tagName}> `
                                + `${el.scrollWidth}>${el.clientWidth}`
                            );
                        }
                    }
                });
            }
            return out;
        }"""
    )
    assert clipped == []


@pytest.mark.parametrize("width", WIDTHS)
def test_runs_long_tier_name_stays_in_its_track(page: Page, report_dir: Path, width: int) -> None:
    _inject_long_values(page, tier=True)
    _goto(page, report_dir, width, "/runs")
    chip = page.locator('[data-testid="run-row-ci-01/firmware"] [data-testid="tier-chip"]')
    expect(chip).to_be_visible()
    assert LONG_TIER in (chip.text_content() or "")
    # The chip's own box against the NEXT cell: a self-scrollWidth check can
    # pass while the whole chip spills over its neighbour. The chip sits in
    # the row's tier cell (one chip per tier the context has captures in).
    rects = chip.evaluate(
        """el => {
            const r = el.getBoundingClientRect();
            const cell = el.closest('[data-testid="run-tiers"]');
            const n = cell.nextElementSibling.getBoundingClientRect();
            return {right: r.right, nextLeft: n.left};
        }"""
    )
    assert rects["right"] <= rects["nextLeft"] + 0.5, rects


@pytest.mark.parametrize("width", [NARROW_MAX_WIDTH, 960])
def test_runs_expanded_details_hold_long_values(page: Page, report_dir: Path, width: int) -> None:
    _inject_long_values(page, cells=True)
    _goto(page, report_dir, width, "/runs")
    page.locator(f'[data-testid="run-row-{FOCUS_CTX}"]').click()
    detail = page.locator(f'[data-testid="run-detail-{FOCUS_CTX}"]')
    expect(detail).to_be_visible()
    problems = detail.evaluate(
        """el => {
            const out = [];
            const kids = [...el.children].filter(c => c.tagName === 'DIV');
            const secs = kids.map(s => s.getBoundingClientRect());
            kids.forEach((sec, i) => {
                const box = secs[i];
                for (const d of sec.querySelectorAll('*')) {
                    const r = d.getBoundingClientRect();
                    if (r.width && (r.right > box.right + 0.5 || r.left < box.left - 0.5)) {
                        out.push(`section ${i} <${d.tagName}> ${r.left}-${r.right} `
                            + `outside ${box.left}-${box.right}`);
                    }
                }
                if (i > 0 && box.left < secs[i - 1].right - 0.5) {
                    out.push(`section ${i} overlaps ${i - 1}`);
                }
            });
            return out;
        }"""
    )
    assert problems == []


def test_runs_scrollport_is_keyboard_reachable(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, 960, "/runs")
    port = _scrollport(page)
    expect(port).to_be_visible()
    assert port.get_attribute("role") == "region"
    assert port.get_attribute("aria-label") == "Runs table"
    assert port.get_attribute("tabindex") == "0"
    port.focus()
    # Horizontal scrolling by key is ArrowRight (Home/End scroll vertically).
    for _ in range(30):
        page.keyboard.press("ArrowRight")
    # Keyboard scrolling is animated: wait for it to reach the far end.
    page.wait_for_function(
        "el => el.scrollLeft >= el.scrollWidth - el.clientWidth - 1", arg=port.element_handle()
    )
    assert port.evaluate("el => el.scrollLeft") > 0
    status = page.evaluate(
        """() => {
            const port = document.querySelector('[data-testid="runs-card"] > div.overflow-x-auto');
            const head = port.firstElementChild.firstElementChild.lastElementChild;
            const r = head.getBoundingClientRect();
            return {l: r.left, r: r.right, vw: window.innerWidth};
        }"""
    )
    assert status["l"] >= 0
    assert status["r"] <= status["vw"] + 0.5
