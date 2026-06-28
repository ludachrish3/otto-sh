"""Tests for the CoverageReporter and helper functions."""

import json
from pathlib import Path

import pytest

from otto.coverage.reporter import (
    CoverageReporter,
    discover_gcda_dirs,
    read_cov_source_root,
    read_cov_source_roots,
)


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
