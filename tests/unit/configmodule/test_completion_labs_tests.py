"""Static, user-code-free collectors behind --lab / --tests completion."""

import json
from pathlib import Path
from types import SimpleNamespace

from otto.configmodule.completion_cache import collect_lab_names, collect_test_names

_HOSTS = [
    {"ip": "10.0.0.1", "element": "r", "labs": ["tech1", "shared"]},
    {"ip": "10.0.0.2", "element": "s", "labs": ["tech2", "shared"]},
]

_TEST_FILE = """\
from otto.suite import OttoSuite


def test_top_level():
    pass


async def test_async_top_level():
    pass


def not_a_test():
    pass


class TestThing(OttoSuite):
    def test_method(self):
        pass

    def helper(self):
        pass
"""


def _repo(*, labs: list[Path] | None = None, tests: list[Path] | None = None):
    """A stand-in exposing only the .labs / .tests the collectors read."""
    return SimpleNamespace(labs=labs or [], tests=tests or [])


def test_collect_lab_names_reads_lab_tags(tmp_path: Path) -> None:
    (tmp_path / "lab.json").write_text(json.dumps({"hosts": _HOSTS}))
    assert collect_lab_names([_repo(labs=[tmp_path])]) == ["shared", "tech1", "tech2"]


def test_collect_lab_names_empty_without_hosts_file(tmp_path: Path) -> None:
    assert collect_lab_names([_repo(labs=[tmp_path])]) == []
    assert collect_lab_names([]) == []


def test_collect_test_names_static_scan(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(_TEST_FILE)
    names = collect_test_names([_repo(tests=[tmp_path])])

    # Bare functions (sync + async) and the suite method, both bare and scoped.
    assert names == [
        "TestThing::test_method",
        "test_async_top_level",
        "test_method",
        "test_top_level",
    ]
    # Non-test callables and non-Test classes are never offered.
    assert "not_a_test" not in names
    assert "helper" not in names


def test_collect_test_names_matches_pytest_file_patterns(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_one(): pass\n")
    (tmp_path / "b_test.py").write_text("def test_two(): pass\n")
    (tmp_path / "helper.py").write_text("def test_ignored(): pass\n")  # not a test file
    names = collect_test_names([_repo(tests=[tmp_path])])
    assert names == ["test_one", "test_two"]


def test_collect_test_names_skips_unparseable(tmp_path: Path) -> None:
    (tmp_path / "test_broken.py").write_text("def test_x(:\n")  # syntax error
    (tmp_path / "test_ok.py").write_text("def test_ok(): pass\n")
    assert collect_test_names([_repo(tests=[tmp_path])]) == ["test_ok"]


def test_collect_test_names_empty_without_dirs(tmp_path: Path) -> None:
    assert collect_test_names([_repo(tests=[tmp_path / "missing"])]) == []
    assert collect_test_names([]) == []
