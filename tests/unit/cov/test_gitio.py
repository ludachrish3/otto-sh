"""Git plumbing used by coverage captures. All repos live in tmp_path."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import (
    GitUnavailableError,
    blob_exists,
    blob_sha,
    cat_blob,
    diff_no_index_u0,
    diff_worktree_file_u0,
    hash_object,
    head_commit,
    is_dirty,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@x",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@x",
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
            },
        )

    git("init", "-q")
    (root / "a.c").write_text("line1\nline2\nline3\n")
    git("add", "a.c")
    git("commit", "-qm", "init")
    return root


def test_head_and_dirty(repo: Path) -> None:
    sha = head_commit(repo)
    assert len(sha) == 40
    assert is_dirty(repo) is False
    (repo / "a.c").write_text("line1\nX\nline3\n")
    assert is_dirty(repo) is True


def test_blob_roundtrip(repo: Path) -> None:
    sha = blob_sha(repo, Path("a.c"))
    assert sha is not None
    assert blob_exists(repo, sha)
    assert cat_blob(repo, sha) == b"line1\nline2\nline3\n"
    assert hash_object(repo, repo / "a.c") == sha
    assert blob_sha(repo, Path("missing.c")) is None


def test_worktree_diff_u0(repo: Path) -> None:
    (repo / "a.c").write_text("line1\nADDED\nline2\nline3\n")
    out = diff_worktree_file_u0(repo, Path("a.c"))
    assert "@@" in out
    assert "+ADDED" in out


def test_no_index_diff_exit_1_ok(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("x\n")
    b = tmp_path / "b.txt"
    b.write_text("y\n")
    out = diff_no_index_u0(a, b)
    assert "@@" in out
    assert diff_no_index_u0(a, a) == ""


def test_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        head_commit(tmp_path)


def test_blob_sha_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        blob_sha(tmp_path, Path("a.c"))


def test_blob_exists_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        blob_exists(tmp_path, "0" * 40)
