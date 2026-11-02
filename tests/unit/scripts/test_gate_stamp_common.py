"""Pins for scripts/gate_stamp_common.py — what the coverage stamp and the lane stamps share."""

from pathlib import Path

import pytest

from scripts import coverage_stamp
from scripts import gate_stamp_common as common
from scripts import gate_tip as gt
from tests.unit.scripts._gate_repos import _git

pytestmark = pytest.mark.interpreter_agnostic


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tracked.txt").write_text("v1\n")
    (repo / ".gitignore").write_text("ignored/\n")
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
    assert common.dirt_refusals(common.TreeState([], [], []), ["tests"]) == []


def test_a_missing_git_is_named_as_unavailable(tmp_path: Path, monkeypatch):
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(common.subprocess, "run", no_git)
    with pytest.raises(common.GateError, match="git is not available"):
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
    assert gt.tree_state is common.tree_state
    assert gt.dirt_refusals is common.dirt_refusals
    assert gt.GateError is common.GateError
    assert coverage_stamp.tree_state is common.tree_state
    assert coverage_stamp.tree_hash is common.tree_hash
    assert coverage_stamp.dirt_refusals is common.dirt_refusals
    assert coverage_stamp.test_roots is common.test_roots


def test_write_atomically_sets_the_mode_it_is_given(tmp_path: Path):
    """mkstemp creates 0o600 whatever the umask; the mode is set before the rename."""
    target = tmp_path / "shared.json"
    common.write_atomically(target, "{}\n", mode=0o666)
    assert target.stat().st_mode & 0o777 == 0o666
    common.write_atomically(target, "{}\n")
    assert target.stat().st_mode & 0o777 == 0o600, "no mode: mkstemp's, the rename replaces"


def test_store_lock_sets_modes_only_on_what_it_created(tmp_path: Path):
    root = tmp_path / "store"
    with common.store_lock(root, dir_mode=0o777, file_mode=0o666):
        pass
    assert root.stat().st_mode & 0o777 == 0o777
    assert (root / common.LOCK_FILE).stat().st_mode & 0o777 == 0o666
    # An existing root and lock are left alone: another user's objects.
    root.chmod(0o755)
    (root / common.LOCK_FILE).chmod(0o644)
    with common.store_lock(root, dir_mode=0o777, file_mode=0o666):
        pass
    assert root.stat().st_mode & 0o777 == 0o755
    assert (root / common.LOCK_FILE).stat().st_mode & 0o777 == 0o644


def test_store_lock_is_the_one_lock_both_scripts_use():
    from scripts import gate_tip

    assert gate_tip.store_lock is common.store_lock


def test_utc_now_renders_through_utc_iso():
    """One rendering of a UTC time: the stamps' and the bed lease's."""
    from datetime import datetime, timezone

    when = datetime(2026, 11, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert common.utc_iso(when) == "2026-11-01T12:00:00+00:00"
    assert len(common.utc_now()) == len(common.utc_iso(when))


def test_the_error_class_is_gate_error_and_nothing_else():
    assert issubclass(common.GateError, RuntimeError)
    assert not hasattr(common, "GateFreshError")


def test_an_ignored_python_file_under_a_read_root_is_dirt(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("*.local.*\nbuild/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore")
    (repo / "src/otto").mkdir(parents=True)
    (repo / "src/otto/x.local.py").write_text("x = 1\n")
    (repo / "tests/build").mkdir(parents=True)
    (repo / "tests/build/y.py").write_text("y = 1\n")
    # ignored too: an ignored non-Python file is not a source
    (repo / "src/otto/asset.local.js").write_text("")
    state = common.tree_state(repo)
    assert state.untracked == []
    assert state.ignored_sources == ["src/otto/x.local.py", "tests/build/y.py"]
    assert common.blocking_untracked(state, common.test_roots(repo)) == [
        "src/otto/x.local.py",
        "tests/build/y.py",
    ]
    lines = common.dirt_refusals(state, common.test_roots(repo))
    assert any("tests/build/y.py" in line and "ignored source" in line for line in lines)


def test_fetch_main_reads_fetch_head_from_a_path_remote(tmp_path):
    repo = _repo(tmp_path)
    bare = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "push", "-q", str(bare), "main")
    assert common.fetch_main(repo, str(bare)) == _git(repo, "rev-parse", "main").strip()
    empty = tmp_path / "empty.git"
    _git(repo, "init", "--bare", str(empty))
    with pytest.raises(common.GateError, match="has no main"):
        common.fetch_main(repo, str(empty))


def test_at_main_tip_is_whether_head_is_the_fetched_main(tmp_path):
    repo = _repo(tmp_path)
    bare = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "push", "-q", str(bare), "main")
    assert common.at_main_tip(repo, str(bare)) is True
    (repo / "tracked.txt").write_text("v2\n")
    _git(repo, "commit", "-qam", "ahead")
    assert common.at_main_tip(repo, str(bare)) is False
    with pytest.raises(common.GateError, match="has no main"):
        common.at_main_tip(repo, str(tmp_path / "absent.git"))


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git",
    ],
)
def test_remote_slug_reads_owner_repo_from_every_github_url_form(tmp_path, url):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", url)
    assert common.remote_slug(repo, "origin") == "o/r"


