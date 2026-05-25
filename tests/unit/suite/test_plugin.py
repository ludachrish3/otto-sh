import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import UnixHost
from otto.suite.plugin import OttoPlugin


SUT_DIR = Path('/sut/repo1/tests')


def make_plugin(*sut_dirs: Path) -> OttoPlugin:
    return OttoPlugin(sut_test_dirs=list(sut_dirs))


def ignore(plugin: OttoPlugin, path: Path) -> bool | None:
    return plugin.pytest_ignore_collect(path, MagicMock())


def test_no_sut_dirs_allows_any_path():
    plugin = OttoPlugin()
    assert ignore(plugin, Path('/anywhere/test_foo.py')) is None


def test_ignores_path_outside_sut_dirs():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, Path('/other/tests/test_foo.py')) is True


def test_allows_file_inside_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR / 'test_example.py') is None


def test_allows_sut_dir_itself():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR) is None


def test_allows_ancestor_of_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, Path('/sut/repo1')) is None
    assert ignore(plugin, Path('/sut')) is None


def test_allows_nested_path_inside_sut_dir():
    plugin = make_plugin(SUT_DIR)
    assert ignore(plugin, SUT_DIR / 'subdir' / 'test_deep.py') is None


def test_multiple_sut_dirs_either_allows():
    sut1 = Path('/sut/repo1/tests')
    sut2 = Path('/sut/repo2/tests')
    plugin = make_plugin(sut1, sut2)
    assert ignore(plugin, sut1 / 'test_a.py') is None
    assert ignore(plugin, sut2 / 'test_b.py') is None
    assert ignore(plugin, Path('/sut/repo3/tests/test_c.py')) is True


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
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_session_monitor_disabled_is_noop():
    """When ``--monitor`` is off the fixture must not touch hosts or write files."""
    plugin = OttoPlugin(monitor=False)
    with patch('otto.configmodule.all_hosts') as p_hosts, \
         patch('otto.monitor.factory.build_monitor_collector') as p_build:
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)
    p_hosts.assert_not_called()
    p_build.assert_not_called()


@pytest.mark.asyncio
async def test_session_monitor_no_matching_hosts_skips(tmp_path):
    """An empty host match must not crash; no collector built, no file written."""
    plugin = OttoPlugin(
        monitor=True,
        monitor_hosts='will-not-match',
        monitor_output=tmp_path / 'm.json',
    )
    with patch('otto.configmodule.all_hosts', return_value=iter([])) as p_hosts, \
         patch('otto.monitor.factory.build_monitor_collector') as p_build:
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)
    p_hosts.assert_called_once()
    p_build.assert_not_called()
    assert not (tmp_path / 'm.json').exists()


@pytest.mark.asyncio
async def test_session_monitor_publishes_collector_and_exports_json(tmp_path):
    """When enabled with hosts, the fixture must:
    - publish the collector on OttoSuite for per-test fixtures to find
    - run the collector as a background task
    - export JSON when monitor_output has a non-.db suffix
    - clear the class-level collector reference on teardown
    """
    from otto.suite.suite import OttoSuite

    out_path = tmp_path / 'monitor.json'
    plugin = OttoPlugin(
        monitor=True,
        monitor_interval=1.0,
        monitor_output=out_path,
    )

    fake_host = MagicMock(spec=UnixHost, id='router1')
    fake_collector = MagicMock()
    fake_collector.run = AsyncMock()
    fake_collector.close = AsyncMock()
    fake_collector.export_json = MagicMock()

    with patch('otto.configmodule.all_hosts', return_value=iter([fake_host])), \
         patch('otto.monitor.factory.build_monitor_collector',
               return_value=fake_collector) as p_build:
        gen = await _FixtureRunner.setup(plugin)
        # While the fixture body is suspended at yield the class attr is set.
        assert OttoSuite._session_monitor_collector is fake_collector
        await _FixtureRunner.teardown(gen)

    # Build was invoked with the host list; db_path is None for .json output.
    assert p_build.call_args.kwargs['db_path'] is None
    fake_collector.export_json.assert_called_once_with(str(out_path))
    fake_collector.close.assert_awaited_once()
    assert OttoSuite._session_monitor_collector is None


