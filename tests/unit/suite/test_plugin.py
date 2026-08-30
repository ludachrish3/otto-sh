import contextlib
import sys
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import UnixHost
from otto.host.login_proxy import Cred
from otto.models import MetricPoint
from otto.monitor.collector import MetricCollector
from otto.suite.plugin import OttoPlugin

pytest_plugins = ["pytester"]

SUT_DIR = Path("/sut/repo1/tests")


def make_plugin(*sut_dirs: Path) -> OttoPlugin:
    return OttoPlugin(sut_test_dirs=list(sut_dirs))


def ignore(plugin: OttoPlugin, path: Path) -> bool | None:
    return plugin.pytest_ignore_collect(path, MagicMock())


def _make_host(host_id: str = "router1") -> UnixHost:
    """A real (unconnected — no I/O at construction) UnixHost.

    ``--monitor``'s session fixture now unconditionally builds a
    :func:`~otto.monitor.session.snapshot_lab` snapshot (spec 2026-07-12), and
    ``HostSnapshot``'s fields are real ``str``s — a bare ``MagicMock(spec=
    UnixHost)`` fails that pydantic validation because its unset attributes
    (``element``, ``ip``, ...) are auto-vivified ``Mock`` objects, not
    strings. A plain-element host slugs its ``id`` to itself (see
    ``otto.host.remote_host.make_host_id``), so ``element=host_id`` also
    pins ``host.id == host_id`` for tests that assert on it.
    """
    return UnixHost(ip="10.0.0.1", element=host_id, creds=[Cred(login="admin", password="secret")])


def test_no_sut_dirs_allows_any_path():
    plugin = OttoPlugin()
    assert ignore(plugin, Path("/anywhere/test_foo.py")) is None


def test_ignores_path_outside_sut_dirs():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, Path("/other/tests/test_foo.py")) is True


def test_allows_file_inside_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR / "test_example.py") is None


def test_allows_sut_dir_itself():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR) is None


def test_allows_ancestor_of_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, Path("/sut/repo1")) is None
    assert ignore(plugin, Path("/sut")) is None


def test_allows_nested_path_inside_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR / "subdir" / "test_deep.py") is None


def test_multiple_sut_dirs_either_allows():
    sut1 = Path("/sut/repo1/tests")
    sut2 = Path("/sut/repo2/tests")
    plugin = make_plugin(sut1, sut2)
    assert ignore(plugin, sut1 / "test_a.py") is None
    assert ignore(plugin, sut2 / "test_b.py") is None
    assert ignore(plugin, Path("/sut/repo3/tests/test_c.py")) is True


# ── --monitor session lifecycle ──────────────────────────────────────────────


class _FixtureRunner:
    """Drive the underlying async generator behind a pytest_asyncio.fixture.

    The decorator wraps the user's async function and stashes the original
    on ``__wrapped__``; using that lets us call it directly without invoking
    pytest's fixture machinery.
    """

    @staticmethod
    async def setup(plugin: OttoPlugin):
        gen = plugin._otto_session_monitor.__wrapped__(plugin)
        await gen.__anext__()
        return gen

    @staticmethod
    async def teardown(gen):
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_session_monitor_disabled_is_noop():
    """When ``--monitor`` is off the fixture must not touch hosts or write files."""
    plugin = OttoPlugin(monitor=False)
    with (
        patch("otto.config.all_hosts") as p_hosts,
        patch("otto.monitor.factory.build_monitor_collector") as p_build,
    ):
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)
    p_hosts.assert_not_called()
    p_build.assert_not_called()


# ── --monitor-hosts: three emptinesses, three texts ─────────────────────────

# THE SAME DISCIPLINE `otto monitor` KEEPS (see otto/cli/monitor.py's four
# outcomes). A `--monitor-hosts` run that collects nothing ends up in exactly
# one of three states, and the user must be able to tell which from the text:
#
# 1. the pattern fullmatched nothing        -> EmptySelectionError (regex)
# 2. every match was flag-excluded          -> EmptySelectionError (flag)
# 3. hosts WERE selected, none monitorable  -> this plugin's own warning
#
# The first two are a USAGE mistake in the invocation: the run was asked for
# something it cannot deliver, so it stops with the error's own message and no
# traceback rather than erroring one fixture per test. The third is not the
# user's selection at all — it is a lab where nothing can be sampled — so it
# disables collection and lets the tests run, and it must not blame the regex:
# reaching it PROVES the regex matched.

