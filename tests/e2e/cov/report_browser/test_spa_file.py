"""Pins the SPA file page in a real browser: branch pills for every state,
per-line row-precedence classes, the breadcrumb back to the directory page,
and the context expander's run chips. Ported from the Jinja-era
``test_report_file.py`` (branch pills render all states, pill tooltip names
block/branch, breadcrumb returns to the index) onto the SPA DOM, plus the
row-precedence/context-expander pins the Task 8 fixture upgrade (stale/
aging/excluded lines, a multi-host run) makes reachable.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def _open_file(page: Page, report_dir: Path, display_path: str) -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#/coverage/{display_path}")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()


def test_branch_pills_render_all_states_with_block_tooltip(page: Page, report_dir: Path) -> None:
    """main.c line 4 (``if (a > 0 && b > 0)``) carries all three pill
    states: taken, not-taken, unreachable."""
    _open_file(page, report_dir, "product/main.c")
    pills = page.locator('[data-testid="code-row-4"] [data-testid="branch-pill"]')
    expect(pills).to_have_count(3)
    titles = pills.evaluate_all("els => els.map(el => el.getAttribute('title'))")
    assert titles
    assert all(t is not None and "block" in t for t in titles)


def test_row_precedence_classes_present_on_main_c(page: Page, report_dir: Path) -> None:
    """Row precedence (Global Constraints): a tier hit (line 4, t-system)
    beats aging (line 9, s-aging) beats stale (line 6, s-stale) — all three
    reachable on this one file, matching the fixture's dedicated lines."""
    _open_file(page, report_dir, "product/main.c")
    covered = page.locator('[data-testid="code-row-4"]')
    stale = page.locator('[data-testid="code-row-6"]')
    aging = page.locator('[data-testid="code-row-9"]')

    expect(covered).to_have_class(re.compile(r"\bt-system\b"))
    expect(stale).to_have_class(re.compile(r"\bs-stale\b"))
    expect(aging).to_have_class(re.compile(r"\bs-aging\b"))
    # Tier tint is an inline style (borderLeftColor), data-driven per spec —
    # never hard-coded — so the covered row also carries it as a style.
    style = covered.get_attribute("style") or ""
    assert "border-left-color" in style.replace(" ", "").lower()


def test_excluded_line_class_on_utils_c(page: Page, report_dir: Path) -> None:
    """The one LCOV_EXCL_LINE-marked line in the fixture (utils.c line 6)."""
    _open_file(page, report_dir, "product/utils.c")
    expect(page.locator('[data-testid="code-row-6"]')).to_have_class(re.compile(r"\bs-excl\b"))


def test_context_expander_opens_and_shows_host_pill(page: Page, report_dir: Path) -> None:
    """main.c line 3 is credited to the "router-a" system run — expanding
    its context panel surfaces that host as a pill."""
    _open_file(page, report_dir, "product/main.c")
    page.locator('[data-testid="code-expander-3"]').click()
    panel = page.locator('[data-testid="ctx-panel-3"]')
    expect(panel).to_be_visible()
    host_pills = panel.locator('[data-testid="host-pill"]')
    expect(host_pills.first).to_be_visible()
    assert "router-a" in host_pills.all_inner_texts()


def test_breadcrumb_home_returns_to_directory_page(page: Page, report_dir: Path) -> None:
    _open_file(page, report_dir, "product/main.c")
    page.locator('[data-testid="breadcrumbs"] a', has_text="otto example product").click()
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()


def test_ctx_panel_shows_struck_revoked_chip_for_stale_line(page: Page, report_dir: Path) -> None:
    """main.c line 6 is smoke-old's fully-revoked stale mark (no live
    hits, ``stale_run=[run_smoke_old]``) — its run chip shows the
    struck-through "revoked" label instead of a hit count."""
    _open_file(page, report_dir, "product/main.c")
    page.locator('[data-testid="code-expander-6"]').click()
    panel = page.locator('[data-testid="ctx-panel-6"]')
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("smoke-old")
    revoked_chip = panel.locator('[data-testid="run-chip"]')
    expect(revoked_chip).to_contain_text("revoked")
    count_span = revoked_chip.locator("span").last
    expect(count_span).to_have_class(re.compile(r"\bline-through\b"))


def test_ctx_panel_shows_aging_suffix_for_aging_line(page: Page, report_dir: Path) -> None:
    """main.c line 9 is field bring-up's aging manual claim (``run_hits =
    {run_field: 3}``, ``run_field.aging = True``) — its run chip reads
    "x 3 . aging", not struck (it has live evidence, just old)."""
    _open_file(page, report_dir, "product/main.c")
    page.locator('[data-testid="code-expander-9"]').click()
    panel = page.locator('[data-testid="ctx-panel-9"]')
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("field bring-up")
    aging_chip = panel.locator('[data-testid="run-chip"]')
    expected = "× 3 · aging"  # noqa: RUF001 -- multiplication sign is the rendered glyph under test
    expect(aging_chip).to_contain_text(expected)


def test_sticky_code_columns_header_stays_pinned_on_scroll(page: Page, report_dir: Path) -> None:
    """Real-browser carry-over (Task 5 note, jsdom can't lay out/scroll):
    FilePage.tsx's ``code-card`` wrapper deliberately uses ``overflow-
    clip`` (not ``overflow-hidden``) so CodeView's ``sticky top-0``
    column-header row sticks to the page viewport, not a local scroll
    container — verify it actually does. A short fixture file doesn't fill
    a normal viewport, so this shrinks the viewport height (not the
    fixture) to force real page-level scroll."""
    page.set_viewport_size({"width": 1000, "height": 300})
    _open_file(page, report_dir, "product/main.c")
    header = page.locator('[data-testid="code-columns"]')
    row1 = page.locator('[data-testid="code-row-1"]')
    header_top_before = header.evaluate("el => el.getBoundingClientRect().top")
    row1_top_before = row1.evaluate("el => el.getBoundingClientRect().top")
    assert row1_top_before > header_top_before  # row 1 starts below the header, pre-scroll

    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    header_top_after = header.evaluate("el => el.getBoundingClientRect().top")
    row1_top_after = row1.evaluate("el => el.getBoundingClientRect().top")

    viewport_height = page.evaluate("window.innerHeight")
    assert 0 <= header_top_after < viewport_height  # stuck at/near the viewport top
    assert row1_top_after < header_top_after  # scrolled out from under the sticky header


def test_no_page_level_horizontal_scroll_for_fixture_lines(page: Page, report_dir: Path) -> None:
    """Task 5 carry-over note: CodeView's source cell (``.cv-src``) is
    ``overflow-x-auto`` inside a CSS Grid ``1fr`` track — per the CSS Grid
    spec, a grid item's automatic minimum size is 0 (not its content's
    min-content width) whenever its own ``overflow`` isn't ``visible``, so
    an overlong line scrolls WITHIN that cell instead of forcing the page
    itself wider. The fixture's longest line is short (~40 chars), so this
    only confirms no page-level horizontal scroll exists TODAY — see
    task-9-report.md for why the CSS mechanism generalizes past what this
    fixture can exercise, and why injecting a synthetic wide row (which
    the brief rules out) isn't needed to trust that."""
    _open_file(page, report_dir, "product/main.c")
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width
