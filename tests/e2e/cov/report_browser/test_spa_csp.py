"""Pins the SPA served over HTTP from a non-root subpath under the documented
minimal Jenkins CSP (spec §2, ``tests/_fixtures/_csp_server.py``), the served
counterpart to ``test_spa_index.py``/``test_spa_file.py``/
``test_spa_runs_focus.py``'s ``file://`` lane. The delivery constraint (Global
Constraints: no ES module scripts, no inline ``<script>``, no ``eval``, no
WASM, no network fetches, all asset references relative) is exactly what lets
the bundle survive this — the CSP lane is what actually proves it, rather
than assuming it from the built artifact's shape.

Deliberately narrow: four tests pin CSP survival itself (boot, lazy chunk
load, hash routing, style-src), not UI behavior — the file:// lane above
already owns behavioral depth against the same bundle.

``_pageerror_guard`` (conftest.py, autouse) asserts no ``pageerror``/
``console.error`` fired during any test in this module — under a real CSP, a
blocked script/style/connect surfaces there (a `Refused to ...` CSP
violation is reported as a `console.error`), so this guard doubles as the
CSP-violation detector.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._csp_server import CspReportServer

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


@pytest.fixture(scope="session")
def csp_server(report_dir: Path) -> Iterator[CspReportServer]:
    """Serve the session ``report_dir`` (conftest.py) over HTTP from
    ``SUBPATH``, with the CSP header on every response. Started here, always
    stopped in the `finally` — a test failure between start and teardown must
    not leak the server thread past the session."""
    server = CspReportServer(report_dir).start()
    try:
        yield server
    finally:
        server.stop()


def _goto(page: Page, server: CspReportServer, hash_route: str = "/coverage") -> None:
    page.goto(f"{server.url}#{hash_route}")


def test_boots_under_csp_from_subpath_with_zero_console_errors(
    page: Page, csp_server: CspReportServer
) -> None:
    """THE inline-script/Jenkins regression gate: a stray inline
    ``<script>``, an ``eval``, or an unexpected network fetch is invisible
    to the file:// lane (no CSP there) but surfaces here as a
    `Refused to ...` console error, caught by the autouse
    ``_pageerror_guard``. Booting from ``SUBPATH`` (not the origin root)
    also pins path-agnostic asset resolution (Global Constraints: relative
    asset references)."""
    _goto(page, csp_server)
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()


def test_file_page_lazy_loads_its_chunk_over_http(page: Page, csp_server: CspReportServer) -> None:
    """Navigating to a file page pulls in its own chunk over real HTTP
    (not a `file://` read) — CodeView's code rows rendering proves that
    fetch/chunk-load succeeded under `script-src 'self'`."""
    _goto(page, csp_server, "/coverage/product/main.c")
    expect(page.locator('[data-testid="code-row-1"]')).to_be_visible()


def test_runs_route_renders_under_csp(page: Page, csp_server: CspReportServer) -> None:
    _goto(page, csp_server, "/runs")
    expect(page.locator('[data-testid="run-row-nightly-full"]')).to_be_visible()


def test_theme_toggle_still_works_under_csp(page: Page, csp_server: CspReportServer) -> None:
    """style-src sanity: the toggle both flips the `.dark-mode` class AND
    changes a computed, token-driven color — under `style-src 'self'
    'unsafe-inline'` the build's injected `<style>` tag (Vite CSS output)
    must still apply."""
    _goto(page, csp_server)
    expect(page.locator('[data-testid="tree-row-dir:product"]')).to_be_visible()
    assert "dark-mode" not in page.evaluate("document.documentElement.className")
    bg_before = page.evaluate("getComputedStyle(document.body).backgroundColor")

    page.locator('[data-testid="theme-toggle"]').click()
    assert "dark-mode" in page.evaluate("document.documentElement.className")
    bg_after = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg_after != bg_before
