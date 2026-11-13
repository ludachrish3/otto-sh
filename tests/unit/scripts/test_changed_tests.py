"""``scripts/changed_tests.py``: the test files a diff adds or modifies, by root.

A pull request's CI repeats these files (``tests_changed_repeat_*`` in the
noxfile), so a test that fails only sometimes is caught where it is
introduced. The selection is by file, never by function: a file the diff
touches is repeated whole, and each lane's marker expression then decides
which of its tests run.

The git half runs against a throwaway repo under ``tmp_path``
(``tests._fixtures.gitrepo.TmpGitRepo``), never the dev repo.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.changed_tests import changed_test_files
from scripts.gate_scope import Change
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT

# The subject is a repository script, not otto under this interpreter.
pytestmark = pytest.mark.interpreter_agnostic

SCRIPT = CHECKOUT_ROOT / "scripts" / "changed_tests.py"


def test_added_and_modified_test_files_under_the_roots_are_selected():
    changes = [
        Change("tests/unit/test_a.py", "M"),
        Change("tests/unit/cli/test_b.py", "A"),
        Change("tests/e2e/cli/test_c_e2e.py", "A"),
    ]
    assert changed_test_files(changes, ["tests/unit"]) == [
        "tests/unit/cli/test_b.py",
        "tests/unit/test_a.py",
    ]
    assert changed_test_files(changes, ["tests/e2e"]) == ["tests/e2e/cli/test_c_e2e.py"]


def test_a_deleted_test_file_is_never_selected():
    """pytest would fail on a path that no longer exists."""
    changes = [Change("tests/unit/test_gone.py", "D"), Change("tests/unit/test_kept.py", "M")]
    assert changed_test_files(changes, ["tests/unit"]) == ["tests/unit/test_kept.py"]


def test_only_test_modules_are_selected():
    """A conftest or a helper is not a file pytest collects tests from."""
    changes = [
        Change("tests/unit/conftest.py", "M"),
        Change("tests/unit/helpers.py", "M"),
        Change("tests/unit/_private.py", "A"),
        Change("tests/unit/test_real.py", "M"),
        Change("tests/unit/test_data.json", "M"),
    ]
    assert changed_test_files(changes, ["tests/unit"]) == ["tests/unit/test_real.py"]


def test_a_root_is_a_directory_not_a_name_prefix():
    """``tests/unit`` must not claim ``tests/unit_extra/…``."""
    changes = [Change("tests/unit_extra/test_x.py", "A"), Change("src/otto/test_y.py", "A")]
    assert changed_test_files(changes, ["tests/unit"]) == []


def test_a_file_under_two_roots_is_listed_once():
    changes = [Change("tests/e2e/monitor/dashboard/test_undo.py", "M")]
    roots = ["tests/e2e", "tests/e2e/monitor/dashboard"]
    assert changed_test_files(changes, roots) == ["tests/e2e/monitor/dashboard/test_undo.py"]


def test_no_changes_is_no_files():
    assert changed_test_files([], ["tests/unit"]) == []


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=git_env(repo),
    )


def test_the_cli_lists_what_a_commit_added_modified_and_renamed_to(tmp_path: Path):
    """A rename repeats under its new path; a deletion is not listed."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("tests/unit/test_modified.py", "def test_a(): pass\n")
    repo.write("tests/unit/test_old_name.py", "def test_b(): pass\n# a line to match on\n")
    repo.write("tests/unit/test_deleted.py", "def test_c(): pass\n")
    repo.write("tests/e2e/test_other_lane.py", "def test_d(): pass\n")
    repo.commit("base")

    repo.write("tests/unit/test_modified.py", "def test_a(): assert True\n")
    repo.write("tests/unit/test_added.py", "def test_e(): pass\n")
    repo.git("mv", "tests/unit/test_old_name.py", "tests/unit/test_new_name.py")
    repo.git("rm", "-q", "tests/unit/test_deleted.py")
    repo.write("tests/e2e/test_other_lane.py", "def test_d(): assert True\n")
    repo.commit("change")

    result = _run(repo.root, "--range", "HEAD^1..HEAD", "tests/unit")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "tests/unit/test_added.py",
        "tests/unit/test_modified.py",
        "tests/unit/test_new_name.py",
    ]


def test_the_cli_prints_nothing_when_the_diff_holds_no_test_file(tmp_path: Path):
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/a.py", "x = 1\n")
    repo.commit("base")
    repo.write("src/a.py", "x = 2\n")
    repo.commit("change")

    result = _run(repo.root, "--range", "HEAD^1..HEAD", "tests/unit", "tests/e2e")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_the_cli_refuses_a_range_git_cannot_read(tmp_path: Path):
    """A shallow checkout without the parent must fail loudly, not repeat nothing."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/a.py", "x = 1\n")
    repo.commit("only")

    result = _run(repo.root, "--range", "HEAD^1..HEAD", "tests/unit")

    assert result.returncode != 0
    assert "HEAD^1" in result.stderr
