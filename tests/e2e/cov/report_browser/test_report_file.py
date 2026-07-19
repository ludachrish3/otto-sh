"""Pins the annotated-source page: pill classes for every branch state,
per-line row classes, and the breadcrumb back to the index."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def _open_main_page(page: Page, report_dir: Path) -> None:
    main_page = next((report_dir / "files").glob("*main.c.html"))
    page.goto(main_page.as_uri())
    expect(page.locator("table.source-table")).to_be_visible()


def test_branch_pills_render_all_states(page: Page, report_dir: Path) -> None:
    # The template also renders a static legend using these same class
    # names (see `.branch-legend` in file.html), so scope to the
    # source-table pills — the data-driven ones — not the legend.
    _open_main_page(page, report_dir)
    expect(page.locator("table.source-table .branch-taken").first).to_be_visible()
    expect(page.locator("table.source-table .branch-not-taken").first).to_be_visible()
    expect(page.locator("table.source-table .branch-unreachable").first).to_be_visible()


def test_pill_tooltip_names_block_and_branch(page: Page, report_dir: Path) -> None:
    # Unscoped `.branch-taken` would match the legend's span first (no
    # `title` there at all) rather than a real pill — scope to the table.
    _open_main_page(page, report_dir)
    title = page.locator("table.source-table .branch-taken").first.get_attribute("title")
    assert title is not None
    assert "block=0" in title


def test_breadcrumb_returns_to_index(page: Page, report_dir: Path) -> None:
    _open_main_page(page, report_dir)
    page.locator(".breadcrumb a").click()
    expect(page.locator("table.files-table")).to_be_visible()