_INNER_SUITE = """\
def test_one():
    assert True
"""

_INNER_ARGS = (
    # A nested in-process session: pytest-playwright's session-wide call
    # wrapper rejects re-entry, and the outer suite's `filterwarnings=error`
    # turns pytest-asyncio's unset-loop-scope deprecation into an
    # INTERNALERROR at inner configure. Same overrides as
    # test_options_plugin.py's inner runs.
    "-p",
    "no:cacheprovider",
    "-p",
    "no:playwright",
    "-o",
    "asyncio_default_fixture_loop_scope=function",
    # The warning below is the ARTIFACT under test, so the inner session has to
    # print it: pytest holds captured logs back unless a test fails.
    "-o",
    "log_cli=true",
    "-o",
    "log_cli_level=WARNING",
)


def _inner_run(pytester, plugin, hosts):
    """Run a one-test session under *plugin*, with ``all_hosts`` answering *hosts*.

    *hosts* is either a list to yield or an exception to raise from INSIDE the
    generator — which is where the real ``all_hosts`` raises it, at the first
    ``next()``, so a guard around the call alone would not catch it. The two
    doubles are written separately for exactly that reason: one has to BE a
    generator function, and folding the yield into the other would make its
    ``return iter(hosts)`` a ``StopIteration`` value nobody sees.
    """
    if isinstance(hosts, BaseException):

        def _all_hosts(*args, **kwargs):
            del args, kwargs
            raise hosts
            yield  # pragma: no cover — unreachable; makes this a generator function

    else:

        def _all_hosts(*args, **kwargs):
            del args, kwargs
            return iter(hosts)

    pytester.makepyfile(test_inner=_INNER_SUITE)
    with patch("otto.config.all_hosts", _all_hosts):
        return pytester.runpytest_inprocess(*_INNER_ARGS, plugins=[plugin])


