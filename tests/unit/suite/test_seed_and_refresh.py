"""Only pytest ever learns a test name: the table's seed and its per-file refresh.

``_refresh_tables`` is how a reader of test names (the collect child,
``--list-markers``) brings a repo's collected-tests table up to date, through
the run's own refresh: a cold table (none, another ``env``, past its TTL) is
seeded by one whole-tree ``--collect-only`` session, a current one is left
alone, and a warm one collects only what moved: the changed files, and each
directory whose stat moved, taken in full so that pytest, not otto, decides
what in it is a test file. Design:
``docs/superpowers/specs/2026-09-27-test-name-cache-design.md`` §9.

Every session here is a real in-process pytest session over a generated
repo. Each test module logs its import (:mod:`tests._fixtures.import_log_repo`),
so "which files did the collection import" is read from disk, and each test
logs its run, so "nothing ran" is too.
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

from otto.config import collected_tests as tr
from otto.config import completion_cache as cc
from otto.suite.run import _refresh_tables
from tests._fixtures.import_log_repo import ImportLogRepo, logged_test

_A = "class TestA:\n" + logged_test("test_a1", "    ") + "\n" + logged_test("test_a2", "    ")


def _bump(path: Path, seconds: int = 5) -> None:
    """Move *path*'s mtime forward, as the seconds between two real edits would."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _the_repo():
    from otto.config import get_repos

    [repo] = get_repos()
    return repo


@pytest.fixture
def repo(sut_repo, tmp_path) -> ImportLogRepo:
    """``tests/test_a.py`` (``TestA``), ``tests/test_b.py``, ``tests/sub/test_c.py``, a conftest."""
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_a.py": shell.module("test_a", _A),
            "tests/test_b.py": shell.module("test_b", logged_test("test_b1")),
            "tests/sub/conftest.py": "",
            "tests/sub/test_c.py": shell.module("test_c", logged_test("test_c1")),
        }
    )
    return shell


@pytest.fixture
def sessions(monkeypatch) -> list:
    """Every pytest session started: its files (``None`` = whole tree), directories and kind."""
    import otto.suite.run as run_module

    seen: list = []
    real = run_module._run_pytest_session

    def spy(repo, selection, run=None, **kwargs):
        files = selection.candidates
        seen.append(
            {
                "files": None if files is None else sorted(Path(c).name for c in files),
                "dirs": sorted(Path(d).name for d in selection.candidate_dirs),
                "collect_only": run is None,
            }
        )
        return real(repo, selection, run, **kwargs)

    monkeypatch.setattr(run_module, "_run_pytest_session", spy)
    return seen


_WHOLE_TREE = {"files": None, "dirs": [], "collect_only": True}


def _refresh() -> "tr.RepoTable | None":
    [table] = _refresh_tables([_the_repo()])
    return table


def _seed(repo: ImportLogRepo, sessions: list) -> tr.RepoTable:
    assert tr.read_table(_the_repo()) is None, "it was cold"
    table = _refresh()
    assert table is not None, "and it is seeded"
    assert sessions == [_WHOLE_TREE]
    repo.next_run()
    sessions.clear()
    return table


# ── the seed ─────────────────────────────────────────────────────────────────


def test_a_cold_table_is_seeded_by_one_whole_tree_collection(repo, sessions, capfd):
    seeded = _refresh()

    assert seeded is not None
    assert sessions == [_WHOLE_TREE]
    assert repo.imported() == ["test_a", "test_b", "test_c"]
    assert repo.ran_tests() == [], "a collection runs nothing"
    assert capfd.readouterr().out == "", "and prints nothing"
    table = tr.read_table(_the_repo())
    assert table == seeded
    assert {"TestA", "TestA::test_a1", "test_a2", "test_b1", "test_c1"} <= set(table.names)
    tests = repo.root / "tests"
    assert set(table.dirs) == {str(tests), str(tests / "sub")}
    assert str(tests / "sub" / "conftest.py") in table.files
    assert tr.classify(_the_repo(), table).is_current


def test_a_current_table_is_returned_without_a_collection(repo, sessions):
    seeded = _seed(repo, sessions)

    assert _refresh() == seeded
    assert sessions == []
    assert repo.imported() == []


def test_the_seed_writes_nothing_into_the_repo(repo, sessions, real_shell_bytecode):
    """No ``__pycache__`` beside a test file, no ``.pytest_cache``: a TAB must not move the tree."""
    before = sorted(p.relative_to(repo.root) for p in repo.root.rglob("*"))

    _refresh()

    assert repo.imported() == ["test_a", "test_b", "test_c"]
    assert sorted(p.relative_to(repo.root) for p in repo.root.rglob("*")) == before


