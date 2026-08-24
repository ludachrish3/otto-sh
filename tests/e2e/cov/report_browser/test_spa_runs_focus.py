"""Pins the SPA runs page, the report-wide focus filter, real Back/Forward
history while focused, both themes, the not-found route, the guard screen,
and the stats card — every UI state Tasks 1-8 shipped that
``test_spa_index.py``/``test_spa_file.py`` don't already cover, driven
against the real built bundle in a real browser (file:// lane).

Three of these are carry-over duties jsdom genuinely cannot exercise:
real ``page.go_back()``/``go_forward()`` history traversal (focus.tsx's
jsdom-side tests, e.g. ``focus.test.tsx``'s "costs exactly ONE history
entry" pin, can only assert the *mechanism* — push vs. replace — not the
browser's actual traversal semantics or event ordering), and the file
page's sticky column header / horizontal-overflow behavior
(``test_spa_file.py`` additions at the bottom of this module),  which
depend on real layout/scroll, not jsdom's non-rendering DOM.

Every assertion below is either testid-anchored or derived from the
fixture's own constants (named locals with a comment pointing at
``tests/_fixtures/_report_fixture.py`` — no magic numbers). See
``.superpowers/sdd/2026-07-25-coverage-spa/task-9-report.md`` for the one
real bug this suite found (focus.tsx's Back/Forward reconciliation) and
its fix.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module.
"""

import math
import re
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

# ---------------------------------------------------------------------
# Fixture-derived constants (tests/_fixtures/_report_fixture.py) — every
# expected string below is computed from these, never hand-typed.
# ---------------------------------------------------------------------

# main_rec.lines gets LineRecords for 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12 (11
# entries — Task 12 added 1 and 2 for the bench tier); of those, only line 6
# (state="stale") and line 9 (state="aging") carry no tier hit — the rest
# (1,2,3,4,5,7,10,11,12) do (line 2's bench hit is override-sourced only,
# but it's still a hit for the unfocused "any tier" count).
MAIN_C_TOTAL_LINES = 11
MAIN_C_HIT_LINES = 9
# nightly-full's member runs (run_sys_a/run_sys_b) post run_hits on lines
# 3,4,5 (router-a) and 10,11,12 (router-b) — 6 of main.c's 11 lines; the
# bench-tier lines 1/2 carry no run_hits for this context.
MAIN_C_NIGHTLY_FULL_LINES = 6
# index.total_lines = main.c's 11 + utils.c's 2 (utils.c has LineRecords for
# lines 2 and 10 only). Line 6 is LCOV_EXCL_LINE-marked, so the exclusion
# filter deleted its record before rendering — it is not in this total, which
# is the whole point of the feature: an excluded line leaves the denominator.
TOTAL_LINES = 13
TOTAL_HIT_LINES = 10  # main.c's 9 + utils.c's 1 (line 2; line 10 is uncovered)


def _fmt_pct(hit: int, total: int) -> str:
    """Mirror ``web/src/covapp/stats.ts``'s ``pct``/``fmtPct`` exactly,
    including JS ``Math.round``'s round-half-up (not Python's banker's
    rounding)."""
    p = 100 * hit / total
    rounded = math.floor(p * 10 + 0.5) / 10
    return f"{rounded:.1f}%"


def _goto(page: Page, report_dir: Path, hash_route: str) -> None:
    page.goto((report_dir / "index.html").as_uri() + f"#{hash_route}")


def _pin_nightly_full(page: Page, report_dir: Path) -> None:
    """Real UI pin (per the brief: focus from a run detail's own button),
    used by every focus-flow test below as their common starting state."""
    _goto(page, report_dir, "/runs")
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_be_visible()
    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="focus-context-btn"]').click()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()


# ---------------------------------------------------------------------
# Runs page
# ---------------------------------------------------------------------


def test_runs_page_one_row_per_context_with_multihost_pills(page: Page, report_dir: Path) -> None:
    """4 contexts (nightly-full, unit harvest, smoke-old, field bring-up);
    nightly-full is the only multi-host one (router-a + router-b)."""
    _goto(page, report_dir, "/runs")
    for label in ("nightly-full", "unit harvest", "smoke-old", "field bring-up"):
        expect(page.locator(f'[data-testid="run-row-{label}"]')).to_be_visible()

    # Host column is the run row's 3rd direct child (Run, Tier, Host, ...,
    # matching RunsPage.tsx's ROW_GRID order) — HostPills itself carries no
    # testid, so this is the precise way to scope to just that column
    # rather than substring-matching row text (the Board column can
    # legitimately repeat one of the same host names).
    host_col = page.locator('[data-testid="run-row-nightly-full"] > *').nth(2)
    expect(host_col.locator("span")).to_have_count(2)
    assert host_col.locator("span").all_inner_texts() == ["router-a", "router-b"]

    single_host_col = page.locator('[data-testid="run-row-unit harvest"] > *').nth(2)
    expect(single_host_col.locator("span")).to_have_count(1)


