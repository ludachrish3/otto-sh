"""Pins the ⌘K search palette in a real browser against the fixture report:
open, type, Enter lands on the file with the row highlighted and every match
marked; ``@`` jumps to a function; the open file's group lists first.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from otto.coverage.renderer.spa_data import mangle_path

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def _open(page: Page, report_dir: Path, route: str) -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#{route}")


def _open_palette(page: Page) -> None:
    page.keyboard.press("Control+K")
    expect(page.get_by_test_id("search-palette")).to_be_visible()


def test_text_search_lands_on_the_file_with_matches_marked(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, "/coverage")
    expect(page.get_by_test_id("tree-row-dir:product")).to_be_visible()
    _open_palette(page)
    page.get_by_role("searchbox").fill("checked_add")
    expect(page.get_by_test_id("search-result-0")).to_be_visible()
    expect(page.get_by_test_id("search-footer")).to_contain_text("2 lines in 1 file")
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("search-palette")).to_be_hidden()
    assert page.url.endswith("#/coverage/product/main.c?lines=3&q=checked_add")
    expect(page.locator('[data-testid="code-row-3"]')).to_have_attribute("data-highlighted", "true")
    expect(page.locator('[data-testid="code-row-3"]')).to_have_attribute("data-match", "true")
    expect(page.locator('[data-testid="code-row-11"]')).to_have_attribute("data-match", "true")
    expect(page.get_by_test_id("search-pill")).to_contain_text("2 matches")
    # The span paint is registered under the contract name (Chromium has the API).
    assert page.evaluate("CSS.highlights.has('otto-search')") is True


def test_function_mode_jumps_to_the_definition(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, "/coverage")
    expect(page.get_by_test_id("tree-row-dir:product")).to_be_visible()
    _open_palette(page)
    page.get_by_role("searchbox").fill("@main")
    expect(page.get_by_test_id("search-result-0")).to_contain_text("main")
    page.keyboard.press("Enter")
    assert page.url.endswith("#/coverage/product/main.c?lines=10")
    expect(page.locator('[data-testid="code-row-10"]')).to_have_attribute(
        "data-highlighted", "true"
    )


def test_open_file_group_lists_first(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, "/coverage/product/utils.c")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    _open_palette(page)
    page.get_by_role("searchbox").fill("int ")
    expect(page.get_by_test_id("search-result-0")).to_be_visible()
    first_group = page.locator('[data-testid^="search-group-"]').first
    # ``chunk`` is the mangled full canonical path (spa_data.mangle_path),
    # not the short display path — under this fixture's tmp_path root that
    # is a long, run-specific string, so the expectation is computed with
    # the same function production code uses rather than hardcoded.
    expected_chunk = mangle_path(report_dir.parent / "product" / "utils.c")
    expect(first_group).to_have_attribute("data-testid", f"search-group-{expected_chunk}")


def test_ctrl_enter_steps_without_closing(page: Page, report_dir: Path) -> None:
    _open(page, report_dir, "/coverage/product/main.c")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    _open_palette(page)
    page.get_by_role("searchbox").fill("return")
    expect(page.get_by_test_id("search-result-1")).to_be_visible()
    page.keyboard.press("Control+Enter")
    expect(page.get_by_test_id("search-palette")).to_be_visible()
    first = page.url
    # Two ArrowDowns, not one: whether react-aria's Autocomplete pre-focused
    # the first row or the palette's first-row fallback took it, two steps
    # land on a different row than the first Ctrl+Enter did (main.c has
    # three "return" lines, so the second row always exists).
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Control+Enter")
    assert page.url != first
    assert "q=return" in page.url


def test_query_survives_navigation(page: Page, report_dir: Path) -> None:
    """Spec §3.2: the palette's query is session-sticky (a module-level
    variable in SearchPalette.tsx), so reopening after landing on a file
    restores the last-typed query rather than resetting to empty."""
    _open(page, report_dir, "/coverage")
    expect(page.get_by_test_id("tree-row-dir:product")).to_be_visible()
    _open_palette(page)
    page.get_by_role("searchbox").fill("checked_add")
    expect(page.get_by_test_id("search-result-0")).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("search-palette")).to_be_hidden()
    assert page.url.endswith("#/coverage/product/main.c?lines=3&q=checked_add")
    page.keyboard.press("Control+K")
    expect(page.get_by_role("searchbox")).to_have_value("checked_add")
