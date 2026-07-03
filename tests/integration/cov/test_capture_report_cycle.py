"""get → modify → report: valid/stale split over real git history."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import write_manual_capture
from otto.coverage.reporter import run_coverage_report
from otto.coverage.tiers import load_tiers

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin",
}

COV = {
    "tiers": {
        "system": {"kind": "e2e", "precedence": 1},
        "manual": {"kind": "manual", "precedence": 2, "max_age": "180d"},
    }
}


@pytest.mark.asyncio
async def test_manual_survives_unrelated_commit_and_stales_on_edit(tmp_path: Path) -> None:
    repo = tmp_path / "sut"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={**ENV, "HOME": str(tmp_path)},
        )

    git("init", "-q")
    (repo / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")

    cap = Capture(
        tier="manual",
        pin=head_commit(repo),
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-9",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines={2: 4})},
    )
    write_manual_capture(cap, repo)

    # Unrelated commit: line 2 untouched → still covered.
    (repo / "g.c").write_text("int z;\n")
    git("add", "g.c")
    git("commit", "-qm", "unrelated")

    report1 = tmp_path / "r1"
    store = await run_coverage_report([], report1, repo_root=repo, tier_configs=load_tiers(COV))
    (frec,) = [f for f in store.files() if f.path.name == "f.c"]
    assert frec.lines[2].hits.for_tier("manual") == 4
    assert frec.lines[2].state is None

    # Edit line 2 → stale.
    (repo / "f.c").write_text("int a;\nint EDITED;\nint c;\n")
    git("commit", "-aqm", "edit line 2")

    report2 = tmp_path / "r2"
    store2 = await run_coverage_report([], report2, repo_root=repo, tier_configs=load_tiers(COV))
    (frec2,) = [f for f in store2.files() if f.path.name == "f.c"]
    assert frec2.lines[2].hits.for_tier("manual") == 0
    assert frec2.lines[2].state == "stale"
    assert (report2 / "index.html").is_file()
    assert store2.provenance
    assert store2.provenance[0]["ticket"] == "T-9"