def test_another_env_seeds_the_whole_tree_again(repo, sessions):
    _seed(repo, sessions)
    repo.write("pytest.ini", "[pytest]\n")

    assert _refresh() is not None
    assert sessions == [_WHOLE_TREE]
    assert repo.imported() == ["test_a", "test_b", "test_c"]


def test_a_table_past_its_ttl_is_seeded_again(repo, sessions):
    seeded = _seed(repo, sessions)
    old = seeded.generated_at - cc.CACHE_TTL_SECONDS - 1
    tr.write_tables([dataclasses.replace(seeded, generated_at=old)])

    again = _refresh()

    assert again is not None
    assert again.generated_at > old
    assert sessions == [_WHOLE_TREE]


_STOPS = "import pytest\n\npytest.exit('the lab is not configured')\n"


def test_a_seed_whose_collection_cannot_finish_writes_nothing(repo, sessions):
    repo.write("tests/conftest.py", _STOPS)

    assert _refresh() is None

    assert sessions == [_WHOLE_TREE]
    assert tr.read_table(_the_repo()) is None


def test_a_cold_table_whose_seed_cannot_finish_is_not_offered(repo, sessions):
    """Another env made the stored table cold; a seed that stops early leaves no table to offer."""
    _seed(repo, sessions)
    repo.write("pytest.ini", "[pytest]\n")
    repo.write("tests/conftest.py", _STOPS)

    assert _refresh() is None

    assert sessions == [_WHOLE_TREE]


@pytest.mark.parametrize("tests", [[], ["tests"]], ids=["none configured", "one not there"])
def test_a_repo_with_no_test_directory_collects_nothing_wherever_otto_runs(
    tmp_path, monkeypatch, tests
):
    """pytest given no path collects its cwd: such a repo never starts a session.

    Its table is seeded empty, and warm: a directory that later appears is
    read by the next refresh.
    """
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    sut = make_sut_repo(tmp_path / "sut", name="sut", tests=tests)
    shell = tmp_path / "shell"
    shell.mkdir()
    imported = tmp_path / "imported"
    shell.joinpath("test_in_the_shell_cwd.py").write_text(
        f"open({str(imported)!r}, 'a').close()\n\n\ndef test_stray():\n    pass\n"
    )
    monkeypatch.chdir(shell)

    def no_session(*_args, **_kwargs):
        raise AssertionError("a pytest session started")

    real_main = pytest.main
    monkeypatch.setattr(pytest, "main", no_session)
    repo = Repo(sut_dir=sut)

    [table] = _refresh_tables([repo])

    assert not imported.exists()
    assert table is not None
    assert table.names == []
    assert all(Path(key).is_relative_to(sut) for key in table.files)
    assert tr.read_table(repo) == table
    assert not tr.classify(repo, table).whole_tree
    if tests:
        monkeypatch.setattr(pytest, "main", real_main)
        ImportLogRepo(sut).write("tests/test_late.py", logged_test("test_late"))
        [late] = _refresh_tables([repo])
        assert late is not None
        assert "test_late" in late.names


# ── the refresh: only what moved ─────────────────────────────────────────────


def test_a_deleted_file_is_dropped_without_a_session(repo, sessions):
    """Its directory's stat is as it was (a rename elsewhere, a restored mtime): nothing to read."""
    _seed(repo, sessions)
    tests = repo.root / "tests"
    st = tests.stat()
    (tests / "test_b.py").unlink()
    os.utime(tests, ns=(st.st_atime_ns, st.st_mtime_ns))

    table = _refresh()

    assert sessions == []
    assert repo.imported() == []
    assert table is not None
    assert "test_b1" not in table.names
    assert tr.read_table(_the_repo()) == table
    assert tr.classify(_the_repo(), table).is_current


def test_cold_only_leaves_a_warm_table_whose_files_moved_alone(repo, sessions):
    seeded = _seed(repo, sessions)
    repo.write("tests/test_b.py", repo.module("test_b", logged_test("test_b9")))
    _bump(repo.root / "tests" / "test_b.py")

    assert _refresh_tables([_the_repo()], cold_only=True) == [seeded]
    assert sessions == []
    assert repo.imported() == []


