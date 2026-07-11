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
        base_commit=head_commit(repo),
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
    assert store2.runs
    assert store2.runs[0].ticket == "T-9"


@pytest.mark.asyncio
async def test_runs_traceable_end_to_end(tmp_path: Path) -> None:
    """Two manual runs on one file: drilldown credits each valid run per line,
    a staled line names the revoked run, and store.json round-trips it all."""
    from otto.coverage.store.model import CoverageStore

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

    def cap(ticket: str, lines: dict[int, int], display_name: str | None) -> Capture:
        return Capture(
            tier="manual",
            base_commit=head_commit(repo),
            captured_at=f"2026-07-0{len(ticket)}T00:00:00Z",
            ticket=ticket,
            labs=["lab1"],
            board="b1",
            display_name=display_name,
            files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines=lines)},
        )

    write_manual_capture(cap("T-1", {1: 2, 2: 1}, "Rack 2 Slot 4"), repo)
    write_manual_capture(cap("T-22", {2: 3}, None), repo)

    # Edit line 1 → T-1's evidence for it is revoked.
    (repo / "f.c").write_text("int EDITED;\nint b;\nint c;\n")
    git("commit", "-aqm", "edit line 1")

    report = tmp_path / "r"
    store = await run_coverage_report([], report, repo_root=repo, tier_configs=load_tiers(COV))

    by_ticket = {c.ticket: c for c in store.runs}
    assert by_ticket["T-1"].label == "Rack 2 Slot 4"
    assert by_ticket["T-22"].label == "b1"

    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    t1, t22 = by_ticket["T-1"].id, by_ticket["T-22"].id
    assert fr.lines[2].run_hits == {t1: 1, t22: 3}  # both runs credited
    assert fr.lines[1].stale_runs == [t1]  # revoked run named
    assert fr.lines[1].run_hits == {}

    # store.json round-trip preserves the run table + per-line run data.
    reloaded = CoverageStore.load(report / "store.json")
    (fr2,) = [f for f in reloaded.files() if f.path.name == "f.c"]
    assert fr2.lines[2].run_hits == {t1: 1, t22: 3}
    assert reloaded.runs[t1].label == "Rack 2 Slot 4"

    # The rendered page carries the drilldown.
    page = next((report / "files").glob("*.html")).read_text()
    assert "Rack 2 Slot 4" in page
    assert "run-stale" in page
