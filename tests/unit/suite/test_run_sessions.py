"""``run_tests`` resolves names inside the run's own pytest session, one per project.

Every session collects its project's whole tree: the collected-tests tables are
a hint, never a narrowing of what pytest collects (#592). They decide the
order of the sessions and how early a name no project holds is refused: one project
searches for a name no record places, its session runs first and must find
it, and a name found nowhere is a did-you-mean before any test runs. Every
session writes back what it collected. Design:
``docs/superpowers/specs/2026-09-27-test-name-cache-design.md`` §3.3, §3.5, §8,
§11 and §12, as amended by #592.

Each test module in these projects appends its name to an import log when it is
imported, and each test appends its own name to a run log when it runs, so
"which files did the run import" and "did anything run" are read from disk.
Between two runs in one test the generated modules are evicted from
``sys.modules``: a second import is then a real one.
"""

import dataclasses
import io
import logging
from pathlib import Path

import pytest
from _pytest._io import TerminalWriter

from otto.config import completion_cache as cc
from otto.config.collected_tests import classify, read_table, write_tables
from otto.suite import NoTestsMatchedError, RunOptions, UnknownSelectionError, run_tests
from tests._fixtures.import_log_project import ImportLogProject, logged_lines, logged_test

_Project = ImportLogProject
_test = logged_test


_A = "class TestA:\n" + _test("test_a1", "    ") + "\n" + _test("test_a2", "    ")
_B = _test("test_b1")
_C = _test("test_c1")


@pytest.fixture
def project(one_project, tmp_path) -> _Project:
    """Holds ``tests/test_a.py`` (``TestA``), ``tests/test_b.py``, ``tests/sub/test_c.py``."""
    shell = _Project(tmp_path / "sut")
    shell.root = one_project(
        files={
            "tests/test_a.py": shell.module("test_a", _A),
            "tests/test_b.py": shell.module("test_b", _B),
            "tests/sub/conftest.py": "",
            "tests/sub/test_c.py": shell.module("test_c", _C),
        }
    )
    return shell


@pytest.fixture
def sessions(monkeypatch) -> list:
    """The candidate set of every pytest session a run starts (``None`` = the whole tree).

    A candidate directory, taken in full, is listed after the files as ``name/``.
    """
    import otto.suite.run as run_module

    seen: list = []
    real = run_module._run_pytest_session

    def spy(project, selection, run=None, **kwargs):
        candidates = selection.candidates
        if candidates is None:
            seen.append(None)
        else:
            dirs = [f"{Path(d).name}/" for d in selection.candidate_dirs]
            seen.append(sorted(Path(c).name for c in candidates) + sorted(dirs))
        return real(project, selection, run, **kwargs)

    monkeypatch.setattr(run_module, "_run_pytest_session", spy)
    return seen


def _the_project():
    from otto.bootstrap import get_projects

    [only] = get_projects()
    return only


def _warm(project: _Project, tmp_path: Path, sessions: list) -> None:
    """Run once so the table holds what pytest collected; then forget the run."""
    assert run_tests(["test_b1"], output_dir=tmp_path / "warm").exit_code == 0
    project.next_run()
    sessions.clear()


# ── the warm path ────────────────────────────────────────────────────────────


