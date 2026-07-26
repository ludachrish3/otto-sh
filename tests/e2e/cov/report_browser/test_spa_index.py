"""Pins the SPA directory page in a real browser: the built covapp bundle
actually boots, tier columns render in precedence order, the tree sorts
numerically, file rows navigate, and the runs disclosure lists the fixture's
run table. Ported from the Jinja-era ``test_report_index.py`` (index boots
without page errors, tier columns render, header-click sorts, numeric sort)
onto the SPA DOM — a broken ``covapp.js`` boot would pass string-level tests
and fail only here.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def _goto(page: Page, report_dir: Path, hash_route: str = "/coverage") -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#{hash_route}")


def _row_testids(page: Page) -> list[str]:
    return page.locator('[role="treeitem"]').evaluate_all(
        "els => els.map(el => el.getAttribute('data-testid'))"
    )


def test_boots_without_pageerrors_and_shows_project_name(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir)
    # Root only has one child ("product/") — its tree row is the boot pin.
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    expect(page.locator("h1")).to_have_text("otto example product")


def test_tier_columns_render_in_tier_order(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir)
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    testids = page.locator('[data-testid^="tree-col-tier:"]').evaluate_all(
        "els => els.map(el => el.getAttribute('data-testid'))"
    )
    assert testids == ["tree-col-tier:system", "tree-col-tier:unit", "tree-col-tier:manual"]


def test_line_pct_header_click_sorts_and_flips_row_order(page: Page, report_dir: Path) -> None:
    """Display paths are deterministic (``--prefix`` via the fixture):
    utils.c (1/2 = 50%) sorts before main.c (7/9 ~= 78%) ascending, and
    after it descending."""
    _goto(page, report_dir, "/coverage/product")
    expect(page.locator('[data-testid="tree-row-file:product/utils.c"]')).to_be_visible()

    page.locator('[data-testid="tree-col-line"]').click()
    assert _row_testids(page) == [
        "tree-row-file:product/utils.c",
        "tree-row-file:product/main.c",
    ]

    page.locator('[data-testid="tree-col-line"]').click()
    assert _row_testids(page) == [
        "tree-row-file:product/main.c",
        "tree-row-file:product/utils.c",
    ]


def test_file_name_click_routes_to_file_and_code_rows_render(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage/product")
    page.locator('[data-testid="name-file:product/main.c"]').click()
    assert page.url.endswith("#/coverage/product/main.c")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()


def test_runs_disclosure_lists_nightly_full_and_remap_marker(page: Page, report_dir: Path) -> None:
    """The fixture's two-host "nightly-full" system runs, and the "✎
    remapped" marker on the dirty-remapped "field bring-up" manual run."""
    _goto(page, report_dir)
    disclosure = page.locator('[data-testid="runs-disclosure"]')
    disclosure.get_by_role("button").click()
    expect(disclosure).to_contain_text("nightly-full")
    expect(disclosure).to_contain_text("✎ remapped")
