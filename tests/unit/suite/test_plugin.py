from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

    fake_host = MagicMock(id='router1')
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

    fake_host = MagicMock(id='router1')
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
