"""Batched gitio helpers: same git primitives, O(1) spawns instead of O(files)."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import (
    blobs_exist,
    cat_blobs,
    diff_no_index_dir_u0,
    hash_object,
    hash_objects,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin",
}


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
            env={**_GIT_ENV, "HOME": str(tmp_path)},
        )

    git("init", "-q")
    (root / "a.c").write_text("line1\nline2\nline3\n")
    (root / "empty.c").write_text("")
    (root / "sub").mkdir()
    (root / "sub" / "c.c").write_text("int x;\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    return root


def test_hash_objects_matches_per_file_including_empty(repo: Path) -> None:
    paths = [repo / "a.c", repo / "empty.c", repo / "sub" / "c.c"]
    expected = [hash_object(repo, p) for p in paths]
    assert hash_objects(repo, paths) == expected


def test_blobs_exist_partitions_present_and_absent(repo: Path) -> None:
    present_sha = hash_object(repo, repo / "a.c")
    absent_sha = "1" * 40
    assert blobs_exist(repo, [present_sha, absent_sha]) == {present_sha}


def test_cat_blobs_roundtrips_embedded_newlines_and_non_utf8(repo: Path) -> None:
    (repo / "bin.dat").write_bytes(b"line1\nline2\x00\xffmore\n")
    subprocess.run(
        ["git", "add", "bin.dat"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**_GIT_ENV, "HOME": str(repo.parent)},
    )
    subprocess.run(
        ["git", "commit", "-qm", "bin"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**_GIT_ENV, "HOME": str(repo.parent)},
    )
    sha_a = hash_object(repo, repo / "a.c")
    sha_bin = hash_object(repo, repo / "bin.dat")

    result = cat_blobs(repo, [sha_a, sha_bin])

    assert result[sha_a] == b"line1\nline2\nline3\n"
    assert result[sha_bin] == b"line1\nline2\x00\xffmore\n"


def test_cat_blobs_duplicate_shas_last_write_wins(repo: Path) -> None:
    sha_a = hash_object(repo, repo / "a.c")
    result = cat_blobs(repo, [sha_a, sha_a])
    assert result[sha_a] == b"line1\nline2\nline3\n"


def test_diff_no_index_dir_u0_yields_hunks_with_dir_prefix(tmp_path: Path) -> None:
    base = tmp_path / "base"
    current = tmp_path / "current"
    base.mkdir()
    current.mkdir()
    (base / "f.c").write_text("l1\nl2\nl3\n")
    (current / "f.c").write_text("l1\nCHANGED\nl3\n")

    out = diff_no_index_dir_u0(base, current)

    assert "@@" in out
    assert "base/f.c" in out
    assert "current/f.c" in out


def test_diff_no_index_dir_u0_omits_whitespace_only_pair(tmp_path: Path) -> None:
    base = tmp_path / "base"
    current = tmp_path / "current"
    base.mkdir()
    current.mkdir()
    (base / "f.c").write_text("int a;\nint b;\n")
    (current / "f.c").write_text("int a;\n        int b;\n")

    assert diff_no_index_dir_u0(base, current) == ""


def test_diff_no_index_dir_u0_tolerates_exit_code_1(tmp_path: Path) -> None:
    base = tmp_path / "base"
    current = tmp_path / "current"
    base.mkdir()
    current.mkdir()
    (base / "f.c").write_text("x\n")
    (current / "f.c").write_text("y\n")

    # git exits 1 for "files differ" under --no-index; that must not raise.
    out = diff_no_index_dir_u0(base, current)
    assert "@@" in out
