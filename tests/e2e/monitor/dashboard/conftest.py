"""Dashboard e2e fixtures: a scripted live server and a historical server."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from otto.monitor.collector import MetricCollector
from otto.monitor.server import _dist_index_path
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

HISTORICAL_JSON = Path(__file__).parent / "data" / "historical.json"


def _browser_tests_could_run(config: pytest.Config) -> bool:
    """Would a bare ``browser``-marked item survive this session's ``-m`` filter?

    ``pytest_configure`` fires before collection, so there's no item list to
    consult yet — evaluate the compiled ``-m`` expression (the same
    ``_pytest.mark.expression.Expression`` pytest itself uses for
    ``-m``/``-k``) directly against a synthetic item that carries only the
    ``browser`` marker, the one every test that needs the real build here
    also carries. An empty expression means nothing is filtered, so browser
    tests trivially survive.
    """
    markexpr = config.option.markexpr
    if not markexpr:
        return True
    return Expression.compile(markexpr).evaluate(lambda name, **_kwargs: name == "browser")


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

    ``test_harness.py`` is unaffected either way: it's the one module in
    this directory that is *not* ``browser``-marked (it hits ``/api/*``
    only, never the rendered page) and carries its own fixture that
    tolerates a missing build by standing in a marker page — it must keep
    running in every hostless lane on a checkout that skipped ``make web``.

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
    """
    if not _browser_tests_could_run(config):
        return
    try:
        _dist_index_path()
    except RuntimeError as exc:
        pytest.exit(str(exc), returncode=1)


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


@pytest.fixture
def historical_dash() -> Iterator[DashboardHarness[MetricCollector]]:
    harness = DashboardHarness(MetricCollector.from_json(str(HISTORICAL_JSON))).start()
    yield harness
    harness.stop()
