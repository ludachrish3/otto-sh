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


def test_branch_reachability_applied(repo: Path) -> None:
    cap = Capture(
        tier="manual",
        pin=head_commit(repo),
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b",
        files={
            "f.c": CaptureFileCov(
                blob=blob_sha(repo, Path("f.c")),
                branches={3: [(0, 0, 2), (0, 1, 0), (1, 0, None)]},
            )
        },
    )
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap, repo, max_age_days=None)
    line3 = _find(store, repo, 3)
    branches = {b.branch_id: b for b in line3.branches}

    reached = branches[(0, 0)]
    assert reached.is_reachable("manual") is True
    assert reached.hits.for_tier("manual") == 2

    not_taken = branches[(0, 1)]
    assert not_taken.is_reachable("manual") is True
    assert not_taken.hits.for_tier("manual") == 0

    never_reached = branches[(1, 0)]
    assert never_reached.is_reachable("manual") is False
    assert never_reached.hits.for_tier("manual") == 0


def _commit_edit(repo: Path, text: str) -> None:
    (repo / "f.c").write_text(text)
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


def test_blank_captured_at_is_not_aging_and_warns(repo: Path, caplog) -> None:
    """A blank/damaged captured_at with a max_age configured must not crash
    ``_is_aging``'s strptime — the capture is treated as not-aging and a
    warning names it."""
    cap = _capture(repo, captured_at="")
    store = CoverageStore(tier_order=["manual"])
    with caplog.at_level("WARNING"):
        apply_manual_capture(store, cap, repo, max_age_days=180)
    line1 = _find(store, repo, 1)
    assert line1.hits.for_tier("manual") == 2  # loaded fine
    assert line1.state is None  # not aging
    assert any("captured_at" in rec.message for rec in caplog.records)


def test_later_capture_clears_stale_from_earlier(repo: Path) -> None:
    """Covered wins: when a later (post-edit-pin) capture validly credits a
    line an earlier capture left flagged stale, the stale state is cleared."""
    cap1 = _capture(repo)  # pinned at HEAD1, covers line 3

    # Edit line 3 and commit → HEAD2. cap1's line 3 now anchors to a changed
    # line → stale.
    _commit_edit(repo, "int a;\nint b;\nint CHANGED;\n")

    cap2 = Capture(
        tier="manual",
        pin=head_commit(repo),  # HEAD2, matching the edited source
        captured_at="2026-07-02T00:00:00Z",
        ticket="T-2",
        labs=["lab1"],
        board="b",
        files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines={3: 5})},
    )

    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap1, repo, max_age_days=None)
    assert _find(store, repo, 3).state == "stale"  # earlier capture went stale

    apply_manual_capture(store, cap2, repo, max_age_days=None)
    line3 = _find(store, repo, 3)
    assert line3.state is None  # covered wins over the earlier stale marker
    assert line3.hits.for_tier("manual") == 5


def test_unverifiable_all_stale(repo: Path) -> None:
    cap = _capture(repo)
    bogus = cap.model_copy(update={"pin": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, bogus, repo, max_age_days=None)
    assert _find(store, repo, 1).state == "stale"


def test_unverifiable_pin_warns_with_ticket_and_remedy(repo: Path, caplog) -> None:
    """The unverifiable-pin warning is not fatal — it names the ticket and the remedy."""
    cap = _capture(repo)
    bogus = cap.model_copy(update={"pin": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    with caplog.at_level("WARNING"):
        apply_manual_capture(store, bogus, repo, max_age_days=None)
    # Not fatal: apply_manual_capture raises nothing and still loads a (stale) line.
    assert _find(store, repo, 1).state == "stale"
    (rec,) = [r for r in caplog.records if "unverifiable" in r.message]
    assert cap.ticket in rec.message  # "T-1"
    assert "re-capture" in rec.message