def test_a_path_remote_has_no_slug(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(tmp_path / "bare.git"))
    with pytest.raises(common.GateError, match="not a GitHub remote"):
        common.remote_slug(repo, "origin")


@pytest.mark.parametrize("pyproject", [None, "[tool.other]\nx = 1\n", "not = [toml"])
def test_a_checkout_without_pytest_testpaths_is_a_gate_error(tmp_path, pyproject):
    repo = _repo(tmp_path)
    if pyproject is None:
        _git(repo, "rm", "-q", "pyproject.toml")
    else:
        (repo / "pyproject.toml").write_text(pyproject)
        _git(repo, "commit", "-qam", "no testpaths")
    with pytest.raises(common.GateError, match="no pytest testpaths"):
        common.tree_state(repo)


class TestTreeState:
    def test_clean_repo_reports_nothing(self, tmp_path: Path):
        state = common.tree_state(_repo(tmp_path))
        assert state.tracked_dirty == []
        assert state.untracked == []

    def test_modified_tracked_file_is_reported(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        assert common.tree_state(repo).tracked_dirty == ["tracked.txt"]

    def test_staged_tracked_file_is_reported(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        _git(repo, "add", "tracked.txt")
        assert common.tree_state(repo).tracked_dirty == ["tracked.txt"]

    def test_renamed_file_reports_destination_path(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _git(repo, "mv", "tracked.txt", "renamed.txt")
        # git mv stages the change, so the rename is tracked_dirty.
        # tree_state should extract the destination path (renamed.txt), not the
        # double-path string (tracked.txt -> renamed.txt).
        assert common.tree_state(repo).tracked_dirty == ["renamed.txt"]

    def test_renamed_and_modified_file_reports_destination_path(self, tmp_path: Path):
        # A rename staged with `git mv`, then edited again, reports status code
        # "RM" (rename in the index column, modified in the worktree column).
        # A bare `code[0] in ("R", "C")` check still catches this one, but a
        # bare `code in ("R ", "C ")` equality check (the first fix attempt)
        # does not — pinning it here regardless of parsing strategy.
        repo = _repo(tmp_path)
        _git(repo, "mv", "tracked.txt", "renamed.txt")
        _git(repo, "add", "renamed.txt")
        (repo / "renamed.txt").write_text("v2\n")
        assert common.tree_state(repo).tracked_dirty == ["renamed.txt"]

    def test_rename_detected_only_in_worktree_column_reports_destination(self, tmp_path: Path):
        # The "R" can land in the *second* status column instead of the
        # first: delete a tracked file from the worktree only (a plain `rm`,
        # leaving the index entry alone — an unstaged deletion), then stage a
        # similarly-named new path with `git add -N` (intent-to-add, so its
        # content stays on disk rather than in the index). git's unstaged
        # rename detection then pairs the two, emitting " R" (space, R) for
        # the surviving path. A `code[0] in ("R", "C")` check (the second fix
        # attempt) misses this because the R is in code[1], not code[0].
        repo = _repo(tmp_path)
        (repo / "tracked.txt").unlink()
        (repo / "renamed.txt").write_text("v1\n")
        _git(repo, "add", "-N", "renamed.txt")
        state = common.tree_state(repo)
        assert state.tracked_dirty == ["renamed.txt"]
        assert state.untracked == []

    def test_modified_file_with_space_in_name_is_reported(self, tmp_path: Path):
        # The newline-delimited porcelain form has no quoting for spaces, so a
        # naive line-based parser can't tell a path's internal space from the
        # "code SP path" separator. `-z` sidesteps this: the path is a single
        # NUL-terminated field regardless of its contents.
        repo = _repo(tmp_path)
        (repo / "has space.txt").write_text("v1\n")
        _git(repo, "add", "has space.txt")
        _git(repo, "commit", "-qm", "add spaced file")
        (repo / "has space.txt").write_text("v2\n")
        assert common.tree_state(repo).tracked_dirty == ["has space.txt"]

    def test_untracked_file_is_separated_from_tracked_dirt(self, tmp_path: Path):
        # The whole point of the split: untracked files must NOT block, because
        # the gate going red on a forgotten `git add` is the feature.
        repo = _repo(tmp_path)
        (repo / "new.py").write_text("x = 1\n")
        state = common.tree_state(repo)
        assert state.tracked_dirty == []
        assert state.untracked == ["new.py"]

    def test_gitignored_file_is_neither(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "ignored").mkdir()
        (repo / "ignored" / "artifact.js").write_text("//\n")
        state = common.tree_state(repo)
        assert state.tracked_dirty == []
        assert state.untracked == []
        assert state.ignored_sources == [], "not a .py under src/ or a test root"

    def test_subdirectory_of_repo_works(self, tmp_path: Path):
        repo = _repo(tmp_path)
        subdir = repo / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content\n")
        # git finds the repository from a subdirectory, and porcelain paths are
        # relative to the repository root wherever git runs.
        state = common.tree_state(subdir)
        assert state.untracked == ["subdir/"]

    def test_non_repo_raises(self, tmp_path: Path):
        with pytest.raises(common.GateError, match="not a git repository"):
            common.tree_state(tmp_path / "nope")
