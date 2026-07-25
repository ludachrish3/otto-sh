"""Pin O(1) git subprocesses per capture — not O(files) (spec §9).

The counter wraps gitio._run_raw, the single chokepoint every helper
funnels through. Budget: resolver construction = 1 (tree diff) with +1
allowed for the shallow probe on the fallback path; per-file resolves
on the tree path spawn ZERO except the existence stat (not git).
"""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture

N_FILES = 50


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
        },
    ).stdout


@pytest.fixture
def counted_git(monkeypatch):
    calls: list[list[str]] = []
    real = gitio._run_raw

    def counting(args, cwd, ok_codes=(0,)):
        calls.append(list(args))
        return real(args, cwd, ok_codes)

    monkeypatch.setattr(gitio, "_run_raw", counting)
    return calls


@pytest.fixture
def big_repo(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for i in range(N_FILES):
        (root / f"f{i:03}.c").write_text(f"int f{i}(void)\n{{\n    return {i};\n}}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    files = {
        f"f{i:03}.c": CaptureFileCov(blob=gitio.blob_sha(root, Path(f"f{i:03}.c")), lines={3: 1})
        for i in range(N_FILES)
    }
    cap = Capture(
        tier="manual",
        base_commit=base,
        captured_at="2026-07-01T00:00:00Z",
        board="bench-1",
        files=files,
    )
    return root, cap


def test_fold_spawns_constant_git_calls(counted_git, big_repo):
    root, cap = big_repo
    # Touch a handful of files so the tree diff is non-trivial.
    for i in (3, 7):
        (root / f"f{i:03}.c").write_text(f"int f{i}(void)\n{{\n    return -1;\n}}\n")
    counted_git.clear()
    store = CoverageStore()
    apply_manual_capture(store, cap, root, max_age_days=None)
    assert len(counted_git) <= 2, [" ".join(c) for c in counted_git]
