#!/usr/bin/env python3
"""Generate the docs' GUI media — screenshots and video clips — at build time.

Nothing under ``docs/_static/generated/`` is committed: docs/conf.py invokes
this script on every HTML build (dev VM, CI, Read the Docs), so the media is
produced from the *live* dashboard code and can never drift from what otto
actually looks like — the same zero-rot property the architecture tree gets
from ``inheritance_diagram``.

The pipeline reuses the browser-e2e fixtures (``DashboardHarness`` +
``FakeCollector`` from ``tests/_fixtures``): the real ``MonitorServer`` serves
the real dashboard over the production store/SSE paths, seeded with
deterministic dummy data, and headless Chromium captures it. When the
frontend (or the harness) changes, the media regenerates on the next build.

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
import math
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # for the shared tests/_fixtures harness

OUT_DIR = REPO_ROOT / "docs" / "_static" / "generated"
STAMP = OUT_DIR / ".stamp"

# Inputs whose change invalidates the media: this script, the harness
# fixtures it drives, and the whole monitor subsystem (server, collector,
# static frontend assets).
_STAMP_INPUTS = [
    Path(__file__).resolve(),
    REPO_ROOT / "tests" / "_fixtures" / "_dashboard_harness.py",
    REPO_ROOT / "tests" / "_fixtures" / "_fake_collector.py",
    REPO_ROOT / "tests" / "_fixtures" / "_report_fixture.py",
    REPO_ROOT / "src" / "otto" / "monitor",
    REPO_ROOT / "src" / "otto" / "coverage" / "renderer",
]

# The files this script promises to produce (docs pages reference them).
ARTIFACTS = ["dashboard-live.png", "dashboard-live.webm", "coverage-report.png"]

_VIEWPORT = {"width": 1280, "height": 720}

# Live-clip shape: ticks pushed while recording, and the pacing between them.
_CLIP_TICKS = 18
_CLIP_TICK_SECONDS = 0.4

# 1x1 transparent PNG for placeholder mode.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

_PROC_META = {
    "Command": "stress",
    "User": "root",
    "Mem": "1.0%",
    "RSS": "10 M",
    "Stat": "R",
    "CPU Time": "0:01.00",
}


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
    (OUT_DIR / "dashboard-live.png").write_bytes(_PLACEHOLDER_PNG)
    (OUT_DIR / "dashboard-live.webm").write_bytes(b"")
    (OUT_DIR / "coverage-report.png").write_bytes(_PLACEHOLDER_PNG)
    STAMP.unlink(missing_ok=True)  # placeholders are never "fresh"
    print("docs media: wrote PLACEHOLDERS (no browser run) — media is degraded", flush=True)


def _seed(harness) -> None:  # noqa: ANN001 — DashboardHarness import is deferred
    """Five minutes of smooth, deterministic history for two hosts."""
    rng = random.Random(42)  # noqa: S311 — deterministic dummy data, not cryptography
    t0 = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    push = harness.collector.push
    for tick in range(60):
        ts = t0 + timedelta(seconds=5 * tick)
        for i, host in enumerate(("router1", "server1")):
            phase = tick / 60 * 2 * math.pi + i
            cpu = 45 + 25 * math.sin(phase) + rng.uniform(-4, 4)
            mem = 35 + tick / 4 + rng.uniform(-1, 1)
            load = max(0.1, 1.2 + math.sin(phase / 2) + rng.uniform(-0.2, 0.2))
            harness.run(push(host, "Overall CPU", cpu, ts=ts))
            harness.run(push(host, "proc/101", cpu * 0.55, meta=_PROC_META, ts=ts))
            harness.run(push(host, "proc/202", cpu * 0.25, meta=_PROC_META, ts=ts))
            harness.run(push(host, "Memory Usage", mem, chart="memory", ts=ts))
            harness.run(push(host, "Load (1m)", load, chart="load", ts=ts))
    harness.run(harness.collector.add_event(label="deploy complete", color="#2ca02c"))


def _open_dashboard(page, url: str) -> None:  # noqa: ANN001 — playwright import is deferred
    from playwright.sync_api import expect

    page.goto(url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", "router1")
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()
    page.wait_for_timeout(750)  # let Plotly finish its initial layout pass


def _capture_coverage_report(browser) -> None:  # noqa: ANN001 — playwright import is deferred
    from tests._fixtures._report_fixture import build_fixture_report

    with tempfile.TemporaryDirectory(prefix="otto-docs-cov-") as tmp:
        report_dir = build_fixture_report(Path(tmp))
        page = browser.new_page(viewport=_VIEWPORT)
        page.goto((report_dir / "index.html").as_uri())
        page.wait_for_selector("table.files-table")
        page.screenshot(path=OUT_DIR / "coverage-report.png", full_page=True)
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

        # Still screenshot of the seeded dashboard.
        page = browser.new_page(viewport=_VIEWPORT)
        _open_dashboard(page, harness.url)
        page.screenshot(path=OUT_DIR / "dashboard-live.png", full_page=True)
        page.close()

        # Still shot of the coverage HTML report (same fixture the
        # report_browser Playwright suite pins).
        _capture_coverage_report(browser)

        # Live clip: keep pushing points while Playwright records, so the
        # traces visibly extend — the "live lab monitoring" money shot.
        ctx = browser.new_context(
            viewport=_VIEWPORT, record_video_dir=OUT_DIR, record_video_size=_VIEWPORT
        )
        page = ctx.new_page()
        _open_dashboard(page, harness.url)
        rng = random.Random(7)  # noqa: S311 — deterministic dummy data, not cryptography
        push = harness.collector.push
        for tick in range(_CLIP_TICKS):
            cpu = 45 + 25 * math.sin(tick / 6) + rng.uniform(-4, 4)
            harness.run(push("router1", "Overall CPU", cpu))
            harness.run(push("router1", "proc/101", cpu * 0.55, meta=_PROC_META))
            harness.run(push("router1", "Load (1m)", 1.2 + rng.uniform(-0.2, 0.2), chart="load"))
            if tick == _CLIP_TICKS // 2:  # an event landing mid-clip, for the timeline marker
                harness.run(harness.collector.add_event(label="test_load start", color="#d62728"))
            time.sleep(_CLIP_TICK_SECONDS)
        video = page.video
        page.close()
        ctx.close()  # finalizes the recording
        browser.close()
        if video is None:
            raise SystemExit("docs media: Playwright returned no video for the live clip")
        shutil.move(video.path(), OUT_DIR / "dashboard-live.webm")


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

    from tests._fixtures._dashboard_harness import DashboardHarness
    from tests._fixtures._fake_collector import FakeCollector

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    harness = DashboardHarness(FakeCollector()).start()
    try:
        _seed(harness)
        _capture(harness)
    finally:
        harness.stop()
    STAMP.write_text(digest + "\n")
    names = ", ".join(ARTIFACTS)
    print(f"docs media: captured {names} in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
