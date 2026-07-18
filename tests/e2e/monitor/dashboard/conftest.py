"""Dashboard e2e fixtures: a scripted live server and a historical server.

``live_dash`` backs the wire-contract pins in ``test_harness.py``
(untouchable — see that module's docstring) as well as the harness's own
self-tests; it stays even though the browser-marked DOM-parity specs that
used to exercise it through a page were retired in the Playwright pivot
(plan 2026-07-11) in favor of ``shell_dash`` below. ``historical_dash`` (the
--file-replay historical server) was retired with it in the sessionized
producer's review-mode cutover (plan 2026-07-12, Task 4) — the mode/document
pins in ``test_harness.py`` exercise the same "not live" wire contract via
its own ``review_dash`` there instead.

This module also defines a ``review_dash`` (Task 7) — same name, deliberately
distinct fixture. pytest resolves a same-named fixture defined directly in a
test module (test_harness.py's) ahead of one from a conftest.py, so the two
never collide: test_harness.py keeps its hand-built, hostless wire-contract
document, while every other module here (test_review_shell.py) gets this
one — a *renderable* document (built from the committed
web/fixtures/minimal.json) for driving the real built page through
Playwright, matching this file's own dist-serving ``shell_dash`` idiom.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.models import HostSnapshot, LabSnapshot, MonitorExport
from otto.monitor.collector import MetricCollector
from otto.monitor.server import _dist_index_path
from otto.monitor.session import new_frame
from tests._fixtures._browser_guard import browser_tests_could_run
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector
from tests._fixtures._ts_coverage import (
    collect_ts_coverage,
    start_ts_coverage,
    write_ts_coverage,
)

_FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_SRC = _REPO_ROOT / "web" / "src"


def _stale_dist_reason() -> str | None:
    """Report the newest ``web/src`` file that post-dates the built bundle.

    These tests drive the BUILT dashboard, not the sources — and ``pytest``
    does not build it. Only ``make web`` (and CI, which runs ``make web``
    ahead of the nox call) does. So editing ``web/src`` and re-running pytest
    directly silently re-tests the PREVIOUS bundle, and every assertion about
    the change passes or fails for reasons that have nothing to do with the
    code under test.

    That is not hypothetical: it is how issue #131 shipped. The final edge
    routing constants were verified green in this lane three times without the
    bundle ever being rebuilt; CI, which does build, was the first place they
    ran, and they were broken. A green browser test against a stale bundle is
    worse than no test — it actively certifies the wrong artifact.

    Returns ``None`` when the bundle is current (or when there are no web
    sources to compare against, e.g. an installed sdist).
    """
    if not _WEB_SRC.is_dir():
        return None
    try:
        built = _dist_index_path().stat().st_mtime
    except RuntimeError:
        return None  # missing entirely — the existing guard below reports that
    newest, newest_path = 0.0, None
    for path in _WEB_SRC.rglob("*"):
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if mtime > newest:
            newest, newest_path = mtime, path
    if newest_path is None or newest <= built:
        return None
    return (
        f"The built dashboard is STALE: {newest_path.relative_to(_REPO_ROOT)} is newer "
        f"than the bundle these tests serve. pytest does not build the web dist — run "
        f"`make web` first, or you will be testing the previous bundle (see issue #131)."
    )


def pytest_configure(config: pytest.Config) -> None:
    """Session guard: fail fast with one clear message if the React build is missing.

    Every ``browser``-marked test in this suite drives the *real* built page
    through Playwright (via ``DashboardHarness``), which needs an actual
    ``dist/index.html`` — a marker page won't do, since these tests assert
    on real rendered DOM/JS/CSS. ``MonitorServer.__init__`` already refuses
    to build without it (see ``_dist_index_path``); without this guard, that
    surfaces as a fixture error on whichever test happens to run first —
    accurate but easy to mistake for a one-off failure. Checking once here
    gives the same fix-it message as a single clear failure instead of N
    noisy fixture errors.

    ``test_harness.py`` is unaffected in every real lane: it's the one module
    in this directory that is *not* ``browser``-marked (it hits ``/api/*``
    only, never the rendered page) and carries its own fixture that tolerates
    a missing build by standing in a marker page — it must keep running in
    every hostless lane on a checkout that skipped ``make web``. The check is
    ``-m``-based, not path-based, so one niche invocation is a knowing false
    positive: ``pytest .../test_harness.py`` by itself with no ``-m`` on a
    dist-less checkout trips the guard even though that module is hermetic.
    That's accepted over path/nodeid introspection (which would have to
    re-derive "which files hold browser tests" that only collection knows):
    the guard never lets browser tests fail confusingly, the false positive
    is a single ``run make web`` message, and it self-remedies on the next
    build.

    This must stay a plain ``config``-based check, not an item-based one
    (e.g. ``pytest_collection_modifyitems``/``pytest_collection_finish``
    inspecting ``session.items`` post-deselection): under
    ``pytest-xdist``, raising ``pytest.exit()`` (or any exception) from a
    hook that fires *after* a worker's ``pytest_sessionstart`` — which
    every collection-time hook does — crashes the controller
    (``AssertionError`` / ``RuntimeError`` in ``xdist/dsession.py``,
    verified empirically) instead of the clean single-message exit
    ``pytest_configure`` gives, because the worker has already announced
    itself ready (and, depending on hook ordering, sent its collection
    report) by then. ``pytest_configure`` is historic (pytest replays it
    for every conftest registered mid-collection, which is how this fires
    for a directory a run never intended to select from), so instead of
    keying off "did anything from this directory survive collection" we
    key off "could a ``browser`` item survive this session's ``-m``
    expression at all" — computable from ``config`` alone, before
    collection exists, and evaluated with pytest's own expression engine
    rather than a hand-rolled string check.

    The ``-m``-expression check itself now lives in
    ``tests/_fixtures/_browser_guard.py``, shared with the coverage-report
    browser suite.
    """
    if not browser_tests_could_run(config):
        return
    try:
        _dist_index_path()
    except RuntimeError as exc:
        pytest.exit(str(exc), returncode=1)
    stale = _stale_dist_reason()
    if stale is not None:
        pytest.exit(stale, returncode=1)


@pytest.fixture(autouse=True)
def _generous_playwright_timeout(request: pytest.FixtureRequest) -> None:
    """Give browser actions/navigations more headroom than Playwright's 30s default.

    These suites are solid — they pass hundreds of consecutive runs at ~33s
    each even under coverage instrumentation. But a rare, purely environmental
    ~10x slowdown (a loaded gate host: one run clocked ~340s for the same
    command and coverage) makes an otherwise-fine ``page.click``/navigation
    blow past Playwright's default 30s action timeout and fail as a flake, not
    a bug. Doubling the ceiling to 60s absorbs that transient with zero cost on
    fast runs (fast actions still return immediately; the timeout is only an
    upper bound). Scoped to ``browser``-marked tests so the hermetic
    ``test_harness.py`` lane never instantiates ``page``.
    """
    if request.node.get_closest_marker("browser") is None:
        return
    page = request.getfixturevalue("page")
    page.set_default_timeout(60_000)
    page.set_default_navigation_timeout(60_000)


_PROC_META = {
    "Command": "stress",
    "User": "root",
    "Mem": "1.0%",
    "RSS": "10 M",
    "Stat": "R",
    "CPU Time": "0:01.00",
}


def _preload(harness: DashboardHarness[FakeCollector]) -> None:
    """Three 5s-spaced ticks for two hosts: overall CPU, two procs, memory, load."""
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=15)
    push = harness.collector.push
    for tick in range(3):
        ts = t0 + timedelta(seconds=5 * tick)
        for host in ("host1", "host2"):
            harness.run(push(host, "Overall CPU", 20.0 + tick, ts=ts))
            harness.run(push(host, "proc/101", 5.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "proc/202", 3.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "Memory Usage", 40.0 + tick, chart="memory", ts=ts))
            harness.run(push(host, "Load (1m)", 0.5 + tick, chart="load", ts=ts))


@pytest.fixture
def live_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    harness = DashboardHarness(FakeCollector()).start()
    _preload(harness)
    yield harness
    harness.stop()


# Every host id any Plan 5b Task 13 live-shell/soak spec pushes a point for.
# Declared up front (one shared lab snapshot) rather than per-test: a host
# tile/subject-link/chart only exists in the DOM for a host that is a member
# of `lab.hosts` (deriveElements(), web/src/data/exportDoc.ts) — pushing a
# point for an undeclared host records it server-side but never surfaces a
# clickable tile, so a test that goes straight from `push()` to
# `page.locator('[data-testid="subject-link-..."]')` would simply hang
# against an element that can never appear.
_LIVE_STREAM_HOSTS = ["r1", "r2", *[f"h{i}" for i in range(7)]]

# healthForHosts (web/src/data/health.ts) only considers a sample "seen" when
# its timestamp falls >= session.startMs — a point pushed with an EARLIER
# timestamp is invisible to health, not "stale" (a real gap needs a real
# prior sample within the session's own lifetime; otherwise it's genuinely
# "no-data", a different status). Backdating the frame by an hour gives every
# spec in this lane room to push a "went silent N seconds ago" point without
# racing the wall clock: real-time frame.start (`new_frame`'s default) would
# put session.startMs at fixture setup, only milliseconds before the test
# body's own push() call, leaving no room for a point old enough to cross the
# down threshold (cadence x HEALTH_K) while still landing inside the session.
_FRAME_BACKDATE = timedelta(hours=1)


@pytest.fixture
def live_stream_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    """A live-mode, dist-serving harness the test can push points into.

    Unlike ``live_dash``, this one carries a ``frame`` + ``lab``, so
    ``/api/monitor_sessions`` serves a real live snapshot and the shell
    hydrates without an Import step (see ``bootstrapFromServer``, which
    fetches ``/api/mode`` then ``/api/monitor_sessions`` for both server
    modes). ``lab`` declares every host id the Task 13 specs push to, each
    its own singleton element — see ``_LIVE_STREAM_HOSTS``.

    ``interval=5.0`` stamps the run's collection cadence up front (see
    ``FakeCollector.__init__``'s docstring): without it, ``push()`` alone
    never sets it (unlike a real ``collector.run()``), and both the
    liveness clock (``OverviewPage``'s ``useNow``) and dimming
    (``data/health.ts``'s cadence resolution) stay permanently unresolvable.
    The frame itself is backdated (see ``_FRAME_BACKDATE``) so a "went silent
    N seconds ago" spec has room to land inside the session.
    """
    harness = DashboardHarness(
        FakeCollector(interval=5.0),
        mode="live",
        frame=new_frame(
            label="live run", note=None, now=datetime.now(tz=timezone.utc) - _FRAME_BACKDATE
        ),
        lab=LabSnapshot(hosts=[HostSnapshot(id=h, element=h) for h in _LIVE_STREAM_HOSTS]),
    ).start()
    yield harness
    harness.stop()


@pytest.fixture
def shell_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    """A dist-serving harness with an empty collector — the review shell
    makes no boot-time API calls; data arrives via client-side Import."""
    harness = DashboardHarness(FakeCollector())
    harness.start()
    yield harness
    harness.stop()


def _review_boot_document() -> MonitorExport:
    """A two-session ``format:1`` document for the boot-hydration specs.

    Built from the committed ``web/fixtures/minimal.json`` — the same file
    every Import-driven spec in this directory already trusts — duplicated
    into a second, noted session (Task 7 brief: no new fixture file, no
    generator change). ``model_copy`` is shallow, so both sessions share the
    same lab/metrics/chart_map objects; that's fine here since nothing
    mutates them and only the id/label/note need to differ for the picker.
    """
    raw = json.loads((_FIXTURES / "minimal.json").read_text(encoding="utf-8"))
    doc = MonitorExport.model_validate(raw)
    first = doc.sessions[0]
    second = first.model_copy(
        update={"id": f"{first.id}-2", "label": "second", "note": "second run"}
    )
    return MonitorExport(format=1, sessions=[first, second])


@pytest.fixture
def review_dash() -> Iterator[DashboardHarness[MetricCollector]]:
    """A dist-serving, review-mode harness that boots already hydrated.

    Unlike ``shell_dash`` (empty collector; data arrives via client-side
    Import), this server answers ``/api/mode``/``/api/monitor_sessions``
    (Task 6, endpoint renamed in Plan 5b Task 3) with a real two-session
    document — the browser-lane proof that ``otto monitor <source>`` opens
    straight into the dashboard with no Import interaction.
    """
    harness = DashboardHarness(
        MetricCollector(hosts=[], parsers=[]),
        mode="review",
        document=_review_boot_document(),
        source_name="minimal.json",
    )
    harness.start()
    yield harness
    harness.stop()


@pytest.fixture(scope="session")
def _ts_coverage_sink():
    entries: list[dict] = []
    yield entries
    write_ts_coverage(entries)


@pytest.fixture(autouse=True)
def _ts_coverage(request, _ts_coverage_sink):
    """Per-test V8 coverage; suite-wide accumulation. See _ts_coverage.py.

    Guarded on the `browser` marker BEFORE touching any Playwright fixture:
    a bare `page` parameter would force browser parametrization onto this
    conftest's non-browser tests (test_harness.py) and pull sync
    Playwright's event loop into the shared hostless CI process (same trap
    _generous_playwright_timeout documents above).
    """
    if (
        request.node.get_closest_marker("browser") is None
        or request.node.get_closest_marker("soak") is not None
    ):
        yield
        return
    if request.getfixturevalue("browser_name") != "chromium":
        yield
        return
    client = start_ts_coverage(request.getfixturevalue("page"))
    yield
    collect_ts_coverage(client, _ts_coverage_sink)