def test_monitor_hosts_matching_nothing_stops_the_run_with_one_line(pytester):
    """A stale ``--monitor-hosts`` regex fails LOUD, framed — never a fixture traceback.

    Post-D6 the fleet surface refuses an empty selection instead of yielding
    nothing, and that refusal was escaping a session-scoped fixture raw: a
    traceback through pytest-asyncio's internals, repeated for every test in
    the session, for what is a one-line mistake in the invocation. The user
    asked for a monitored run over hosts that do not exist; the honest outcome
    is to stop and say so, in the error's own words.
    """
    from otto.config.scope import EmptySelectionError

    result = _inner_run(
        pytester,
        OttoPlugin(monitor=True, monitor_hosts="sensor"),
        EmptySelectionError("sensor", 3),
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    out = " ".join(result.outlines)
    assert "--monitor-hosts" in out
    assert "fullmatches none of the 3 host(s)" in out
    assert "'sensor.*'" in out  # the wildcard to add
    assert "Traceback" not in out
    assert "no tests ran" in out


def test_monitor_hosts_hidden_by_a_flag_says_the_regex_is_not_the_problem(pytester):
    """The other EmptySelectionError, and it must arrive with its own text intact.

    A pattern that MATCHED and was then emptied by a membership flag needs the
    opposite advice from one that matched nothing — widening it changes
    nothing — so the framing must not flatten the two into one message.
    """
    from otto.config.scope import EmptySelectionError

    result = _inner_run(
        pytester,
        OttoPlugin(monitor=True, monitor_hosts="box.*"),
        EmptySelectionError("box.*", 3, excluded_by=["include_containers"], matched_size=2),
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    out = " ".join(result.outlines)
    assert "--monitor-hosts" in out
    assert "The regex is not the problem" in out
    assert "include_containers=True" in out


def test_selected_but_unmonitorable_hosts_blame_the_hosts_not_the_regex(pytester):
    """The surviving false blame, fixed: reaching this branch PROVES the regex matched.

    ``no hosts matching "X"`` was the old label, and after the D6 guard landed
    it could only fire here — where the pattern matched and every match was
    unmonitorable. It sent the reader to widen a regex that was already right,
    past the real cause. The truthful message names the hosts and the reason,
    and the run CONTINUES: an unmonitorable lab is not a reason to fail tests
    that were never about monitoring.
    """
    from otto.host.factory import create_host_from_dict

    # Through the factory, like the monitor CLI's twin test: a MagicMock would
    # answer the production predicate (``isinstance(h, UnixHost)``) however the
    # test wanted, and prove nothing about a real console host.
    board = create_host_from_dict(
        {"element": "board", "os_type": "zephyr", "ip": "10.0.0.9", "board": "mps2"}
    )
    result = _inner_run(pytester, OttoPlugin(monitor=True, monitor_hosts="board.*"), [board])

    assert result.ret == pytest.ExitCode.OK
    out = " ".join(result.outlines)
    assert board.id in out
    assert "shell" in out  # what a monitorable host has to offer
    assert "collection disabled" in out
    assert "board.*" not in out  # the retracted claim: the regex is innocent here


@pytest.mark.asyncio
async def test_session_monitor_no_matching_hosts_skips(tmp_path):
    """An empty host match must not crash; no collector built, no file written."""
    plugin = OttoPlugin(
        monitor=True,
        monitor_hosts="will-not-match",
        monitor_output=tmp_path / "m.json",
    )
    with (
        patch("otto.config.all_hosts", return_value=iter([])) as p_hosts,
        patch("otto.monitor.factory.build_monitor_collector") as p_build,
    ):
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)
    p_hosts.assert_called_once()
    p_build.assert_not_called()
    assert not (tmp_path / "m.json").exists()


@pytest.mark.asyncio
async def test_session_monitor_publishes_collector_and_exports_json(tmp_path):
    """When enabled with hosts, the fixture must:
    - publish the collector on OttoSuite for per-test fixtures to find
    - export a format:1 monitor document (with a session) when monitor_output
      has a non-.db suffix
    - stamp that session's end on a clean teardown (a null end is the
      producer's deliberate crash marker; see test_export_producer.py)
    - clear the class-level collector reference on teardown
    """
    from otto.models import MonitorExport
    from otto.suite.suite import OttoSuite

    out_path = tmp_path / "monitor.json"
    plugin = OttoPlugin(
        monitor=True,
        monitor_interval=1.0,
        monitor_output=out_path,
    )

    fake_host = _make_host("router1")
    # A REAL (empty) collector: build_live_export() calls its get_series()/
    # get_events()/get_meta_model()/etc for real, and a MagicMock's
    # auto-vivified return values fail those reshapes' pydantic validation.
    real_collector = MetricCollector(targets=[])

    with (
        patch("otto.config.all_hosts", return_value=iter([fake_host])),
        patch(
            "otto.monitor.factory.build_monitor_collector", return_value=real_collector
        ) as p_build,
    ):
        gen = await _FixtureRunner.setup(plugin)
        # While the fixture body is suspended at yield the class attr is set.
        assert OttoSuite._session_monitor_collector is real_collector
        await _FixtureRunner.teardown(gen)

    # Build was invoked with the host list; db is None for .json output.
    assert p_build.call_args.kwargs["db"] is None
    # Verify the actual artifact — not a mock-call assertion: it must parse
    # as a format:1 MonitorExport carrying exactly one (this run's) session.
    export = MonitorExport.model_validate_json(out_path.read_text())
    assert export.format == 1
    assert len(export.sessions) == 1
    assert export.sessions[0].end is not None, "a clean teardown left the session's end unstamped"
    assert OttoSuite._session_monitor_collector is None


@pytest.mark.asyncio
async def test_session_monitor_db_output_persists_real_lab_and_meta(tmp_path):
    """A ``.db`` output writes a session row carrying the REAL lab AND meta.

    Asserts on the persisted artifact (round-tripped back out via
    ``build_db_export``), not on the ``MetricDB``'s constructor args: an
    empty ``meta_json`` would render a DB-backed suite run with no chart
    specs and no units — the same degradation an empty ``chart_map`` caused
    (see otto.monitor.export / web/src/data/seriesTree.ts).

    Deliberately drives the REAL factory (no ``build_monitor_collector``
    patch) so the collector under test is the one the plugin actually
    builds: its parser catalog is what populates ``meta``, and a MagicMock
    would prove nothing about it. ``init_db()`` is awaited by hand because
    the session fixture does not drive ``run()`` (the class-scoped fixture
    does) — that is what INSERTs the session row.

    Also asserts the teardown stamps ``end`` on a clean run. Checked via
    ``read_sessions()``'s RAW row, not ``build_db_export()``'s reshaped
    ``SessionRecord``: the producer's ``_fallback_end`` deliberately papers
    over a null ``end`` (falling back to the last metric's timestamp, or —
    as here, where no tick ever ran — the session's own ``start``), so
    ``SessionRecord.end`` is NEVER ``None`` and can't tell a finalized
    session from a crashed one. Only the archive's own column can.
    """
    from otto.monitor.db import read_sessions
    from otto.monitor.export import build_db_export
    from otto.suite.suite import OttoSuite

    out_path = tmp_path / "monitor.db"
    plugin = OttoPlugin(monitor=True, monitor_interval=3.0, monitor_output=out_path)

    with patch("otto.config.all_hosts", return_value=iter([_make_host("router1")])):
        gen = await _FixtureRunner.setup(plugin)
        collector = OttoSuite._session_monitor_collector
        assert collector is not None
        await collector.init_db()  # session row INSERTed here
        await _FixtureRunner.teardown(gen)  # collector.close() closes the DB

    (session,) = build_db_export(str(out_path)).sessions
    # lab: the real snapshot, not "{}"
    assert [h.id for h in session.lab.hosts] == ["router1"]
    # meta: the real parser catalog, not "{}" — chart specs carry the units
    # and grouping the review shell renders from.
    assert session.meta.charts, "session meta persisted with no chart specs"
    assert "CPU" in [c.chart for c in session.meta.charts]
    assert session.meta.tabs, "session meta persisted with no tabs"
    # meta.interval: --monitor-interval, threaded through explicitly. The
    # collector has not run() at meta-write time, so its own recorded interval
    # is still None — a null here would persist forever and leave the replayed
    # session's derived health unresolvable (web/src/data/health.ts).
    assert session.meta.interval == 3.0
    # end: a clean teardown must stamp the RAW column, not rely on the
    # producer's crash-tolerant fallback to paper over a null one.
    (raw_session,) = read_sessions(str(out_path))
    assert raw_session.end is not None, "a clean teardown left the session's end unstamped"
    assert OttoSuite._session_monitor_collector is None


@pytest.mark.asyncio
async def test_session_monitor_does_not_start_run_task(tmp_path):
    """Regression: the session fixture must not start ``collector.run()``.

    OttoSuite tests use ``loop_scope='class'``; a task on the session loop
    is starved while class loops drive tests, so metrics never collect.
    The class-scoped fixture is responsible for the run task instead.
    """
    plugin = OttoPlugin(
        monitor=True,
        monitor_output=tmp_path / "monitor.json",
    )
    fake_host = _make_host("router1")
    # A real (empty) collector — see test_session_monitor_publishes_collector_
    # and_exports_json for why a MagicMock can't stand in here (the .json
    # output branch calls build_live_export() for real) — with .run swapped
    # for a spy so this test can assert it was never awaited.
    real_collector = MetricCollector(targets=[])
    real_collector.run = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("otto.config.all_hosts", return_value=iter([fake_host])),
        patch("otto.monitor.factory.build_monitor_collector", return_value=real_collector),
    ):
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)

    real_collector.run.assert_not_awaited()
    real_collector.run.assert_not_called()


