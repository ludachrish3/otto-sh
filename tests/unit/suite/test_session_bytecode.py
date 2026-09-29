"""otto's pytest sessions keep their bytecode and pytest's cache out of the repo.

Every session otto starts (a run, a collect-only seed or refresh) writes the
bytecode of what it imports, assertion-rewritten test files included, under
``sys.pycache_prefix``: the workspace's ``pycache`` directory under
``OTTO_HOME``, unless the user already set a prefix. A run keeps pytest's
cache (where pytest-randomly writes its seed) in the workspace's
``pytest-cache`` directory. So nothing otto runs adds an entry to a test
directory, and a directory's stat moves only when a file appears, goes away
or is renamed. Design: ``docs/superpowers/specs/2026-09-27-test-name-cache-design.md``
§11.3 (b) and §12.3.

The suite itself runs under a pycache prefix (``tests/conftest.py``); these
tests take it away, as a real shell has none, unless they are about a prefix
the user set.
"""

import os
import sys
from pathlib import Path

import pytest
from _pytest.assertion.rewrite import PYC_TAIL

from otto.config import collected_tests as tr
from otto.config.home import PYCACHE_DIRNAME, PYTEST_CACHE_DIRNAME, workspace_home
from otto.suite import run_tests
from otto.suite.run import _refresh_tables
from tests._fixtures.import_log_repo import ImportLogRepo, logged_test


def _a(seen: Path) -> str:
    """``TestA::test_a1``, which also writes the ``sys.pycache_prefix`` it runs under to *seen*."""
    return (
        "import sys\n\n"
        "class TestA:\n"
        "    def test_a1(self):\n"
        "        _ran('test_a1')\n"
        f"        with open({str(seen)!r}, 'w') as f:\n"
        "            f.write(str(sys.pycache_prefix))\n"
    )


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A private ``OTTO_HOME`` and a real shell's bytecode settings: no prefix, writing on."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    return tmp_path / "home"


@pytest.fixture
def repo(sut_repo, tmp_path, home) -> ImportLogRepo:
    """``tests/test_a.py`` (``TestA``), ``tests/test_b.py``, ``tests/sub/test_c.py``, a conftest."""
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_a.py": shell.module("test_a", _a(tmp_path / "prefix-seen")),
            "tests/test_b.py": shell.module("test_b", logged_test("test_b1")),
            "tests/sub/conftest.py": "",
            "tests/sub/test_c.py": shell.module("test_c", logged_test("test_c1")),
        }
    )
    return shell


def _the_repo():
    from otto.config import get_repos

    [only] = get_repos()
    return only


def _listings(root: Path) -> dict[str, list[str]]:
    """Every directory under *root* (itself included) -> the names ``os.scandir`` lists in it."""
    listings: dict[str, list[str]] = {}
    for dirpath, _dirs, _files in os.walk(root):
        with os.scandir(dirpath) as entries:
            listings[dirpath] = sorted(entry.name for entry in entries)
    return listings


def _stat(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def _rewritten(prefix: Path, test_file: Path) -> Path:
    """Where pytest's assertion rewriter keeps *test_file*'s ``.pyc`` under *prefix*."""
    return prefix / Path(*test_file.parent.parts[1:]) / (test_file.stem + PYC_TAIL)


def _prefix() -> Path:
    return workspace_home() / PYCACHE_DIRNAME


def test_a_first_run_adds_nothing_to_any_directory_of_the_repo(repo, tmp_path):
    tests = repo.root / "tests"
    before = _listings(repo.root)
    stats = {d: _stat(d) for d in [tests, tests / "sub"]}

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert repo.ran_tests() == ["test_a1"]
    assert _listings(repo.root) == before, "no __pycache__, no .pytest_cache"
    table = tr.read_table(_the_repo())
    assert table is not None
    assert {Path(d): stat for d, stat in table.dirs.items()} == {
        tests: stats[tests],
        tests / "sub": stats[tests / "sub"],
    }, "the stats stored are the ones from before the session"
    assert tr.classify(_the_repo(), table).is_current


def test_a_pruned_run_after_a_collect_only_seed_leaves_the_table_current(repo, tmp_path):
    assert tr.read_table(_the_repo()) is None, "cold"
    [seeded] = _refresh_tables([_the_repo()])
    assert seeded is not None
    repo.next_run()
    before = _listings(repo.root)

    assert run_tests(["test_b1"], output_dir=tmp_path / "out").exit_code == 0

    assert repo.imported() == ["test_b"], "a pruned session"
    assert _listings(repo.root) == before
    table = tr.read_table(_the_repo())
    assert table is not None
    assert tr.classify(_the_repo(), table).is_current


def test_a_runs_rewritten_bytecode_lands_under_the_workspace_prefix(repo, tmp_path):
    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    test_a = repo.root / "tests" / "test_a.py"
    assert _rewritten(_prefix(), test_a).is_file()
    seen = (tmp_path / "prefix-seen").read_text()
    assert seen == str(_prefix()), "the prefix the session's tests ran under"
    assert sys.pycache_prefix is None, "and it is gone once the session is"


def test_a_collect_only_seed_writes_its_bytecode_under_the_prefix(repo):
    before = _listings(repo.root)

    _refresh_tables([_the_repo()])

    assert repo.imported() == ["test_a", "test_b", "test_c"]
    assert _listings(repo.root) == before
    tests = repo.root / "tests"
    for test_file in [tests / "test_a.py", tests / "test_b.py", tests / "sub" / "test_c.py"]:
        assert _rewritten(_prefix(), test_file).is_file(), test_file.name
    assert sys.pycache_prefix is None
    assert sys.dont_write_bytecode is False


def test_a_prefix_the_user_set_is_honoured(repo, tmp_path, monkeypatch):
    mine = tmp_path / "my-pycache"
    monkeypatch.setattr(sys, "pycache_prefix", str(mine))

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    test_a = repo.root / "tests" / "test_a.py"
    assert _rewritten(mine, test_a).is_file()
    assert (tmp_path / "prefix-seen").read_text() == str(mine)
    assert not _prefix().exists(), "otto's own prefix stays unused"
    assert sys.pycache_prefix == str(mine)


def test_a_run_keeps_pytests_cache_in_the_workspace(repo, tmp_path):
    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert (workspace_home() / PYTEST_CACHE_DIRNAME / "v" / "randomly_seed").is_file()
    assert not list(repo.root.rglob(".pytest_cache"))


def test_a_session_that_raises_takes_its_prefix_away(repo, tmp_path, monkeypatch):
    seen: list[object] = []

    def exploding_main(*_args, **_kwargs):
        seen.append(sys.pycache_prefix)
        raise RuntimeError("pytest blew up")

    monkeypatch.setattr(pytest, "main", exploding_main)

    with pytest.raises(RuntimeError, match="pytest blew up"):
        run_tests(["TestA"], output_dir=tmp_path / "out")

    assert seen == [str(_prefix())]
    assert sys.pycache_prefix is None
