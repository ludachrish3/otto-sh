"""diff_tree_u0: one rename-detecting, whitespace-immune diff for the whole tree."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import gitio
from otto.coverage.capture.treediff import parse_multifile_u0
from tests._fixtures.gitrepo import git_env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=git_env(cwd),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("int a(void)\n{\n    return 1;\n}\n")
    (root / "b.c").write_text("int b(void)\n{\n    return 2;\n}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


class TestDiffTreeU0:
    def test_clean_tree_diffs_empty(self, repo):
        root, base = repo
        assert parse_multifile_u0(gitio.diff_tree_u0(root, base)) == {}

    def test_rename_is_detected(self, repo):
        root, base = repo
        _git(root, "mv", "a.c", "renamed.c")
        _git(root, "commit", "-qm", "mv")
        fd = parse_multifile_u0(gitio.diff_tree_u0(root, base))["a.c"]
        assert fd.new_path == "renamed.c"
        assert fd.hunks == []

    def test_whitespace_only_change_is_invisible(self, repo):
        root, base = repo
        (root / "a.c").write_text("int a(void)\n{\n        return 1;\n}\n")  # re-indent
        assert "a.c" not in parse_multifile_u0(gitio.diff_tree_u0(root, base))

    def test_worktree_edit_visible_without_commit(self, repo):
        root, base = repo
        (root / "b.c").write_text("int b(void)\n{\n    return 3;\n}\n")
        assert "b.c" in parse_multifile_u0(gitio.diff_tree_u0(root, base))

    def test_subdir_of_larger_repo_paths_are_relative_and_scoped(self, tmp_path):
        # sut_dir is a plain subdirectory of ONE larger repo (shared .git):
        # --relative must scope the diff to the subdir AND re-root its paths.
        outer = tmp_path / "outer"
        (outer / "sub").mkdir(parents=True)
        _git(outer, "init", "-q", "-b", "main")
        (outer / "y.c").write_text("int y;\n")
        (outer / "sub" / "x.c").write_text("int x;\n")
        _git(outer, "add", "-A")
        _git(outer, "commit", "-qm", "base")
        base = _git(outer, "rev-parse", "HEAD").strip()
        (outer / "y.c").write_text("int y = 1;\n")  # outside sub/: must NOT appear
        (outer / "sub" / "x.c").write_text("int x = 2;\n")  # inside sub/: must appear re-rooted
        out = parse_multifile_u0(gitio.diff_tree_u0(outer / "sub", base))
        assert set(out) == {"x.c"}

    def test_unknown_base_raises(self, repo):
        root, _ = repo
        with pytest.raises(gitio.GitUnavailableError):
            gitio.diff_tree_u0(root, "0" * 40)


class TestIsShallow:
    def test_full_repo_is_not_shallow(self, repo):
        root, _ = repo
        assert gitio.is_shallow(root) is False

    def test_depth1_clone_is_shallow(self, repo, tmp_path):
        root, _ = repo
        _git(root, "commit", "-qm", "second", "--allow-empty")
        clone = tmp_path / "shallow"
        _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{root}", str(clone))
        assert gitio.is_shallow(clone) is True