def test_stale_context_shows_revoked_count_and_stale_badge(page: Page, report_dir: Path) -> None:
    """smoke-old is fully revoked (Global Constraints/fixture: no live
    hits, one stale mark on main.c line 6) -> ContribCell shows "N
    revoked" instead of a hit fraction, and the status badge reads
    "stale"."""
    _goto(page, report_dir, "/runs")
    row = page.locator('[data-testid="run-row-smoke-old"]')
    expect(row).to_contain_text("1 revoked")  # main.c line 6's one stale mark
    expect(row).to_contain_text("stale")


def test_remapped_context_shows_marker_and_detail_base_commit(page: Page, report_dir: Path) -> None:
    """field bring-up's ``dirty_remap=True`` -> "✎ remapped" on the row and
    a "→ HEAD (remapped)" suffix on the detail's base-commit line."""
    _goto(page, report_dir, "/runs")
    row = page.locator('[data-testid="run-row-field bring-up"]')
    expect(row).to_contain_text("✎ remapped")

    row.click()
    detail = page.locator('[data-testid="run-detail-field bring-up"]')
    expect(detail).to_be_visible()
    # _BASE_COMMIT_FIELD = "b2" * 20, sliced to 12 chars by ContextDetail.
    expect(detail).to_contain_text("b2b2b2b2b2b2 → HEAD (remapped)")


def test_tier_chip_filter_narrows_to_manual_rows(page: Page, report_dir: Path) -> None:
    """smoke-old and field bring-up are the only "manual" tier contexts."""
    _goto(page, report_dir, "/runs")
    page.locator('[data-testid="tier-chip-manual"]').click()
    expect(page.locator('[data-testid^="run-row-"]')).to_have_count(2)
    expect(page.locator('[data-testid="run-row-smoke-old"]')).to_be_visible()
    expect(page.locator('[data-testid="run-row-field bring-up"]')).to_be_visible()
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_have_count(0)


def test_search_by_ticket_narrows_to_field_bring_up(page: Page, report_dir: Path) -> None:
    """FW-1188 is field bring-up's ticket (searchHaystack includes it)."""
    _goto(page, report_dir, "/runs")
    page.locator('[data-testid="runs-search"] input').fill("FW-1188")
    expect(page.locator('[data-testid^="run-row-"]')).to_have_count(1)
    expect(page.locator('[data-testid="run-row-field bring-up"]')).to_be_visible()


def test_nightly_full_detail_shows_per_host_lines_for_both_hosts(
    page: Page, report_dir: Path
) -> None:
    """router-a covers main.c lines 3/4/5 (3 lines), router-b covers
    10/11/12 (3 lines) — both hosts' per-host bar shows a nonzero count."""
    _goto(page, report_dir, "/runs")
    page.locator('[data-testid="run-row-nightly-full"]').click()
    detail = page.locator('[data-testid="run-detail-nightly-full"]')
    expect(detail).to_be_visible()
    per_host = detail.get_by_text("Per-host lines").locator("..")
    expect(per_host).to_contain_text("router-a")
    expect(per_host).to_contain_text("router-b")
    # Both hosts contributed 3 lines each (see the constants block above).
    assert per_host.inner_text().count("3") >= 2


def test_top_file_link_routes_to_file_page(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/runs")
    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="file-link-product/main.c"]').click()
    assert page.url.endswith("#/coverage/product/main.c")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()


# ---------------------------------------------------------------------
# Focus flow
# ---------------------------------------------------------------------


def test_focus_pin_shows_chip_and_ctx_in_hash(page: Page, report_dir: Path) -> None:
    _pin_nightly_full(page, report_dir)
    expect(page.locator('[data-testid="focus-chip"]')).to_contain_text("nightly-full")
    assert "ctx=nightly-full" in page.evaluate("location.hash")


def test_focus_changes_directory_page_line_pct_cell(page: Page, report_dir: Path) -> None:
    """A specific tree row's Line % cell differs focused vs. unfocused —
    captured before/after, not asserted from a hard-coded single value."""
    _goto(page, report_dir, "/coverage/product")
    row = page.locator('[data-testid="tree-row-file:product/main.c"]')
    expect(row).to_be_visible()
    unfocused_pct = _fmt_pct(MAIN_C_HIT_LINES, MAIN_C_TOTAL_LINES)
    expect(row).to_contain_text(unfocused_pct)  # "77.8%"

    _pin_nightly_full(page, report_dir)
    page.evaluate("location.hash = '#/coverage/product'")
    row = page.locator('[data-testid="tree-row-file:product/main.c"]')
    expect(row).to_be_visible()
    focused_pct = _fmt_pct(MAIN_C_NIGHTLY_FULL_LINES, MAIN_C_TOTAL_LINES)
    assert focused_pct != unfocused_pct
    expect(row).to_contain_text(focused_pct)  # "66.7%"
    expect(row).not_to_contain_text(unfocused_pct)