def test_a_cold_run_collects_the_whole_tree_once(project, tmp_path, sessions):
    result = run_tests(["TestA"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]
    assert project.imported() == ["test_a", "test_b", "test_c"]
    assert project.ran_tests() == ["test_a1", "test_a2"]


def test_a_cold_run_seeds_the_table_from_its_own_session(project, tmp_path, monkeypatch):
    """The run's whole-tree session is the seed: no ``--collect-only`` session runs first."""
    main_calls: list[list[str]] = []
    real_main = pytest.main

    def counting_main(args, *rest, **kwargs):
        main_calls.append(list(args))
        return real_main(args, *rest, **kwargs)

    monkeypatch.setattr(pytest, "main", counting_main)

    result = run_tests(["TestA"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert len(main_calls) == 1, "one pytest session for the run and the seed"
    assert "--collect-only" not in main_calls[0]
    table = read_table(_the_project())
    assert table is not None
    assert {"TestA", "test_a1", "test_b1", "test_c1"} <= set(table.names)
    tests = project.root / "tests"
    assert set(table.dirs) == {str(tests), str(tests / "sub")}
    assert classify(_the_project(), table).is_current


def test_a_warm_run_collects_the_whole_tree_and_runs_only_the_name(project, tmp_path, sessions):
    """The table places TestA in test_a.py; pytest still decides, over every file (#592)."""
    _warm(project, tmp_path, sessions)

    result = run_tests(["TestA"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]
    assert project.imported() == ["test_a", "test_b", "test_c"]
    assert project.ran_tests() == ["test_a1", "test_a2"]


def test_a_new_test_in_an_edited_file_runs_in_the_one_session(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    project.write("tests/test_b.py", project.module("test_b", _B + "\n\n" + _test("test_b_new")))

    result = run_tests(["test_b_new"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]
    assert project.ran_tests() == ["test_b_new"]


def test_a_new_file_is_collected_in_the_first_session(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    project.write("tests/sub/test_d.py", project.module("test_d", _test("test_d1")))

    result = run_tests(["test_d1"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]
    assert project.ran_tests() == ["test_d1"]


def test_a_file_saved_as_a_pruned_session_starts_is_not_vouched_for(
    one_project, tmp_path, sessions, monkeypatch, real_shell_bytecode
):
    """A file created after the table's stat, before pytest lists a directory it passes through.

    The session enters ``tests/`` only for the candidate below it, so it
    ignores the new file; the table keeps the stat it took before the file
    appeared, and the next run takes ``tests/`` in full.
    """
    shell = _Project(tmp_path / "sut")
    new_file = shell.module("test_new", "def test_alpha():\n    _ran('new::test_alpha')\n")
    shell.root = one_project(
        files={
            "tests/conftest.py": (
                "import os, pathlib\n"
                "if os.environ.get('SAVE_DURING_STARTUP'):\n"
                "    new = pathlib.Path(__file__).parent / 'test_new.py'\n"
                "    if not new.exists():\n"
                f"        new.write_text({new_file!r})\n"
            ),
            "tests/test_a.py": shell.module(
                "test_a", "def test_alpha():\n    _ran('a::test_alpha')\n"
            ),
            "tests/test_b.py": shell.module("test_b", _test("test_beta")),
        }
    )
    for out in ["seed", "warm"]:
        assert run_tests(["test_alpha"], output_dir=tmp_path / out).exit_code == 0
        shell.next_run()
    monkeypatch.setenv("SAVE_DURING_STARTUP", "1")
    assert run_tests(["test_alpha"], output_dir=tmp_path / "saved").exit_code == 0
    monkeypatch.delenv("SAVE_DURING_STARTUP")
    assert (shell.root / "tests" / "test_new.py").exists()
    shell.next_run()

    assert run_tests(["test_alpha"], output_dir=tmp_path / "after").exit_code == 0

    assert shell.ran_tests() == ["a::test_alpha", "new::test_alpha"]


def test_a_file_deleted_and_saved_again_as_the_session_starts_keeps_its_record(
    one_project, tmp_path, sessions, monkeypatch, real_shell_bytecode
):
    """The table saw the file gone; pytest, starting, saw it back and collected it.

    What this session read wins over what the table saw before it: the file
    keeps a record, stamped with the stat taken just before pytest read it,
    so later runs find its tests without collecting it every time.
    """
    shell = _Project(tmp_path / "sut")
    again = shell.module("test_old", "def test_alpha():\n    _ran('old::test_alpha')\n")
    shell.root = one_project(
        files={
            "tests/conftest.py": (
                "import os, pathlib\n"
                "if os.environ.get('SAVE_DURING_STARTUP'):\n"
                "    old = pathlib.Path(__file__).parent / 'test_old.py'\n"
                "    if not old.exists():\n"
                f"        old.write_text({again!r})\n"
            ),
            "tests/test_a.py": shell.module(
                "test_a", "def test_alpha():\n    _ran('a::test_alpha')\n"
            ),
            "tests/test_old.py": shell.module("test_old", _test("test_gone")),
        }
    )
    for out in ["seed", "warm"]:
        assert run_tests(["test_alpha"], output_dir=tmp_path / out).exit_code == 0
        shell.next_run()
    (shell.root / "tests" / "test_old.py").unlink()
    monkeypatch.setenv("SAVE_DURING_STARTUP", "1")
    assert run_tests(["test_alpha"], output_dir=tmp_path / "saved").exit_code == 0
    monkeypatch.delenv("SAVE_DURING_STARTUP")
    shell.next_run()

    table = read_table(_the_project())
    assert table is not None
    old = str(shell.root / "tests" / "test_old.py")
    assert old in table.files
    assert table.files[old].stat is not None, "stat'ed before pytest read it"
    assert classify(_the_project(), table).is_current

    sessions.clear()
    assert run_tests(["test_alpha"], output_dir=tmp_path / "after").exit_code == 0

    assert shell.ran_tests() == ["a::test_alpha", "old::test_alpha"]


def test_a_file_saved_in_a_listed_directory_during_the_session_is_not_vouched_for(
    project, tmp_path, sessions, real_shell_bytecode
):
    """A test writes a sibling file after pytest listed its directory: the listing missed it."""
    _warm(project, tmp_path, sessions)
    sub = project.root / "tests" / "sub"
    later = project.module("test_e", "def test_d1():\n    _ran('e::test_d1')\n")
    project.write(
        "tests/sub/test_d.py",
        project.module(
            "test_d",
            "import pathlib\n\n\ndef test_d1():\n    _ran('d::test_d1')\n"
            f"    (pathlib.Path(__file__).parent / 'test_e.py').write_text({later!r})\n",
        ),
    )
    assert run_tests(["test_d1"], output_dir=tmp_path / "saved").exit_code == 0
    assert (sub / "test_e.py").exists()
    project.next_run()

    assert run_tests(["test_d1"], output_dir=tmp_path / "after").exit_code == 0

    assert project.ran_tests() == ["d::test_d1", "e::test_d1"]


def test_an_edited_conftest_is_read_again_and_recorded(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    project.write(
        "tests/sub/conftest.py",
        "def pytest_configure(config):\n"
        "    config.addinivalue_line('markers', 'edited_mine: from tests/sub')\n",
    )

    result = run_tests(["TestA"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]
    table = read_table(_the_project())
    assert table is not None
    assert "edited_mine" in table.registered_markers


def test_a_changed_pytest_config_collects_the_whole_tree(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    project.write("pytest.ini", "[pytest]\n")

    result = run_tests(["TestA"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]


def test_a_run_records_what_it_collected(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    project.write("tests/test_b.py", project.module("test_b", _B + "\n\n" + _test("test_b2")))

    run_tests(["TestA"], output_dir=tmp_path / "out")

    from otto.bootstrap import get_projects

    [the_project] = get_projects()
    table = read_table(the_project)
    assert table is not None
    record = table.files[str(project.root / "tests" / "test_b.py")]
    assert [t.name for t in record.tests] == ["test_b1", "test_b2"]
    assert "TestA::test_a1" in table.names
    assert "test_c1" in table.names


def test_a_named_run_keeps_the_markers_every_conftest_registers(project, tmp_path, sessions):
    """A named run collects the whole tree, so it sees every conftest's markers."""
    project.write(
        "tests/sub/conftest.py",
        "def pytest_configure(config):\n"
        "    config.addinivalue_line('markers', 'nested_mine: from tests/sub')\n",
    )
    _warm(project, tmp_path, sessions)

    run_tests(["TestA"], output_dir=tmp_path / "out")

    from otto.bootstrap import get_projects

    assert sessions == [None]
    table = read_table(get_projects()[0])
    assert table is not None
    assert "nested_mine" in table.registered_markers


# ── names the table cannot vouch for ─────────────────────────────────────────


def test_an_unknown_name_is_refused_before_any_test(project, tmp_path, sessions):
    """No record places test_a3, yet a file may generate it: pytest looks, then otto refuses."""
    _warm(project, tmp_path, sessions)

    with pytest.raises(UnknownSelectionError, match=r"test_a3.*did you mean: test_a"):
        run_tests(["test_a3"], output_dir=tmp_path / "out")

    assert sessions == [None]
    assert project.ran_tests() == []


def test_one_unknown_name_among_known_ones_runs_nothing(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)

    with pytest.raises(UnknownSelectionError, match="test_a3"):
        run_tests(["TestA", "test_a3"], output_dir=tmp_path / "out")

    assert sessions == [None]
    assert project.ran_tests() == []


def _count_sessions(monkeypatch) -> list[list[str]]:
    """Every ``pytest.main`` call's arguments, in order."""
    calls: list[list[str]] = []
    real_main = pytest.main

    def counting_main(args, *rest, **kwargs):
        calls.append(list(args))
        return real_main(args, *rest, **kwargs)

    monkeypatch.setattr(pytest, "main", counting_main)
    return calls


def test_the_add_a_test_loop_is_one_pytest_session(project, tmp_path, sessions, monkeypatch):
    """The saved file is the one uncertain place: its collection and the run are one session."""
    _warm(project, tmp_path, sessions)
    project.write("tests/test_b.py", project.module("test_b", _B + "\n\n" + _test("test_b_new")))
    calls = _count_sessions(monkeypatch)

    assert run_tests(["test_b_new"], output_dir=tmp_path / "out").exit_code == 0

    assert len(calls) == 1
    assert "--collect-only" not in calls[0]
    assert project.ran_tests() == ["test_b_new"]


def test_a_warm_run_is_one_pytest_session(project, tmp_path, sessions, monkeypatch):
    _warm(project, tmp_path, sessions)
    calls = _count_sessions(monkeypatch)

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert len(calls) == 1
    assert project.ran_tests() == ["test_a1", "test_a2"]


def test_the_session_runs_with_capture_off(project, tmp_path, monkeypatch):
    """``-s``: the session must not capture the descriptors otto's terminal output uses.

    On the CLI path the log listener's thread writes everything that reaches
    the terminal, the session's own report included (it goes through
    ``sys.stdout``, whose text joins the log queue). pytest's descriptor-level
    capture would swallow those writes for as long as each test runs.
    """
    calls = _count_sessions(monkeypatch)

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert "-s" in calls[0]


def test_a_typo_runs_nothing_and_says_nothing_else(project, tmp_path, sessions, capfd):
    """The name was placed nowhere and the session did not find it: one message, no JUnit.

    The session stops with a usage error pytest itself has no words for:
    the did-you-mean is otto's, said once, and the empty JUnit file pytest
    wrote is neither moved into place nor named.
    """
    _warm(project, tmp_path, sessions)
    project.write("tests/test_b.py", project.module("test_b", _B + "\n\n" + _test("test_b2")))
    capfd.readouterr()
    out = tmp_path / "out"

    with pytest.raises(UnknownSelectionError, match=r"'test_b3' \(did you mean: .*test_b2"):
        run_tests(["test_b3"], output_dir=out)

    assert sessions == [None]
    assert project.ran_tests() == []
    captured = capfd.readouterr()
    assert "ERROR" not in captured.err
    assert "generated xml file" not in captured.out
    assert not out.exists() or sorted(p.name for p in out.iterdir()) == []
    table = read_table(_the_project())
    assert table is not None
    assert "test_b2" in table.names, "what the session collected is recorded"


def test_a_typo_names_the_file_that_did_not_collect(project, tmp_path, sessions):
    """The broken file might have held the name: the refusal says it did not collect."""
    _warm(project, tmp_path, sessions)
    project.write("tests/sub/test_broken.py", "def test_broken(:\n")
    broken = r"tests/sub/test_broken\.py did not collect: SyntaxError"

    with pytest.raises(UnknownSelectionError, match=broken):
        run_tests(["test_c2"], output_dir=tmp_path / "first")
    project.next_run()

    with pytest.raises(UnknownSelectionError, match=broken):
        run_tests(["test_c2"], output_dir=tmp_path / "again")
    assert sessions == [None, None]
    assert project.ran_tests() == []


def test_a_name_in_a_project_where_nothing_collects_names_the_broken_file(one_project, tmp_path):
    """No test is known at all, but a file failed to collect: that is what the error says."""
    one_project(files={"tests/test_only.py": "def test_only(:\n"})

    with pytest.raises(UnknownSelectionError, match=r"tests/test_only\.py did not collect"):
        run_tests(["test_only"], output_dir=tmp_path / "out")


def test_a_deleted_holder_leaves_its_name_unknown(project, tmp_path, sessions):
    _warm(project, tmp_path, sessions)
    (project.root / "tests" / "test_b.py").unlink()

    with pytest.raises(UnknownSelectionError, match="test_b1"):
        run_tests(["test_b1"], output_dir=tmp_path / "out")

    assert project.ran_tests() == []


_BASE = "class Base:\n    def test_gone(self):\n        pass\n"
_DERIVED = "from helpers.base import Base\n\n\nclass TestDerived(Base):\n    pass\n"


@pytest.fixture
def inherited(project, tmp_path, sessions) -> _Project:
    """``tests/test_derived.py`` inherits its tests from a helper no stat watches; warm."""
    project.write("tests/helpers/__init__.py", "")
    project.write("tests/helpers/base.py", _BASE)
    project.write("tests/test_derived.py", project.module("test_derived", _DERIVED))
    # Two runs: the first finds the helper as a dependency, the second stamps it.
    for warm in ("warm1", "warm2"):
        assert run_tests(["test_gone"], output_dir=tmp_path / warm).exit_code == 0
        project.next_run()
    sessions.clear()
    return project


def test_a_name_a_helper_lost_is_unknown_after_the_one_session(inherited, tmp_path, sessions):
    inherited.write("tests/helpers/base.py", _BASE.replace("test_gone", "test_goner"))

    with pytest.raises(UnknownSelectionError, match=r"'test_gone' \(did you mean: test_goner"):
        run_tests(["test_gone"], output_dir=tmp_path / "out")

    assert sessions == [None]
    assert inherited.ran_tests() == []


def test_a_name_a_helper_gained_is_found_where_it_is_inherited(inherited, tmp_path, sessions):
    inherited.write("tests/helpers/base.py", _BASE + "\n    def test_added(self):\n        pass\n")

    result = run_tests(["test_added"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None]


# ── markers ──────────────────────────────────────────────────────────────────

_MARKED = (
    "import pytest\n\n\n@pytest.mark.slow\n" + _test("test_slow") + "\n\n" + _test("test_quick")
)
_SLOW_DECLARED = (
    "def pytest_configure(config):\n    config.addinivalue_line('markers', 'slow: a slow test')\n"
)


def test_a_name_the_marker_expression_excludes_matches_nothing(one_project, tmp_path):
    shell = _Project(tmp_path / "sut")
    one_project(
        files={
            "tests/conftest.py": _SLOW_DECLARED,
            "tests/test_m.py": shell.module("test_m", _MARKED),
        }
    )

    with pytest.raises(NoTestsMatchedError):
        run_tests(
            ["test_slow"], run_options=RunOptions(markers="not slow"), output_dir=tmp_path / "o"
        )

    assert shell.ran_tests() == []


def test_a_marker_alone_runs_one_whole_tree_session_per_project(two_projects, tmp_path, sessions):
    shell = _Project(tmp_path / "project_a")
    two_projects(
        a={
            "tests/conftest.py": _SLOW_DECLARED,
            "tests/test_m.py": shell.module("test_m", _MARKED),
        },
        b={
            "tests/conftest.py": _SLOW_DECLARED,
            "tests/test_other.py": "def test_other():\n    pass\n",
        },
    )

    result = run_tests(run_options=RunOptions(markers="slow"), output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert sessions == [None, None]
    assert shell.ran_tests() == ["test_slow"]
    assert [p.name for p in result.junit_paths] == ["junit_project_a.xml"]
    assert not (tmp_path / "out" / "junit_project_b.xml").exists()


# ── a file that fails to collect ─────────────────────────────────────────────


def test_a_broken_file_does_not_stop_the_named_tests_and_fails_every_run_as_in_pytest(
    project, tmp_path, sessions, caplog
):
    """It might hold the name, so every run collects it: each one reports it and exits 1."""
    _warm(project, tmp_path, sessions)
    project.write("tests/sub/test_broken.py", "def test_broken(:\n")

    for out in ("first", "again"):
        result = run_tests(["TestA"], output_dir=tmp_path / out)

        assert result.exit_code == 1
        assert project.ran_tests() == ["test_a1", "test_a2"]
        assert "test_broken.py" in caplog.text
        project.next_run()
        caplog.clear()


# ── several projects ────────────────────────────────────────────────────────────


def test_a_name_in_one_project_runs_there_only(two_projects, tmp_path):
    two_projects(
        a={"tests/test_a.py": "def test_in_a():\n    pass\n"},
        b={"tests/test_b.py": "def test_in_b():\n    pass\n"},
    )

    result = run_tests(["test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert [p.name for p in result.junit_paths] == ["junit_project_b.xml"]
    assert not (tmp_path / "out" / "junit_project_a.xml").exists()


def test_an_unknown_name_across_projects_runs_nothing(two_projects, tmp_path):
    shell = _Project(tmp_path / "project_a")
    two_projects(
        a={"tests/test_a.py": shell.module("test_a", _test("test_in_a"))},
        b={"tests/test_b.py": "def test_in_b():\n    pass\n"},
    )

    with pytest.raises(UnknownSelectionError, match="test_nowhere"):
        run_tests(["test_in_a", "test_nowhere"], output_dir=tmp_path / "out")

    assert shell.ran_tests() == []


def test_names_split_across_projects_each_run_where_they_live(two_projects, tmp_path):
    shell_a = _Project(tmp_path / "project_a")
    shell_b = _Project(tmp_path / "project_b")
    two_projects(
        a={"tests/test_a.py": shell_a.module("test_a", _test("test_in_a"))},
        b={"tests/test_b.py": shell_b.module("test_b", _test("test_in_b"))},
    )

    result = run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert shell_a.ran_tests() == ["test_in_a"]
    assert shell_b.ran_tests() == ["test_in_b"]
    assert [p.name for p in result.junit_paths] == ["junit_project_a.xml", "junit_project_b.xml"]


@pytest.fixture
def kinds(monkeypatch) -> list:
    """Each pytest session a run starts, as ``(project dir name, "collect" or "run")``."""
    import otto.suite.run as run_module

    seen: list = []
    real = run_module._run_pytest_session

    def spy(project, selection, run=None, **kwargs):
        seen.append((project.root.name, "collect" if run is None else "run"))
        return real(project, selection, run, **kwargs)

    monkeypatch.setattr(run_module, "_run_pytest_session", spy)
    return seen


def _two_warm(two_projects, tmp_path) -> list[_Project]:
    """``project_a`` holds ``test_in_a`` and ``project_b`` ``test_in_b``; both tables written."""
    shells = [_Project(tmp_path / "project_a"), _Project(tmp_path / "project_b")]
    two_projects(
        a={"tests/test_ra.py": shells[0].module("test_ra", _test("test_in_a"))},
        b={"tests/test_rb.py": shells[1].module("test_rb", _test("test_in_b"))},
    )
    assert run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "warm").exit_code == 0
    for shell in shells:
        shell.next_run()
    return shells


def test_an_unplaced_name_is_searched_first_where_a_test_was_just_written(
    two_projects, tmp_path, kinds
):
    """No collect-only pass: the changed project's run session finds it, then the other runs."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write(
        "tests/test_ra.py", a.module("test_ra", _test("test_in_a") + "\n\n" + _test("test_new"))
    )
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    result = run_tests(["test_new"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == ["test_new"]
    assert b.ran_tests() == []


def test_a_name_a_new_base_class_brings_is_found_by_the_search(two_projects, tmp_path, kinds):
    """The search is the run session of the project whose file changed: it finds and runs it."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/newbase.py", a.module("newbase", "class NewBase:\n" + _method("test_new")))
    a.write(
        "tests/test_ra.py",
        a.module(
            "test_ra",
            "from newbase import NewBase\n\n\nclass TestRa(NewBase):\n    pass\n\n\n"
            + _test("test_in_a"),
        ),
    )
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    result = run_tests(["test_new"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == ["TestRa::test_new"]


def test_two_cold_projects_read_each_file_once_and_start_no_empty_session(
    two_projects, tmp_path, kinds
):
    """``project_a`` searches first and lacks the name: it ran nothing, holds none, so it is done.

    ``project_b`` then searches, finds and runs it.
    """
    a, b = _Project(tmp_path / "project_a"), _Project(tmp_path / "project_b")
    two_projects(
        a={
            "tests/base.py": a.module("base", "class Base:\n" + _method("test_inherited")),
            "tests/test_ra.py": a.module(
                "test_ra",
                "from base import Base\n\n\nclass TestRa(Base):\n    pass\n\n\n"
                + _test("test_in_a"),
            ),
        },
        b={"tests/test_rb.py": b.module("test_rb", _test("test_in_b"))},
    )

    result = run_tests(["test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.imported() == ["base", "test_ra"], "read once"
    assert b.ran_tests() == ["test_in_b"]
    assert a.ran_tests() == []


def test_a_session_that_must_find_a_missing_name_runs_none_of_its_tests(
    two_projects, tmp_path, kinds
):
    """``project_b`` searches for ``test_nowhere`` first and lacks it: it runs neither name."""
    a, b = _two_warm(two_projects, tmp_path)
    assert a.ran_tests() == []
    kinds.clear()
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    with pytest.raises(UnknownSelectionError, match="test_nowhere"):
        run_tests(["test_in_b", "test_nowhere"], output_dir=tmp_path / "out")

    assert kinds == [("project_b", "run"), ("project_a", "run")]
    assert a.ran_tests() == b.ran_tests() == []


def test_a_typo_beside_a_new_dependency_runs_no_test(two_projects, tmp_path, kinds):
    """Each project searches in turn, its run session stopping before any test."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/newbase.py", a.module("newbase", "class NewBase:\n" + _method("test_new")))
    a.write(
        "tests/test_ra.py",
        a.module(
            "test_ra",
            "from newbase import NewBase\n\n\nclass TestRa(NewBase):\n    pass\n\n\n"
            + _test("test_in_a"),
        ),
    )
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    with pytest.raises(UnknownSelectionError, match="test_nowhere"):
        run_tests(["test_nowhere"], output_dir=tmp_path / "out")

    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == b.ran_tests() == []


def test_an_unknown_name_with_one_uncertain_project_runs_nothing_in_any_project(
    two_projects, tmp_path, kinds
):
    """The changed project searches first, then the other: neither runs a test."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    with pytest.raises(UnknownSelectionError, match="test_nowhere"):
        run_tests(["test_in_a", "test_nowhere"], output_dir=tmp_path / "out")

    assert kinds == [("project_b", "run"), ("project_a", "run")]
    assert a.ran_tests() == []
    assert b.ran_tests() == []


def test_an_unknown_name_across_two_uncertain_projects_runs_nothing(two_projects, tmp_path, kinds):
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/test_ra.py", a.module("test_ra", _test("test_in_a") + "\n\n" + _test("test_a2")))
    b.write("tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_b2")))

    with pytest.raises(UnknownSelectionError, match=r"test_nowhere"):
        run_tests(["test_in_a", "test_nowhere"], output_dir=tmp_path / "out")

    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == b.ran_tests() == []


def test_a_cold_project_beside_a_warm_one_runs_the_name_in_both(two_projects, tmp_path, kinds):
    """``project_b``'s pytest config changed: searched whole though project_a holds the name."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    b.write(
        "tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_in_a"))
    )
    b.write("pytest.ini", "[pytest]\n")

    result = run_tests(["test_in_a"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == ["test_in_a"]
    assert b.ran_tests() == ["test_in_a"]


def test_a_name_a_fresh_record_holds_also_runs_where_a_changed_file_now_holds_it(
    two_projects, tmp_path, kinds
):
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    b.write(
        "tests/test_rb.py", b.module("test_rb", _test("test_in_b") + "\n\n" + _test("test_in_a"))
    )

    result = run_tests(["test_in_a"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == ["test_in_a"]
    assert b.ran_tests() == ["test_in_a"]


def _claim(project_root: Path, rel: str, name: str) -> None:
    """Make the table's record of *rel* say it holds *name*, which the file does not.

    What an edit that keeps the file's size and mtime leaves behind (Python's
    own bytecode cache would not see it either): a fresh record that is wrong.
    """
    import dataclasses

    from otto.bootstrap import get_projects
    from otto.config import collected_tests as tr

    [project] = [r for r in get_projects() if r.root == project_root]
    table = tr.read_table(project)
    assert table is not None
    key = str(project_root / rel)
    record = table.files[key]
    lying = dataclasses.replace(record, tests=[*record.tests, tr.RecordedTest([], name)])
    tr.write_tables([dataclasses.replace(table, files={**table.files, key: lying})])


def test_a_name_every_holder_only_claims_is_an_error_after_the_run(two_projects, tmp_path, kinds):
    """Both records say ``test_ghost`` is there and neither file has it.

    Neither session had to find it (two projects hold it), so the run says so
    once both are done: loud, never a silent run of fewer tests.
    """
    _two_warm(two_projects, tmp_path)
    kinds.clear()
    _claim(tmp_path / "project_a", "tests/test_ra.py", "test_ghost")
    _claim(tmp_path / "project_b", "tests/test_rb.py", "test_ghost")

    with pytest.raises(UnknownSelectionError, match="test_ghost"):
        run_tests(["test_ghost"], output_dir=tmp_path / "out")

    assert kinds == [("project_a", "run"), ("project_b", "run")]


def test_a_name_its_one_holder_only_claims_is_an_error_after_the_run(two_projects, tmp_path, kinds):
    """Placed in ``project_a`` by a wrong record, maybe in ``project_b`` too: neither must find it.

    So the run says so once both are done: loud, never a silent run of fewer tests.
    """
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    _claim(tmp_path / "project_a", "tests/test_ra.py", "test_ghost")

    with pytest.raises(UnknownSelectionError, match="test_ghost"):
        run_tests(["test_ghost", "test_in_b"], output_dir=tmp_path / "out")

    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == []
    assert b.ran_tests() == ["test_in_b"]


_STOPS_THE_SESSION = "import pytest\n\npytest.exit('the lab is not configured')\n"


def test_a_search_that_cannot_finish_ends_the_run_with_its_exit_code_and_why(
    two_projects, tmp_path, kinds, caplog
):
    """``project_b`` was never searched: no later session starts, no name is called unknown."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/test_ra.py", a.module("test_ra", _test("test_in_a") + "\n\n" + _test("test_a2")))
    b.write("tests/conftest.py", _STOPS_THE_SESSION)

    result = run_tests(["test_nowhere"], output_dir=tmp_path / "out")

    assert result.exit_code == pytest.ExitCode.USAGE_ERROR
    assert result.junit_paths == []
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    [error] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "'project_b'" in error
    assert "the lab is not configured" in error
    assert a.ran_tests() == b.ran_tests() == []


_EXITS_WHEN_COLLECTING_ONLY = (
    "import pytest\n\n\n"
    "def pytest_configure(config):\n"
    "    if config.option.collectonly:\n"
    "        pytest.exit('no collect-only here', returncode=3)\n"
)


def test_a_typo_is_searched_for_by_run_sessions_never_a_collect_only_one(
    two_projects, tmp_path, kinds
):
    """A run's search is the run sessions themselves, each stopping before its tests."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/conftest.py", _EXITS_WHEN_COLLECTING_ONLY)
    b.write("tests/conftest.py", _EXITS_WHEN_COLLECTING_ONLY)

    with pytest.raises(UnknownSelectionError, match="test_typo"):
        run_tests(["test_in_a", "test_typo"], output_dir=tmp_path / "out")

    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == b.ran_tests() == []


def test_the_one_uncertain_project_that_cannot_finish_ends_the_run_before_any_test(
    two_projects, tmp_path, kinds, caplog
):
    """``test_new`` could only be in ``project_a``, which never got through collection."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/conftest.py", _STOPS_THE_SESSION)

    result = run_tests(["test_new", "test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == pytest.ExitCode.USAGE_ERROR
    assert result.junit_paths == []
    assert kinds == [("project_a", "run")]
    [error] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "'project_a'" in error
    assert "the lab is not configured" in error
    assert a.ran_tests() == b.ran_tests() == []


def test_a_marker_run_goes_on_past_a_project_that_cannot_finish(
    two_projects, tmp_path, kinds, caplog
):
    """No name was left for that project to find: the next project still runs, and the run fails."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    a.write("tests/conftest.py", _STOPS_THE_SESSION)

    result = run_tests(run_options=RunOptions(markers="not slow"), output_dir=tmp_path / "out")

    assert result.exit_code == pytest.ExitCode.USAGE_ERROR
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert b.ran_tests() == ["test_in_b"]
    [error] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "Could not collect the tests of project 'project_a'" in error
    assert "the lab is not configured" in error


def test_a_changed_project_whose_conftest_raises_is_logged_by_name(
    two_projects, tmp_path, kinds, caplog
):
    """``project_b`` only had its changed files to re-read; its failure still names it."""
    a, b = _two_warm(two_projects, tmp_path)
    kinds.clear()
    b.write("tests/conftest.py", "raise RuntimeError('boom conftest')\n")

    result = run_tests(["test_in_a"], output_dir=tmp_path / "out")

    assert result.exit_code == pytest.ExitCode.USAGE_ERROR
    assert kinds == [("project_a", "run"), ("project_b", "run")]
    assert a.ran_tests() == ["test_in_a"]
    [error] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "Could not collect the tests of project 'project_b'" in error
    assert "RuntimeError: boom conftest" in error


def test_a_run_that_learns_nothing_new_leaves_the_cache_file_alone(project, tmp_path, sessions):
    """Not even to date what it read again: the table was written a minute ago."""

    _warm(project, tmp_path, sessions)
    table = read_table(_the_project())
    assert table is not None
    earlier = {
        k: dataclasses.replace(r, collected_at=r.collected_at - 60) for k, r in table.files.items()
    }
    write_tables([dataclasses.replace(table, files=earlier)])
    cache = cc._cache_path()
    assert cache is not None
    before = cache.stat()

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert sessions == [None]
    after = cache.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_run_finds_the_workspace_home_once(project, tmp_path, sessions, monkeypatch):
    """The table's read, the session's bytecode and cache dirs and the table's write share it."""
    from otto.config import home

    _warm(project, tmp_path, sessions)
    calls: list[object] = []
    real = home.workspace_home

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(home, "workspace_home", counting)

    assert run_tests(["TestA"], output_dir=tmp_path / "out").exit_code == 0

    assert len(calls) == 1


def test_a_broken_file_in_another_project_is_reported_once_and_fails_the_run(
    two_projects, tmp_path, sessions, caplog
):
    """``project_b``'s session reads its broken file once: logged once; pytest's exit 1 stands."""
    shell_a = _Project(tmp_path / "project_a")
    shell_b = _Project(tmp_path / "project_b")
    root_a, root_b = two_projects(
        a={"tests/test_ra.py": shell_a.module("test_ra", _test("test_in_a"))},
        b={"tests/test_rb.py": shell_b.module("test_rb", _test("test_in_b"))},
    )
    assert run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "warm").exit_code == 0
    shell_a.next_run()
    shell_b.next_run()
    sessions.clear()
    shell_a.write(
        "tests/test_ra.py",
        shell_a.module("test_ra", _test("test_in_a") + "\n\n" + _test("test_new")),
    )
    shell_b.write("tests/test_broken.py", "def test_broken(:\n")

    result = run_tests(["test_new"], output_dir=tmp_path / "out")

    assert result.exit_code == 1
    assert sessions == [None, None]
    assert shell_a.ran_tests() == ["test_new"]
    failures = [r for r in caplog.records if "test_broken.py" in r.getMessage()]
    assert len(failures) == 1
    assert root_a != root_b


# ── a record's ABSENCE of a name is not trusted blindly ──────────────────────
#
# The reviewer's probes: a run must never run fewer tests than its names
# select. A static (``ast``) record proves only presence, and a file gains
# tests from its dependencies (a base class in another module, a library)
# without its own stat moving.


def _holders(project: _Project) -> dict[str, str]:
    base = project.module(
        "test_base",
        "class TestBase:\n"
        "    def test_x(self):\n"
        "        _ran(f'{type(self).__name__}@{__name__}::test_x')\n",
    )
    derived = project.module(
        "test_d", "from test_base import TestBase\n\n\nclass TestD(TestBase):\n    pass\n"
    )
    plain = project.module("test_p", "def test_x():\n    _ran('plain::test_x')\n")
    return {"tests/test_base.py": base, "tests/test_d.py": derived, "tests/test_p.py": plain}


def test_a_name_a_base_class_gains_runs_in_every_file_holding_it(one_project, tmp_path, sessions):
    shell = _Project(tmp_path / "sut")
    files = _holders(shell)
    root = one_project(files=files)
    assert run_tests(["test_x"], output_dir=tmp_path / "warm").exit_code == 0
    shell.next_run()
    sessions.clear()
    base = files["tests/test_base.py"]
    shell.write(
        "tests/test_base.py",
        base + "\n    def test_y(self):\n        _ran(f'{type(self).__name__}::test_y')\n",
    )
    assert root == shell.root

    result = run_tests(["test_y"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert shell.ran_tests() == ["TestBase::test_y", "TestBase::test_y", "TestD::test_y"]


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_a_name_a_library_base_class_gains_runs_where_it_is_inherited(
    tmp_path, monkeypatch, sessions
):
    from tests._fixtures.project_scaffold import make_project
    from tests._fixtures.run_projects import _wire_projects

    shell = _Project(tmp_path / "sut")
    lib_base = "class LibBase:\n    def test_x(self):\n        pass\n"
    make_project(
        shell.root,
        tests=["tests"],
        extra='libs = ["pylib"]\n',
        files={
            "pylib/libbase.py": lib_base,
            "tests/test_uses.py": shell.module(
                "test_uses",
                "from libbase import LibBase\n\n\nclass TestUses(LibBase):\n    pass\n",
            ),
            "tests/test_other.py": shell.module("test_other", _test("test_other")),
        },
    )
    monkeypatch.syspath_prepend(str(shell.root / "pylib"))
    _wire_projects(monkeypatch, [shell.root])
    assert run_tests(["test_x"], output_dir=tmp_path / "warm").exit_code == 0
    shell.next_run()
    sessions.clear()
    (shell.root / "pylib" / "libbase.py").write_text(
        lib_base + "\n    def test_y(self):\n        _ran('TestUses::test_y')\n"
    )
    # The library imports `_ran` from nowhere: give it one through the module.
    (shell.root / "pylib" / "libbase.py").write_text(
        f"def _ran(name):\n    with open({str(shell.ran)!r}, 'a') as f:\n"
        "        f.write(name + '\\n')\n\n\n"
        + lib_base
        + "\n    def test_y(self):\n        _ran('TestUses::test_y')\n"
    )

    result = run_tests(["test_y"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert shell.ran_tests() == ["TestUses::test_y"]
    assert sessions == [None]


# A file's dependencies come from its module's namespace too, not only from its
# items: a class that collected nothing yet, and what a star-import brought in.
# Each case edits a dependency, then checks the warm run against a cold one.


def _plain() -> str:
    return _test("test_x") + "\n\n" + _test("test_y")


def _warm_then_cold(
    shell: _Project, tmp_path: Path, monkeypatch, sessions, edit
) -> list[list[str]]:
    """Warm the table, apply *edit*, run ``test_y``; then run it again on an empty table."""
    assert run_tests(["test_x"], output_dir=tmp_path / "warm").exit_code == 0
    shell.next_run()
    sessions.clear()
    edit()
    assert run_tests(["test_y"], output_dir=tmp_path / "out").exit_code == 0
    warm = shell.ran_tests()
    shell.next_run()
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "cold-home"))
    assert run_tests(["test_y"], output_dir=tmp_path / "cold").exit_code == 0
    return [warm, shell.ran_tests()]


def _method(name: str) -> str:
    return f"    def {name}(self):\n        _ran(f'{{type(self).__name__}}::{name}')\n"


def test_a_class_that_collected_nothing_runs_the_first_test_its_base_gains(
    one_project, tmp_path, monkeypatch, sessions
):
    shell = _Project(tmp_path / "sut")
    one_project(
        files={
            "tests/helpers_base.py": shell.module("helpers_base", "class Base:\n    pass\n"),
            "tests/test_d.py": shell.module(
                "test_d",
                "from helpers_base import Base\n\n\nclass TestD(Base):\n    pass\n\n\n"
                + _test("test_d_own"),
            ),
            "tests/test_p.py": shell.module("test_p", _plain()),
        }
    )

    def edit() -> None:
        shell.write(
            "tests/helpers_base.py",
            shell.module("helpers_base", "class Base:\n" + _method("test_y")),
        )

    warm, cold = _warm_then_cold(shell, tmp_path, monkeypatch, sessions, edit)

    assert warm == cold == ["TestD::test_y", "test_y"]


def test_a_test_a_star_imported_helper_module_gains_runs(
    one_project, tmp_path, monkeypatch, sessions
):
    shell = _Project(tmp_path / "sut")
    one_project(
        files={
            "tests/common.py": shell.module("common", "def helper():\n    pass\n"),
            "tests/test_s.py": shell.module(
                "test_s", "from common import *  # noqa: F403\n\n\n" + _test("test_s_own")
            ),
            "tests/test_p.py": shell.module("test_p", _plain()),
        }
    )

    def edit() -> None:
        shell.write(
            "tests/common.py",
            shell.module(
                "common", "def helper():\n    pass\n\n\ndef test_y():\n    _ran('common::test_y')\n"
            ),
        )

    warm, cold = _warm_then_cold(shell, tmp_path, monkeypatch, sessions, edit)

    assert warm == cold == ["common::test_y", "test_y"]


def test_a_base_class_outside_the_project_and_site_packages_is_tracked(
    one_project, tmp_path, monkeypatch, sessions
):
    """An editable install or a ``PYTHONPATH`` dir: its edits reach no ``env`` stat."""
    shell = _Project(tmp_path / "sut")
    shared = tmp_path / "shared_src"
    shared.mkdir()
    base = shell.module("sharedbase", "class SharedBase:\n" + _method("test_x"))
    (shared / "sharedbase.py").write_text(base)
    monkeypatch.syspath_prepend(str(shared))
    one_project(
        files={
            "tests/test_d.py": shell.module(
                "test_d",
                "from sharedbase import SharedBase\n\n\nclass TestD(SharedBase):\n    pass\n",
            ),
            "tests/test_p.py": shell.module("test_p", _plain()),
        }
    )

    def edit() -> None:
        (shared / "sharedbase.py").write_text(base + _method("test_y"))

    warm, cold = _warm_then_cold(shell, tmp_path, monkeypatch, sessions, edit)

    assert warm == cold == ["TestD::test_y", "test_y"]


def test_a_test_a_constant_in_an_imported_module_enables_runs(
    one_project, tmp_path, monkeypatch, sessions
):
    """``import featcfg`` makes ``featcfg.py`` a dependency: flipping its flag re-collects."""
    shell = _Project(tmp_path / "sut")
    one_project(
        files={
            "tests/featcfg.py": "FEATURE = False\n",
            "tests/test_d.py": shell.module(
                "test_d",
                "import featcfg\n\n\n"
                + _test("test_d_own")
                + "\n\nif featcfg.FEATURE:\n\n    def test_y():\n        _ran('gated::test_y')\n",
            ),
            "tests/test_p.py": shell.module("test_p", _plain()),
        }
    )

    def edit() -> None:
        shell.write("tests/featcfg.py", "FEATURE = True\n")

    warm, cold = _warm_then_cold(shell, tmp_path, monkeypatch, sessions, edit)

    assert warm == cold == ["gated::test_y", "test_y"]


def test_a_sourceless_library_is_followed_through_its_pyc(
    one_project, tmp_path, monkeypatch, sessions
):
    """A ``.pyc``-only base class: its ``.pyc`` is the dependency, not the missing ``.py``.

    The functions' code still names the source path they were compiled from;
    that file is not there, and keeping it would re-collect the holder on
    every run. A third run finds the holder fresh.
    """
    import py_compile

    import otto.bootstrap
    from otto.config import collected_tests as tr

    shell = _Project(tmp_path / "sut")
    lib = tmp_path / "pyconly"
    lib.mkdir()
    source = tmp_path / "srcs" / "pycbase.py"
    source.parent.mkdir()
    base = shell.module("pycbase", "class PycBase:\n" + _method("test_x"))

    def compile_lib(text: str) -> None:
        source.write_text(text)
        py_compile.compile(str(source), cfile=str(lib / "pycbase.pyc"))
        source.unlink()

    compile_lib(base)
    monkeypatch.syspath_prepend(str(lib))
    one_project(
        files={
            "tests/test_d.py": shell.module(
                "test_d", "from pycbase import PycBase\n\n\nclass TestD(PycBase):\n    pass\n"
            ),
            "tests/test_p.py": shell.module("test_p", _plain()),
        }
    )

    def edit() -> None:
        compile_lib(base + _method("test_y"))

    warm, cold = _warm_then_cold(shell, tmp_path, monkeypatch, sessions, edit)

    assert warm == cold == ["TestD::test_y", "test_y"]
    [the_project] = otto.bootstrap.get_projects()
    table = tr.read_table(the_project)
    assert table is not None
    assert list(table.deps) == [str(lib / "pycbase.pyc")]
    shell.next_run()
    assert run_tests(["test_y"], output_dir=tmp_path / "third").exit_code == 0
    table = tr.read_table(the_project)
    assert table is not None
    fresh = tr.classify(the_project, table).fresh
    assert shell.root / "tests" / "test_d.py" in fresh


def test_a_global_whose_class_raises_never_stops_the_run(one_project, tmp_path):
    """The namespace walk reads each value's ``type()``, never its ``__class__``.

    Before, ``isinstance`` on such a global ended the session in an
    INTERNALERROR. (That a lazy proxy is never set up by the walk is pinned in
    ``test_plugin_names.py``: pytest's own collection probes every global.)
    """
    shell = _Project(tmp_path / "sut")
    hostile = (
        "class _Hostile:\n"
        "    @property\n"
        "    def __class__(self):\n"
        "        raise RuntimeError('lazy settings not configured')\n\n\n"
        "settings = _Hostile()\n"
    )
    one_project(
        files={
            "tests/lazyset.py": hostile,
            "tests/test_d.py": shell.module(
                "test_d", "from lazyset import settings\n\n\n" + _test("test_x")
            ),
        }
    )

    result = run_tests(["test_x"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert shell.ran_tests() == ["test_x"]


# ── the coverage pre-clean waits for a session that will run tests ───────────


@pytest.fixture
def pre_clean(monkeypatch):
    """Coverage forced on; the remote pre-clean and the post-run collection recorded."""
    import dataclasses
    from unittest.mock import AsyncMock

    import otto.suite.run as run_module

    monkeypatch.setattr(
        run_module,
        "resolve_coverage",
        lambda opts, projects, *, command: dataclasses.replace(opts, cov=True),
    )
    clean = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_coverage", clean)
    monkeypatch.setattr(run_module, "_post_run_coverage", AsyncMock())
    return clean


def test_a_typo_never_cleans_the_remote_coverage(project, tmp_path, sessions, pre_clean):
    _warm(project, tmp_path, sessions)
    pre_clean.reset_mock()

    with pytest.raises(UnknownSelectionError):
        run_tests(["TestA", "test_a3"], output_dir=tmp_path / "out")

    pre_clean.assert_not_awaited()


def test_a_typo_the_session_finds_never_touches_the_remote_coverage(
    project, tmp_path, sessions, pre_clean
):
    """The session stops for the name: no pre-clean, and no coverage fetch either."""
    import otto.suite.run as run_module

    _warm(project, tmp_path, sessions)
    project.write("tests/test_a.py", project.module("test_a", _A + "\n\n" + _test("test_extra")))
    pre_clean.reset_mock()
    run_module._post_run_coverage.reset_mock()

    with pytest.raises(UnknownSelectionError):
        run_tests(["TestA", "test_a3"], output_dir=tmp_path / "out")

    assert sessions == [None]
    assert project.ran_tests() == []
    pre_clean.assert_not_awaited()
    run_module._post_run_coverage.assert_not_awaited()


def test_a_name_the_marker_excludes_never_cleans_the_remote_coverage(
    one_project, tmp_path, pre_clean
):
    shell = _Project(tmp_path / "sut")
    one_project(
        files={
            "tests/conftest.py": _SLOW_DECLARED,
            "tests/test_m.py": shell.module("test_m", _MARKED),
        }
    )

    with pytest.raises(NoTestsMatchedError):
        run_tests(
            ["test_slow"], run_options=RunOptions(markers="not slow"), output_dir=tmp_path / "o"
        )

    pre_clean.assert_not_awaited()


def test_the_remote_coverage_is_cleaned_once_before_the_first_test(
    two_projects, tmp_path, pre_clean
):
    from otto.coverage.reports import CleanReport

    shell_a = _Project(tmp_path / "project_a")
    shell_b = _Project(tmp_path / "project_b")
    two_projects(
        a={"tests/test_a.py": shell_a.module("test_a", _test("test_in_a"))},
        b={"tests/test_b.py": shell_b.module("test_b", _test("test_in_b"))},
    )

    def _clean_and_record(projects: object) -> CleanReport:
        shell_a.ran.write_text("PRECLEAN\n")
        return CleanReport(hosts={})

    pre_clean.side_effect = _clean_and_record

    result = run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    pre_clean.assert_awaited_once()
    assert logged_lines(shell_a.ran) == ["PRECLEAN", "test_in_a"]


def test_a_failing_pre_clean_stops_the_run_with_its_own_error(project, tmp_path, pre_clean):
    pre_clean.side_effect = ValueError("no route to the coverage host")

    with pytest.raises(ValueError, match="no route to the coverage host"):
        run_tests(["TestA"], output_dir=tmp_path / "out")

    assert project.ran_tests() == []


# ── one seed per run; JUnit only from a session that ran ─────────────────────


def test_every_session_of_a_run_uses_the_one_seed(two_projects, tmp_path, monkeypatch, caplog):

    shell_a = _Project(tmp_path / "project_a")
    shell_b = _Project(tmp_path / "project_b")
    two_projects(
        a={"tests/test_ra.py": shell_a.module("test_ra", _test("test_in_a"))},
        b={"tests/test_rb.py": shell_b.module("test_rb", _test("test_in_b"))},
    )
    seen: list[str] = []
    real_main = pytest.main

    def main(args, **kwargs):
        seen.extend(a for a in args if a.startswith("--randomly-seed="))
        return real_main(args, **kwargs)

    monkeypatch.setattr("pytest.main", main)

    with caplog.at_level(logging.INFO):
        result = run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert len(seen) >= 2, "the run sessions, and the search that stopped before its tests"
    assert len(set(seen)) == 1
    seeds = [r for r in caplog.records if "random test order, seed" in r.getMessage()]
    assert len(seeds) == 1


def test_a_typo_leaves_an_existing_results_file_alone(project, tmp_path, sessions):
    """The session stops for the typo: pytest's empty JUnit goes."""
    _warm(project, tmp_path, sessions)
    project.write("tests/test_b.py", project.module("test_b", _B + "\n\n" + _test("test_b2")))
    results = tmp_path / "keep.xml"
    results.write_text("<previous/>")

    with pytest.raises(UnknownSelectionError):
        run_tests(
            ["test_b3"],
            run_options=RunOptions(results=str(results)),
            output_dir=tmp_path / "o",
        )

    assert sessions == [None]
    assert results.read_text() == "<previous/>"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".keep")) == []


def test_a_run_names_the_junit_file_where_it_ends_up(project, tmp_path, capfd):
    out = tmp_path / "out"

    assert run_tests(["TestA"], output_dir=out).exit_code == 0

    printed = capfd.readouterr().out
    assert f"generated xml file: {out / 'junit.xml'}" in printed
    assert ".partial" not in printed
    assert sorted(p.name for p in out.iterdir()) == ["junit.xml"]


# One PID per length, 1 to 7 digits (pid_max is 4194304 on the dev VM): the
# temporary JUnit name, and so the banner pytest would print for it, grows
# with the PID.
_PIDS = [1, 42, 999, 1234, 99999, 123456, 4194304]


@pytest.mark.parametrize("pid", _PIDS)
def test_pytest_prints_the_junit_banner_for_the_file_it_names(
    project, tmp_path, capfd, monkeypatch, pid
):
    """The banner is pytest's own, for the final file, whatever the PID.

    This guards pytest's behaviour as ``JUnitPromotion`` relies on it: that
    the junitxml plugin writes the file before ``pytest_terminal_summary``,
    that a ``tryfirst`` implementation runs before the one that prints the
    banner, and that the banner names the plugin's ``logfile``, found under
    ``_pytest.junitxml.xml_key``.
    """
    # Wide enough that the banner is padded for either name: a long tmp_path
    # would leave one "-" a side of both.
    monkeypatch.setenv("COLUMNS", "250")
    out = tmp_path / "out"
    monkeypatch.setattr(
        "otto.suite.run._partial_junit_path",
        lambda final: final.with_name(f".{final.name}.{pid}.partial"),
    )
    banner = io.StringIO()
    writer = TerminalWriter(banner)
    writer.fullwidth = 250
    writer.sep("-", f"generated xml file: {out / 'junit.xml'}")

    assert run_tests(["TestA"], output_dir=out).exit_code == 0

    assert banner.getvalue() in capfd.readouterr().out
    assert sorted(p.name for p in out.iterdir()) == ["junit.xml"]


def test_a_kept_run_leaves_only_its_final_junit_file(project, tmp_path):
    """A kept run ends with its JUnit file at the final name and no temporary file left."""
    out = tmp_path / "out"

    result = run_tests(["TestA"], output_dir=out)

    assert result.exit_code == 0
    assert result.junit_paths == [out / "junit.xml"]
    assert sorted(p.name for p in out.iterdir()) == ["junit.xml"]
    assert "<testcase" in (out / "junit.xml").read_text()


# ── a refusal after another project's tests ran ─────────────────────────────────

_REFUSING = (
    "from otto.registry import RegistrationRefused\n"
    "raise RegistrationRefused('register from an init module')\n"
)


def _refusing_after_a_warm_run(two_projects, tmp_path) -> Path:
    """Two warm projects; then project_b's test file registers something as it loads.

    The name lives in project_a, so nothing needs a refresh: project_a's session
    runs first and project_b's (its changed file) refuses afterwards.
    """
    import otto.suite.run as run_module

    two_projects(
        a={"tests/test_a.py": "def test_in_a():\n    pass\n"},
        b={"tests/test_b.py": "def test_in_b():\n    pass\n"},
    )
    assert run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "warm").exit_code == 0
    _Project(tmp_path / "project_a").next_run()
    run_module._post_run_coverage.reset_mock()
    test_b = tmp_path / "project_b" / "tests" / "test_b.py"
    test_b.write_text(_REFUSING)
    return test_b


def test_a_refusal_in_a_later_project_still_collects_the_earlier_projects_coverage(
    two_projects, tmp_path, pre_clean
):
    import otto.suite.run as run_module
    from otto.registry import RegistrationRefused

    _refusing_after_a_warm_run(two_projects, tmp_path)
    out = tmp_path / "out"

    with pytest.raises(RegistrationRefused):
        run_tests(["test_in_a"], output_dir=out)

    run_module._post_run_coverage.assert_awaited_once()
    assert (out / "junit_project_a.xml").exists()
    assert not (out / "junit_project_b.xml").exists()


def test_a_refusal_a_search_finds_stops_the_run_before_any_test(
    two_projects, tmp_path, pre_clean, sessions
):
    """Both projects cold and the name placed nowhere: ``project_a`` searches first and loads it."""
    import otto.suite.run as run_module
    from otto.registry import RegistrationRefused

    shell = _Project(tmp_path / "project_b")
    two_projects(
        a={"tests/test_a.py": _REFUSING},
        b={"tests/test_b.py": shell.module("test_b", _test("test_in_b"))},
    )

    with pytest.raises(RegistrationRefused):
        run_tests(["test_in_b"], output_dir=tmp_path / "out")

    assert sessions == [None]
    assert shell.ran_tests() == []
    pre_clean.assert_not_awaited()
    run_module._post_run_coverage.assert_not_awaited()


def test_a_coverage_failure_after_a_refusal_never_hides_the_refusal(
    two_projects, tmp_path, pre_clean, monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    import otto.suite.run as run_module
    from otto.registry import RegistrationRefused

    _refusing_after_a_warm_run(two_projects, tmp_path)
    monkeypatch.setattr(
        run_module, "_post_run_coverage", AsyncMock(side_effect=RuntimeError("no gcda"))
    )

    with caplog.at_level("ERROR", logger="otto.suite.run"), pytest.raises(RegistrationRefused):
        run_tests(["test_in_a"], output_dir=tmp_path / "out")

    run_module._post_run_coverage.assert_awaited_once()
    [record] = [r for r in caplog.records if "coverage collection" in r.getMessage()]
    assert "project_a" in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "no gcda"


def test_a_coverage_failure_with_nothing_else_wrong_is_raised(
    project, tmp_path, pre_clean, monkeypatch
):
    from unittest.mock import AsyncMock

    import otto.suite.run as run_module

    monkeypatch.setattr(
        run_module, "_post_run_coverage", AsyncMock(side_effect=RuntimeError("no gcda"))
    )

    with pytest.raises(RuntimeError, match="no gcda"):
        run_tests(["test_b1"], output_dir=tmp_path / "out")


# ── a search that stops: what it learned stays, what it wrote does not ───────


def test_a_broken_file_a_stopped_search_read_still_fails_the_run(
    two_projects, tmp_path, sessions, caplog
):
    """``project_a`` searches first, lacks the name and holds none: no session of its runs again.

    Its collection error is still the run's, logged once: pytest over both
    trees exits 1.
    """
    shell_b = _Project(tmp_path / "project_b")
    two_projects(
        a={"tests/test_broken.py": "def test_broken(:\n"},
        b={"tests/test_rb.py": shell_b.module("test_rb", _test("test_in_b"))},
    )

    result = run_tests(["test_in_b"], output_dir=tmp_path / "out")

    assert sessions == [None, None]
    assert shell_b.ran_tests() == ["test_in_b"]
    assert result.exit_code == 1
    failures = [r for r in caplog.records if "test_broken.py" in r.getMessage()]
    assert len(failures) == 1


def test_a_broken_file_a_search_read_is_logged_once_though_its_project_runs_again(
    two_projects, tmp_path, sessions, caplog
):
    """``project_a`` searches for ``test_in_b`` and stops; it runs again for ``test_in_a``."""
    shell_a = _Project(tmp_path / "project_a")
    shell_b = _Project(tmp_path / "project_b")
    two_projects(
        a={
            "tests/test_ra.py": shell_a.module("test_ra", _test("test_in_a")),
            "tests/test_broken.py": "def test_broken(:\n",
        },
        b={"tests/test_rb.py": shell_b.module("test_rb", _test("test_in_b"))},
    )

    result = run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "out")

    assert sessions == [None, None, None]
    assert shell_a.ran_tests() == ["test_in_a"]
    assert result.exit_code == 1
    failures = [r for r in caplog.records if "test_broken.py" in r.getMessage()]
    assert len(failures) == 1


_EXITS_WHILE_COLLECTING = (
    "import pytest\n\n\n"
    "def pytest_collection_modifyitems(items):\n"
    "    pytest.exit('the lab is not configured', returncode=2)\n"
)


def test_a_search_a_conftest_stops_while_collecting_keeps_the_earlier_results(
    project, tmp_path, sessions
):
    """pytest writes JUnit on its way out of a session no test ran in: it is not put in place."""
    _warm(project, tmp_path, sessions)
    project.write("tests/conftest.py", _EXITS_WHILE_COLLECTING)
    results = tmp_path / "keep.xml"
    results.write_text("<previous/>")

    result = run_tests(
        ["test_new"], run_options=RunOptions(results=str(results)), output_dir=tmp_path / "o"
    )

    assert result.exit_code == pytest.ExitCode.INTERRUPTED
    assert result.junit_paths == []
    assert project.ran_tests() == []
    assert results.read_text() == "<previous/>"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".keep")) == []


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_split_names_on_cold_projects_collect_each_project_at_most_twice(
    tmp_path, monkeypatch, kinds
):
    """Refusing a typo before any test costs a stopped search per project but the last: 2N - 1.

    Cold, the names one per project: a and b search and stop, c finds its
    name and runs, then a and b run.
    """
    from tests._fixtures.project_scaffold import make_project
    from tests._fixtures.run_projects import _wire_projects

    roots = []
    for name in ("project_a", "project_b", "project_c"):
        shell = _Project(tmp_path / name)
        make_project(
            shell.root,
            name=name,
            tests=["tests"],
            files={f"tests/test_{name}.py": shell.module(f"test_{name}", _test(f"test_in_{name}"))},
        )
        roots.append(shell.root)
    _wire_projects(monkeypatch, roots)

    result = run_tests(
        ["test_in_project_a", "test_in_project_b", "test_in_project_c"], output_dir=tmp_path / "out"
    )

    assert result.exit_code == 0
    assert kinds == [
        ("project_a", "run"),
        ("project_b", "run"),
        ("project_c", "run"),
        ("project_a", "run"),
        ("project_b", "run"),
    ]
