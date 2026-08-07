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

import pytest
from playwright.sync_api import CDPSession, Page

from tests._ambient_env import ambient
from tests._fixtures._ts_bundle_filter import bundle_url_matches
from tests._fixtures.paths import PROJECT_ROOT

_REPO_ROOT = PROJECT_ROOT
RAW_DIR = _REPO_ROOT / "reports" / "ts-e2e-cov" / "raw"


def start_ts_coverage(page: Page) -> CDPSession:
    """Begin precise V8 coverage on the page's main frame target."""
    client = page.context.new_cdp_session(page)
    client.send("Profiler.enable")
    client.send("Profiler.startPreciseCoverage", {"callCount": False, "detailed": True})
    return client


def collect_ts_coverage(
    client: CDPSession, sink: list[dict], *, allow_no_match: bool = False
) -> None:
    """Take the coverage snapshot and keep only our served bundles.

    Two bundle shapes reach here: the monitor dashboard's hashed
    `.../dist/assets/index-*.js`, and the covapp SPA's unhashed
    `.../dist/covapp.js` (both `file://` and, since Task 10, served-CSP —
    `tests/_fixtures/_csp_server.py`). `endswith("covapp.js")` is the
    narrowest predicate that also matches it: a bare `"/dist/"` substring
    check would additionally match non-script dist assets (fonts, CSS) that
    `Profiler.takePreciseCoverage` never actually returns, so it would be a
    no-op broadening, not a meaningfully different filter — but naming the
    exact bundle file keeps this list self-documenting as new bundles are
    added, rather than silently widening to "anything under dist/". The
    predicate itself lives in ``tests/_fixtures/_ts_bundle_filter.py``,
    shared with the browser conftests' configure-time drift guard so the two
    cannot diverge (that twin exists because this function's zero-match guard
    is armed only under ``make dashboard`` — see ``ts_coverage`` below — and
    CI's nox-driven matrix would otherwise never run any bundle-drift check).

    Every armed chromium browser test loads one of our bundles EXCEPT a test
    explicitly marked ``@pytest.mark.no_bundle_page`` (e.g. the access-key
    refusal path, which deliberately renders a script-free 403 hint page) —
    that marker is the exhaustive exception list, threaded through here as
    ``allow_no_match`` by the ``ts_coverage`` generator below. For every
    unmarked test, a zero-match snapshot is always a broken filter or a
    stale/missing build, never a legitimate state — see the guard at the end
    of this function.
    """
    data = client.send("Profiler.takePreciseCoverage")
    client.send("Profiler.stopPreciseCoverage")
    matched = False
    for entry in data["result"]:
        if bundle_url_matches(entry.get("url", "")):
            sink.append(entry)
            matched = True
    if not matched and not allow_no_match:
        seen = sorted({e.get("url", "") for e in data["result"]})[:10]
        raise RuntimeError(
            "ts-coverage: collection was armed and a snapshot taken, but no "
            "script URL matched the bundle filter — the filter and the built "
            "bundles have drifted, or the build is stale/missing. URLs seen: "
            f"{seen}"
        )


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

    ``@pytest.mark.no_bundle_page`` opts a test out of the zero-match guard
    in ``collect_ts_coverage`` (not out of collection itself — the CDP
    session still starts and the snapshot is still taken and merged into
    ``sink``): it names a test that deliberately renders a page with none of
    our bundles, e.g. the access-key refusal path's 403 hint HTML.
    """
    if (
        not ambient("OTTO_TS_COVERAGE")
        or request.node.get_closest_marker("browser") is None
        or request.node.get_closest_marker("soak") is not None
    ):
        yield
        return
    if request.getfixturevalue("browser_name") != "chromium":
        yield
        return
    allow_no_match = request.node.get_closest_marker("no_bundle_page") is not None
    client = start_ts_coverage(request.getfixturevalue("page"))
    yield
    collect_ts_coverage(client, sink, allow_no_match=allow_no_match)