def test_focus_marks_non_member_line_uncovered_on_file_page(page: Page, report_dir: Path) -> None:
    """main.c line 7 is hit only by "unit harvest" (run_unit) — under
    nightly-full's focus (router-a/router-b only), it has no member-run
    hit, so it renders the uncovered class instead of its unfocused
    ``t-unit`` tier tint."""
    _goto(page, report_dir, "/coverage/product/main.c")
    expect(page.locator('[data-testid="code-row-7"]')).to_have_class(re.compile(r"\bt-unit\b"))

    _pin_nightly_full(page, report_dir)
    page.evaluate("location.hash = '#/coverage/product/main.c'")
    row7 = page.locator('[data-testid="code-row-7"]')
    expect(row7).to_be_visible()
    expect(row7).to_have_class(re.compile(r"\bs-unc\b"))
    assert "ctx=nightly-full" in page.evaluate("location.hash")


def test_focus_reload_persists_then_clear_removes_it(page: Page, report_dir: Path) -> None:
    _pin_nightly_full(page, report_dir)
    page.evaluate("location.hash = '#/coverage/product/main.c'")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    focused_url = page.url

    page.reload()
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert page.url == focused_url

    page.locator('[data-testid="focus-clear"]').click()
    expect(page.locator('[data-testid="focus-chip"]')).to_have_count(0)
    assert "ctx=" not in page.evaluate("location.hash")


def test_focus_localstorage_key_is_stamp_namespaced(page: Page, report_dir: Path) -> None:
    _pin_nightly_full(page, report_dir)
    keys = page.evaluate("Object.keys(localStorage)")
    focus_keys = [k for k in keys if re.match(r"^otto-cov:.+:focus$", k)]
    assert len(focus_keys) == 1, keys
    stamp_segment = focus_keys[0].split(":")[1]
    assert stamp_segment == page.evaluate("window.__OTTO_COV__.stamp")


# ---------------------------------------------------------------------
# REAL Back/Forward with focus pinned (Task 7 carry-over — jsdom can't
# fire real popstate/history traversal; see the module docstring).
# ---------------------------------------------------------------------


def test_back_forward_with_focus_pinned_history_not_trapped(page: Page, report_dir: Path) -> None:
    """Entry A: #/runs, unfocused (this initial ``page.goto`` navigation).
    Entry B: #/runs?ctx=nightly-full (a real click on "Focus this
    context" — a user action, must push). Entry C: #/coverage (a real
    click on the breadcrumb's project-name link — genuine in-app
    navigation, must push; focus.tsx's own re-append then corrects it
    in place to carry ``?ctx=`` too, still entry C).

    Back/Forward navigation itself uses ``page.go_back()``/
    ``go_forward()`` (real session-history traversal) exclusively — no
    ``page.goto()``/``location.hash=`` stand-ins — so the entries under
    test are the browser's real ones, not simulated.
    """
    _goto(page, report_dir, "/runs")  # Entry A
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_be_visible()
    assert page.locator('[data-testid="focus-chip"]').count() == 0

    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="focus-context-btn"]').click()  # Entry B (push)
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert page.evaluate("location.hash") == "#/runs?ctx=nightly-full"

    page.locator('[data-testid="breadcrumbs"] a', has_text="otto example product").click()
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()  # Entry C
    assert page.evaluate("location.hash") == "#/coverage?ctx=nightly-full"
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()

    # Back #1: MOVES to entry B — ctx still in the hash, chip still visible.
    page.go_back()
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_be_visible()
    assert page.evaluate("location.hash") == "#/runs?ctx=nightly-full"
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()

    # Back #2: MOVES to entry A — the genuinely pre-focus entry. Must not
    # bounce forward, and must not get "corrected" right back to focused
    # (the bug this test found and task-9-report.md documents the fix
    # for): the chip is gone and the hash carries no ctx.
    page.go_back()
    expect(page.locator('[data-testid="focus-chip"]')).to_have_count(0)
    assert page.evaluate("location.hash") == "#/runs"

    # Forward: MOVES back to entry B, re-focusing.
    page.go_forward()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert page.evaluate("location.hash") == "#/runs?ctx=nightly-full"


