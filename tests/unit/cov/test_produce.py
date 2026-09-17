"""produce_captures orchestration (merger stubbed, no lcov binary)."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import produce as produce_mod
from otto.coverage.capture.model import Capture
from otto.coverage.capture.produce import produce_captures
from tests._fixtures.gitrepo import git_env


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
            env=git_env(tmp_path),
        )

    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


def _write_meta(cov_dir: Path, repo: Path) -> None:
    (cov_dir / ".otto_cov_meta.json").write_text(
        f'{{"repo_name": "r", "sut_dir": "{repo}", "toolchains": {{}}, "source_roots": {{}}}}'
    )


def _stub_merger(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)


@pytest.mark.asyncio
async def test_produce_writes_per_product_captures(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for host in ("host1", "host2"):
        (cov_dir / host / "app").mkdir(parents=True)
        (cov_dir / host / "app" / "x.gcda").write_bytes(b"")
    _write_meta(cov_dir, repo)
    _stub_merger(monkeypatch, repo)

    written = await produce_captures(cov_dir, tier="system", repo_root=repo, labs=["lab1"])

    assert written == [
        cov_dir / "host1" / "app" / "capture.json",
        cov_dir / "host2" / "app" / "capture.json",
    ]
    cap = Capture.load(written[0])
    assert cap.tier == "system"
    assert cap.board == "host1"
    assert cap.product == "app"
    assert cap.files["f.c"].lines == {1: 3}

    # Debug artifacts stay on disk (spec decision 18).
    product_dir = cov_dir / "host1" / "app"
    assert (product_dir / "board.info").is_file()
    assert (product_dir / "board.resolved.info").is_file()


@pytest.mark.asyncio
async def test_produce_writes_one_capture_per_product_under_one_host(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for product in ("agent", "app"):
        (cov_dir / "host1" / product).mkdir(parents=True)
        (cov_dir / "host1" / product / "x.gcda").write_bytes(b"")
    _write_meta(cov_dir, repo)
    _stub_merger(monkeypatch, repo)

    written = await produce_captures(cov_dir, tier="system", repo_root=repo, labs=["lab1"])

    assert written == [
        cov_dir / "host1" / "agent" / "capture.json",
        cov_dir / "host1" / "app" / "capture.json",
    ]
    assert [Capture.load(p).product for p in written] == ["agent", "app"]
    assert {Capture.load(p).board for p in written} == {"host1"}


@pytest.mark.asyncio
async def test_produce_skips_product_dirs_without_counters(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    (cov_dir / "host1" / "app").mkdir(parents=True)
    (cov_dir / "host1" / "app" / "x.gcda").write_bytes(b"")
    (cov_dir / "host1" / "empty_dir").mkdir(parents=True)
    _write_meta(cov_dir, repo)
    _stub_merger(monkeypatch, repo)

    with caplog.at_level("WARNING"):
        written = await produce_captures(cov_dir, tier="system", repo_root=repo, labs=["lab1"])

    assert written == [cov_dir / "host1" / "app" / "capture.json"]
    assert any("empty_dir" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_produce_annotates_display_names_by_host(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for host in ("host1", "host2"):
        (cov_dir / host / "app").mkdir(parents=True)
        (cov_dir / host / "app" / "x.gcda").write_bytes(b"")
    _write_meta(cov_dir, repo)
    _stub_merger(monkeypatch, repo)

    written = await produce_captures(
        cov_dir,
        tier="system",
        repo_root=repo,
        labs=["lab1"],
        display_names={"host1": "Rack 2 Slot 4"},
    )

    by_host = {Capture.load(p).board: Capture.load(p) for p in written}
    assert by_host["host1"].display_name == "Rack 2 Slot 4"
    assert by_host["host2"].display_name is None  # no entry -> not annotated


def test_a_gcda_directly_under_the_host_dir_is_refused(tmp_path):
    from otto.coverage.capture.produce import _product_dirs
    from otto.coverage.errors import CoverageConfigError

    (tmp_path / "h1").mkdir()
    (tmp_path / "h1" / "x.gcda").write_bytes(b"")
    with pytest.raises(CoverageConfigError, match=r"cov/<host>/<product>/"):
        _product_dirs(tmp_path)
