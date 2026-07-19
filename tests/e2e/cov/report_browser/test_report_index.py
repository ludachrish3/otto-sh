"""Pins the report index in a real browser: the built JS actually loads and
sorts, tier columns render, and no page errors fire. These assertions are
the cutover guard — a broken covreport.js path would pass string-level
tests and fail only here."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def _open_index(page: Page, report_dir: Path) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto((report_dir / "index.html").as_uri())
    expect(page.locator("table.files-table")).to_be_visible()
    return errors


def _file_column(page: Page) -> list[str]:
    return page.locator("table.files-table tbody tr td:first-child").all_inner_texts()


def test_index_renders_without_page_errors(page: Page, report_dir: Path) -> None:
    errors = _open_index(page, report_dir)
    expect(page.locator("h1")).to_have_text("otto example product")
    assert errors == []


def test_tier_columns_render(page: Page, report_dir: Path) -> None:
    _open_index(page, report_dir)
    # `all_inner_texts()` returns rendered (CSS-applied) text, and
    # report.css uppercases `.files-table th` via text-transform — match
    # case-insensitively rather than the title-case template source.
    headers = [h.lower() for h in page.locator("table.files-table thead th").all_inner_texts()]
    assert any("system %" in h for h in headers)
    assert any("unit %" in h for h in headers)


def test_click_sorts_files_and_marks_header(page: Page, report_dir: Path) -> None:
    """The real built bundle sorts the real table — the JS-loads pin.

    Display paths are deterministic (`--prefix` via the fixture), so the
    assertions are exact strings, not order relations."""
    _open_index(page, report_dir)
    file_header = page.locator("table.files-table thead th", has_text="File")
    file_header.click()
    assert _file_column(page) == ["product/main.c", "product/utils.c"]
    assert "sort-asc" in (file_header.get_attribute("class") or "")

    file_header.click()
    assert _file_column(page) == ["product/utils.c", "product/main.c"]
    assert "sort-desc" in (file_header.get_attribute("class") or "")


def test_numeric_sort_uses_data_sort(page: Page, report_dir: Path) -> None:
    """Line % sorts numerically: utils.c (partial) before main.c (full) asc."""
    _open_index(page, report_dir)
    page.locator("table.files-table thead th", has_text="Line %").click()
    assert _file_column(page) == ["product/utils.c", "product/main.c"]
