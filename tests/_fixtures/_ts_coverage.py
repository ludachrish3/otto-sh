"""Chromium V8 JS-coverage collection for the browser e2e suites.

Feeds the merged TS coverage gate (``make coverage-ts``): raw CDP precise-
coverage dumps land in ``reports/ts-e2e-cov/raw/`` and are converted to
istanbul JSON on the web side (``web/scripts/e2e_coverage_report.mjs``, via
the hidden sourcemaps built next to the dist bundles). Chromium-only by
design — coverage numbers are engine-independent, the same reason
``make coverage`` pins a single Python — and skipped for ``soak`` (per-call
CDP overhead on the SSE firehose is exactly what that test measures without).

Intra-test full navigations (``page.goto`` twice in one test) drop the first
page's V8 data — precise coverage reports only currently-loaded scripts.
Same-document hash navigation (the dashboard's routing) is unaffected, and
the suite-wide accumulation makes per-test loss statistically irrelevant;
do not add per-navigation flushing complexity for it.
"""

import json
import os
import uuid
from pathlib import Path

from playwright.sync_api import CDPSession, Page

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = _REPO_ROOT / "reports" / "ts-e2e-cov" / "raw"


def start_ts_coverage(page: Page) -> CDPSession:
    """Begin precise V8 coverage on the page's main frame target."""
    client = page.context.new_cdp_session(page)
    client.send("Profiler.enable")
    client.send("Profiler.startPreciseCoverage", {"callCount": False, "detailed": True})
    return client


def collect_ts_coverage(client: CDPSession, sink: list[dict]) -> None:
    """Take the coverage snapshot and keep only our served bundles."""
    data = client.send("Profiler.takePreciseCoverage")
    client.send("Profiler.stopPreciseCoverage")
    for entry in data["result"]:
        url = entry.get("url", "")
        if "/assets/" in url or url.endswith("covreport.js"):
            sink.append(entry)


def write_ts_coverage(sink: list[dict]) -> None:
    """Persist one raw dump per pytest session (per xdist worker)."""
    if not sink:
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"cdp-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    out.write_text(json.dumps({"result": sink}))
