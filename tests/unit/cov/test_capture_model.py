"""Capture JSON model, .info parsing, and dirty-tree remap on build."""

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, build_capture, parse_info

INFO = """TN:
SF:{src}
DA:1,5
DA:2,0
DA:3,7
BRDA:3,0,0,4
BRDA:3,0,1,-
end_of_record
"""


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
    (root / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


def _write_info(tmp_path: Path, src: Path) -> Path:
    p = tmp_path / "x.info"
    p.write_text(INFO.format(src=src))
    return p


def test_parse_info(tmp_path: Path) -> None:
    src = tmp_path / "f.c"
    files = parse_info(_write_info(tmp_path, src))
    lines, branches = files[str(src)]
    assert lines == {1: 5, 2: 0, 3: 7}
    assert branches[3] == [(0, 0, 4), (0, 1, None)]


def test_build_capture_clean(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(
        info_path=info, tier="system", repo_root=repo, board="board1", labs=["lab1"]
    )
    assert cap.pin == head_commit(repo)
    assert cap.dirty_remap is False
    fc = cap.files["f.c"]
    assert fc.lines == {1: 5, 2: 0, 3: 7}
    assert fc.blob == blob_sha(repo, Path("f.c"))


def test_build_capture_dirty_remaps(repo: Path, tmp_path: Path) -> None:
    # Insert a printf as new line 1; old line N is now N+1 in the working tree.
    (repo / "f.c").write_text("printf();\nint a;\nint b;\nint c;\n")
    info = _write_info(tmp_path, repo / "f.c")  # DA lines are worktree coords
    cap = build_capture(
        info_path=info, tier="manual", repo_root=repo, board="b", labs=["lab1"], ticket="T-1"
    )
    assert cap.dirty_remap is True
    # worktree line 1 (the printf) dropped; 2→1, 3→2
    assert cap.files["f.c"].lines == {1: 0, 2: 7}


def test_build_capture_dirty_whitespace_only_keeps_all_lines(repo: Path, tmp_path: Path) -> None:
    # A whitespace-only reindent of the working tree is dirty byte-wise but
    # carries no code change: -w hides it, so every DA line maps verbatim to
    # pin coordinates instead of being dropped as "changed".
    (repo / "f.c").write_text("    int a;\n\tint b;\n  int c;\n")
    info = _write_info(tmp_path, repo / "f.c")  # DA lines are worktree coords
    cap = build_capture(
        info_path=info, tier="manual", repo_root=repo, board="b", labs=["lab1"], ticket="T-1"
    )
    assert cap.dirty_remap is True  # tree is dirty (byte-wise)
    assert cap.files["f.c"].lines == {1: 5, 2: 0, 3: 7}  # nothing dropped
    assert cap.files["f.c"].blob == blob_sha(repo, Path("f.c"))


def test_build_capture_nested_sut_dir(repo: Path, tmp_path: Path) -> None:
    # The sut repo may be a subdirectory of a larger git repo (the e2e bed:
    # tests/repo1 inside otto-sh).  Blob anchoring must then still resolve —
    # a bare "HEAD:<rel>" lookup misses because git resolves it against the
    # repo toplevel, silently producing an empty (useless) capture.
    nested = repo / "nested" / "sut" / "product"
    nested.mkdir(parents=True)
    # No toplevel product/main.c exists, and the content differs from every
    # toplevel file — so a toplevel-relative lookup can neither find it nor
    # accidentally anchor a same-content blob.
    (nested / "main.c").write_text("int nested_only;\nint b;\nint c;\n")
    subprocess.run(["git", "add", "nested"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "nest"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    info = _write_info(tmp_path, nested / "main.c")
    sut_dir = repo / "nested" / "sut"
    cap = build_capture(
        info_path=info, tier="system", repo_root=sut_dir, board="board1", labs=["lab1"]
    )

    fc = cap.files["product/main.c"]
    assert fc.lines == {1: 5, 2: 0, 3: 7}
    assert fc.blob == blob_sha(repo, Path("nested/sut/product/main.c"))


def test_untracked_file_skipped(
    repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (repo / "untracked.c").write_text("int z;\n")
    info = tmp_path / "x.info"
    info.write_text(INFO.format(src=repo / "f.c") + INFO.format(src=repo / "untracked.c"))
    with caplog.at_level("WARNING"):
        cap = build_capture(info_path=info, tier="system", repo_root=repo, board="b", labs=["lab1"])
    assert "untracked.c" not in cap.files
    assert "f.c" in cap.files
    assert cap.dirty_remap is True
    assert any("no committed version" in r.message for r in caplog.records)


def test_gitignored_file_skipped(repo: Path, tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
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

    (repo / ".gitignore").write_text("gen.c\n")
    git("add", ".gitignore")
    git("commit", "-qm", "ignore gen.c")
    (repo / "gen.c").write_text("int g;\n")

    info = tmp_path / "x.info"
    info.write_text(INFO.format(src=repo / "f.c") + INFO.format(src=repo / "gen.c"))
    cap = build_capture(info_path=info, tier="system", repo_root=repo, board="b", labs=["lab1"])
    assert "gen.c" not in cap.files
    assert "f.c" in cap.files
    assert cap.dirty_remap is False


def test_roundtrip_and_strictness(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(info_path=info, tier="system", repo_root=repo, board="b", labs=[])
    assert cap.files["f.c"].branches[3] == [(0, 0, 4), (0, 1, None)]
    out = tmp_path / "capture.json"
    cap.save(out)
    loaded = Capture.load(out)
    assert loaded == cap
    raw = json.loads(out.read_text())
    assert raw["schema"] == 1
    # never-reached branch ("-") must round-trip as JSON null, not 0.
    assert raw["files"]["f.c"]["branches"]["3"][1] == [0, 1, None]
    raw["surprise"] = True
    out.write_text(json.dumps(raw))
    with pytest.raises(ValidationError):
        Capture.load(out)
