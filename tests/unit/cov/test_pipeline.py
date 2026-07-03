"""Tests for the CoverageReporter and helper functions."""

import json
import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.errors import CoverageDataMismatchError
from otto.coverage.reporter import (
    CoverageReporter,
    discover_gcda_dirs,
    read_cov_source_root,
    read_cov_source_roots,
    run_coverage_report,
)
from otto.coverage.tiers import load_tiers

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin",
}


class TestReadCovSourceRoot:
    def test_reads_from_meta_file(self, tmp_path):
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        meta = {"repo_name": "myrepo", "sut_dir": str(tmp_path)}
        (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta))
        assert read_cov_source_root([cov_dir]) == tmp_path

    def test_searches_multiple_cov_dirs(self, tmp_path):
        cov1 = tmp_path / "run1" / "cov"
        cov1.mkdir(parents=True)
        cov2 = tmp_path / "run2" / "cov"
        cov2.mkdir(parents=True)
        meta = {"repo_name": "myrepo", "sut_dir": "/some/path"}
        (cov2 / ".otto_cov_meta.json").write_text(json.dumps(meta))
        assert read_cov_source_root([cov1, cov2]) == Path("/some/path")

    def test_raises_when_no_meta(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="otto_cov_meta"):
            read_cov_source_root([tmp_path])

    def test_raises_on_empty_list(self):
        with pytest.raises(FileNotFoundError):
            read_cov_source_root([])


class TestDiscoverGcdaDirs:
    def test_discovers_host_dirs(self, tmp_path):
        cov_dir = tmp_path / "run1" / "cov"
        (cov_dir / "host_a").mkdir(parents=True)
        (cov_dir / "host_b").mkdir(parents=True)

        result = discover_gcda_dirs([cov_dir])
        assert len(result) == 2
        names = {d.name for d in result}
        assert names == {"host_a", "host_b"}

    def test_multiple_cov_dirs(self, tmp_path):
        for run in ("run1", "run2"):
            (tmp_path / run / "cov" / "host1").mkdir(parents=True)

        result = discover_gcda_dirs(
            [tmp_path / "run1" / "cov", tmp_path / "run2" / "cov"],
        )
        assert len(result) == 2

    def test_skips_missing_cov_dir(self, tmp_path):
        result = discover_gcda_dirs([tmp_path / "run_no_cov" / "cov"])
        assert len(result) == 0

    def test_skips_files_in_cov_dir(self, tmp_path):
        cov_dir = tmp_path / "run1" / "cov"
        cov_dir.mkdir(parents=True)
        (cov_dir / "stray_file.txt").write_text("not a dir")
        (cov_dir / "host1").mkdir()

        result = discover_gcda_dirs([cov_dir])
        assert len(result) == 1
        assert result[0].name == "host1"


class TestReadCovSourceRoots:
    def test_read_cov_source_roots(self, tmp_path):
        cov = tmp_path / "cov"
        cov.mkdir()
        (cov / ".otto_cov_meta.json").write_text(
            json.dumps(
                {
                    "sut_dir": "/x",
                    "toolchains": {},
                    "source_roots": {"sprout": "/b/v3_7", "sprout44": "/b/v4_4"},
                }
            )
        )
        assert read_cov_source_roots([cov]) == {
            "sprout": Path("/b/v3_7"),
            "sprout44": Path("/b/v4_4"),
        }

    def test_read_cov_source_roots_missing_meta_returns_empty(self, tmp_path):
        assert read_cov_source_roots([tmp_path / "nope"]) == {}

    def test_read_cov_source_roots_no_key_returns_empty(self, tmp_path):
        cov = tmp_path / "cov"
        cov.mkdir()
        (cov / ".otto_cov_meta.json").write_text(json.dumps({"sut_dir": "/x"}))
        assert read_cov_source_roots([cov]) == {}


class TestCoverageReporterPerHostGcno:
    def test_per_host_gcno_dirs_uses_source_roots_then_fallback(self, tmp_path):
        gcda_dirs = [
            tmp_path / "cov" / "sprout",
            tmp_path / "cov" / "sprout44",
            tmp_path / "cov" / "other",
        ]
        root_a = tmp_path / "build_v3_7"
        root_b = tmp_path / "build_v4_4"
        fallback = tmp_path / "fallback"
        r = CoverageReporter(
            gcda_dirs=gcda_dirs,
            source_root=fallback,
            output_dir=tmp_path / "out",
            source_roots={"sprout": root_a, "sprout44": root_b},
        )
        assert r._per_host_gcno_dirs() == [root_a, root_b, fallback]  # 3rd falls back


