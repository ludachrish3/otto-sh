"""Command layer e2e (spec 2026-07-17: untitledui-command-layer): palette
open -> filter -> navigate, one chord smoke test, and the ``/`` search-focus
routing. Contract: ``data-testid`` attributes only.

Fixtures import through the client-side Import front door against
``shell_dash`` (an empty-collector, dist-serving harness), zero backend --
same idiom as ``test_review_shell.py``'s ``_import_fixture``, mirrored here
rather than imported, matching this directory's existing per-module-helper
convention (``test_live_shell.py``, ``test_topology_tunnels.py`` each keep
their own copy too).
"""

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page: Page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def test_palette_opens_and_navigates_to_a_host(page: Page, shell_dash) -> None:
    """Ctrl+K opens the palette; typing a host id + Enter lands on its
    subject page (spec: palette flow). The host id is read off the loaded
    hosts grid rather than pinned to fixture contents -- "/" is the topology
    landing now (route swap, spec 2026-07-17), so the grid itself needs an
    explicit ``#/hosts`` hop first.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/hosts")
    first_tile = page.locator('[data-testid^="subject-link-"]').first
    first_tile.wait_for()
    host_id = first_tile.get_attribute("data-testid").removeprefix("subject-link-")

    page.keyboard.press("Control+KeyK")
    expect(_tid(page, "command-menu")).to_be_visible()
    _tid(page, "command-input").fill(host_id)
    expect(_tid(page, f"command-item-nav-host-{host_id}")).to_be_visible()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(_tid(page, "command-menu")).not_to_be_visible()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert page.url.endswith(f"#/host/{host_id}")


def test_search_trigger_opens_palette(page: Page, shell_dash) -> None:
    """The AppBar's search-style trigger (hidden until data loads) opens the
    same palette Ctrl+K does; Escape closes it."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    _tid(page, "search-trigger").click()
    expect(_tid(page, "command-menu")).to_be_visible()
    page.keyboard.press("Escape")
    expect(_tid(page, "command-menu")).not_to_be_visible()


def test_theme_chord_toggles_dark_mode(page: Page, shell_dash) -> None:
    """Ctrl+L flips the html dark-mode class (chord smoke test -- proves the
    chord path end-to-end in a real browser, preventDefault included)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    before = page.evaluate("document.documentElement.classList.contains('dark-mode')")
    page.keyboard.press("Control+KeyL")
    page.wait_for_function(
        f"document.documentElement.classList.contains('dark-mode') !== {str(before).lower()}"
    )


def test_slash_opens_palette_off_subject_pages(page: Page, shell_dash) -> None:
    """Bare "/" opens the palette when no series-search input is registered
    (SeriesPanel is the only registrant, and it mounts only on SubjectPage --
    searchFocus.ts). "/" is the topology landing post-import (route swap),
    so this is already "off subject pages" without any extra navigation."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.keyboard.press("Slash")
    expect(_tid(page, "command-menu")).to_be_visible()
