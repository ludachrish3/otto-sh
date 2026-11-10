"""Data colours never paint text (#887, #919).

Tier and state colours are any CSS colour the project configures, so none can
be assumed legible as text on the page background. Every place one is shown
keeps its label in the theme's own ink and puts the hue only in a dot and a
tint. Each test injects a colour that would fail as ink in its theme (yellow
on white, near-black on the dark surface), checks the injected hue reached the
page, and then asserts WCAG AA contrast of the text.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.cov.report_browser._support import (
    background_rgb,
    hex_rgb,
    set_state_colors,
    text_contrast,
)

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

CI_FIRMWARE = "ci-01/firmware"  # fixture: the Unit-tier (yellow) context
BENCH_3_BOOTLOADER = "bench-3/bootloader"  # fixture: fully revoked, one stale mark on line 6
AA = 4.5

# (theme, state colour): a colour that fails as text ink in that theme.
THEME_COLORS = [("light", "#ffff00"), ("dark", "#101030")]


def _index_url(report_dir: Path, route: str = "") -> str:
    return (report_dir / "index.html").as_uri() + (f"#{route}" if route else "")


def _use_theme(page: Page, theme: str) -> None:
    if theme == "dark":
        page.locator('[data-testid="theme-toggle"]').click()
        assert "dark-mode" in page.evaluate("document.documentElement.className")


def _assert_legible(
    page: Page, text_el: Locator, dot: Locator | None, theme: str, color: str | None, what: str
) -> None:
    """The text stays AA-legible, and the injected hue reached the dot."""
    expect(text_el).to_be_visible()
    ink, ratio = text_contrast(page, text_el)
    assert ratio >= AA, f"{theme}: {what} text {ink} is {ratio:.2f}:1"
    if dot is not None and color is not None:
        assert background_rgb(page, dot) == pytest.approx(hex_rgb(color), abs=0.01), (
            f"{what}: the injected {color} never reached the dot"
        )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tier_chip_text_contrast(page: Page, report_dir: Path, theme: str) -> None:
    """The Unit tier's default colour is yellow: as text it is unreadable on
    white. The chip's label ink must stay legible on the chip's tint."""
    page.goto(_index_url(report_dir, "/runs"))
    _use_theme(page, theme)
    page.locator('[data-testid="tier-chip-unit"]').click()
    chip = page.locator(f'[data-testid="run-row-{CI_FIRMWARE}"] [data-testid="tier-chip"]')
    _assert_legible(page, chip, None, theme, None, "tier chip")


@pytest.mark.parametrize(("theme", "color"), THEME_COLORS)
def test_flag_pill_text_contrast(page: Page, report_dir: Path, theme: str, color: str) -> None:
    """The directory's stale flag pill takes its hue from ``state_colors.stale``."""
    set_state_colors(page, {"stale": color})
    page.goto(_index_url(report_dir, "/coverage"))
    _use_theme(page, theme)
    pill = page.locator('[data-testid="flag-pill"]', has_text="stale").first
    _assert_legible(page, pill, pill.locator('span[aria-hidden="true"]'), theme, color, "flag pill")


@pytest.mark.parametrize(("theme", "color"), THEME_COLORS)
def test_stale_row_text_contrast(page: Page, report_dir: Path, theme: str, color: str) -> None:
    """A fully revoked context's detail row ("N credits revoked") is stale-coloured."""
    set_state_colors(page, {"stale": color})
    page.goto(_index_url(report_dir, "/runs"))
    _use_theme(page, theme)
    page.locator(f'[data-testid="run-row-{BENCH_3_BOOTLOADER}"]').click()
    detail = page.locator(f'[data-testid="run-detail-{BENCH_3_BOOTLOADER}"]')
    cell = detail.locator("td", has_text="credits revoked")
    _assert_legible(page, cell, cell.locator('span[aria-hidden="true"]'), theme, color, "stale row")


@pytest.mark.parametrize(("theme", "color"), THEME_COLORS)
def test_revoked_run_chip_text_contrast(
    page: Page, report_dir: Path, theme: str, color: str
) -> None:
    """main.c line 6's run chip is revoked, so it is stale-coloured."""
    set_state_colors(page, {"stale": color})
    page.goto(_index_url(report_dir, "/coverage/product/main.c"))
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()
    _use_theme(page, theme)
    page.locator('[data-testid="code-expander-6"]').click()
    chip = page.locator('[data-testid="ctx-panel-6"] [data-testid="run-chip"]')
    # The chip carries the tier dot and, when revoked, a state dot marked by testid.
    _assert_legible(
        page, chip, chip.locator('[data-testid="state-dot"]'), theme, color, "revoked chip"
    )
