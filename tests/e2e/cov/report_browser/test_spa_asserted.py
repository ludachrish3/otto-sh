"""Pins the SPA's manual-testing-overrides surface (Tasks 9-11) in a real
browser: the file page's hollow "asserted" marker sits next to a solid
proven-count on neighboring rows, expanding the asserted line surfaces the
override's own reason, the app bar's overrides badge and ⋮-menu listing
reflect the report's one override entry, toggling "Hide asserted coverage"
recomputes the bench StatsCard row and appends the " · asserted hidden"
scope suffix (never silently), and that toggle composes with a pinned run
focus without crashing.

The fixture's manual-override data (``tests/_fixtures/_report_fixture.py``,
Task 12 addendum — see its module docstring) adds a fourth tier, ``bench``,
to main.c: line 1 is really hit (``hits.add("bench", 3)``, no override
provenance) and line 2 is asserted-only (``hits.add("bench", 1)`` plus
``line.asserted = {"bench": [0]}``) — override entry id 0 is the report's
one ``store.overrides`` row: ``key="ticket:PROJ-204"``,
``reason="legacy bench regression pass"``. Every constant/string below is
named after that fixture rather than hand-typed.

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

OVERRIDE_KEY = "ticket:PROJ-204"
OVERRIDE_REASON = "legacy bench regression pass"

# main.c chunk carries 11 tracked lines (Task 12 fixture docstring); the
# bench tier really-hits line 1 (3) and is asserted-only on line 2 (1), so
# the default (hideAsserted=false) bench row counts BOTH as hit ("2/11"),
# and hiding drops to just the really-hit one ("1/11").
BENCH_ROW_SHOWN = "2/11"
BENCH_ROW_HIDDEN = "1/11"


def _goto(page: Page, report_dir: Path, hash_route: str) -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#{hash_route}")


def _toggle_hide_asserted(page: Page) -> None:
    """Open the ⋮ menu and click "Hide asserted coverage" — mirrors
    AppShell.test.tsx's click sequence; the menu closes on selecting the
    (disabled-free) toggle item, so a caller wanting to flip it again must
    reopen the menu itself."""
    page.locator('[data-testid="appbar-menu"]').click()
    page.locator('[data-testid="toggle-hide-asserted"]').click()


def test_asserted_marker_renders_distinct_from_proven(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage/product/main.c")
    row2 = page.locator('[data-testid="code-row-2"]')
    expect(row2).to_be_visible()
    asserted_cell = row2.locator('[data-testid="hit-asserted"]')
    expect(asserted_cell).to_be_visible()
    expect(asserted_cell).to_have_text("1")

    row1 = page.locator('[data-testid="code-row-1"]')
    expect(row1).to_be_visible()
    expect(row1.locator('[data-testid="hit-asserted"]')).to_have_count(0)
    expect(row1).to_contain_text("3")


def test_expanding_asserted_line_shows_reason(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage/product/main.c")
    page.locator('[data-testid="code-expander-2"]').click()
    panel = page.locator('[data-testid="ctx-panel-2"]')
    expect(panel).to_be_visible()
    chip = panel.locator('[data-testid="asserted-chip"]')
    expect(chip).to_be_visible()
    expect(chip).to_contain_text(OVERRIDE_KEY)
    expect(chip).to_contain_text(OVERRIDE_REASON)


def test_overrides_badge_and_menu_listing(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage")
    badge = page.locator('[data-testid="overrides-badge"]')
    expect(badge).to_be_visible()
    expect(badge).to_have_text("1 override")

    page.locator('[data-testid="appbar-menu"]').click()
    entries = page.locator('[data-testid="override-entry"]')
    expect(entries).to_have_count(1)
    expect(entries.first).to_contain_text(OVERRIDE_KEY)
    expect(entries.first).to_contain_text(OVERRIDE_REASON)


def test_hide_asserted_recomputes_and_announces(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage/product/main.c")
    bench_row = page.locator('[data-testid="stats-row-bench"]')
    expect(bench_row).to_be_visible()
    expect(bench_row).to_contain_text(BENCH_ROW_SHOWN)

    _toggle_hide_asserted(page)

    expect(bench_row).to_contain_text(BENCH_ROW_HIDDEN)
    expect(page.locator('[data-testid="stats-card"]')).to_contain_text("asserted hidden")

    # Toggle back — recomputes forward too, no leftover suffix.
    _toggle_hide_asserted(page)
    expect(bench_row).to_contain_text(BENCH_ROW_SHOWN)
    expect(page.locator('[data-testid="stats-card"]')).not_to_contain_text("asserted hidden")


def test_hide_asserted_composes_with_run_focus(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/runs")
    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="focus-context-btn"]').click()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert "ctx=nightly-full" in page.evaluate("location.hash")

    _toggle_hide_asserted(page)

    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert "ctx=nightly-full" in page.evaluate("location.hash")
    assert "asserted=1" in page.evaluate("location.hash")

    # Navigate to the file page with both still pinned — real render, no
    # crash (the autouse `_pageerror_guard` would fail this test otherwise).
    page.evaluate("location.hash = '#/coverage/product/main.c'")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert "ctx=nightly-full" in page.evaluate("location.hash")
    assert "asserted=1" in page.evaluate("location.hash")
