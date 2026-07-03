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
    assert branches[3] == [(0, 0, 4), (0, 1, 0)]


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


def test_roundtrip_and_strictness(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(info_path=info, tier="system", repo_root=repo, board="b", labs=[])
    out = tmp_path / "capture.json"
    cap.save(out)
    loaded = Capture.load(out)
    assert loaded == cap
    raw = json.loads(out.read_text())
    assert raw["schema"] == 1
    raw["surprise"] = True
    out.write_text(json.dumps(raw))
    with pytest.raises(ValidationError):
        Capture.load(out)
