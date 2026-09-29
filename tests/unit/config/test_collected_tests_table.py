"""The per-file ``__collected_tests__`` table: its store, and ``classify``'s one stat per path.

Every test builds a real SUT repo in ``tmp_path`` and a table shaped as a
whole-tree pytest collection of it would leave it (:func:`_table`: the
records and directory stats pytest's plugin hands :func:`updated_table`, no
pytest session), then moves the filesystem and asks ``classify`` what
changed. The real collections are pinned in
``tests/unit/suite/test_seed_and_refresh.py``. The interpreter half of the
``env`` key is pinned by :func:`fake_install`, so the site-packages directory
is a ``tmp_path`` directory a test can touch.

Directory mtimes are bumped explicitly (:func:`_bump`) after adding or
removing a file: the kernel stamps a directory with its coarse clock, so a
file added within one tick of the table being built can leave the mtime
where it was. A real edit happens seconds later; the bump says so.
"""

import dataclasses
import json
import os
import pathlib
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from otto.config import collected_tests as tr
from otto.config import completion_cache as cc
from otto.config.repo import TOML_SETTINGS_PATH
from tests._fixtures.sutrepo import make_sut_repo

_T = tr.RecordedTest

_TOP = """
import pytest


class TestTop:
    def test_x(self):
        pass

    @pytest.mark.slow
    def test_y(self):
        pass


def test_plain():
    pass
"""

_DEEP = "import pytest\n\npytestmark = pytest.mark.hw\n\n\ndef test_deep():\n    pass\n"
_OTHER = "def test_other():\n    pass\n"


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """Pin the interpreter half of ``env`` to a site-packages dir the test owns."""
    site = tmp_path / "site-packages"
    site.mkdir()
    install = tr.Installation(
        python="3.10.0",
        prefix=str(tmp_path / "venv"),
        pytest="9.1.1",
        otto="0.0.0",
        site_packages=[str(site)],
    )
    monkeypatch.setattr(tr, "_installation", lambda: install)
    return install


@pytest.fixture
def repo(tmp_path, fake_install):
    from otto.config.repo import Repo

    root = make_sut_repo(
        tmp_path / "sut",
        tests=["tests"],
        files={
            "tests/conftest.py": "",
            "tests/test_top.py": _TOP,
            "tests/sub/conftest.py": "",
            "tests/sub/test_deep.py": _DEEP,
            "tests/other/test_other.py": _OTHER,
        },
    )
    return Repo(sut_dir=root)


_COLLECTED = {
    "tests/test_top.py": (
        [_T(["TestTop"], "test_x"), _T(["TestTop"], "test_y"), _T([], "test_plain")],
        ["slow"],
    ),
    "tests/sub/test_deep.py": ([_T([], "test_deep")], ["hw"]),
    "tests/other/test_other.py": ([_T([], "test_other")], []),
}
"""What pytest collects from each test file of :func:`repo`."""


