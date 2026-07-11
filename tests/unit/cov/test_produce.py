"""produce_captures orchestration (merger stubbed, no lcov binary)."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import produce as produce_mod
from otto.coverage.capture.model import Capture
from otto.coverage.capture.produce import produce_captures


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
    (root / "f.c").write_text("int a;\nint b;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


@pytest.mark.asyncio
async def test_produce_writes_per_board_captures(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for board in ("board1", "board2"):
        (cov_dir / board).mkdir(parents=True)
        (cov_dir / board / "x.gcda").write_bytes(b"")
    (cov_dir / ".otto_cov_meta.json").write_text(
        f'{{"repo_name": "r", "sut_dir": "{repo}", "toolchains": {{}}, "source_roots": {{}}}}'
    )

    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

    written = await produce_captures(cov_dir, tier="system", repo_root=repo, labs=["lab1"])

    assert sorted(p.parent.name for p in written) == ["board1", "board2"]
    cap = Capture.load(written[0])
    assert cap.tier == "system"
    assert cap.board == "board1"
    assert cap.files["f.c"].lines == {1: 3}

    # Debug artifacts stay on disk (spec decision 18).
    board1 = cov_dir / "board1"
    assert (board1 / "board.info").is_file()
    assert (board1 / "board.resolved.info").is_file()


@pytest.mark.asyncio
async def test_produce_skips_boardless_dirs(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    (cov_dir / "board1").mkdir(parents=True)
    (cov_dir / "board1" / "x.gcda").write_bytes(b"")
    (cov_dir / "empty_dir").mkdir(parents=True)
    (cov_dir / ".otto_cov_meta.json").write_text(
        f'{{"repo_name": "r", "sut_dir": "{repo}", "toolchains": {{}}, "source_roots": {{}}}}'
    )

    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

    with caplog.at_level("WARNING"):
        written = await produce_captures(cov_dir, tier="system", repo_root=repo, labs=["lab1"])

    assert [p.parent.name for p in written] == ["board1"]
    assert any("empty_dir" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_produce_annotates_display_names_by_board(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for board in ("board1", "board2"):
        (cov_dir / board).mkdir(parents=True)
        (cov_dir / board / "x.gcda").write_bytes(b"")
    (cov_dir / ".otto_cov_meta.json").write_text(
        f'{{"repo_name": "r", "sut_dir": "{repo}", "toolchains": {{}}, "source_roots": {{}}}}'
    )

    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

    written = await produce_captures(
        cov_dir,
        tier="system",
        repo_root=repo,
        labs=["lab1"],
        display_names={"board1": "Rack 2 Slot 4"},
    )

    by_board = {p.parent.name: Capture.load(p) for p in written}
    assert by_board["board1"].display_name == "Rack 2 Slot 4"
    assert by_board["board2"].display_name is None  # no entry -> not annotated