# ── --monitor class-scoped run task ─────────────────────────────────────────


class _ClassFixtureRunner:
    """Drive the class-scoped ``_otto_class_monitor_task`` fixture directly."""

    @staticmethod
    async def setup(plugin: OttoPlugin):
        gen = plugin._otto_class_monitor_task.__wrapped__(plugin)
        await gen.__anext__()
        return gen

    @staticmethod
    async def teardown(gen):
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_class_monitor_task_disabled_is_noop():
    """When ``--monitor`` is off the class fixture must not touch the collector."""
    from otto.suite.suite import OttoSuite

    OttoSuite._session_monitor_collector = None
    plugin = OttoPlugin(monitor=False)
    gen = await _ClassFixtureRunner.setup(plugin)
    await _ClassFixtureRunner.teardown(gen)
    # No collector available, no harm.
    assert OttoSuite._session_monitor_collector is None


@pytest.mark.asyncio
async def test_class_monitor_task_no_collector_is_noop():
    """If the session fixture didn't publish a collector, the class fixture is inert."""
    from otto.suite.suite import OttoSuite

    OttoSuite._session_monitor_collector = None
    plugin = OttoPlugin(monitor=True)
    # Should not raise even though collector is None.
    gen = await _ClassFixtureRunner.setup(plugin)
    await _ClassFixtureRunner.teardown(gen)