def _stat(path: Path) -> list[int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _table(repo):
    """The table a whole-tree pytest collection of *repo* writes, from scratch.

    The plugin's records (a stat taken before each read, the tests, the
    markers), a record for every conftest, and the stat of every directory
    pytest listed; no pytest session is started.
    """
    root = repo.sut_dir
    records: dict[str, tr.FileRecord] = {}
    for rel, (tests, markers) in _COLLECTED.items():
        if (root / rel).exists():
            records[str(root / rel)] = tr.FileRecord(
                stat=_stat(root / rel), tests=list(tests), markers=list(markers)
            )
    dirs: dict[str, list[int] | None] = {}
    for test_dir in repo.tests:
        for path in [test_dir, *sorted(test_dir.rglob("*"))]:
            if path.is_dir() and path.name != "__pycache__":
                dirs[str(path)] = _stat(path)
            elif path.name == "conftest.py":
                records.setdefault(str(path), tr.FileRecord(stat=_stat(path)))
    return tr.updated_table(
        repo,
        None,
        tr.classify(repo, None),
        records,
        registered_markers=["hw", "slow"],
        whole_tree=True,
        dirs=dirs,
    )


def _bump(path: Path, seconds: int = 5) -> None:
    """Move *path*'s mtime forward, as the seconds between two real edits would."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _edit(path: Path, text: str) -> None:
    path.write_text(text)
    _bump(path)


def _names(paths: list[Path], repo) -> list[str]:
    return sorted(str(p.relative_to(repo.sut_dir)) for p in paths)


@pytest.fixture
def spy_io(tmp_path, monkeypatch):
    """Count ``os.stat`` and ``os.scandir`` calls on this test's own paths.

    Both are process-global; coverage's tracer stats source files too, so only
    paths under ``tmp_path`` count (the `corpus_snapshot` tests' rule).
    """
    stats: list[str] = []
    scans: list[str] = []
    real_stat, real_scandir = os.stat, os.scandir

    def stat(p, *a, **k):
        if str(p).startswith(str(tmp_path)):
            stats.append(str(p))
        return real_stat(p, *a, **k)

    def scandir(p="."):
        if str(p).startswith(str(tmp_path)):
            scans.append(str(p))
        return real_scandir(p)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "scandir", scandir)
    # 3.10's pathlib bound `os.stat` into its accessor at import, so a
    # `Path.exists()`/`stat()`/`is_file()` would bypass the spy above; count
    # those too. (3.11 dropped the accessor: pathlib calls `os.stat` itself.)
    accessor = getattr(pathlib, "_NormalAccessor", None)
    if accessor is not None:
        monkeypatch.setattr(accessor, "stat", staticmethod(stat))
    return {"stats": stats, "scans": scans}


# --- the store ---------------------------------------------------------------


def test_a_collected_table_holds_each_file_and_directory(repo):
    table = _table(repo)
    tests = repo.sut_dir / "tests"
    top = table.files[str(tests / "test_top.py")]
    assert top.tests == [
        tr.RecordedTest(classes=["TestTop"], name="test_x"),
        tr.RecordedTest(classes=["TestTop"], name="test_y"),
        tr.RecordedTest(classes=[], name="test_plain"),
    ]
    assert top.markers == ["slow"]
    # A conftest is a record with no tests; the one above the tests dir is
    # watched while absent, so creating it is seen by its own stat.
    assert table.files[str(tests / "conftest.py")].tests == []
    assert table.files[str(repo.sut_dir / "conftest.py")].stat is None
    assert set(table.dirs) == {str(tests), str(tests / "sub"), str(tests / "other")}
    assert "TestTop::test_x" in table.names
    assert table.markers == ["hw", "slow"]


def test_a_record_whose_content_is_not_known_still_offers_its_last_names(repo):
    """Every record offers what pytest last recorded in it, as the console-script shim's does."""
    table = _table(repo)
    key = str(repo.sut_dir / "tests" / "test_top.py")
    unread = dataclasses.replace(table.files[key], stat=None)
    table = dataclasses.replace(table, files={**table.files, key: unread})

    assert {"TestTop", "TestTop::test_x", "test_plain"} <= set(table.names)
    assert "slow" in table.markers


def test_every_record_says_when_it_was_read(repo):
    before = int(time.time())
    table = _table(repo)
    stamps = {rec.collected_at for rec in table.files.values()}
    assert all(before <= at <= int(time.time()) for at in stamps)


def test_a_table_round_trips_through_the_cache_file(repo):
    table = _table(repo)
    tr.write_tables([table])
    back = tr.read_table(repo)
    assert back is not None
    assert back.files == table.files
    assert back.dirs == table.dirs
    assert back.env == table.env
    raw = json.loads(cc._cache_path().read_text())[cc.COLLECTED_TESTS_KEY][str(repo.sut_dir)]
    assert raw["schema_version"] == tr.RECORDS_SCHEMA_VERSION == 5
    assert all("source" not in record for record in raw["files"].values())
    # Only the records are stored: every reader, the shim included, derives
    # the names from them, dropping a file that is gone.
    assert "names" not in raw
    assert "markers" not in raw
    assert raw["files"][str(repo.sut_dir / "tests" / "test_top.py")]["tests"][0] == [
        ["TestTop"],
        "test_x",
    ]


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda raw: raw.update(schema_version=4), id="older-schema"),
        pytest.param(lambda raw: raw.update(files="nope"), id="malformed-files"),
        pytest.param(lambda raw: raw["files"].update(x={"stat": "?"}), id="malformed-record"),
    ],
)
def test_a_table_from_another_schema_or_malformed_reads_as_empty(repo, mangle):
    tr.write_tables([_table(repo)])
    path = cc._cache_path()
    data = json.loads(path.read_text())
    mangle(data[cc.COLLECTED_TESTS_KEY][str(repo.sut_dir)])
    path.write_text(json.dumps(data))
    assert tr.read_table(repo) is None