class TestCoverageReporter:
    @pytest.mark.asyncio
    async def test_run_empty_dirs(self, tmp_path):
        reporter = CoverageReporter(
            gcda_dirs=[],
            source_root=tmp_path,
            output_dir=tmp_path / "out",
        )
        store = await reporter.run()
        assert store.file_count() == 0


def _init_repo(tmp_path: Path) -> Path:
    """Create a one-commit git repo and return its root."""
    repo = tmp_path / "sut"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={**_GIT_ENV, "HOME": str(tmp_path)},
        )

    git("init", "-q")
    (repo / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return repo


_PIN_GUARD_COV = {"tiers": {"system": {"kind": "e2e", "precedence": 1}}}


class TestE2ePinGuard:
    """A board capture pinned to a different commit than HEAD is a hard error."""

    @pytest.mark.asyncio
    async def test_capture_pin_mismatch_raises_naming_both_shas(self, tmp_path):
        repo = _init_repo(tmp_path)
        head = head_commit(repo)

        cap = Capture(
            tier="system",
            pin="f" * 40,
            files={"f.c": CaptureFileCov(lines={2: 1})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "capture.json")

        with pytest.raises(CoverageDataMismatchError) as excinfo:
            await run_coverage_report(
                [cov],
                tmp_path / "report",
                repo_root=repo,
                tier_configs=load_tiers(_PIN_GUARD_COV),
            )

        message = str(excinfo.value)
        assert "f" * 12 in message  # capture pin (short)
        assert head[:12] in message  # tree HEAD (short)

    @pytest.mark.asyncio
    async def test_capture_pin_matches_head_loads_into_store(self, tmp_path):
        repo = _init_repo(tmp_path)
        head = head_commit(repo)

        cap = Capture(
            tier="system",
            pin=head,
            files={"f.c": CaptureFileCov(lines={2: 7})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "capture.json")

        store = await run_coverage_report(
            [cov],
            tmp_path / "report",
            repo_root=repo,
            tier_configs=load_tiers(_PIN_GUARD_COV),
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[2].hits.for_tier("system") == 7


class TestUnitHarvest:
    """kind==unit tiers with harvest_dirs are captured+loaded via the merger."""

    @pytest.mark.asyncio
    async def test_harvest_dir_loads_under_unit_tier(self, tmp_path, monkeypatch):
        from otto.coverage.correlator import merger as merger_mod

        repo = _init_repo(tmp_path)
        # A harvest dir that is both the gcda and gcno root.
        hdir = tmp_path / "unit_build"
        hdir.mkdir()
        (hdir / "f.gcda").write_bytes(b"")
        (hdir / "f.gcno").write_bytes(b"")

        src = repo / "f.c"

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            # The merger is handed the harvest dir as *both* roots.
            assert gcda_dir == hdir
            assert gcno_dir == hdir
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fake_capture)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]},
                "system": {"kind": "e2e", "precedence": 2},
            }
        }
        store = await run_coverage_report(
            [],
            tmp_path / "report",
            repo_root=repo,
            tier_configs=load_tiers(cov_config),
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    @pytest.mark.asyncio
    async def test_missing_harvest_dir_warns_and_skips(self, tmp_path, monkeypatch, caplog):
        from otto.coverage.correlator import merger as merger_mod

        repo = _init_repo(tmp_path)

        async def fail_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            raise AssertionError("merger.capture must not run for a missing harvest dir")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fail_capture)

        cov_config = {
            "tiers": {
                "unit": {
                    "kind": "unit",
                    "precedence": 1,
                    "harvest_dirs": [str(tmp_path / "does_not_exist")],
                },
            }
        }
        with caplog.at_level("WARNING"):
            store = await run_coverage_report(
                [],
                tmp_path / "report",
                repo_root=repo,
                tier_configs=load_tiers(cov_config),
            )
        assert store is not None
        assert any("does not exist" in rec.message for rec in caplog.records)
        # The unit tier is still registered (seeded from tier_configs).
        assert "unit" in store.tier_order