def test_a_refresh_collects_exactly_the_edited_file(repo, sessions):
    _seed(repo, sessions)
    repo.write(
        "tests/test_b.py",
        repo.module("test_b", logged_test("test_b1") + "\n\n" + logged_test("test_b2")),
    )
    _bump(repo.root / "tests" / "test_b.py")

    classification = tr.classify(_the_repo(), tr.read_table(_the_repo()))
    assert [p.name for p in classification.changed] == ["test_b.py"]
    table = _refresh()

    assert sessions == [{"files": ["test_b.py"], "dirs": [], "collect_only": True}]
    assert repo.imported() == ["test_b"]
    assert table is not None
    assert "test_b2" in table.names
    assert tr.read_table(_the_repo()) == table
    assert tr.classify(_the_repo(), table).is_current


def test_an_added_file_sends_its_directory_and_pytest_keeps_its_ignores(
    sut_repo, tmp_path, sessions
):
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_a.py": shell.module("test_a", _A),
            "tests/sub/conftest.py": 'collect_ignore = ["test_ignored.py"]\n',
            "tests/sub/test_c.py": shell.module("test_c", logged_test("test_c1")),
            "tests/sub/test_ignored.py": shell.module("test_ignored", logged_test("test_i1")),
        }
    )
    _seed(shell, sessions)
    sub = shell.root / "tests" / "sub"
    shell.write("tests/sub/test_d.py", shell.module("test_d", logged_test("test_d1")))
    _bump(sub)

    classification = tr.classify(_the_repo(), tr.read_table(_the_repo()))
    assert classification.candidate_dirs == [sub]
    assert classification.new == [], "no listing: the directory is the candidate"
    table = _refresh()

    assert sessions == [{"files": [], "dirs": ["sub"], "collect_only": True}]
    assert shell.imported() == ["test_c", "test_d"], "the ignored sibling stays unimported"
    assert table is not None
    assert "test_d1" in table.names
    assert "test_i1" not in table.names
    assert tr.classify(_the_repo(), table).is_current


def test_python_files_is_pytests_to_apply(sut_repo, tmp_path, sessions, monkeypatch):
    """A ``python_files = check_*.py`` repo is collected as pytest reads it.

    otto has no ``python_files`` parser of its own: the collect child asks
    pytest, so the table holds exactly the files pytest would collect.
    """
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "pytest.ini": "[pytest]\npython_files = check_*.py\n",
            "tests/check_a.py": shell.module("check_a", logged_test("test_a1")),
            "tests/test_b.py": shell.module("test_b", logged_test("test_b1")),
        }
    )

    seeded = _seed(shell, sessions)
    assert "test_a1" in seeded.names
    assert "test_b1" not in seeded.names

    shell.write("tests/check_new.py", shell.module("check_new", logged_test("test_new1")))
    shell.write("tests/test_other.py", shell.module("test_other", logged_test("test_o1")))
    _bump(shell.root / "tests")
    table = _refresh()

    assert table is not None
    assert "test_new1" in table.names
    assert "test_o1" not in table.names
    assert "test_b" not in shell.imported()


def test_a_file_edited_while_pytest_reads_it_is_stamped_stale(sut_repo, tmp_path, sessions):
    """The record carries the stat from before pytest imported the file: the missed edit shows."""
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_self.py": (
                "with open(__file__, 'a') as _me:\n"
                "    _me.write('\\n# edited while it was read\\n')\n\n"
                + logged_test("test_s1").replace("_ran(", "(lambda n: n)(")
            ),
        }
    )

    table = _refresh()

    assert table is not None
    assert "test_s1" in table.names
    after = tr.classify(_the_repo(), table)
    assert [p.name for p in after.changed] == ["test_self.py"]


def test_a_directory_whose_conftest_breaks_is_tried_again_once_it_is_fixed(repo, sessions):
    _seed(repo, sessions)
    repo.write("tests/new/conftest.py", "raise RuntimeError('broken conftest')\n")
    repo.write("tests/new/test_n.py", repo.module("test_n", logged_test("test_n1")))
    _bump(repo.root / "tests")

    assert _refresh() is None, "a directory it set out to list is still to be listed"

    first = tr.read_table(_the_repo())
    assert first is not None
    assert "test_n1" not in first.names
    still = tr.classify(_the_repo(), first)
    assert repo.root / "tests" in still.candidate_dirs, "its listing did not complete"

    repo.write("tests/new/conftest.py", "")
    repo.next_run()
    second = _refresh()

    assert second is not None
    assert "test_n1" in second.names
    assert tr.classify(_the_repo(), second).is_current


def test_the_table_has_no_source_field(repo, sessions):
    _seed(repo, sessions)
    raw = json.loads(cc._cache_path().read_text())[cc.COLLECTED_TESTS_KEY][str(repo.root)]
    assert raw["schema_version"] == tr.RECORDS_SCHEMA_VERSION == 5
    assert all("source" not in record for record in raw["files"].values())