def test_writing_a_table_keeps_everything_else_in_the_file(repo, tmp_path):
    """Other sections, other namespaces and a live repo's table ride along untouched."""
    path = cc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    other = tmp_path / "other-sut"
    other.mkdir()
    other_repo = {"schema_version": 5, "files": {}}
    path.write_text(
        json.dumps(
            {
                "schema": cc.SCHEMA_VERSION,
                "sections": {"names": {"payload": {}}},
                cc.COLLECTED_TESTS_KEY: {str(other): other_repo},
                cc.DYNAMIC_TUNNELS_KEY: {"x": 1},
            }
        )
    )
    tr.write_tables([_table(repo)])
    data = json.loads(path.read_text())
    assert data["sections"] == {"names": {"payload": {}}}
    assert data[cc.DYNAMIC_TUNNELS_KEY] == {"x": 1}
    namespace = data[cc.COLLECTED_TESTS_KEY]
    assert namespace[str(other)] == other_repo
    assert str(repo.sut_dir) in namespace


def test_writing_a_table_starts_the_check_window(repo):
    """Every writer classified the whole table first (a run, a listing, the collect
    child), so the write restarts the window the shim's background check waits out."""
    from otto.config.cache_maintenance import MARKER_FILENAMES

    marker = cc._cache_path().parent / MARKER_FILENAMES["tests"]
    tr.write_tables([_table(repo)])
    assert marker.is_file()
    then = time.time() - 3600
    os.utime(marker, (then, then))
    tr.write_tables([_table(repo)])
    assert time.time() - marker.stat().st_mtime < 60


def test_writing_a_table_prunes_the_tables_of_repos_that_are_gone(repo, tmp_path):
    """The shim parses the whole file on every TAB: a table no repo can use is dropped.

    A key that is no directory now (a SUT dir removed or renamed) and a key
    that is no path at all (a whole-corpus entry an older otto stored under a
    digest) both go at the next write; a live repo's table stays.
    """
    path = cc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    live = tmp_path / "live-sut"
    live.mkdir()
    namespace = {
        "f" * 64: {"schema_version": 3, "generated_at": 1, "names": ["test_old"]},
        str(tmp_path / "removed-sut"): {"schema_version": 5, "files": {}},
        str(live): {"schema_version": 5, "files": {}},
    }
    path.write_text(json.dumps({cc.COLLECTED_TESTS_KEY: namespace}))
    tr.write_tables([_table(repo)])
    kept = json.loads(path.read_text())[cc.COLLECTED_TESTS_KEY]
    assert sorted(kept) == sorted([str(live), str(repo.sut_dir)])


def test_read_tables_reads_every_repo_from_one_open(repo, tmp_path, monkeypatch):
    tr.write_tables([_table(repo)])
    opened: list[str] = []
    real = Path.read_text

    def read_text(self, *a, **k):
        opened.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    missing = type("R", (), {"sut_dir": tmp_path / "never"})()
    tables = tr.read_tables([repo, missing])
    assert list(tables) == [str(repo.sut_dir)]
    assert opened == [str(cc._cache_path())]


def test_the_installation_is_where_pytest_really_is():
    """Unpatched: the versions and site-packages dir come from the installed distributions."""
    install = tr._installation()
    assert install.pytest == pytest.__version__
    assert install.otto is not None
    assert str(Path(pytest.__file__).parent.parent) in install.site_packages


# --- classify ----------------------------------------------------------------


def test_an_unchanged_repo_is_all_fresh_at_one_stat_per_path(repo, spy_io, fake_install):
    table = _table(repo)
    spy_io["stats"].clear()
    spy_io["scans"].clear()

    result = tr.classify(repo, table)

    assert not result.whole_tree
    assert result.changed == result.new == result.deleted == []
    # Every existing file record is fresh; the absent watcher is not listed.
    existing = [Path(p) for p, rec in table.files.items() if rec.stat is not None]
    assert result.fresh == sorted(existing)
    expected = [
        *table.files,
        *table.dirs,
        *table.env["configs"],
        str(repo.sut_dir / TOML_SETTINGS_PATH),
        *fake_install.site_packages,
    ]
    assert Counter(spy_io["stats"]) == Counter(expected), "one stat per stored path, no more"
    assert spy_io["scans"] == [], "nothing moved, so nothing is listed"


