#!/usr/bin/env python3
"""Generate the docs' GUI media — screenshots — at build time.

Nothing under ``docs/_static/generated/`` is committed: docs/conf.py invokes
this script on every HTML build (dev VM, CI, Read the Docs), so the media is
produced from the *live* frontend code and can never drift from what otto
actually looks like — the same zero-rot property the architecture tree gets
from ``inheritance_diagram``.

The pipeline reuses the browser-e2e harness (``DashboardHarness`` +
``FakeCollector`` from ``tests/_fixtures``): the real ``MonitorServer`` serves
the real review shell (the built ``web/`` dist) with an EMPTY collector — no
server-seeded data, no boot-time API calls, exactly the production shell's
own boot behavior — and headless Chromium feeds it client-side through the
Import front door with a committed ``web/fixtures/`` document, the same way
``tests/e2e/monitor/dashboard/test_review_shell.py`` does. When the frontend
(or the fixture) changes, the media regenerates on the next build.

Live monitoring has no capture of its own: the review-first shell has no live
page to photograph, and that mode returns at a later phase (see
``docs/guide/monitor.md``). The coverage-report capture is unrelated to any
of this and always runs for real.

Modes — ``--mode`` flag, or the ``OTTO_DOCS_MEDIA`` env var:

- ``auto`` (default): regenerate only when the stamp says the inputs changed
  (this script, the fixtures, or anything under ``src/otto/monitor``).
- ``force``: always regenerate (``make docs-media``).
- ``placeholder``: write tiny placeholder assets without launching a browser.
  Emergency escape hatch only — e.g. a broken Chromium install on the docs
  host — so a docs deploy is degraded, not blocked.

Chromium comes from ``make browsers`` (installed by ``make dev``); a missing
browser is a loud error naming that target, per the dev-environment contract.
"""

import argparse
import base64
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # for the shared tests/_fixtures harness

OUT_DIR = REPO_ROOT / "docs" / "_static" / "generated"
STAMP = OUT_DIR / ".stamp"

# Inputs whose change invalidates the media: this script, the harness
# fixtures it drives, the fixture documents it imports, and the whole monitor
# subsystem (server, collector, static frontend assets).
_STAMP_INPUTS = [
    Path(__file__).resolve(),
    REPO_ROOT / "tests" / "_fixtures" / "_dashboard_harness.py",
    REPO_ROOT / "tests" / "_fixtures" / "_fake_collector.py",
    REPO_ROOT / "tests" / "_fixtures" / "_report_fixture.py",
    REPO_ROOT / "web" / "fixtures" / "kitchen-sink.json",
    REPO_ROOT / "web" / "fixtures" / "isp-core.json",
    REPO_ROOT / "src" / "otto" / "monitor",
    REPO_ROOT / "src" / "otto" / "coverage" / "renderer",
]

# The files this script promises to produce (docs pages reference them).
ARTIFACTS = [
    "dashboard-topology.png",
    "dashboard-review.png",
    "dashboard-review-charts.png",
    "coverage-report.png",
]

_VIEWPORT = {"width": 1280, "height": 720}

# Per-operation Playwright ceiling for the capture. Playwright's 30s default is
# tight for a full-page screenshot of the dashboard with its ECharts canvases
# rendered — the heaviest step in the docs build. On a busy host (e.g. a dev
# VM also running the test suite) the render can be starved past 30s and fail
# the whole `make docs`/`make release` on transient CPU load, not a real fault
# (an idle run finishes in ~15s). This generous ceiling keeps the build
# tolerant of a loaded box while still failing eventually if a capture
# genuinely wedges.
_CAPTURE_TIMEOUT_MS = 90_000

