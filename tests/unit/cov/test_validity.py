"""Manual-capture validity: anchor chain, stale/aging states."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture


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


def _capture(repo: Path, captured_at: str = "2026-07-01T00:00:00Z") -> Capture:
    return Capture(
        tier="manual",
        pin=head_commit(repo),
        captured_at=captured_at,
        ticket="T-1",
        labs=["lab1"],
        board="b",
        files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines={1: 2, 3: 1})},
    )


def _find(store: CoverageStore, repo: Path, lineno: int):
    (rec,) = [f for f in store.files() if f.path == (repo / "f.c").resolve()]
    return rec.lines.get(lineno)


def test_unchanged_file_all_valid(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, _capture(repo), repo, max_age_days=None)
    assert _find(store, repo, 1).hits.for_tier("manual") == 2
    assert _find(store, repo, 1).state is None
    assert store.provenance[0]["ticket"] == "T-1"


def test_edited_line_goes_stale(repo: Path) -> None:
    cap = _capture(repo)
    (repo / "f.c").write_text("int a;\nint b;\nint CHANGED;\n")
    subprocess.run(
        ["git", "commit", "-aqm", "edit"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "PATH": "/usr/bin:/bin",
        },
    )
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap, repo, max_age_days=None)
    assert _find(store, repo, 1).hits.for_tier("manual") == 2  # unchanged line: valid
    line3 = _find(store, repo, 3)
    assert line3.state == "stale"
    assert line3.hits.for_tier("manual") == 0  # no credit


def test_aging_flag(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(
        store,
        _capture(repo, "2025-01-01T00:00:00Z"),
        repo,
        max_age_days=180,
        today=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    line1 = _find(store, repo, 1)
    assert line1.hits.for_tier("manual") == 2  # still counts
    assert line1.state == "aging"


def test_unverifiable_all_stale(repo: Path) -> None:
    cap = _capture(repo)
    bogus = cap.model_copy(update={"pin": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, bogus, repo, max_age_days=None)
    assert _find(store, repo, 1).state == "stale"