def test_an_edited_file_is_changed(repo):
    table = _table(repo)
    _edit(repo.sut_dir / "tests" / "other" / "test_other.py", _OTHER + "\n# edited\n")
    result = tr.classify(repo, table)
    assert _names(result.changed, repo) == ["tests/other/test_other.py"]
    assert "tests/test_top.py" in _names(result.fresh, repo)
    assert result.new == result.deleted == []


def test_a_new_file_makes_its_directory_a_candidate_and_nothing_is_listed(repo, spy_io):
    """No ``python_files`` of otto's own: the directory goes to pytest, which decides."""
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "test_added.py").write_text(_OTHER)
    (other / "helper.py").write_text("")
    _bump(other)
    spy_io["stats"].clear()
    spy_io["scans"].clear()

    result = tr.classify(repo, table)

    assert result.candidate_dirs == [other]
    assert result.new == result.changed == result.deleted == []
    assert not result.is_current
    assert spy_io["scans"] == [], "nothing is listed"
    assert Counter(spy_io["stats"])[str(other / "conftest.py")] == 1, "one stat for a new conftest"
    assert str(other / "test_added.py") not in spy_io["stats"]


def test_a_new_directory_makes_its_parent_a_candidate(repo, spy_io):
    table = _table(repo)
    tests = repo.sut_dir / "tests"
    (tests / "fresh" / "deeper").mkdir(parents=True)
    (tests / "fresh" / "deeper" / "test_new.py").write_text(_OTHER)
    (tests / "fresh" / "conftest.py").write_text("")
    _bump(tests)
    spy_io["scans"].clear()

    result = tr.classify(repo, table)

    assert result.candidate_dirs == [tests]
    assert result.new == [], "the new directory is pytest's to walk"
    assert spy_io["scans"] == []


def test_a_deleted_file_is_deleted(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "test_other.py").unlink()
    _bump(other)
    result = tr.classify(repo, table)
    assert _names(result.deleted, repo) == ["tests/other/test_other.py"]
    assert result.changed == result.new == []
    assert result.candidate_dirs == [other]


def test_a_touched_conftest_marks_its_subtree_changed(repo):
    table = _table(repo)
    _edit(repo.sut_dir / "tests" / "sub" / "conftest.py", "# edited\n")
    result = tr.classify(repo, table)
    assert _names(result.changed, repo) == ["tests/sub/conftest.py", "tests/sub/test_deep.py"]
    assert _names(result.fresh, repo) == [
        "tests/conftest.py",
        "tests/other/test_other.py",
        "tests/test_top.py",
    ]
    assert result.candidate_dirs == [repo.sut_dir / "tests" / "sub"]


def test_a_touched_conftest_makes_every_directory_below_it_a_candidate(repo):
    table = _table(repo)
    _edit(repo.sut_dir / "tests" / "conftest.py", "# edited\n")
    tests = repo.sut_dir / "tests"
    assert tr.classify(repo, table).candidate_dirs == [tests, tests / "other", tests / "sub"]


def test_a_new_conftest_marks_its_directory_changed(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "conftest.py").write_text("")
    _bump(other)
    result = tr.classify(repo, table)
    assert _names(result.new, repo) == ["tests/other/conftest.py"]
    assert _names(result.changed, repo) == ["tests/other/test_other.py"]
    assert result.candidate_dirs == [other]


def test_a_deleted_conftest_marks_its_directory_changed(repo):
    table = _table(repo)
    sub = repo.sut_dir / "tests" / "sub"
    (sub / "conftest.py").unlink()
    _bump(sub)
    result = tr.classify(repo, table)
    assert _names(result.deleted, repo) == ["tests/sub/conftest.py"]
    assert _names(result.changed, repo) == ["tests/sub/test_deep.py"]
    assert result.candidate_dirs == [sub]