@pytest.mark.asyncio
async def test_class_monitor_task_starts_and_cancels_run():
    """Verify the class fixture creates the ``run()`` task on the active loop
    and cancels it cleanly on teardown."""
    import asyncio

    from otto.suite.suite import OttoSuite

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_run(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    fake_collector = MagicMock()
    fake_collector.run = fake_run
    OttoSuite._session_monitor_collector = fake_collector

    try:
        plugin = OttoPlugin(monitor=True, monitor_interval=1.0)
        gen = await _ClassFixtureRunner.setup(plugin)
        # Yield once so the task gets a chance to start.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await _ClassFixtureRunner.teardown(gen)
        assert cancelled.is_set()
    finally:
        OttoSuite._session_monitor_collector = None


@pytest.mark.asyncio
async def test_class_monitor_task_runs_on_class_loop_collecting_metrics():
    """End-to-end behavioural test for the original bug.

    With the broken design (run task on session loop, tests on class loop),
    ``_collect_one`` never executes. Drive the class fixture on a loop that's
    actively ticking (this test's own loop) and verify that the run coroutine
    actually progresses and populates a fake collector's series during the
    "test" body. This is the regression guard for the empty-metrics bug.
    """
    import asyncio

    from otto.suite.suite import OttoSuite

    fake_collector = MagicMock()
    series_appends = []

    async def fake_run(*args, **kwargs):
        # Simulate a collection loop ticking every 10ms.
        while True:
            await asyncio.sleep(0.01)
            series_appends.append(1)

    fake_collector.run = fake_run
    OttoSuite._session_monitor_collector = fake_collector

    try:
        plugin = OttoPlugin(monitor=True, monitor_interval=0.01)
        gen = await _ClassFixtureRunner.setup(plugin)
        # "Test" body: yield control so the run task can tick repeatedly.
        await asyncio.sleep(0.1)
        await _ClassFixtureRunner.teardown(gen)
    finally:
        OttoSuite._session_monitor_collector = None

    # If the task didn't run, this list would be empty — exactly the bug.
    assert len(series_appends) > 0, (
        "collector.run() never progressed — this is the original bug "
        "(metrics empty in monitor.json)."
    )


# ── End-to-end: monitor with class-scoped-loop tests writes metrics to JSON ─


def test_e2e_monitor_collects_metrics_under_class_loop_scope(tmp_path):
    """Run an embedded pytest session that mirrors the original bug shape.

    Simulates the user's failing setup:
      * ``OttoPlugin`` configured with ``--monitor`` and a JSON output path.
      * A test class using ``@pytest.mark.asyncio(loop_scope="class")`` so its
        tests run on a class-scoped event loop, NOT the session loop.

    The patched ``collector.run()`` ticks every 10ms and writes synthetic
    metrics into ``_series``. Pre-fix this task was created on the session
    loop (dormant during class-scoped tests) and never ticked — exported
    JSON had empty ``metrics``. Post-fix the task is created on the class
    loop and ticks freely while the test runs.
    """
    import asyncio
    import json
    import textwrap
    from collections import deque
    from datetime import datetime

    suite_path = tmp_path / "test_suite.py"
    suite_path.write_text(
        textwrap.dedent("""
        import asyncio
        import pytest

        @pytest.mark.asyncio(loop_scope="class")
        class TestClassLoopSuite:
            async def test_a(self):
                # Yield repeatedly so whichever loop is hosting the run task
                # has many opportunities to tick. With the bug, the task is
                # on the session loop (dormant) and ticks zero times here.
                for _ in range(20):
                    await asyncio.sleep(0.01)
    """)
    )

    out_path = tmp_path / "monitor.json"
    plugin = OttoPlugin(
        monitor=True,
        monitor_interval=0.01,
        monitor_output=out_path,
    )

    real_collector = MetricCollector(targets=[])

    async def fake_run(*_a, **_kw):
        # Mimic _process_host_results: append to the store's series on each tick.
        while True:
            await asyncio.sleep(0.01)
            real_collector._store.series.setdefault("host1/cpu", deque()).append(
                MetricPoint(ts=datetime.now(tz=timezone.utc), value=42.0, meta=None)
            )

    real_collector.run = fake_run  # type: ignore[method-assign]

    fake_host = _make_host("host1")
    with (
        patch("otto.config.all_hosts", return_value=iter([fake_host])),
        patch("otto.monitor.factory.build_monitor_collector", return_value=real_collector),
    ):
        try:
            exit_code = pytest.main(
                [
                    "-s",
                    "-p",
                    "no:cacheprovider",
                    # This inner session runs in-process and shares the
                    # interpreter with the outer one. pytest-playwright installs
                    # a session-wide pytest_runtest_call wrapper (for its
                    # soft-assertion expect()) that runs for every test, not
                    # just ones using its fixtures; the outer test's call is
                    # already inside that wrapper, so entering it again here
                    # raises "nested soft assertion scopes are not supported".
                    # The generated suite doesn't need Playwright, so disabling
                    # it here just avoids the collision.
                    "-p",
                    "no:playwright",
                    "--override-ini",
                    "addopts=",
                    "-o",
                    "asyncio_mode=auto",
                    "-o",
                    "asyncio_default_fixture_loop_scope=function",
                    str(suite_path),
                ],
                plugins=[plugin],
            )
        finally:
            # This in-process pytest.main() imports the generated suite as a
            # top-level module keyed by stem. Evict it so a second run of this
            # test in the same process (e.g. under `pytest --count`) imports a
            # fresh module instead of hitting "import file mismatch".
            sys.modules.pop(suite_path.stem, None)
            # The inner pytest.main() leaks a pytest-asyncio loop; the
            # root-conftest loop reaper (tests/_loop_reaper.py) closes it at
            # this test's teardown boundary, so no local cleanup is needed.

    assert exit_code == 0, f"embedded pytest run failed: {exit_code}"
    assert out_path.exists(), "monitor.json was not written"

    # format:1 document now — one session, its metrics nested underneath
    # (not the legacy flat {"metrics": [...]} shape).
    payload = json.loads(out_path.read_text())
    assert payload["format"] == 1
    metrics = payload["sessions"][0]["metrics"]
    assert len(metrics) > 0, f"Regression: monitor.json contains no metrics. payload={payload}"
    assert metrics[0]["host"] == "host1"
    assert metrics[0]["label"] == "cpu"


# ── pytest_report_teststatus ─────────────────────────────────────────────────


def _status(
    when: str,
    outcome: str,
    *,
    wasxfail: bool = False,
) -> tuple[str, str, str] | None:
    """Run the hook over a synthetic report for *when* x *outcome*."""
    report = pytest.TestReport(
        nodeid="test_x.py::test_a",
        location=("test_x.py", 0, "test_a"),
        keywords={},
        outcome=outcome,  # type: ignore[arg-type]
        longrepr=None,
        when=when,  # type: ignore[arg-type]
    )
    if wasxfail:
        report.wasxfail = ""  # type: ignore[attr-defined]
    return OttoPlugin().pytest_report_teststatus(report, MagicMock())


@pytest.mark.parametrize(
    ("when", "outcome", "expected"),
    [
        # Only the call phase contributes to the "passed" count — a passing
        # setup/teardown must return the empty category, or one test is
        # counted three times ("3 passed" for a one-test suite).
        ("setup", "passed", ("", "", "")),
        ("teardown", "passed", ("", "", "")),
        ("call", "passed", ("passed", "", "PASSED")),
        ("setup", "failed", ("error", "", "ERROR")),
        ("teardown", "failed", ("error", "", "ERROR")),
        ("call", "failed", ("failed", "", "FAILED")),
        ("setup", "skipped", ("skipped", "", "SKIPPED")),
        ("teardown", "skipped", ("skipped", "", "SKIPPED")),
        ("call", "skipped", ("skipped", "", "SKIPPED")),
    ],
)
def test_teststatus_mirrors_pytest_categories(
    when: str, outcome: str, expected: tuple[str, str, str]
) -> None:
    assert _status(when, outcome) == expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("skipped", ("xfailed", "", "XFAIL")),
        ("passed", ("xpassed", "", "XPASS")),
    ],
)
def test_teststatus_mirrors_xfail_categories(outcome: str, expected: tuple[str, str, str]) -> None:
    assert _status("call", outcome, wasxfail=True) == expected


def test_teststatus_letter_is_always_blank() -> None:
    """The whole point of the override: no per-test progress character."""
    for when in ("setup", "call", "teardown"):
        for outcome in ("passed", "failed", "skipped"):
            status = _status(when, outcome)
            assert status is not None
            assert status[1] == ""
