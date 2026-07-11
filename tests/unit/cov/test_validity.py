"""Manual-capture validity: anchor chain, stale/aging states."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import (
    apply_manual_capture,
    load_capture_into_store,
    register_capture_run,
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
    (root / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


def _capture(repo: Path, captured_at: str = "2026-07-01T00:00:00Z") -> Capture:
    return Capture(
        tier="manual",
        base_commit=head_commit(repo),
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


def test_whitespace_only_reindent_stays_valid(repo: Path) -> None:
    """A pure reformat (reindent) of the covered lines must NOT stale them:
    ``-w`` in the anchor-chain diff hides whitespace-only modifications, so
    the manual hits carry through at their (unshifted) line numbers."""
    cap = _capture(repo)  # covers lines 1 and 3
    # Reindent every line — no code change — and commit past base_commit.
    _commit_edit(repo, "    int a;\n\tint b;\n        int c;\n")
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap, repo, max_age_days=None)
    line1 = _find(store, repo, 1)
    line3 = _find(store, repo, 3)
    assert line1.hits.for_tier("manual") == 2
    assert line1.state is None  # not stale
    assert line3.hits.for_tier("manual") == 1
    assert line3.state is None


def test_whitespace_reindent_plus_insertion_remaps_and_stays_valid(repo: Path) -> None:
    """Reindent the covered lines AND insert a blank-ish code line above line 3:
    the whitespace-only reindents are ignored, while the real insertion shifts
    line 3 down. The manual hit follows to its new line number, still valid."""
    cap = _capture(repo)  # covers lines 1 and 3
    _commit_edit(repo, "  int a;\n  int b;\n  int inserted;\n  int c;\n")
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap, repo, max_age_days=None)
    # line 1 unchanged position, still valid
    assert _find(store, repo, 1).hits.for_tier("manual") == 2
    assert _find(store, repo, 1).state is None
    # original line 3 (int c;) moved to line 4; the hit remaps there, no stale
    line4 = _find(store, repo, 4)
    assert line4.hits.for_tier("manual") == 1
    assert line4.state is None
    assert _find(store, repo, 3) is None or _find(store, repo, 3).state is None


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
        base_commit=head_commit(repo),
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
    """Covered wins: when a later (post-edit base_commit) capture validly credits a
    line an earlier capture left flagged stale, the stale state is cleared."""
    cap1 = _capture(repo)  # anchored at HEAD1, covers line 3

    # Edit line 3 and commit → HEAD2. cap1's line 3 now anchors to a changed
    # line → stale.
    _commit_edit(repo, "int a;\nint b;\nint CHANGED;\n")

    cap2 = Capture(
        tier="manual",
        base_commit=head_commit(repo),  # HEAD2, matching the edited source
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
    bogus = cap.model_copy(update={"base_commit": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, bogus, repo, max_age_days=None)
    assert _find(store, repo, 1).state == "stale"


def test_unverifiable_zero_count_line_not_staled(repo: Path) -> None:
    """A never-executed (count 0) line in an unverifiable capture must not be
    marked stale or recorded as a revoked run — mirrors the mapped path's
    ``stale_linenos`` count>0 gate, so a run is never listed STALE on a line
    it never actually executed."""
    cap = _capture(repo)
    cap.files["f.c"].lines[2] = 0  # instrumented but never executed
    bogus = cap.model_copy(update={"base_commit": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    rid = register_capture_run(store, bogus)
    apply_manual_capture(store, bogus, repo, max_age_days=None, run_id=rid)
    assert _find(store, repo, 1).state == "stale"
    assert rid in _find(store, repo, 1).stale_runs
    line2 = _find(store, repo, 2)
    assert line2 is None or rid not in line2.stale_runs


def test_unverifiable_base_commit_warns_with_ticket_and_remedy(repo: Path, caplog) -> None:
    """The unverifiable-base_commit warning is not fatal — it names the ticket and the remedy."""
    cap = _capture(repo)
    bogus = cap.model_copy(update={"base_commit": "f" * 40})
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


def test_register_capture_run_prefers_display_name(repo: Path) -> None:
    cap = _capture(repo)
    cap.display_name = "Rack 2 Slot 4"
    store = CoverageStore(tier_order=["manual"])
    rid = register_capture_run(store, cap)
    rec = store.runs[rid]
    assert rec.label == "Rack 2 Slot 4"
    assert rec.board == "b"
    assert rec.ticket == "T-1"
    assert rec.base_commit == cap.base_commit


def test_register_capture_run_falls_back_to_board(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    rid = register_capture_run(store, _capture(repo))
    assert store.runs[rid].label == "b"


def test_apply_manual_capture_credits_run_hits(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo)  # lines {1: 2, 3: 1}
    rid = register_capture_run(store, cap)
    apply_manual_capture(store, cap, repo, max_age_days=None, run_id=rid)
    assert _find(store, repo, 1).run_hits == {rid: 2}
    assert _find(store, repo, 3).run_hits == {rid: 1}


def test_stale_line_records_revoked_run(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo)
    rid = register_capture_run(store, cap)
    _commit_edit(repo, "int a;\nint b;\nint CHANGED;\n")  # stales line 3
    apply_manual_capture(store, cap, repo, max_age_days=None, run_id=rid)
    line3 = _find(store, repo, 3)
    assert line3.state == "stale"
    assert line3.stale_runs == [rid]
    assert line3.run_hits == {}  # no credit for the revoked run


def test_aging_capture_flags_its_run_record(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo, "2025-01-01T00:00:00Z")
    rid = register_capture_run(store, cap)
    apply_manual_capture(
        store,
        cap,
        repo,
        max_age_days=180,
        today=datetime(2026, 7, 2, tzinfo=timezone.utc),
        run_id=rid,
    )
    assert store.runs[rid].aging is True
    assert _find(store, repo, 1).run_hits == {rid: 2}


def test_e2e_capture_load_credits_run(repo: Path) -> None:
    store = CoverageStore(tier_order=["system"])
    cap = _capture(repo)
    rid = register_capture_run(store, cap)
    load_capture_into_store(store, cap, repo, run_id=rid)
    assert _find(store, repo, 1).run_hits == {rid: 2}
    # zero-count DA lines never credit a run (line 3 has count 1; craft a 0):
    cap0 = _capture(repo)
    cap0.files["f.c"].lines = {2: 0}
    rid0 = register_capture_run(store, cap0)
    load_capture_into_store(store, cap0, repo, run_id=rid0)
    assert _find(store, repo, 2).run_hits == {}