def test_a_conftest_created_above_the_tests_dir_changes_everything_below_it(repo):
    table = _table(repo)
    (repo.sut_dir / "conftest.py").write_text("")
    result = tr.classify(repo, table)
    assert not result.whole_tree
    assert result.fresh == []
    assert _names(result.changed, repo) == [
        "conftest.py",
        "tests/conftest.py",
        "tests/other/test_other.py",
        "tests/sub/conftest.py",
        "tests/sub/test_deep.py",
        "tests/test_top.py",
    ]
    tests = repo.sut_dir / "tests"
    assert result.candidate_dirs == [tests, tests / "other", tests / "sub"]


def test_a_swapped_pytest_config_changes_everything(repo, spy_io):
    table = _table(repo)
    (repo.sut_dir / "pytest.ini").write_text("[pytest]\npython_files = test_*.py check_*.py\n")
    # `_table` lists the tree with `rglob`, which reaches the spied `os.scandir`
    # on 3.11, 3.12 and 3.14 (not 3.10 or 3.13): count only what classify does.
    spy_io["stats"].clear()
    spy_io["scans"].clear()

    result = tr.classify(repo, table)

    assert result.whole_tree
    assert result.fresh == []
    assert "tests/test_top.py" in _names(result.changed, repo)
    # Nothing is walked: which files are test files is the collection's to say.
    assert result.new == result.candidate_dirs == []
    assert spy_io["scans"] == []
    counts = Counter(spy_io["stats"])
    assert all(counts[p] == 1 for p in [*table.files, *table.dirs])


def test_a_moved_site_packages_changes_everything(repo, fake_install):
    table = _table(repo)
    _bump(Path(fake_install.site_packages[0]))
    result = tr.classify(repo, table)
    assert result.whole_tree
    assert result.fresh == []
    assert "tests/sub/test_deep.py" in _names(result.changed, repo)


def test_another_pytest_version_changes_everything(repo, monkeypatch, fake_install):
    table = _table(repo)
    upgraded = dataclasses.replace(fake_install, pytest="9.2.0")
    monkeypatch.setattr(tr, "_installation", lambda: upgraded)
    assert tr.classify(repo, table).whole_tree


def test_another_venv_changes_everything(repo, monkeypatch, fake_install):
    """Worktree venvs share one ``OTTO_HOME`` and one Python: the table says which venv
    wrote it, so another venv's pytest and plugins never read its records as theirs."""
    table = _table(repo)
    elsewhere = dataclasses.replace(fake_install, prefix="/another/venv")
    monkeypatch.setattr(tr, "_installation", lambda: elsewhere)
    assert tr.classify(repo, table).whole_tree


def test_the_installation_names_this_interpreters_prefix():
    tr._installation.cache_clear()
    try:
        assert tr._installation().prefix == sys.prefix
    finally:
        tr._installation.cache_clear()


def test_no_table_is_the_whole_tree_and_nothing_is_walked(repo, spy_io):
    result = tr.classify(repo, None)
    assert result.whole_tree
    assert spy_io["scans"] == []
    assert result.fresh == result.changed == result.candidate_dirs == []
    # Only the conftests pytest loads before it lists anything are known.
    assert _names(result.new, repo) == ["tests/conftest.py"]
    assert set(result.dirs) == {str(repo.sut_dir / "tests")}


def test_a_table_past_its_ttl_is_the_whole_tree(repo, monkeypatch):
    table = _table(repo)
    assert not tr.classify(repo, table).whole_tree
    later = table.generated_at + cc.CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(tr.time, "time", lambda: later)
    result = tr.classify(repo, table)
    assert result.whole_tree
    assert result.fresh == []


# --- merging a collection's records -----------------------------------------


def _pytest_record(*tests: tr.RecordedTest) -> tr.FileRecord:
    return tr.FileRecord(stat=None, tests=list(tests))


def test_updated_table_takes_the_records_and_refreshes_the_conftests(repo):
    table = _table(repo)
    sub = repo.sut_dir / "tests" / "sub"
    _edit(sub / "conftest.py", "# edited\n")
    result = tr.classify(repo, table)
    deep = str(sub / "test_deep.py")
    generated = tr.RecordedTest(classes=[], name="test_generated")

    updated = tr.updated_table(
        repo, table, result, {deep: _pytest_record(generated)}, registered_markers=["hw"]
    )

    assert updated.files[deep].tests == [generated]
    assert updated.files[deep].stat == result.stats[deep], "stamped with the stat classify saw"
    assert updated.registered_markers == ["hw"]
    after = tr.classify(repo, updated)
    assert after.changed == after.new == [], "conftest re-stamped, subtree recorded"


