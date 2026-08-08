"""get → modify → report: valid/stale split over real git history."""

import json
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import write_manual_capture
from otto.coverage.reporter import run_coverage_report
from otto.coverage.tiers import load_tiers
from tests._fixtures.gitrepo import TmpGitRepo


def _mangle_path(path: str) -> str:
    """Replicate ``spa_data.mangle_path`` — independent oracle, not
    imported, same stance ``tests/e2e/cov/test_coverage_e2e.py``'s local
    copy takes: a real mangling regression would still be caught here."""
    return path.replace("/", "_").replace("\\", "_").lstrip("_")


def _file_chunk(report_dir: Path, path: str) -> dict:
    """Parse a per-file chunk's ``window.__OTTO_COV_FILE__({...});`` body."""
    chunk_path = report_dir / "cov_data" / "files" / f"{_mangle_path(path)}.js"
    text = chunk_path.read_text()
    return json.loads(text[len("window.__OTTO_COV_FILE__(") : -3])


def _index_payload(report_dir: Path) -> dict:
    """Parse ``cov_data/index.js``'s ``window.__OTTO_COV__ = {...};`` body."""
    text = (report_dir / "cov_data" / "index.js").read_text()
    return json.loads(text[len("window.__OTTO_COV__ = ") : -2])


COV = {
    "tiers": {
        "system": {"kind": "e2e", "precedence": 1},
        "manual": {"kind": "manual", "precedence": 2, "max_age": "180d"},
    }
}


@pytest.mark.asyncio
async def test_manual_survives_unrelated_commit_and_stales_on_edit(tmp_path: Path) -> None:
    sut = TmpGitRepo(tmp_path / "sut")
    repo, git = sut.root, sut.git

    sut.write("f.c", "int a;\nint b;\nint c;\n")
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

    sut = TmpGitRepo(tmp_path / "sut")
    repo, git = sut.root, sut.git

    sut.write("f.c", "int a;\nint b;\nint c;\n")
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

    # The rendered SPA report carries the drilldown too (spec §9/§10): the
    # per-file chunk's line JSON names each valid run's credit and the
    # revoked run on the staled line — the JSON analog of the retired
    # Jinja template's per-line run chips (a live chip per `run_hits`
    # entry, a struck "run-stale" chip per `stale_runs` entry) — and the
    # index payload's run table carries the label the old HTML assertion
    # looked for.
    chunk = _file_chunk(report, str(fr.path))
    assert chunk["lines"]["2"]["run"] == {str(t1): 1, str(t22): 3}  # live chips
    assert chunk["lines"]["1"]["stale_run"] == [t1]  # struck "run-stale" chip
    assert "run" not in chunk["lines"]["1"]  # revoked run carries no live hit

    payload = _index_payload(report)
    labels_by_id = {r["id"]: r["label"] for r in payload["runs"]}
    assert labels_by_id[t1] == "Rack 2 Slot 4"
