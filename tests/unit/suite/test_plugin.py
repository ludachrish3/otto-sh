from pathlib import Path
from unittest.mock import MagicMock

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