def test_collected_at_moves_only_for_what_was_read(repo, monkeypatch):
    table = _table(repo)
    top = str(repo.sut_dir / "tests" / "test_top.py")
    other = str(repo.sut_dir / "tests" / "other" / "test_other.py")
    stamped = table.files[other].collected_at
    _edit(Path(other), _OTHER + "\n# edited\n")
    later = stamped + 3600
    monkeypatch.setattr(tr.time, "time", lambda: later)
    updated = tr.updated_table(
        repo,
        table,
        tr.classify(repo, table),
        {top: _pytest_record(tr.RecordedTest(classes=[], name="test_plain"))},
    )
    assert updated.files[top].collected_at == later, "read now"
    assert updated.files[other].collected_at == stamped, "changed but not read: keeps its age"
    deep = str(repo.sut_dir / "tests" / "sub" / "test_deep.py")
    assert updated.files[deep].collected_at == table.files[deep].collected_at, "fresh: kept"
    tr.write_tables([updated])
    back = tr.read_table(repo)
    assert back is not None
    assert back.files[top].collected_at == later


def test_a_changed_file_without_a_record_stays_changed(repo):
    table = _table(repo)
    top = repo.sut_dir / "tests" / "test_top.py"
    _edit(top, _TOP + "\n# edited\n")
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {})
    assert tr.classify(repo, updated).changed == [top]


def test_a_moved_directory_no_collection_listed_stays_a_candidate(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "test_added.py").write_text(_OTHER)
    _bump(other)
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {})
    assert tr.classify(repo, updated).candidate_dirs == [other]


def test_a_candidate_directory_the_collection_listed_is_stamped(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "test_added.py").write_text(_OTHER)
    _bump(other)
    listed = {str(other): _stat(other)}
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {}, dirs=listed)
    assert tr.classify(repo, updated).is_current


def test_a_directory_whose_listing_failed_keeps_the_stat_the_table_had(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    (other / "test_added.py").write_text(_OTHER)
    _bump(other)
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {}, dirs={str(other): None})
    assert updated.dirs[str(other)] == table.dirs[str(other)]
    assert tr.classify(repo, updated).candidate_dirs == [other]


def test_a_candidate_directory_pytest_no_longer_enters_leaves_the_table(repo):
    table = _table(repo)
    other = repo.sut_dir / "tests" / "other"
    _edit(repo.sut_dir / "tests" / "conftest.py", "collect_ignore = ['other']\n")
    tests = repo.sut_dir / "tests"
    listed = {str(tests): _stat(tests), str(tests / "sub"): _stat(tests / "sub")}
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {}, dirs=listed)
    assert str(other) not in updated.dirs
    assert str(tests) in updated.dirs


def test_a_new_directory_that_failed_is_left_out_but_a_tests_dir_stays(repo):
    tests = repo.sut_dir / "tests"
    updated = tr.updated_table(
        repo,
        None,
        tr.classify(repo, None),
        {},
        whole_tree=True,
        dirs={str(tests): None, str(tests / "sub"): None},
    )
    assert updated.dirs == {str(tests): None}, "a tests dir stays, to be listed again"
    assert tr.classify(repo, updated).candidate_dirs == [tests]


def test_only_a_whole_tree_collection_dates_the_table(repo, monkeypatch):
    table = _table(repo)
    _edit(repo.sut_dir / "tests" / "test_top.py", _TOP + "\n# edited\n")
    later = table.generated_at + 3600
    monkeypatch.setattr(tr.time, "time", lambda: later)
    classification = tr.classify(repo, table)
    partial = tr.updated_table(repo, table, classification, {})
    assert partial.generated_at == table.generated_at
    whole = tr.updated_table(repo, table, classification, {}, whole_tree=True)
    assert whole.generated_at == later


def test_a_changed_conftest_without_its_subtree_recorded_keeps_the_subtree_changed(repo):
    """The conftest is re-stamped, so its subtree must not read as fresh by default."""
    table = _table(repo)
    sub = repo.sut_dir / "tests" / "sub"
    _edit(sub / "conftest.py", "# edited\n")
    updated = tr.updated_table(repo, table, tr.classify(repo, table), {})
    assert tr.classify(repo, updated).changed == [sub / "test_deep.py"]


