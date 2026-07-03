"""Tests for the CoverageReporter and helper functions."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.errors import CoverageDataMismatchError
from otto.coverage.reporter import (
    CollectionInputs,
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


class TestExclusionDisplayIsRenderTime:
    """Routed from Task 10's review: exclusion display is render-time, not baked
    into the store by the reporter (single-valued LineRecord.state can't express
    "excluded always wins"). ``extra_markers`` still flows reporter -> renderer.
    """

    def test_apply_exclusions_removed_from_reporter(self):
        assert not hasattr(CoverageReporter, "_apply_exclusions")

    @pytest.mark.asyncio
    async def test_run_passes_extra_markers_to_renderer(self, tmp_path, monkeypatch):
        from otto.coverage import reporter as reporter_module

        captured: dict[str, object] = {}

        class FakeRenderer:
            def __init__(self, output_dir, *, project_name="Coverage Report", extra_markers=None):
                captured["output_dir"] = output_dir
                captured["extra_markers"] = extra_markers

            def render(self, store):
                captured["rendered"] = store

        monkeypatch.setattr(reporter_module, "HtmlRenderer", FakeRenderer)

        reporter = CoverageReporter(
            gcda_dirs=[],
            source_root=tmp_path,
            output_dir=tmp_path / "out",
            collection=CollectionInputs(extra_markers=["MYPROJ_NO_COV"]),
        )
        await reporter.run()
        assert captured["extra_markers"] == ["MYPROJ_NO_COV"]
        assert "rendered" in captured


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
        assert "re-run" in message  # remedy

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


class TestE2eDirtyTreeRemap:
    """A dirty working tree at report time: pin==HEAD still holds (nothing was
    committed), but the renderer reads the edited on-disk source, so every e2e
    hit past a local edit must be remapped HEAD -> worktree, and hits on
    locally-modified lines dropped.
    """

    @pytest.mark.asyncio
    async def test_dirty_tree_remaps_pin_to_worktree_and_drops_edited_lines(self, tmp_path, caplog):
        repo = _init_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"
        head = head_commit(repo)

        # Capture in HEAD (pin) coordinates: all three lines covered.
        cap = Capture(
            tier="system",
            pin=head,
            files={"f.c": CaptureFileCov(lines={1: 5, 2: 3, 3: 9})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "capture.json")

        # Dirty the worktree WITHOUT committing (HEAD, and thus the pin guard,
        # is unaffected): insert a line at the top (shifts every line down one)
        # and edit line 3 ("int c;" -> "int CHANGED;").
        (repo / "f.c").write_text("int NEW;\nint a;\nint b;\nint CHANGED;\n")

        with caplog.at_level("WARNING"):
            store = await run_coverage_report(
                [cov],
                tmp_path / "report",
                repo_root=repo,
                tier_configs=load_tiers(_PIN_GUARD_COV),
            )

        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        # old line 1 -> new line 2; old line 2 -> new line 3 (the insert shift).
        assert frec.lines[2].hits.for_tier("system") == 5
        assert frec.lines[3].hits.for_tier("system") == 3
        # old line 3 was locally edited -> no worktree counterpart -> dropped.
        assert 4 not in frec.lines
        assert frec.lines[2].hits.for_tier("system") == 5  # not misaligned to line 1
        # The dirty-tree remap warning fired (names the omission of edited lines).
        assert any("uncommitted changes" in rec.message for rec in caplog.records)


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

    @pytest.mark.asyncio
    async def test_empty_harvest_dir_warns_and_skips(self, tmp_path, monkeypatch, caplog):
        """A harvest dir that exists but has no .gcda files under it warns and is skipped
        (distinct from the missing-dir case above)."""
        from otto.coverage.correlator import merger as merger_mod

        repo = _init_repo(tmp_path)
        hdir = tmp_path / "unit_build_empty"
        hdir.mkdir()

        async def fail_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            raise AssertionError("merger.capture must not run for a harvest dir with no .gcda")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fail_capture)

        cov_config = {
            "tiers": {
                "unit": {
                    "kind": "unit",
                    "precedence": 1,
                    "harvest_dirs": [str(hdir)],
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
        assert any("no .gcda files" in rec.message for rec in caplog.records)
        assert "unit" in store.tier_order

    @pytest.mark.asyncio
    async def test_stale_counters_warn_but_still_load(self, tmp_path, monkeypatch, caplog):
        """A .gcda older than the newest .gcno is flagged in the report but still loaded
        (Task 10's `_warn_if_stale_counters`, deferred-untested finding closed here)."""
        from otto.coverage.correlator import merger as merger_mod

        repo = _init_repo(tmp_path)
        hdir = tmp_path / "unit_build_stale"
        hdir.mkdir()
        gcda = hdir / "f.gcda"
        gcno = hdir / "f.gcno"
        gcda.write_bytes(b"")
        gcno.write_bytes(b"")

        now = 1_800_000_000.0
        os.utime(gcda, (now - 100, now - 100))  # counters predate the build notes
        os.utime(gcno, (now, now))

        src = repo / "f.c"

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fake_capture)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]},
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
        assert any(
            "stale" in rec.message and "loading anyway" in rec.message for rec in caplog.records
        )
        # Still loaded despite the staleness warning — a warning, not a fatal error.
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    @pytest.mark.asyncio
    async def test_relative_harvest_dir_resolves_against_repo_root(self, tmp_path, monkeypatch):
        """A relative ``harvest_dirs`` entry is repo-relative (spec §4), not
        CWD-relative — it must resolve even when ``otto cov report`` runs
        from a directory other than the repo root."""
        from otto.coverage.correlator import merger as merger_mod

        repo = _init_repo(tmp_path)
        hdir = repo / "unit_build"
        hdir.mkdir()
        (hdir / "f.gcda").write_bytes(b"")
        (hdir / "f.gcno").write_bytes(b"")

        src = repo / "f.c"

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            # Resolved against repo_root, matching the absolute harvest dir.
            assert gcda_dir == hdir
            assert gcno_dir == hdir
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fake_capture)

        # CWD is a sibling of the repo, not the repo root itself.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["unit_build"]},
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
