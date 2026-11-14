"""The source view's Columns menu at narrow and wide windows (issue #888).

Below ``NARROW_MAX_WIDTH`` the source view defaults to line numbers plus the
Total hits column only, giving the source text its room back; the reader's own
choices then survive resizes and navigation. Runs on the REAL built bundle in
every engine, because widths and layout are what these tests pin.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.cov.report_browser.test_spa_layout import NARROW_MAX_WIDTH, VIEWPORT_HEIGHT

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

MAIN_ROUTE = "/coverage/product/main.c"
RING_ROUTE = "/coverage/lib/ring.c"
EXPANDER_PX = 66
ROW_BORDER_PX = 3
MIN_ROOM_BACK_PX = 270
RING_LINE_3 = "struct ring {"
SHOW_ITEMS = ["Tickets", "Line numbers", "Branches", "Hit counts"]


def _open(page: Page, report_dir: Path, width: int, route: str = MAIN_ROUTE) -> None:
    page.set_viewport_size({"width": width, "height": VIEWPORT_HEIGHT})
    page.goto((report_dir / "index.html").as_uri() + f"#{route}")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()


def _headers(page: Page) -> list[str]:
    """The non-empty column headers as written (CSS uppercases them on screen)."""
    texts = page.locator('[data-testid="code-columns"] > div').all_text_contents()
    return [t.strip() for t in texts if t.strip()]


def _menu(page: Page) -> Locator:
    return page.get_by_role("menu", name="Columns")


def _open_menu(page: Page) -> Locator:
    page.locator('[data-testid="columns-menu"]').click()
    menu = _menu(page)
    expect(menu).to_be_visible()
    return menu


def _close_menu(page: Page) -> None:
    page.keyboard.press("Escape")
    expect(_menu(page)).to_be_hidden()


def _set_checked(menu: Locator, name: str, want: bool) -> None:
    item = menu.get_by_role("menuitemcheckbox", name=name)
    if (item.get_attribute("aria-checked") == "true") != want:
        item.click()
    expect(item).to_have_attribute("aria-checked", "true" if want else "false")


def _choose_mode(menu: Locator, name: str) -> None:
    item = menu.get_by_role("menuitemradio", name=name)
    item.click()
    expect(item).to_have_attribute("aria-checked", "true")


def _source_width(page: Page, line: int = 3) -> float:
    """Border-box width of the row's source cell (the last-but-one child)."""
    row = page.locator(f'[data-testid="code-row-{line}"]')
    box = row.locator(":scope > div").nth(-2).bounding_box()
    assert box is not None
    return box["width"]


def test_narrow_boundary(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, NARROW_MAX_WIDTH - 1)
    expect(page.locator('[data-testid="code-col-hits"]')).to_be_visible()
    assert _headers(page) == ["#", "Hits", "Source"]

    page.set_viewport_size({"width": NARROW_MAX_WIDTH, "height": VIEWPORT_HEIGHT})
    expect(page.locator('[data-testid="code-col-branches"]')).to_be_visible()
    headers = _headers(page)
    assert "Hits" not in headers
    assert headers[0] == "#"
    assert headers[-2:] == ["Branches", "Source"]


def test_room_back_at_600(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, 600)
    expect(page.locator('[data-testid="code-col-hits"]')).to_be_visible()
    default_width = _source_width(page)

    menu = _open_menu(page)
    for name in SHOW_ITEMS:
        _set_checked(menu, name, True)
    _choose_mode(menu, "Per tier")
    _close_menu(page)
    expect(page.locator('[data-testid="code-col-branches"]')).to_be_visible()
    expect(page.locator('[data-testid="code-col-hits"]')).to_have_count(0)
    full_width = _source_width(page)

    assert default_width - full_width >= MIN_ROOM_BACK_PX, (default_width, full_width)


def test_everything_hidden_at_600(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, 600)
    menu = _open_menu(page)
    for name in SHOW_ITEMS:
        _set_checked(menu, name, False)
    _close_menu(page)
    expect(page.locator('[data-testid^="code-col-"]')).to_have_count(0)

    row_box = page.locator('[data-testid="code-row-3"]').bounding_box()
    assert row_box is not None
    expected = row_box["width"] - EXPANDER_PX - ROW_BORDER_PX
    actual = _source_width(page)
    assert abs(actual - expected) <= 1, (actual, expected)


def test_choices_survive_navigation(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, 1000)
    menu = _open_menu(page)
    _choose_mode(menu, "Total")
    _close_menu(page)
    expect(page.locator('[data-testid="code-col-hits"]')).to_be_visible()

    page.evaluate("route => { window.location.hash = route; }", RING_ROUTE)
    # Line 3 of ring.c (tests/_fixtures/_report_fixture.py); main.c has no
    # such line, so this waits for the new page, not the departing one.
    expect(page.locator('[data-testid="code-row-3"]')).to_contain_text(RING_LINE_3)
    expect(page.locator('[data-testid="code-col-hits"]')).to_be_visible()
    assert "Hits" in _headers(page)


def test_resize_keeps_chosen(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, 1000)
    menu = _open_menu(page)
    _set_checked(menu, "Branches", False)
    _close_menu(page)
    expect(page.locator('[data-testid="code-col-branches"]')).to_have_count(0)

    page.set_viewport_size({"width": 700, "height": VIEWPORT_HEIGHT})
    expect(page.locator('[data-testid="code-col-hits"]')).to_be_visible()
    expect(page.locator('[data-testid="code-col-branches"]')).to_have_count(0)

    page.set_viewport_size({"width": 1000, "height": VIEWPORT_HEIGHT})
    expect(page.locator('[data-testid="code-col-hits"]')).to_have_count(0)
    expect(page.locator('[data-testid="code-col-branches"]')).to_have_count(0)


def test_mode_items_render_as_one_switch(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, 1000)
    menu = _open_menu(page)
    per_tier = menu.get_by_role("menuitemradio", name="Per tier")
    total = menu.get_by_role("menuitemradio", name="Total")
    a, b = per_tier.bounding_box(), total.bounding_box()
    assert a is not None
    assert b is not None
    assert abs(a["y"] - b["y"]) <= 1
    assert b["x"] > a["x"]
    gap = b["x"] - (a["x"] + a["width"])
    assert -1 <= gap <= 1, gap  # touch, or overlap by at most 1 px

    def fill(item: Locator, item_name: str) -> str:
        return item.get_by_text(item_name, exact=True).first.evaluate(
            "e => getComputedStyle(e).backgroundColor"
        )

    assert fill(per_tier, "Per tier") != fill(total, "Total")

    menu.get_by_role("menuitemcheckbox", name="Hit counts").focus()
    page.keyboard.press("ArrowDown")
    expect(per_tier).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(total).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("menuitem", name="Reset columns")).to_be_focused()
