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
from collections.abc import Iterator
from pathlib import Path

import pytest
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


# Shared body of the browser suites' autouse `_ts_coverage` fixture. It is a
# plain generator, NOT a fixture: pytest honors `autouse` only for fixtures
# DEFINED in a conftest/plugin, not ones imported into one. Each browser
# conftest therefore keeps thin, local `_ts_coverage_sink` + autouse
# `_ts_coverage` fixtures (the unavoidable pytest boilerplate) that delegate the
# actual work here, so this logic lives once.
def ts_coverage(request: pytest.FixtureRequest, sink: list[dict]) -> Iterator[None]:
    """Per-test V8 coverage; suite-wide accumulation. See the module docstring.

    Collection is gated on ``OTTO_TS_COVERAGE`` (set only by the ``make
    dashboard`` recipe, and allowlisted in tests/conftest.py's ambient-env
    strip). Ad-hoc or ``nox`` runs of these suites therefore do NOT append
    dumps to ``reports/ts-e2e-cov/raw/`` outside make's rm-and-stamp protocol —
    otherwise ``make coverage-ts`` could merge in a browser run make never
    scheduled.

    Guarded on the ``browser`` marker BEFORE touching any Playwright fixture:
    a bare ``page`` parameter would force browser parametrization onto a
    conftest's non-browser tests and pull sync Playwright's event loop into
    the shared hostless process.
    """
    if (
        not os.environ.get("OTTO_TS_COVERAGE")
        or request.node.get_closest_marker("browser") is None
        or request.node.get_closest_marker("soak") is not None
    ):
        yield
        return
    if request.getfixturevalue("browser_name") != "chromium":
        yield
        return
    client = start_ts_coverage(request.getfixturevalue("page"))
    yield
    collect_ts_coverage(client, sink)