# 1x1 transparent PNG for placeholder mode.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _input_digest() -> str:
    """Content hash of everything that should invalidate the media."""
    h = hashlib.sha256()
    for root in _STAMP_INPUTS:
        files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            h.update(str(f.relative_to(REPO_ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _is_fresh(digest: str) -> bool:
    if not all((OUT_DIR / name).exists() for name in ARTIFACTS):
        return False
    return STAMP.exists() and STAMP.read_text().strip() == digest


def _write_placeholders() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        (OUT_DIR / name).write_bytes(_PLACEHOLDER_PNG)
    STAMP.unlink(missing_ok=True)  # placeholders are never "fresh"
    print("docs media: wrote PLACEHOLDERS (no browser run) — media is degraded", flush=True)


def _capture_coverage_report(browser) -> None:  # noqa: ANN001 — playwright import is deferred
    from tests._fixtures._report_fixture import build_fixture_report

    with tempfile.TemporaryDirectory(prefix="otto-docs-cov-") as tmp:
        report_dir = build_fixture_report(Path(tmp))
        page = browser.new_page(viewport=_VIEWPORT)
        page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
        page.goto((report_dir / "index.html").as_uri())
        page.wait_for_selector("table.files-table")
        page.screenshot(path=OUT_DIR / "coverage-report.png", full_page=True)
        page.close()


def _wait_for_edges_settled(
    page,  # noqa: ANN001 — playwright import is deferred
    selector: str,
    *,
    interval_s: float = 0.1,
    stable_reads: int = 3,
    timeout_s: float = 10.0,
) -> None:
    """Poll ``selector``'s match count until it holds steady, not a flat sleep.

    React Flow commits an edge batch in more than one paint (issue #130), so
    "at least one edge exists" (the caller's own prior wait) is not "the
    canvas is done growing" — a fixed sleep either races a slow/loaded host
    (screenshots an incomplete batch) or wastes time on a fast one. Polling
    for the count to stop changing is a real completion signal either way,
    the same idea ``tests/e2e/monitor/dashboard/test_topology_budget.py``'s
    ``_wait_for_links`` uses (there, waiting for a known EXACT total derived
    from the fixture; here, no such total is threaded through, so waiting for
    the count to settle is the equivalent bounded-poll signal).
    """
    deadline = time.monotonic() + timeout_s
    last = -1
    steady = 0
    while time.monotonic() < deadline:
        count = page.locator(selector).count()
        steady = steady + 1 if count == last else 0
        if steady >= stable_reads:
            return
        last = count
        time.sleep(interval_s)
    raise TimeoutError(f"{selector!r} count never settled (last read: {last})")


def _capture_topology(browser, harness) -> None:  # noqa: ANN001 — deferred imports
    """Photograph the topology map — the dashboard's landing view.

    Fed through the Import front door with the densest fixture.
    ``web/fixtures/isp-core.json`` (25 hosts / 44 links) carries, since the
    fixture touch-up landed for this shot (spec 2026-07-17
    topology-default-view) and the addendum review's honesty fix, a degraded
    AND an ok tunnel — each actually riding declared links — alongside its
    existing uncertain one, plus two chassis ``elements`` — one frame shows
    the whole tri-state health story (ok / degraded / uncertain) and element
    grouping. ``/`` is the current landing route, so importing there needs no
    follow-up navigation, unlike the grid capture below.

    React Flow withholds an edge until BOTH its endpoint nodes have been
    measured (see ``tests/e2e/monitor/dashboard/test_review_shell.py``'s
    ``_wait_for_links`` docstring, issue #130): ``topology-page`` — and even
    the node cards — mount a beat before any edge path exists, so waiting on
    node presence alone would screenshot an edgeless canvas. Wait on an
    actually-rendered edge path element, then poll until the rendered count
    stops growing (see ``_wait_for_edges_settled``) instead of guessing a
    flat delay, before the shot.
    """
    fixture = REPO_ROOT / "web" / "fixtures" / "isp-core.json"
    edge_selector = '[data-testid^="topo-link-"] path.react-flow__edge-path'
    page = browser.new_page(viewport=_VIEWPORT)
    page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
    page.goto(harness.url)
    page.locator('[data-testid="import-input"]').set_input_files(fixture)
    page.locator('[data-testid="topology-page"]').wait_for()
    page.locator(edge_selector).first.wait_for()
    _wait_for_edges_settled(page, edge_selector)
    page.screenshot(path=OUT_DIR / "dashboard-topology.png", full_page=True)
    page.close()


def _capture_dashboard(browser, harness) -> None:  # noqa: ANN001 — deferred imports
    """Photograph the review shell fed through the Import front door.

    The review shell has no server-seeded page to open (see the module
    docstring): it boots to an empty Import front door and is fed
    client-side, exactly the way the browser e2e suite does — see
    ``tests/e2e/monitor/dashboard/test_review_shell.py::_import_fixture``.
    The harness's collector stays empty; ``web/fixtures/kitchen-sink.json``
    supplies every session, host, and metric these captures show.

    Two stills: the fleet grid overview, then a subject page's synced chart
    stack (a same-document hash navigation, so the imported data survives).
    ``/`` is the topology landing now (spec 2026-07-17 topology-default-view),
    so the grid needs an explicit ``#/hosts`` hop after import — it no longer
    follows for free from a bare import the way it used to.
    """
    fixture = REPO_ROOT / "web" / "fixtures" / "kitchen-sink.json"
    page = browser.new_page(viewport=_VIEWPORT)
    page.set_default_timeout(_CAPTURE_TIMEOUT_MS)
    page.goto(harness.url)
    page.locator('[data-testid="import-input"]').set_input_files(fixture)
    page.locator('[data-testid="review-bar"]').wait_for()
    page.goto(f"{harness.url}#/hosts")
    page.locator('[data-testid="host-tile-chassis-a_lc1"]').wait_for()
    page.screenshot(path=OUT_DIR / "dashboard-review.png", full_page=True)

    page.goto(f"{harness.url}#/host/chassis-a_lc1")
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.wait_for_timeout(400)  # let ECharts finish its initial render pass
    page.screenshot(path=OUT_DIR / "dashboard-review-charts.png", full_page=True)
    page.close()


def _capture(harness) -> None:  # noqa: ANN001 — DashboardHarness import is deferred
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError as e:
            raise SystemExit(
                f"docs media: Chromium is not installed ({e}).\n"
                "The dev environment provides it — run `make browsers` "
                "(or `make dev`). To ship degraded docs in an emergency, "
                "set OTTO_DOCS_MEDIA=placeholder."
            ) from e
        try:
            _capture_topology(browser, harness)
            _capture_dashboard(browser, harness)

            # Still shot of the coverage HTML report (same fixture the
            # report_browser Playwright suite pins).
            _capture_coverage_report(browser)
        finally:
            browser.close()


def main() -> None:
    """Resolve the mode, then capture (or skip, or write placeholders)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["auto", "force", "placeholder"],
        default=os.environ.get("OTTO_DOCS_MEDIA", "auto"),
        help="auto: regenerate when stale; force: always; placeholder: no browser",
    )
    mode = parser.parse_args().mode

    if mode == "placeholder":
        _write_placeholders()
        return

    digest = _input_digest()
    if mode == "auto" and _is_fresh(digest):
        print("docs media: up to date (stamp matches) — skipping capture", flush=True)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    from tests._fixtures._dashboard_harness import DashboardHarness
    from tests._fixtures._fake_collector import FakeCollector

    harness = DashboardHarness(FakeCollector()).start()
    try:
        _capture(harness)
    finally:
        harness.stop()

    STAMP.write_text(digest + "\n")
    names = ", ".join(ARTIFACTS)
    print(f"docs media: captured {names} in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