def test_a_whole_tree_merge_takes_what_the_collection_listed(repo):
    tests = repo.sut_dir / "tests"
    top = str(tests / "test_top.py")
    updated = tr.updated_table(
        repo,
        None,
        tr.classify(repo, None),
        {top: _pytest_record(tr.RecordedTest(classes=[], name="test_plain"))},
        whole_tree=True,
        dirs={str(tests): _stat(tests), str(tests / "sub"): _stat(tests / "sub")},
    )
    after = tr.classify(repo, updated)
    assert not after.whole_tree
    assert after.is_current
    assert _names(after.fresh, repo) == ["tests/conftest.py", "tests/test_top.py"]
    assert set(updated.dirs) == {str(tests), str(tests / "sub")}


def test_a_rebuild_leaves_the_records_alone(repo):
    tr.write_tables([_table(repo)])
    before = json.loads(cc._cache_path().read_text())[cc.COLLECTED_TESTS_KEY]
    cc.write_cache([repo], [], [])
    after = json.loads(cc._cache_path().read_text())[cc.COLLECTED_TESTS_KEY]
    assert after == before


# --- dependencies: files a record's tests come from, outside the file itself ------


def _with_dependency(repo, dep: Path, *holders: str) -> "tr.RepoTable":
    """The static table, then a pytest collection of *holders* that found tests in *dep*.

    Two rounds, as two runs would do it: the first learns the dependency (its
    stat is not known from before the read, so it is stored unknown), the
    second re-reads the holders and stamps the dependency with the stat
    ``classify`` took before that read.
    """
    table = _table(repo)
    tests = repo.sut_dir / "tests"
    for _ in range(2):
        classification = tr.classify(repo, table)
        records = {
            str(tests / h): tr.FileRecord(
                stat=None,
                tests=[tr.RecordedTest(classes=["TestTop"], name="test_x")],
                deps=[str(dep)],
            )
            for h in holders
        }
        table = tr.updated_table(repo, table, classification, records)
    return table


def test_a_dependency_first_seen_marks_its_holders_changed_until_stamped(repo):
    lib = repo.sut_dir / "pylib" / "base.py"
    lib.parent.mkdir()
    lib.write_text("class Base: pass\n")
    tests = repo.sut_dir / "tests"
    table = _table(repo)
    records = {str(tests / "test_top.py"): tr.FileRecord(stat=None, deps=[str(lib)])}

    first = tr.updated_table(repo, table, tr.classify(repo, table), records)

    assert first.deps == {str(lib): None}
    assert first.files[str(tests / "test_top.py")].deps == [str(lib)]
    assert "tests/test_top.py" in _names(tr.classify(repo, first).changed, repo)

    second = tr.updated_table(repo, first, tr.classify(repo, first), records)

    assert second.deps[str(lib)] == [lib.stat().st_mtime_ns, lib.stat().st_size]
    assert "tests/test_top.py" in _names(tr.classify(repo, second).fresh, repo)


def test_a_changed_dependency_marks_every_holder_changed(repo):
    lib = repo.sut_dir / "pylib" / "base.py"
    lib.parent.mkdir()
    lib.write_text("class Base: pass\n")
    table = _with_dependency(repo, lib, "test_top.py", "other/test_other.py")
    assert tr.classify(repo, table).changed == []

    _edit(lib, "class Base:\n    def test_y(self): pass\n")
    result = tr.classify(repo, table)

    assert _names(result.changed, repo) == ["tests/other/test_other.py", "tests/test_top.py"]
    assert "tests/sub/test_deep.py" in _names(result.fresh, repo)


def test_a_deleted_dependency_marks_its_holder_changed(repo):
    lib = repo.sut_dir / "pylib" / "base.py"
    lib.parent.mkdir()
    lib.write_text("class Base: pass\n")
    table = _with_dependency(repo, lib, "test_top.py")

    lib.unlink()

    assert _names(tr.classify(repo, table).changed, repo) == ["tests/test_top.py"]