@pytest.mark.asyncio
async def test_session_monitor_db_output_skips_export_json(tmp_path):
    """A ``.db`` output must be forwarded as ``db_path`` (no JSON dump)."""
    from otto.suite.suite import OttoSuite

    out_path = tmp_path / 'monitor.db'
    plugin = OttoPlugin(
        monitor=True,
        monitor_output=out_path,
    )

    fake_host = MagicMock(spec=UnixHost, id='router1')
    fake_collector = MagicMock()
    fake_collector.run = AsyncMock()
    fake_collector.close = AsyncMock()
    fake_collector.export_json = MagicMock()

    with patch('otto.configmodule.all_hosts', return_value=iter([fake_host])), \
         patch('otto.monitor.factory.build_monitor_collector',
               return_value=fake_collector) as p_build:
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)

    assert p_build.call_args.kwargs['db_path'] == out_path
    fake_collector.export_json.assert_not_called()
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
        monitor_output=tmp_path / 'monitor.json',
    )
    fake_host = MagicMock(spec=UnixHost, id='router1')
    fake_collector = MagicMock()
    fake_collector.run = AsyncMock()
    fake_collector.close = AsyncMock()
    fake_collector.export_json = MagicMock()

    with patch('otto.configmodule.all_hosts', return_value=iter([fake_host])), \
         patch('otto.monitor.factory.build_monitor_collector',
               return_value=fake_collector):
        gen = await _FixtureRunner.setup(plugin)
        await _FixtureRunner.teardown(gen)

    fake_collector.run.assert_not_awaited()
    fake_collector.run.assert_not_called()


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
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass


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
        try:
            while True:
                await asyncio.sleep(0.01)
                series_appends.append(1)
        except asyncio.CancelledError:
            raise

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
        'collector.run() never progressed — this is the original bug '
        '(metrics empty in monitor.json).'
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

    import pytest as _pytest

    from otto.monitor.collector import MetricCollector
    from otto.suite.plugin import OttoPlugin

    suite_path = tmp_path / 'test_suite.py'
    suite_path.write_text(textwrap.dedent('''
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
    '''))

    out_path = tmp_path / 'monitor.json'
    plugin = OttoPlugin(
        monitor=True,
        monitor_interval=0.01,
        monitor_output=out_path,
    )

    real_collector = MetricCollector(targets=[])

    async def fake_run(*_a, **_kw):
        # Mimic _process_host_results: append to _series on each tick.
        try:
            while True:
                await asyncio.sleep(0.01)
                real_collector._series.setdefault(
                    'host1/cpu', deque()
                ).append((datetime.now(), 42.0, None))
        except asyncio.CancelledError:
            raise

    real_collector.run = fake_run  # type: ignore[method-assign]

    fake_host = MagicMock(spec=UnixHost, id='host1')
    with patch('otto.configmodule.all_hosts', return_value=iter([fake_host])), \
         patch('otto.monitor.factory.build_monitor_collector',
               return_value=real_collector):
        try:
            exit_code = _pytest.main(
                ['-s', '-p', 'no:cacheprovider',
                 '--override-ini', 'addopts=',
                 '-o', 'asyncio_mode=auto',
                 '-o', 'asyncio_default_fixture_loop_scope=function',
                 str(suite_path)],
                plugins=[plugin],
            )
        finally:
            # This in-process pytest.main() imports the generated suite as a
            # top-level module keyed by stem. Evict it so a second run of this
            # test in the same process (e.g. under `pytest --count`) imports a
            # fresh module instead of hitting "import file mismatch".
            sys.modules.pop(suite_path.stem, None)

    assert exit_code == 0, f'embedded pytest run failed: {exit_code}'
    assert out_path.exists(), 'monitor.json was not written'

    payload = json.loads(out_path.read_text())
    assert 'metrics' in payload
    assert len(payload['metrics']) > 0, (
        f'Regression: monitor.json contains no metrics. payload={payload}'
    )
    assert payload['metrics'][0]['host'] == 'host1'
    assert payload['metrics'][0]['label'] == 'cpu'
