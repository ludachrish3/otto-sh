"""Pins the SPA `#/tickets` page and the report-wide ticket-context filter
(Tasks 10-12) in a real browser: the page boots with rows and a stats card,
the search box filters, expanding a row loads its chunk and shows a real
missing-line range, clicking that range navigates to the file page with the
span highlighted, and pinning a ticket (via the app bar's own ticket
search box, which replaced the ⋮ menu's flat list; the tickets page's rows
carry their own pin control too) hides non-participating tree rows behind
a hidden-count banner.

The fixture's per-ticket data (``tests/_fixtures/_report_fixture.py``,
Task-13 addendum — see its module docstring) has exactly two tickets:
``PROJ-204`` owns main.c's ``checked_add()`` body through the stale brace
(lines 3-7: 4 hit, 1 uncovered — the stale line 6, which carries no
per-tier hit) and nothing in utils.c; ``PROJ-9`` owns only utils.c's one,
fully-covered line and carries no tracker ``url``. Every constant below is
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

# tests/_fixtures/_report_fixture.py: PROJ-204 owns main.c lines 3,4,5,6,7 —
# 3,4,5,7 are hit, line 6 (the stale brace) carries no per-tier hit.
PROJ_204_OWNED = 5
PROJ_204_UNCOVERED = 1
# PROJ-9 owns only utils.c line 2, which is hit.
PROJ_9_OWNED = 1
PROJ_9_UNCOVERED = 0
# No overlap between the two tickets in this fixture, so the deduped
# stats-card total is a plain sum.
TICKETS_TOTAL_OWNED = PROJ_204_OWNED + PROJ_9_OWNED
TICKETS_TOTAL_COVERED = (PROJ_204_OWNED - PROJ_204_UNCOVERED) + (PROJ_9_OWNED - PROJ_9_UNCOVERED)


def _goto(page: Page, report_dir: Path, hash_route: str) -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#{hash_route}")


def test_tickets_page_lists_rows_and_stats_card(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/tickets")
    expect(page.locator('[data-testid="ticket-row"]')).to_have_count(2)
    expect(page.locator('[data-testid="ticket-id"]', has_text="PROJ-204")).to_be_visible()
    expect(page.locator('[data-testid="ticket-id"]', has_text="PROJ-9")).to_be_visible()

    all_row = page.locator('[data-testid="stats-row-all"]')
    expect(all_row).to_be_visible()
    expect(all_row).to_contain_text(f"{TICKETS_TOTAL_COVERED}/{TICKETS_TOTAL_OWNED}")

    # PROJ-204 carries a tracker `url` (renders as a link); PROJ-9 doesn't
    # (plain text) — both variants share the same testid (TicketIdCell).
    linked = page.locator('[data-testid="ticket-id"]', has_text="PROJ-204")
    expect(linked).to_have_attribute("href", "https://example.test/issues/204")
    plain = page.locator('[data-testid="ticket-id"]', has_text="PROJ-9")
    assert plain.evaluate("el => el.tagName") == "SPAN"
    assert plain.get_attribute("href") is None


def test_search_box_filters_to_matching_ticket_id(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/tickets")
    expect(page.locator('[data-testid="ticket-row"]')).to_have_count(2)

    page.locator('[data-testid="tickets-search"] input').fill("204")
    expect(page.locator('[data-testid="ticket-row"]')).to_have_count(1)
    expect(page.locator('[data-testid="ticket-id"]', has_text="PROJ-204")).to_be_visible()


def test_expanding_row_loads_chunk_and_shows_missing_range(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/tickets")
    page.locator('[data-testid="ticket-toggle-PROJ-204"]').click()

    detail = page.locator('[data-testid="ticket-detail"]')
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("product/main.c")

    # PROJ-204's one uncovered line (6) is a singleton range -> "6" (
    # fmtLineRange renders a bare number when start == end).
    range_link = page.locator('[data-testid="missing-range-link"]')
    expect(range_link).to_have_count(1)
    expect(range_link).to_have_text("6")


def test_clicking_missing_range_navigates_to_file_with_span_highlighted(
    page: Page, report_dir: Path
) -> None:
    _goto(page, report_dir, "/tickets")
    page.locator('[data-testid="ticket-toggle-PROJ-204"]').click()
    expect(page.locator('[data-testid="ticket-detail"]')).to_be_visible()

    page.locator('[data-testid="missing-range-link"]').click()
    assert page.url.endswith("#/coverage/product/main.c?lines=6")

    row6 = page.locator('[data-testid="code-row-6"]')
    expect(row6).to_be_visible()
    expect(row6).to_have_attribute("data-highlighted", "true")
    # Only the targeted line is highlighted, not the whole file — the
    # attribute is omitted entirely (never "false") on every other row
    # (web/src/ui/CodeView.tsx), so the negated matcher is what actually
    # proves that rather than a literal "false" comparison.
    expect(page.locator('[data-testid="code-row-3"]')).not_to_have_attribute(
        "data-highlighted", "true"
    )


def test_pinning_ticket_hides_non_participating_file_row_and_shows_banner(
    page: Page, report_dir: Path
) -> None:
    """PROJ-204 owns nothing in utils.c (fixture docstring): pinning it at
    the ``product/`` directory page hides utils.c's tree row and reports
    exactly one hidden file, never silently."""
    _goto(page, report_dir, "/coverage/product")
    expect(page.locator('[data-testid="tree-row-file:product/main.c"]')).to_be_visible()
    expect(page.locator('[data-testid="tree-row-file:product/utils.c"]')).to_be_visible()

    # Pinning moved out of the ⋮ menu into its own app-bar search box.
    page.locator('[data-testid="ticket-search"] input').fill("PROJ-204")
    page.locator('[data-testid="ticket-search-option-PROJ-204"]').click()
    expect(page.locator('[data-testid="ticket-chip"]')).to_contain_text("PROJ-204")

    expect(page.locator('[data-testid="tree-row-file:product/main.c"]')).to_be_visible()
    expect(page.locator('[data-testid="tree-row-file:product/utils.c"]')).to_have_count(0)

    banner = page.locator('[data-testid="ticket-scope-banner"]')
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("1 file hidden")
    expect(banner).to_contain_text("1 ticket pinned")

    page.locator('[data-testid="ticket-clear"]').click()
    expect(page.locator('[data-testid="ticket-chip"]')).to_have_count(0)
    expect(page.locator('[data-testid="tree-row-file:product/utils.c"]')).to_be_visible()