def test_back_once_then_forward_click_keeps_focus_pinned(page: Page, report_dir: Path) -> None:
    """Review-found regression on the FIRST fix round's discriminator (a
    `popstate` + "did `history.length` grow" heuristic): a push made from
    MID-STACK — after at least one real Back, with a "forward" entry still
    on the stack — truncates that forward entry when pushing a new one, so
    `history.length` doesn't reliably grow on a push either. That
    misclassified an ordinary in-app click as a Back/Forward landing and
    silently cleared a pinned focus. Sequence: pin on #/runs (push B),
    navigate to #/coverage (push C), Back once (real traversal to B — C is
    still a "forward" entry at this point), then click a REAL link (a
    fresh push FROM B, dropping C): focus must survive — chip still
    visible, `ctx=` still in the new hash. The fix (task-9-report.md,
    round 2) discriminates via an `history.state` stamp instead of the
    push/length heuristic — this pins the exact case that heuristic got
    wrong."""
    _goto(page, report_dir, "/runs")  # Entry A
    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="focus-context-btn"]').click()  # Entry B (push)
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()

    page.locator('[data-testid="breadcrumbs"] a', has_text="otto example product").click()
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()  # Entry C

    page.go_back()  # -> B (real traversal; C remains a "forward" entry)
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_be_visible()
    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()

    # A real link click from B — RunsPage remounted on the Back (a route
    # change), so nightly-full's detail (and its file link) needs
    # re-expanding before this mid-stack push.
    page.locator('[data-testid="run-row-nightly-full"]').click()
    page.locator('[data-testid="file-link-product/main.c"]').click()  # Entry D (mid-stack push)
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()

    expect(page.locator('[data-testid="focus-chip"]')).to_be_visible()
    assert "ctx=nightly-full" in page.evaluate("location.hash")


# ---------------------------------------------------------------------
# Both themes
# ---------------------------------------------------------------------


def test_theme_toggle_dark_mode_class_and_computed_style_change(
    page: Page, report_dir: Path
) -> None:
    _goto(page, report_dir, "/coverage")
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    assert "dark-mode" not in page.evaluate("document.documentElement.className")
    # `body { @apply bg-primary }` (covapp.css) — the app-bar itself has no
    # explicit background (transparent in both themes), so `body`'s
    # semantic-token background is the reliable spot-check the brief asks
    # for ("a token-driven color actually changes").
    bg_before = page.evaluate("getComputedStyle(document.body).backgroundColor")

    page.locator('[data-testid="theme-toggle"]').click()
    assert "dark-mode" in page.evaluate("document.documentElement.className")
    bg_after = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg_after != bg_before


def test_theme_persists_across_reload(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage")
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    page.locator('[data-testid="theme-toggle"]').click()
    assert page.evaluate("localStorage.getItem('otto-theme')") == "dark"

    page.reload()
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    assert "dark-mode" in page.evaluate("document.documentElement.className")


# ---------------------------------------------------------------------
# Not-found route
# ---------------------------------------------------------------------


def test_not_found_route_links_back_to_coverage(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage/does/not/exist")
    not_found = page.locator('[data-testid="not-found"]')
    expect(not_found).to_be_visible()
    link = not_found.locator("a")
    assert link.get_attribute("href") == "#/coverage"
    link.click()
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()


# ---------------------------------------------------------------------
# Guard screen
# ---------------------------------------------------------------------


def test_guard_screen_on_unsupported_data_format(
    page: Page, report_dir: Path, tmp_path: Path
) -> None:
    """Copies the report to a per-test tmp dir (never mutates the shared
    session ``report_dir``) and truncates its index chunk to an
    unsupported format -> ``dataGuard()`` renders GuardScreen instead of
    the router, with no pageerror/console.error (asserted by the autouse
    ``_pageerror_guard``)."""
    corrupted_dir = tmp_path / "corrupted_report"
    shutil.copytree(report_dir, corrupted_dir)
    (corrupted_dir / "cov_data" / "index.js").write_text('window.__OTTO_COV__ = {"format": 999};')
    page.goto((corrupted_dir / "index.html").as_uri() + "#/coverage")
    expect(page.locator('[data-testid="guard-screen"]')).to_be_visible()


# ---------------------------------------------------------------------
# Stats card
# ---------------------------------------------------------------------


def test_stats_card_all_tiers_line_pct_matches_fixture(page: Page, report_dir: Path) -> None:
    _goto(page, report_dir, "/coverage")
    all_row = page.locator('[data-testid="stats-row-all"]')
    expect(all_row).to_be_visible()
    expected_pct = _fmt_pct(TOTAL_HIT_LINES, TOTAL_LINES)  # "72.7%"
    expect(all_row).to_contain_text(expected_pct)
    expect(all_row).to_contain_text(f"{TOTAL_HIT_LINES}/{TOTAL_LINES}")  # "8/11"
