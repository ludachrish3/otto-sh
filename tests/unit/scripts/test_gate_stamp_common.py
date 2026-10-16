"""Pins for scripts/gate_stamp_common.py — what the coverage stamp and the lane stamps share."""

import subprocess
from pathlib import Path

import pytest

from scripts import coverage_stamp, gate_fresh
from scripts import gate_stamp_common as common
from tests._fixtures.gitrepo import git_env

pytestmark = pytest.mark.interpreter_agnostic


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=git_env(repo)
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tracked.txt").write_text("v1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_tree_hash_takes_a_revision(tmp_path: Path):
    repo = _repo(tmp_path)
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.txt").write_text("v2\n")
    _git(repo, "commit", "-qam", "second")
    assert common.tree_hash(repo) == _git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert common.tree_hash(repo, first) == _git(repo, "rev-parse", f"{first}^{{tree}}").strip()
    assert common.tree_hash(repo, first) != common.tree_hash(repo)


def test_blob_sha_is_git_hash_object_of_the_working_file(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("v2\n")
    expected = _git(repo, "hash-object", "tracked.txt").strip()
    assert common.blob_sha(repo, "tracked.txt") == expected


def test_dirt_refusals_name_tracked_dirt_and_blocking_untracked(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("v2\n")
    (repo / "tests" / "test_new.py").write_text("")
    (repo / "note.md").write_text("")
    state = common.tree_state(repo)
    why = common.dirt_refusals(state, common.test_roots(repo))
    assert len(why) == 2
    assert "tracked.txt" in why[0]
    assert "modified or staged" in why[0]
    assert "tests/test_new.py" in why[1]
    assert "note.md" not in why[1]
    assert common.dirt_refusals(common.TreeState([], []), ["tests"]) == []


def test_a_missing_git_is_named_as_unavailable(tmp_path: Path, monkeypatch):
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(common.subprocess, "run", no_git)
    with pytest.raises(common.GateFreshError, match="git is not available"):
        common.tree_hash(tmp_path)


def test_stamps_root_is_under_the_git_common_dir_shared_by_worktrees(tmp_path: Path):
    repo = _repo(tmp_path)
    other = tmp_path / "other"
    _git(repo, "worktree", "add", "--detach", "--quiet", str(other), "HEAD")
    assert common.stamps_root(repo, "x") == common.stamps_root(other, "x")
    assert common.stamps_root(repo, "x").name == "x"


def test_write_atomically_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "dir" / "stamp.json"
    common.write_atomically(target, "{}\n")
    assert target.read_text() == "{}\n"
    assert [p.name for p in target.parent.iterdir()] == ["stamp.json"]


def test_both_stamps_share_the_leaf():
    assert gate_fresh.tree_state is common.tree_state
    assert gate_fresh.GateFreshError is common.GateFreshError
    assert coverage_stamp.tree_state is common.tree_state
    assert coverage_stamp.tree_hash is common.tree_hash
    assert coverage_stamp.dirt_refusals is common.dirt_refusals
    assert coverage_stamp.test_roots is common.test_roots