def test_a_shared_dependency_is_stated_once(repo, spy_io, fake_install):
    lib = repo.sut_dir / "pylib" / "base.py"
    lib.parent.mkdir()
    lib.write_text("class Base: pass\n")
    other_test = repo.sut_dir / "tests" / "test_top.py"
    table = _with_dependency(repo, lib, "test_top.py", "other/test_other.py", "sub/test_deep.py")
    # A test file that is itself another file's dependency: still one stat.
    table = _with_dependency_on_file(repo, table, other_test)
    spy_io["stats"].clear()

    tr.classify(repo, table)

    counts = Counter(spy_io["stats"])
    assert counts[str(lib)] == 1
    assert counts[str(other_test)] == 1
    assert set(counts.values()) == {1}


def _with_dependency_on_file(repo, table, dep: Path):
    tests = repo.sut_dir / "tests"
    for _ in range(2):
        classification = tr.classify(repo, table)
        records = {
            str(tests / "other" / "test_other.py"): tr.FileRecord(stat=None, deps=[str(dep)])
        }
        table = tr.updated_table(repo, table, classification, records)
    return table


def test_every_dependency_but_an_installed_or_standard_module_is_tracked(repo, tmp_path):
    """An editable install or a ``PYTHONPATH`` dir is tracked; ``env`` covers the rest.

    Installed distributions (``site-packages``/``dist-packages``) move the
    ``env`` stat, and the standard library moves with the interpreter version.
    """
    import sysconfig

    outside = tmp_path / "shared_src" / "sharedbase.py"
    outside.parent.mkdir()
    outside.write_text("")
    inside = repo.sut_dir / "pylib" / "base.py"
    inside.parent.mkdir()
    inside.write_text("")
    installed = tmp_path / "venv" / "lib" / "site-packages" / "dist" / "base.py"
    debian = tmp_path / "usr" / "lib" / "python3" / "dist-packages" / "dist" / "base.py"
    standard = Path(sysconfig.get_paths()["stdlib"]) / "unittest" / "case.py"
    table = _table(repo)
    deps = [str(p) for p in [outside, inside, installed, debian, standard]]
    records = {str(repo.sut_dir / "tests" / "test_top.py"): tr.FileRecord(stat=None, deps=deps)}

    updated = tr.updated_table(repo, table, tr.classify(repo, table), records)

    assert list(updated.deps) == sorted([str(outside), str(inside)])
    assert updated.files[str(repo.sut_dir / "tests" / "test_top.py")].deps == sorted(
        [str(outside), str(inside)]
    )


def test_a_dependency_with_no_file_is_not_kept(repo, tmp_path):
    """A sourceless ``.pyc`` lib names a source file that is not there: no stat can follow it.

    Stored, it would read as deleted on every run and re-collect its holders
    each time; so it is dropped whether it is first seen now or was missing
    when ``classify`` stat'ed it.
    """
    gone = tmp_path / "srcs" / "pycbase.py"
    present = repo.sut_dir / "pylib" / "base.py"
    present.parent.mkdir()
    present.write_text("")
    holder = str(repo.sut_dir / "tests" / "test_top.py")
    records = {holder: tr.FileRecord(stat=None, deps=[str(gone), str(present)])}
    table = _table(repo)

    first = tr.updated_table(repo, table, tr.classify(repo, table), records)

    assert list(first.deps) == [str(present)]
    assert first.files[holder].deps == [str(present)]

    # Stored before this change (or deleted since): classify saw it missing.
    stale = dataclasses.replace(
        first,
        deps={**first.deps, str(gone): None},
        files={
            **first.files,
            holder: dataclasses.replace(first.files[holder], deps=[str(gone), str(present)]),
        },
    )
    again = tr.updated_table(repo, stale, tr.classify(repo, stale), records)

    assert str(gone) not in again.deps
    assert again.files[holder].deps == [str(present)]
    third = tr.updated_table(repo, again, tr.classify(repo, again), records)
    assert "tests/test_top.py" in _names(tr.classify(repo, third).fresh, repo)


def test_dependencies_survive_the_round_trip(repo):
    lib = repo.sut_dir / "pylib" / "base.py"
    lib.parent.mkdir()
    lib.write_text("class Base: pass\n")
    table = _with_dependency(repo, lib, "test_top.py")

    again = tr.table_from_json(repo.sut_dir, json.loads(json.dumps(table.to_json())))

    assert again is not None
    assert again.deps == table.deps
    assert again.files[str(repo.sut_dir / "tests" / "test_top.py")].deps == [str(lib)]
