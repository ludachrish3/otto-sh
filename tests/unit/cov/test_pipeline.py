"""Tests for the CoverageReporter and helper functions."""

import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from otto.config.coverage_settings import CoverageConfigError
from otto.coverage.capture.gitio import head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.errors import CaptureCommitError, CoverageNotInstrumentedError
from otto.coverage.report_inputs import ReportInputs
from otto.coverage.reporter import (
    CollectionInputs,
    CoverageReporter,
    _classify_product_dirs,
    read_cov_source_root,
    read_cov_source_roots,
    read_cov_toolchains,
    run_coverage_report,
)
from otto.coverage.tiers import load_tiers
from otto.coverage.toolchains import resolve_toolchains
from otto.coverage.tree import ProductKey
from otto.host.errors import CoverageToolMissingError
from otto.host.toolchain import Toolchain
from tests._fixtures.gitrepo import git_env
from tests.unit.cov._unit_lab import (
    ContainerHost,
    FakeHost,
    FakeProduct,
    install_capture,
    install_lab,
    own_files,
    product_build,
    reference_image,
    stub_tool,
    stub_unit_tools,
    unit_build,
    write_sources,
)

TIER_AND_COMMIT = {"tier": "system", "base_commit": "a" * 40}
"""The two keys every current ``.otto_cov_meta.json`` carries beside the project."""


class TestReadCovSourceRoot:
    def test_reads_from_meta_file(self, tmp_path):
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        meta = {"project_name": "myrepo", "project_root": str(tmp_path), **TIER_AND_COMMIT}
        (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta))
        assert read_cov_source_root([cov_dir]) == tmp_path

    def test_searches_multiple_cov_dirs(self, tmp_path):
        cov1 = tmp_path / "run1" / "cov"
        cov1.mkdir(parents=True)
        cov2 = tmp_path / "run2" / "cov"
        cov2.mkdir(parents=True)
        meta = {"project_name": "myrepo", "project_root": "/some/path", **TIER_AND_COMMIT}
        (cov2 / ".otto_cov_meta.json").write_text(json.dumps(meta))
        assert read_cov_source_root([cov1, cov2]) == Path("/some/path")

    def test_raises_when_no_meta(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="otto_cov_meta"):
            read_cov_source_root([tmp_path])

    def test_raises_on_empty_list(self):
        with pytest.raises(FileNotFoundError):
            read_cov_source_root([])


class TestClassifyProductDirs:
    def test_sorts_product_dirs_by_capture_state(self, tmp_path):
        cov = tmp_path / "cov"
        for product in ("app", "agent", "tool"):
            (cov / "h1" / product).mkdir(parents=True)
        (cov / "h1" / "app" / "capture.json").write_text("{}")
        (cov / "h1" / "tool" / "capture.failed").write_text("lcov --capture failed\n")
        (cov / "h1" / "agent" / "x.gcda").write_bytes(b"adcg")

        states = _classify_product_dirs(cov)

        assert states.captured == [ProductKey("h1", "app")]
        assert states.failed == [ProductKey("h1", "tool")]
        assert states.pending == [ProductKey("h1", "agent")]

    def test_a_capture_directly_under_the_host_dir_is_refused(self, tmp_path):
        cov = tmp_path / "cov"
        (cov / "h1").mkdir(parents=True)
        (cov / "h1" / "capture.json").write_text("{}")

        with pytest.raises(CoverageConfigError, match=r"cov/<host>/<product>/"):
            _classify_product_dirs(cov)


class TestReadCovSourceRoots:
    def test_read_cov_source_roots(self, tmp_path):
        cov = tmp_path / "cov"
        cov.mkdir()
        (cov / ".otto_cov_meta.json").write_text(
            json.dumps(
                {
                    "project_name": "p",
                    "project_root": "/x",
                    **TIER_AND_COMMIT,
                    "toolchains": {},
                    "source_roots": {"zephyr37-fat": "/b/v3_7", "zephyr44-fat": "/b/v4_4"},
                }
            )
        )
        assert read_cov_source_roots([cov]) == {
            "zephyr37-fat": Path("/b/v3_7"),
            "zephyr44-fat": Path("/b/v4_4"),
        }

    def test_read_cov_source_roots_missing_meta_returns_empty(self, tmp_path):
        assert read_cov_source_roots([tmp_path / "nope"]) == {}

    def test_read_cov_source_roots_no_key_returns_empty(self, tmp_path):
        cov = tmp_path / "cov"
        cov.mkdir()
        (cov / ".otto_cov_meta.json").write_text(
            json.dumps({"project_name": "p", "project_root": "/x", **TIER_AND_COMMIT})
        )
        assert read_cov_source_roots([cov]) == {}


# .gcda headers: 4-byte magic (little-endian files store it reversed) + the
# 4-char gcov version stamp, reversed likewise + a build stamp word.
_CLANG_GCDA = b"adcg*804" + b"\x00" * 4
_GCC12_GCDA = b"adcg*42B" + b"\x00" * 4
_GCC13_GCDA = b"adcg*33B" + b"\x00" * 4


def _gcda_dir(root: Path, host: str, product: str, header: bytes) -> Path:
    d = root / "cov" / host / product
    d.mkdir(parents=True)
    (d / "main.gcda").write_bytes(header)
    return d


def _tool(bin_dir: Path, name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


class TestResolveToolchains:
    """Per gcda directory: a record that names a gcov wins; otherwise the
    directory's own stamp chooses, and replaces only the gcov of whatever
    record there is. The system gcov's major is probed once per report."""

    @pytest.fixture(autouse=True)
    def system_gcov_is_13(self, monkeypatch):
        self.probes = 0

        def probe(gcov="gcov"):
            self.probes += 1
            return 13

        monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", probe)

    @pytest.fixture
    def bin_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(d))
        return d

    @staticmethod
    def _resolve(gcda_dirs, toolchains=None):
        return resolve_toolchains(gcda_dirs, toolchains or {})

    def test_a_recorded_default_is_silent_and_the_stamp_chooses(self, tmp_path, bin_dir):
        llvm_cov = _tool(bin_dir, "llvm-cov")
        d = _gcda_dir(tmp_path, "test1", "app", _CLANG_GCDA)
        (tc,) = self._resolve([d], {"test1": Toolchain()})

        assert tc is not None
        assert tc.gcov_bin == str(llvm_cov)
        assert tc.lcov == Path("usr/bin/lcov")  # the record's other fields stay

    def test_an_explicit_gcov_wins_over_a_disagreeing_stamp(self, tmp_path, bin_dir, monkeypatch):
        def never(*a, **k):
            raise AssertionError("the stamp must not be read for an explicit record")

        monkeypatch.setattr("otto.host.toolchain_discovery.discover_toolchain_from_gcda", never)
        cross = Toolchain(sysroot=Path("/opt/cross"), gcov=Path("bin/arm-gcov"))
        d = _gcda_dir(tmp_path, "test1", "app", _CLANG_GCDA)
        assert self._resolve([d], {"test1": cross}) == [cross]
        assert self.probes == 0

    def test_a_record_naming_only_an_lcov_keeps_it_and_takes_the_stamps_gcov(
        self, tmp_path, bin_dir
    ):
        gcov_12 = _tool(bin_dir, "gcov-12")
        d = _gcda_dir(tmp_path, "test1", "app", _GCC12_GCDA)
        wrapper = Toolchain(lcov=Path("/tmp/lcov-wrapper.sh"))
        (tc,) = self._resolve([d], {"test1": wrapper})

        assert tc is not None
        assert tc.gcov_bin == str(gcov_12)
        assert tc.lcov_bin == "/tmp/lcov-wrapper.sh"

    def test_a_record_naming_only_lcov_arguments_keeps_them(self, tmp_path, bin_dir):
        """The stamp replaces only the gcov; the host's extra lcov arguments
        are what the clang arm configures and must not be dropped."""
        llvm_cov = _tool(bin_dir, "llvm-cov")
        d = _gcda_dir(tmp_path, "test1", "app", _CLANG_GCDA)
        record = Toolchain(lcov_args=["--ignore-errors", "source"])
        (tc,) = self._resolve([d], {"test1": record})

        assert tc is not None
        assert tc.gcov_bin == str(llvm_cov)
        assert tc.lcov_args == ["--ignore-errors", "source"]

    def test_a_mixed_run_resolves_each_directory_on_its_own(self, tmp_path, bin_dir):
        llvm_cov = _tool(bin_dir, "llvm-cov")
        gcov_12 = _tool(bin_dir, "gcov-12")
        dirs = [
            _gcda_dir(tmp_path, "test1", "app", _CLANG_GCDA),
            _gcda_dir(tmp_path, "test2", "app", _GCC12_GCDA),
            _gcda_dir(tmp_path, "test3", "app", _GCC13_GCDA),
        ]
        clang, gcc12, gcc13 = self._resolve(dirs)

        assert clang is not None
        assert clang.gcov_bin == str(llvm_cov)
        assert gcc12 is not None
        assert gcc12.gcov_bin == str(gcov_12)
        assert gcc13 is None  # the system gcov's own major: the merger default
        assert self.probes == 1

    def test_a_same_major_stamp_keeps_a_recorded_default_as_recorded(self, tmp_path, bin_dir):
        d = _gcda_dir(tmp_path, "test1", "app", _GCC13_GCDA)
        recorded = Toolchain()
        assert self._resolve([d], {"test1": recorded}) == [recorded]

    def test_a_missing_tool_fails_the_report_before_lcov_runs(self, tmp_path, bin_dir):
        d = _gcda_dir(tmp_path, "test2", "otto_kmod_demo", _GCC12_GCDA)
        with pytest.raises(CoverageToolMissingError, match=r"gcov-12.*apt install gcc-12"):
            self._resolve([d])


class TestReadCovToolchains:
    """The cov metadata is where a deferred ``otto cov report`` gets its
    toolchains from, so anything a host's capture command needs has to survive
    the round trip — ``lcov_args`` included (issue #385)."""

    @staticmethod
    def _meta(tmp_path: Path, toolchains: dict) -> Path:
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        (cov_dir / ".otto_cov_meta.json").write_text(
            json.dumps(
                {
                    "project_name": "r",
                    "project_root": str(tmp_path),
                    **TIER_AND_COMMIT,
                    "toolchains": toolchains,
                }
            )
        )
        return cov_dir

    def test_extra_lcov_arguments_survive_the_metadata(self, tmp_path):
        cov_dir = self._meta(
            tmp_path,
            {
                "test1": {
                    "sysroot": "/",
                    "lcov": "usr/bin/lcov",
                    "lcov_args": ["--ignore-errors", "source"],
                }
            },
        )
        assert read_cov_toolchains([cov_dir])["test1"].lcov_args == ["--ignore-errors", "source"]

    def test_a_record_without_them_stays_at_the_default(self, tmp_path):
        cov_dir = self._meta(tmp_path, {"test1": {"gcov": "/usr/bin/gcov-12"}})
        tc = read_cov_toolchains([cov_dir])["test1"]
        assert tc.lcov_args == []
        assert tc.gcov == Path("/usr/bin/gcov-12")


class TestCoverageReporter:
    @pytest.mark.asyncio
    async def test_run_empty_dirs(self, tmp_path):
        reporter = CoverageReporter(
            source_root=tmp_path,
            output_dir=tmp_path / "out",
        )
        store = await reporter.run()
        assert store.file_count() == 0


class TestExclusionIsAFilterStage:
    """Reverses the former "exclusion is render-time" guard, deliberately.

    That guard existed because a single-valued LineRecord.state cannot
    express "excluded always wins" over covered/stale/aging, so exclusion was
    kept out of the store. This design deletes the records instead of
    assigning a state, so there is no precedence to express and the original
    objection does not apply. Exclusion must now happen in the reporter,
    before attribution, and the renderer must NOT scan source.
    """

    def test_renderer_no_longer_scans_source(self):
        from otto.coverage.renderer import spa_data

        assert not hasattr(spa_data, "scan_excluded_lines")

    @pytest.mark.asyncio
    async def test_run_applies_exclusions_before_rendering(self, tmp_path, monkeypatch):
        # reporter.py defers both imports to their use sites inside run()
        # (import-budget guard: keeps their transitive imports off the
        # `otto cov --help` path), so each name is patched on its own module
        # rather than as a module-level binding on `reporter`.
        from otto.coverage.exclusions import apply as apply_module
        from otto.coverage.renderer import spa_renderer as spa_renderer_module

        order: list[str] = []

        def fake_apply(store, rules, root):
            order.append("apply")

        class FakeRenderer:
            def __init__(self, output_dir, *, report_title="Coverage Report", prefix=None):
                pass

            def render(self, store):
                order.append("render")

        monkeypatch.setattr(apply_module, "apply_exclusions", fake_apply)
        monkeypatch.setattr(spa_renderer_module, "SpaRenderer", FakeRenderer)

        reporter = CoverageReporter(
            source_root=tmp_path,
            output_dir=tmp_path / "out",
        )
        await reporter.run()
        assert order == ["apply", "render"]


def _git(project: Path, tmp_path: Path, *args: str) -> None:
    """Run one git command in the project root *project* (sync helper — keeps subprocess
    calls out of async test bodies directly, avoiding ASYNC221)."""
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        env=git_env(tmp_path),
    )


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a one-commit git repository and return its root."""
    project = tmp_path / "sut"
    project.mkdir()
    _git(project, tmp_path, "init", "-q")
    (project / "f.c").write_text("int a;\nint b;\nint c;\n")
    _git(project, tmp_path, "add", "f.c")
    _git(project, tmp_path, "commit", "-qm", "init")
    return project


_BASE_COMMIT_GUARD_COV = {"tiers": {"system": {"kind": "e2e", "precedence": 1}}}


class TestE2eBaseCommitGuard:
    """A board capture anchored to a different commit than HEAD is a hard error."""

    @pytest.mark.asyncio
    async def test_capture_base_commit_mismatch_raises_naming_both_shas(self, tmp_path):
        project = _init_git_repo(tmp_path)
        head = head_commit(project)

        cap = Capture(
            tier="system",
            product="app",
            base_commit="f" * 40,
            files={"f.c": CaptureFileCov(lines={2: 1})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "app" / "capture.json")

        with pytest.raises(CaptureCommitError) as excinfo:
            await run_coverage_report(
                [cov],
                tmp_path / "report",
                ReportInputs(project_root=project, tier_configs=load_tiers(_BASE_COMMIT_GUARD_COV)),
            )

        message = str(excinfo.value)
        assert "f" * 12 in message  # capture base_commit (short)
        assert head[:12] in message  # tree HEAD (short)
        assert "re-run" in message  # remedy

    @pytest.mark.asyncio
    async def test_capture_base_commit_matches_head_loads_into_store(self, tmp_path):
        project = _init_git_repo(tmp_path)
        head = head_commit(project)

        cap = Capture(
            tier="system",
            product="app",
            base_commit=head,
            files={"f.c": CaptureFileCov(lines={2: 7})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "app" / "capture.json")

        store = await run_coverage_report(
            [cov],
            tmp_path / "report",
            ReportInputs(project_root=project, tier_configs=load_tiers(_BASE_COMMIT_GUARD_COV)),
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[2].hits.for_tier("system") == 7


class TestE2eDirtyTreeRemap:
    """A dirty working tree at report time: base_commit == HEAD still holds (nothing was
    committed), but the renderer reads the edited on-disk source, so every e2e
    hit past a local edit must be remapped HEAD -> worktree, and hits on
    locally-modified lines dropped.
    """

    @pytest.mark.asyncio
    async def test_dirty_tree_remaps_base_commit_to_worktree_and_drops_edited_lines(
        self, tmp_path, caplog
    ):
        project = _init_git_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"
        head = head_commit(project)

        # Capture in HEAD (base_commit) coordinates: all three lines covered.
        cap = Capture(
            tier="system",
            product="app",
            base_commit=head,
            files={"f.c": CaptureFileCov(lines={1: 5, 2: 3, 3: 9})},
        )
        cov = tmp_path / "out" / "cov"
        cap.save(cov / "board1" / "app" / "capture.json")

        # Dirty the worktree WITHOUT committing (HEAD, and thus the
        # base_commit guard, is unaffected): insert a line at the top
        # (shifts every line down one) and edit line 3 ("int c;" -> "int CHANGED;").
        (project / "f.c").write_text("int NEW;\nint a;\nint b;\nint CHANGED;\n")

        with caplog.at_level("WARNING"):
            store = await run_coverage_report(
                [cov],
                tmp_path / "report",
                ReportInputs(project_root=project, tier_configs=load_tiers(_BASE_COMMIT_GUARD_COV)),
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


_UNIT_COV = {"tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["build/tests"]}}}


def _unit_inputs(project: Path, cov_config: dict | None = None) -> ReportInputs:
    return ReportInputs(
        project_root=project,
        project_name="proj",
        tier_configs=load_tiers(cov_config or _UNIT_COV),
    )


def _messages(caplog, level: str | None = None) -> list[str]:
    want = None if level is None else getattr(logging, level)
    return [r.getMessage() for r in caplog.records if want is None or r.levelno == want]


def _store_paths(store, project: Path) -> list[str]:
    return [f.path.relative_to(project).as_posix() for f in store.files()]


class TestUnitHarvest:
    """kind==unit tiers with harvest_dirs are captured+loaded via the merger."""

    @pytest.fixture(autouse=True)
    def _tools(self, tmp_path, monkeypatch):
        stub_unit_tools(monkeypatch, tmp_path / "stub-bin")

    @pytest.mark.asyncio
    async def test_harvest_dir_loads_under_unit_tier(self, tmp_path, monkeypatch):
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])
        # A harvest dir that is both the gcda and gcno root.
        hdir = tmp_path / "unit_build"
        unit_build(hdir, project, ["f.c"])

        src = project / "f.c"

        async def fake_capture(self, directory, output, toolchain=None, includes=None):
            # The merger captures the harvest dir in place.
            assert directory == hdir
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]},
                "system": {"kind": "e2e", "precedence": 2},
            }
        }
        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, cov_config)
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    @pytest.mark.asyncio
    async def test_products_table_creates_one_run_per_product(self, tmp_path, monkeypatch):
        """A unit tier's ``products`` table gets one run record per named
        product plus one unnamed run for ``harvest_dirs``, each loaded from
        its own info file."""
        from otto.coverage.merge import lcov_loader as loader_mod
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        app = product_build(project, "app", ["f.c"])
        agent = product_build(project, "agent", ["f.c"])
        install_lab(monkeypatch, [FakeHost("h1", [app, agent])])
        app_dir = tmp_path / "build" / "app-tests"
        agent_dir = tmp_path / "build" / "agent-tests"
        unit_dir = tmp_path / "build" / "tests"
        for d in (app_dir, agent_dir, unit_dir):
            unit_build(d, project, ["f.c"])

        src = project / "f.c"

        async def fake_capture(self, directory, output, toolchain=None, includes=None):
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)

        load_calls: list[tuple[str, int | None]] = []
        real_load = loader_mod.LCOVLoader.load

        def spy_load(self, info_path, tier, run_id=None):
            load_calls.append((Path(info_path).name, run_id))
            return real_load(self, info_path, tier, run_id=run_id)

        monkeypatch.setattr(loader_mod.LCOVLoader, "load", spy_load)

        cov_config = {
            "tiers": {
                "unit": {
                    "kind": "unit",
                    "precedence": 1,
                    "harvest_dirs": [str(unit_dir)],
                    "products": {"app": [str(app_dir)], "agent": [str(agent_dir)]},
                },
            }
        }
        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, cov_config)
        )
        assert store is not None
        assert {r.product for r in store.runs} == {"app", "agent", ""}

        names = {name for name, _ in load_calls}
        assert names == {
            "unit_unit_app_0.info",
            "unit_unit_agent_0.info",
            "unit_unit_0.info",
        }
        run_ids = {rid for _, rid in load_calls}
        assert len(run_ids) == 3

    @pytest.mark.asyncio
    async def test_missing_harvest_dir_warns_and_skips(self, tmp_path, monkeypatch, caplog):
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])

        async def fail_capture(self, directory, output, toolchain=None, includes=None):
            raise AssertionError("merger.capture_dir must not run for a missing harvest dir")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fail_capture)

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
                [], tmp_path / "report", _unit_inputs(project, cov_config)
            )
        assert store is not None
        assert any("does not exist" in rec.message for rec in caplog.records)
        # The unit tier is still registered (seeded from tier_configs).
        assert "unit" in store.tier_order

    @pytest.mark.asyncio
    async def test_empty_harvest_dir_warns_and_skips(self, tmp_path, monkeypatch, caplog):
        """A harvest dir that exists but has no .gcda files under it warns and is skipped
        (distinct from the missing-dir case above)."""
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])
        hdir = tmp_path / "unit_build_empty"
        hdir.mkdir()

        async def fail_capture(self, directory, output, toolchain=None, includes=None):
            raise AssertionError("merger.capture_dir must not run for a harvest dir with no .gcda")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fail_capture)

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
                [], tmp_path / "report", _unit_inputs(project, cov_config)
            )
        assert store is not None
        assert any(
            "no .gcda files" in rec.message and "run the unit tests first" in rec.message
            for rec in caplog.records
        )
        assert "unit" in store.tier_order

    @pytest.mark.asyncio
    async def test_stale_counters_warn_but_still_load(self, tmp_path, monkeypatch, caplog):
        """A .gcda older than the newest .gcno is flagged in the report but still loaded
        (Task 10's `_warn_if_stale_counters`, deferred-untested finding closed here)."""
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])
        hdir = tmp_path / "unit_build_stale"
        unit_build(hdir, project, ["f.c"])
        gcda = hdir / "f.c.gcda"
        gcno = hdir / "f.c.gcno"

        now = 1_800_000_000.0
        os.utime(gcda, (now - 100, now - 100))  # counters predate the build notes
        os.utime(gcno, (now, now))

        src = project / "f.c"

        async def fake_capture(self, directory, output, toolchain=None, includes=None):
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]},
            }
        }
        with caplog.at_level("WARNING"):
            store = await run_coverage_report(
                [], tmp_path / "report", _unit_inputs(project, cov_config)
            )
        assert store is not None
        assert any(
            "stale" in rec.message and "loading anyway" in rec.message for rec in caplog.records
        )
        # Still loaded despite the staleness warning — a warning, not a fatal error.
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    @pytest.mark.asyncio
    async def test_relative_harvest_dir_resolves_against_project_root(self, tmp_path, monkeypatch):
        """A relative ``harvest_dirs`` entry is project-root-relative (spec §4), not
        CWD-relative — it must resolve even when ``otto cov report`` runs
        from a directory other than the project root."""
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])
        hdir = project / "unit_build"
        unit_build(hdir, project, ["f.c"])

        src = project / "f.c"

        async def fake_capture(self, directory, output, toolchain=None, includes=None):
            # Resolved against project_root, matching the absolute harvest dir.
            assert directory == hdir
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)

        # CWD is a sibling of the project root, not the project root itself.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["unit_build"]},
            }
        }
        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, cov_config)
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    @pytest.mark.asyncio
    async def test_tilde_harvest_dir_expands_via_home(self, tmp_path, monkeypatch):
        """A ``~``-prefixed ``harvest_dirs`` entry expands against ``HOME`` before
        project-root anchoring (path-resolution convention, docs/configuration/settings.md).

        Without the fix, ``~`` is never expanded and the entry is anchored
        literally under ``project_root / "~" / "unit_build"``, which never exists —
        the harvest dir is silently skipped and no unit coverage loads.
        """
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["f.c"])
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        hdir = home / "unit_build"
        unit_build(hdir, project, ["f.c"])

        src = project / "f.c"

        async def fake_capture(self, directory, output, toolchain=None, includes=None):
            # Resolved against HOME, not project_root/"~"/... .
            assert directory == hdir
            output.write_text(f"TN:\nSF:{src}\nDA:1,5\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["~/unit_build"]},
            }
        }
        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, cov_config)
        )
        assert store is not None
        (frec,) = [f for f in store.files() if f.path.name == "f.c"]
        assert frec.lines[1].hits.for_tier("unit") == 5

    # -- the product filter ------------------------------------------------

    @pytest.mark.asyncio
    async def test_products_filter_reaches_capture_and_exact_pass(
        self, tmp_path, monkeypatch, caplog
    ):
        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        unit_build(project / "build/tests", project, ["src/core/add.c", "tests/test_add.cpp"])
        calls = install_capture(monkeypatch)

        with caplog.at_level("DEBUG"):
            store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert [c["includes"] for c in calls] == [["*/src/core/add.c"]]
        assert _store_paths(store, project) == ["src/core/add.c"]
        assert "unit unit: kept 1 product source files, dropped 1 not in any product" in (
            _messages(caplog, "INFO")
        )
        debug = _messages(caplog, "DEBUG")
        assert "unit unit: keep src/core/add.c (product core)" in debug
        assert "unit unit: drop tests/test_add.cpp (not in any product)" in debug

    @pytest.mark.asyncio
    async def test_view_with_no_products_refuses(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        other = product_build(project, "core", ["src/core/add.c"], owner="other")
        install_lab(monkeypatch, [FakeHost("h1", [other])])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        message = str(excinfo.value)
        assert "unit tier unit: no products of project proj found in the active lab (bench)" in (
            message
        )
        assert "coverage instrumentation (--coverage)" in message
        assert calls == []

    @pytest.mark.asyncio
    async def test_no_project_name_selects_no_owner_less_products(self, tmp_path, monkeypatch):
        from otto.coverage.objects import ObjectListCache
        from otto.coverage.pairing import GcnoIndex
        from otto.coverage.sources import SourceResolver
        from otto.coverage.unit_filter import plan_unit_views

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"], owner=None)
        unit_build(project / "build/tests", project, ["src/core/add.c"])

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await plan_unit_views(
                load_tiers(_UNIT_COV),
                project,
                None,
                ObjectListCache(project),
                SourceResolver(project, quiet=True),
                GcnoIndex(project),
            )

        message = str(excinfo.value)
        assert "unit tier unit: no project is named, so no products are selected" in message
        assert "--coverage" in message

    @pytest.mark.asyncio
    async def test_named_view_matching_no_product_refuses(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        unit_build(project / "build/app-tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)
        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1, "products": {"app": ["build/app-tests"]}}
            }
        }

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project, cov_config))

        message = str(excinfo.value)
        assert "unit tier unit/app: no product named 'app'" in message
        assert "--coverage" in message
        assert calls == []

    @pytest.mark.asyncio
    async def test_all_products_declared_uninstrumented_refuses(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        core = own_files(monkeypatch, project, ["src/core/add.c"])
        core.instrumented_override = False
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert "every product of project proj is declared instrumented = false" in str(
            excinfo.value
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_no_lab_context_refuses(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        monkeypatch.setattr("otto.run_context.try_get_context", lambda: None)
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        message = str(excinfo.value)
        assert "unit tier unit: no products of project proj found: no lab is active" in message
        assert "--coverage" in message
        assert calls == []

    @pytest.mark.asyncio
    async def test_nothing_kept_skips_capture(self, tmp_path, monkeypatch, caplog):
        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        hdir = project / "build/tests"
        unit_build(hdir, project, ["tests/test_add.cpp"])
        calls = install_capture(monkeypatch)

        with caplog.at_level("INFO"):
            store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert calls == []
        assert f"unit unit: no product source files under {hdir}; nothing to capture" in (
            _messages(caplog, "INFO")
        )
        assert store.runs == []

    @pytest.mark.asyncio
    async def test_uninstrumented_product_refuses_before_any_capture(self, tmp_path, monkeypatch):
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        core = product_build(project, "core", ["src/core/add.c"])
        app = FakeProduct("app", artifact=str(project / "build/app/app.bin"))  # never built
        install_lab(monkeypatch, [FakeHost("h1", [core, app])])
        unit_build(project / "build/core-tests", project, ["src/core/add.c"])
        unit_build(project / "build/app-tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)
        pairs: list[object] = []

        async def spy_pairs(self, *args, **kwargs):
            pairs.append(args)

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_pairs", spy_pairs)
        produced = _spy_produce(monkeypatch)
        # A product dir collected but not yet captured: the report would capture it now.
        cov = tmp_path / "run" / "cov"
        (cov / "h1" / "core").mkdir(parents=True)
        (cov / "h1" / "core" / "add.c.gcda").write_bytes(b"")
        _write_meta(cov, project)
        cov_config = {
            "tiers": {
                "unit": {
                    "kind": "unit",
                    "precedence": 1,
                    "products": {"core": ["build/core-tests"], "app": ["build/app-tests"]},
                }
            }
        }

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([cov], tmp_path / "report", _unit_inputs(project, cov_config))

        message = str(excinfo.value)
        assert "unit tier unit/app: product 'app'" in message
        assert str(project / "build/app/app.bin") in message
        assert "--coverage" in message
        assert calls == []
        assert pairs == []
        assert produced == []
        assert not (cov / "h1" / "core" / "capture.json").exists()
        assert not (cov / "h1" / "core" / "capture.failed").exists()
        assert not (tmp_path / "report").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("built", [False, True], ids=["artifact-missing", "artifact-readable"])
    async def test_other_projects_products_are_ignored(self, tmp_path, monkeypatch, caplog, built):
        project = _init_git_repo(tmp_path)
        core = product_build(project, "core", ["src/core/add.c"])
        if built:
            other = product_build(project, "other", ["src/other.c"], owner="other")
        else:
            other = FakeProduct("other", owner="other", artifact=str(project / "nope.bin"))
        install_lab(monkeypatch, [FakeHost("h1", [core, other])])
        # The unit build compiles the other project's file too.
        unit_build(project / "build/tests", project, ["src/core/add.c", "src/other.c"])
        calls = install_capture(monkeypatch)

        with caplog.at_level("DEBUG"):
            store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert [c["includes"] for c in calls] == [["*/src/core/add.c"]]
        assert _store_paths(store, project) == ["src/core/add.c"]
        assert "unit unit: drop src/other.c (not in any product)" in _messages(caplog, "DEBUG")

    @pytest.mark.asyncio
    async def test_project_tree_is_walked_once_per_report(self, tmp_path, monkeypatch):
        from tests.unit.cov._gcno import gcc13_gcno

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        hdir = project / "build/tests"
        unit_build(hdir, project, ["src/core/add.c"])
        # Relocated builds: every recorded name needs the resolver's tail match (a walk).
        relocated = gcc13_gcno("/elsewhere", ["/elsewhere/src/core/add.c"])
        (project / "build/core/src/core/add.c.gcno").write_bytes(relocated)
        (hdir / "add.c.gcno").write_bytes(relocated)
        install_capture(monkeypatch)
        walks: list[Path] = []
        real_rglob = Path.rglob

        def counting_rglob(self, pattern, *args, **kwargs):
            if pattern == "*":
                walks.append(self)
            return real_rglob(self, pattern, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", counting_rglob)

        store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert _store_paths(store, project) == ["src/core/add.c"]
        assert walks == [project]

    @pytest.mark.asyncio
    async def test_one_artifact_read_per_report(self, tmp_path, monkeypatch):
        from otto.coverage import objects as objects_mod
        from otto.coverage.errors import CaptureFailedError
        from otto.coverage.merge import merger as merger_mod

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        install_capture(monkeypatch)

        async def no_lcov(self, *args, **kwargs):
            raise CaptureFailedError("lcov --capture failed:\nstub")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_pairs", no_lcov)
        caches: list[objects_mod.ObjectListCache] = []
        real_init = objects_mod.ObjectListCache.__init__

        def spy_init(self, project_root):
            real_init(self, project_root)
            caches.append(self)

        monkeypatch.setattr(objects_mod.ObjectListCache, "__init__", spy_init)
        reads: list[str] = []
        real_read = objects_mod.ObjectListCache.read

        def spy_read(self, product):
            reads.append(product.name)
            return real_read(self, product)

        monkeypatch.setattr(objects_mod.ObjectListCache, "read", spy_read)
        cov = tmp_path / "run" / "cov"
        (cov / "h1" / "core").mkdir(parents=True)
        (cov / "h1" / "core" / "add.c.gcda").write_bytes(b"")
        _write_meta(cov, project)

        await run_coverage_report([cov], tmp_path / "report", _unit_inputs(project))

        # asked twice (the unit preflight and the pending capture), read once
        assert reads == ["core", "core"]
        assert len(caches) == 1
        assert caches[0].files_read == 1

    @pytest.mark.asyncio
    async def test_a_reference_image_filters_by_its_running_containers_objects(
        self, tmp_path, monkeypatch
    ):
        project = _init_git_repo(tmp_path)
        built = product_build(project, "img", ["src/core/add.c"])
        host = ContainerHost("h1", [reference_image()])
        host.files = {"/usr/bin/img": Path(str(built.artifact)).read_bytes()}
        install_lab(monkeypatch, [host])
        unit_build(project / "build/tests", project, ["src/core/add.c", "tests/test_add.cpp"])
        calls = install_capture(monkeypatch)

        store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert [c["includes"] for c in calls] == [["*/src/core/add.c"]]
        assert _store_paths(store, project) == ["src/core/add.c"]
        assert any(
            c.startswith("docker exec -e OTTO_SCAN_ID=") and " img-c sh -c " in c
            for c in host.calls
        )

    @pytest.mark.asyncio
    async def test_a_stopped_reference_image_is_an_input_refusal(self, tmp_path, monkeypatch):
        from otto.coverage.errors import CoverageInputError

        project = _init_git_repo(tmp_path)
        install_lab(monkeypatch, [ContainerHost("h1", [reference_image()], running=False)])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert excinfo.value.field is None
        assert str(excinfo.value) == (
            "unit tier unit: product 'img' reads its object list from container 'img-c', "
            "which is not running — install the product first (otto run install)."
        )
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hits", [[], ["/usr/bin/app"]], ids=["no-hits", "some-hits"])
    async def test_an_unscannable_reference_image_is_a_capture_failure(
        self, tmp_path, monkeypatch, hits
    ):
        from otto.coverage.errors import CaptureFailedError
        from otto.result import Result
        from otto.utils import Status

        project = _init_git_repo(tmp_path)
        host = ContainerHost("h1", [reference_image()])
        host.files = dict.fromkeys(hits, b"\x7fELF\x00/build/app/main.c.gcda\x00")
        listed = "".join(f"{p}\n" for p in hits)
        host.scan = Result(Status.Success, value=listed + "otto-scan-rc:1\notto-scan-rc:2\n")
        install_lab(monkeypatch, [host])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CaptureFailedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert str(excinfo.value).startswith(
            "unit tier unit: could not read product 'img''s object list: "
            "could not scan container img-c: the scan exited 2"
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_uninstrumented_reference_image_refuses(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        host = ContainerHost("h1", [reference_image()])  # the scan finds nothing
        install_lab(monkeypatch, [host])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageNotInstrumentedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert str(excinfo.value).startswith(
            "unit tier unit: product 'img' (registry.example/img:1) is not instrumented: "
            "container img-c holds no coverage paths (not instrumented). Build it with "
            "coverage instrumentation (--coverage)"
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_declared_instrumented_false_is_not_filtered_by(self, tmp_path, monkeypatch):
        project = _init_git_repo(tmp_path)
        core = product_build(project, "core", ["src/core/add.c"])
        # Declared uninstrumented and never built: not validated, so no refusal.
        gen = FakeProduct("gen", artifact=str(project / "build/gen/gen.bin"))
        gen.instrumented_override = False
        install_lab(monkeypatch, [FakeHost("h1", [core, gen])])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        install_capture(monkeypatch)

        store = await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert "src/core/add.c" in _store_paths(store, project)

    @pytest.mark.asyncio
    async def test_unit_harvest_does_not_warn_on_unresolvable_system_headers(
        self, tmp_path, monkeypatch, caplog
    ):
        from tests.unit.cov._gcno import gcc13_gcno

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        write_sources(project, ["src/config.h"])
        hdir = project / "build/tests"
        unit_build(hdir, project, ["src/core/add.c"])
        # gtest's config.h, from an SDK this machine does not have; the project has one too.
        (hdir / "t.cpp.gcno").write_bytes(
            gcc13_gcno(str(project), [str(project / "src/core/add.c"), "/opt/sdk/config.h"])
        )
        (hdir / "t.cpp.gcda").write_bytes(b"")
        install_capture(monkeypatch)

        with caplog.at_level("DEBUG"):
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project))

        assert not [
            r for r in caplog.records if r.levelno == logging.WARNING and "config.h" in r.message
        ]
        assert "unit unit: drop /opt/sdk/config.h (not in any product)" in _messages(
            caplog, "DEBUG"
        )


def _tier(toolchain: dict | None = None, **extra) -> dict:
    tier = {"kind": "unit", "precedence": 1, "harvest_dirs": ["build/tests"], **extra}
    if toolchain is not None:
        tier["toolchain"] = toolchain
    return tier


def _toolchain_inputs(project: Path, toolchain: dict | None = None, **extra) -> ReportInputs:
    return _unit_inputs(project, {"tiers": {"unit": _tier(toolchain, **extra)}})


class TestUnitToolchain:
    """The unit tier's own ``toolchain`` setting over the build's stamps, resolved in the
    preflight; the capture only uses what it resolved."""

    @pytest.fixture
    def tools(self, tmp_path, monkeypatch) -> Path:
        """A ``PATH`` holding only stub tools (and git, which the project needs)."""
        bin_dir = stub_unit_tools(monkeypatch, tmp_path / "bin")
        git = shutil.which("git")
        assert git is not None
        (bin_dir / "git").symlink_to(git)
        monkeypatch.setenv("PATH", str(bin_dir))
        return bin_dir

    @staticmethod
    def _project(tmp_path, monkeypatch, compiler: str = "gcc13") -> Path:
        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        unit_build(project / "build/tests", project, ["src/core/add.c"], compiler)
        return project

    # -- resolution --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_with_no_setting_the_stamp_and_path_choose(self, tmp_path, monkeypatch, tools):
        project = self._project(tmp_path, monkeypatch)
        calls = install_capture(monkeypatch)

        await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert [c["toolchain"] for c in calls] == [
            Toolchain(gcov=tools / "gcov", lcov=tools / "lcov")
        ]

    @pytest.mark.asyncio
    async def test_a_declared_local_hosts_toolchain_is_ignored(self, tmp_path, monkeypatch, tools):
        project = _init_git_repo(tmp_path)
        core = product_build(project, "core", ["src/core/add.c"])
        local = Toolchain(gcov=Path("usr/bin/gcov-12"), lcov_args=["--rc", "x=1"])
        install_lab(monkeypatch, [FakeHost("local", [], local), FakeHost("h1", [core])])
        unit_build(project / "build/tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)

        await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert [c["toolchain"] for c in calls] == [
            Toolchain(gcov=tools / "gcov", lcov=tools / "lcov")
        ]

    @pytest.mark.asyncio
    async def test_only_lcov_args_on_a_clang_directory_keeps_llvm_cov_discovery(
        self, tmp_path, monkeypatch, tools
    ):
        llvm_cov = stub_tool(tools, "llvm-cov")
        project = self._project(tmp_path, monkeypatch, "clang")
        calls = install_capture(monkeypatch)
        args = ["--rc", "geninfo_unexecuted_blocks=1"]

        await run_coverage_report(
            [], tmp_path / "report", _toolchain_inputs(project, {"lcov_args": args})
        )

        assert [c["toolchain"] for c in calls] == [
            Toolchain(gcov=llvm_cov, lcov=tools / "lcov", lcov_args=args)
        ]

    @pytest.mark.asyncio
    async def test_only_lcov_is_a_project_root_path_from_any_cwd(
        self, tmp_path, monkeypatch, tools
    ):
        project = self._project(tmp_path, monkeypatch)
        my_lcov = stub_tool(project / "tools", "my-lcov")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        calls = install_capture(monkeypatch)

        await run_coverage_report(
            [], tmp_path / "report", _toolchain_inputs(project, {"lcov": "tools/my-lcov"})
        )

        assert [c["toolchain"] for c in calls] == [Toolchain(gcov=tools / "gcov", lcov=my_lcov)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("written", ["./gcov", "~/bin/gcov", "gcov-13"])
    async def test_a_value_is_a_path_with_a_slash_and_a_command_without(
        self, tmp_path, monkeypatch, tools, written
    ):
        project = self._project(tmp_path, monkeypatch)
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        expected = {
            "./gcov": stub_tool(project, "gcov"),
            "~/bin/gcov": stub_tool(home / "bin", "gcov"),
            "gcov-13": stub_tool(tools, "gcov-13"),
        }[written]
        calls = install_capture(monkeypatch)

        await run_coverage_report(
            [], tmp_path / "report", _toolchain_inputs(project, {"coverage_tool": written})
        )

        assert calls[0]["toolchain"].gcov == expected

    @pytest.mark.asyncio
    async def test_one_setting_covers_a_plain_and_a_products_view(
        self, tmp_path, monkeypatch, tools
    ):
        reader = stub_tool(tools, "gcov-13")
        project = self._project(tmp_path, monkeypatch)
        unit_build(project / "build/core-tests", project, ["src/core/add.c"])
        calls = install_capture(monkeypatch)
        inputs = _toolchain_inputs(
            project, {"coverage_tool": "gcov-13"}, products={"core": ["build/core-tests"]}
        )

        await run_coverage_report([], tmp_path / "report", inputs)

        assert [c["directory"].name for c in calls] == ["tests", "core-tests"]
        assert [c["toolchain"].gcov for c in calls] == [reader, reader]

    @pytest.mark.asyncio
    async def test_two_tiers_run_their_own_llvm_cov(self, tmp_path, monkeypatch, tools):
        llvm17 = stub_tool(tools / "llvm-17", "llvm-cov")
        llvm18 = stub_tool(tools / "llvm-18", "llvm-cov")
        project = self._project(tmp_path, monkeypatch, "clang")
        unit_build(project / "build/other", project, ["src/core/add.c"], "clang")
        calls = install_capture(monkeypatch)
        cov = {
            "tiers": {
                "unit": _tier({"coverage_tool": str(llvm17)}),
                "unit2": {
                    "kind": "unit",
                    "precedence": 2,
                    "harvest_dirs": ["build/other"],
                    "toolchain": {"coverage_tool": str(llvm18)},
                },
            }
        }

        await run_coverage_report([], tmp_path / "report", _unit_inputs(project, cov))

        assert [c["toolchain"].gcov for c in calls] == [llvm17, llvm18]

    @pytest.mark.asyncio
    async def test_a_gcc_build_of_another_major_reads_with_its_gcov(
        self, tmp_path, monkeypatch, tools, caplog
    ):
        gcov_12 = stub_tool(tools, "gcov-12")
        project = self._project(tmp_path, monkeypatch, "gcc12")
        calls = install_capture(monkeypatch)

        with caplog.at_level("INFO"):
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert [c["toolchain"].gcov for c in calls] == [gcov_12]
        assert f"unit unit: gcov stamp 'B24*' is gcc 12's; reading counters with {gcov_12}" in (
            _messages(caplog, "INFO")
        )

    # -- refusals before the destination ------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "written"),
        [("coverage_tool", "gcov-99"), ("lcov", "no-lcov"), ("coverage_tool", "tools/gcov")],
    )
    async def test_a_missing_configured_tool_is_an_input_error_before_anything(
        self, tmp_path, monkeypatch, tools, field, written
    ):
        from otto.coverage.errors import CoverageInputError

        project = self._project(tmp_path, monkeypatch)
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report(
                [], tmp_path / "report", _toolchain_inputs(project, {field: written})
            )

        assert str(excinfo.value).startswith(f"coverage.tiers.unit.toolchain.{field} = {written!r}")
        assert calls == []
        assert not (tmp_path / "report").exists()

    @pytest.mark.asyncio
    async def test_a_configured_tool_is_checked_though_its_directory_is_skipped(
        self, tmp_path, monkeypatch, tools
    ):
        from otto.coverage.errors import CoverageInputError

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])  # no unit build at all

        with pytest.raises(CoverageInputError, match=r"toolchain\.coverage_tool = 'gcov-99'"):
            await run_coverage_report(
                [], tmp_path / "report", _toolchain_inputs(project, {"coverage_tool": "gcov-99"})
            )

    @pytest.mark.asyncio
    async def test_a_configured_tool_is_checked_on_a_tier_with_no_directories(
        self, tmp_path, monkeypatch, tools
    ):
        from otto.coverage.errors import CoverageInputError

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c"])
        cov = {
            "tiers": {"unit": {"kind": "unit", "precedence": 1, "toolchain": {"lcov": "no-lcov"}}}
        }

        with pytest.raises(CoverageInputError, match=r"toolchain\.lcov = 'no-lcov'"):
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project, cov))

        assert not (tmp_path / "report").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("missing", "key"), [("gcov", "coverage_tool"), ("lcov", "lcov")])
    async def test_a_missing_default_tool_fails_before_the_destination(
        self, tmp_path, monkeypatch, tools, missing, key
    ):
        project = self._project(tmp_path, monkeypatch)
        # The system gcov reads gcc 13 data: the stamp asks for the default `gcov`.
        monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda: 13)
        (tools / missing).unlink()
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageToolMissingError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        message = str(excinfo.value)
        assert message == (
            f"unit tier unit: capturing {project / 'build/tests'} needs `{missing}`, but no "
            f"`{missing}` is on PATH. Install it, or set `coverage.tiers.unit.toolchain.{key}`."
        )
        assert calls == []
        assert not (tmp_path / "report").exists()

    @pytest.mark.asyncio
    async def test_a_failed_probe_reads_every_gcc_build_by_its_versioned_gcov(
        self, tmp_path, monkeypatch, tools
    ):
        """As for e2e: with no system gcov to ask, gcc 13 data is read by `gcov-13`."""
        gcov_13 = stub_tool(tools, "gcov-13")
        (tools / "gcov").unlink()
        project = self._project(tmp_path, monkeypatch)
        calls = install_capture(monkeypatch)

        await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert [c["toolchain"].gcov for c in calls] == [gcov_13]

    @pytest.mark.asyncio
    async def test_the_system_gcov_is_probed_once_per_report(self, tmp_path, monkeypatch, tools):
        probes: list[str] = []

        def probe(gcov="gcov"):
            probes.append(gcov)
            return 13

        monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", probe)
        project = self._project(tmp_path, monkeypatch)
        unit_build(project / "build/core-tests", project, ["src/core/add.c"])
        install_capture(monkeypatch)

        await run_coverage_report(
            [],
            tmp_path / "report",
            _toolchain_inputs(project, products={"core": ["build/core-tests"]}),
        )

        assert probes == ["gcov"]

    @pytest.mark.asyncio
    async def test_a_missing_major_names_the_tier_and_never_a_host(
        self, tmp_path, monkeypatch, tools
    ):
        project = self._project(tmp_path, monkeypatch, "gcc12")

        with pytest.raises(CoverageToolMissingError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        message = str(excinfo.value)
        assert f"unit tier unit (directory {project / 'build/tests'})" in message
        assert "`coverage.tiers.unit.toolchain.coverage_tool`" in message
        assert "host" not in message
        assert not (tmp_path / "report").exists()

    # -- the family check ---------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("compiler", "reader", "proves"),
        [
            ("gcc13", "llvm-cov", "an llvm-cov reads only gcov stamps 402*/408*/B11*"),
            ("clang", "gcov-13", "a GNU gcov cannot read gcov stamps 402*/408*"),
            ("clang", "arm-none-eabi-gcov", "a GNU gcov cannot read gcov stamps 402*/408*"),
            ("clang", "x86_64-linux-gnu-gcov-13", "a GNU gcov cannot read gcov stamps 402*/408*"),
        ],
    )
    async def test_a_reader_whose_name_contradicts_the_stamps_is_refused(
        self, tmp_path, monkeypatch, tools, compiler, reader, proves
    ):
        from otto.coverage.errors import CoverageInputError

        stub_tool(tools, reader)
        project = self._project(tmp_path, monkeypatch, compiler)
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report(
                [], tmp_path / "report", _toolchain_inputs(project, {"coverage_tool": reader})
            )

        message = str(excinfo.value)
        assert message.startswith(
            f"coverage.tiers.unit.toolchain.coverage_tool = {reader!r} cannot read "
            f"{project / 'build/tests'}: {proves}, and {project / 'build/tests/add.c.gcno'} is "
        )
        assert calls == []
        assert not (tmp_path / "report").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["llvm-gcov.sh", "llvm-gcov", "llvm-gcov-18", "x-llvm-gcov"])
    async def test_a_wrapper_name_proves_nothing_and_passes(
        self, tmp_path, monkeypatch, tools, name
    ):
        """``llvm-gcov`` is the usual name of a clang reader wrapper: ``llvm`` in a name never
        proves the GNU family."""
        project = self._project(tmp_path, monkeypatch, "clang")
        wrapper = stub_tool(project / "tools", name)
        calls = install_capture(monkeypatch)

        await run_coverage_report(
            [],
            tmp_path / "report",
            _toolchain_inputs(project, {"coverage_tool": f"tools/{name}"}),
        )

        assert [c["toolchain"].gcov for c in calls] == [wrapper]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", ["clang", "gcc12"])
    @pytest.mark.parametrize("toolchain", [None, {"coverage_tool": "tools/wrap.sh"}])
    async def test_a_directory_of_two_compilers_is_refused_whatever_the_setting(
        self, tmp_path, monkeypatch, tools, second, toolchain
    ):
        from otto.coverage.errors import CoverageInputError

        project = _init_git_repo(tmp_path)
        own_files(monkeypatch, project, ["src/core/add.c", "src/core/sub.c"])
        stub_tool(project / "tools", "wrap.sh")
        hdir = project / "build/tests"
        unit_build(hdir, project, ["src/core/add.c"])
        unit_build(hdir, project, ["src/core/sub.c"], second)
        calls = install_capture(monkeypatch)

        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report(
                [], tmp_path / "report", _toolchain_inputs(project, toolchain)
            )

        theirs = (
            "llvm-cov's (gcov stamp '408*')"
            if second == "clang"
            else "gcc 12's (gcov stamp 'B24*')"
        )
        assert str(excinfo.value) == (
            f"unit tier unit: {hdir} holds notes for more than one reader — "
            f"{hdir / 'add.c.gcno'} is gcc 13's (gcov stamp 'B33*') and {hdir / 'sub.c.gcno'} "
            f"is {theirs}. No single reader decodes both; build them into separate directories."
        )
        assert calls == []

    # -- shell-safe paths -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_configured_tool_lcov_cannot_run_is_an_input_error(
        self, tmp_path, monkeypatch, tools
    ):
        from otto.coverage.errors import CoverageInputError

        project = self._project(tmp_path, monkeypatch)
        spaced = tmp_path / "my tools"
        stub_tool(spaced, "gcov-15")
        monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{spaced}")

        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report(
                [], tmp_path / "report", _toolchain_inputs(project, {"coverage_tool": "gcov-15"})
            )

        assert str(excinfo.value) == (
            f"coverage.tiers.unit.toolchain.coverage_tool = 'gcov-15' resolves to "
            f"{spaced / 'gcov-15'}, which lcov cannot run: it contains white space or a control "
            "character (' '). Name a path without white space or shell metacharacters."
        )

    @pytest.mark.asyncio
    async def test_a_default_tool_lcov_cannot_run_is_a_missing_tool(
        self, tmp_path, monkeypatch, tools
    ):
        project = self._project(tmp_path, monkeypatch)
        spaced = tmp_path / "my tools"
        (tools / "lcov").rename(spaced.mkdir() or spaced / "lcov")
        monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{spaced}")

        with pytest.raises(CoverageToolMissingError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert str(excinfo.value).startswith(
            f"unit tier unit: the tool found on PATH at {spaced / 'lcov'} cannot be run by lcov"
        )
        assert "`coverage.tiers.unit.toolchain.lcov`" in str(excinfo.value)
        assert not (tmp_path / "report").exists()

    # -- after the capture ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_capture_that_records_nothing_fails(self, tmp_path, monkeypatch, tools):
        """A reader of the wrong family that no name revealed: lcov reports success, empty."""
        from otto.coverage.errors import CaptureFailedError
        from otto.coverage.merge import merger as merger_mod

        project = self._project(tmp_path, monkeypatch)
        stub_tool(project / "tools", "wrap.sh")

        async def empty_capture(self, directory, output, toolchain=None, includes=None):
            output.write_text("")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", empty_capture)

        with pytest.raises(CaptureFailedError) as excinfo:
            await run_coverage_report(
                [],
                tmp_path / "report",
                _toolchain_inputs(project, {"coverage_tool": "tools/wrap.sh"}),
            )

        message = str(excinfo.value)
        assert message.startswith(
            f"unit tier unit: the capture of {project / 'build/tests'} with "
            f"{project / 'tools/wrap.sh'} recorded no source file, though 1 product source"
        )
        assert "`coverage.tiers.unit.toolchain.coverage_tool`" in message

    @pytest.mark.asyncio
    async def test_a_failed_capture_names_the_view_and_the_tier_setting(
        self, tmp_path, monkeypatch, tools
    ):
        from otto.coverage.errors import CaptureFailedError
        from otto.coverage.merge import merger as merger_mod

        project = self._project(tmp_path, monkeypatch)

        async def failing_capture(self, directory, output, toolchain=None, includes=None):
            raise CaptureFailedError("lcov --capture failed:\ngeninfo: ERROR: no data")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", failing_capture)

        with pytest.raises(CaptureFailedError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        assert str(excinfo.value) == (
            f"unit tier unit: capturing {project / 'build/tests'} failed; if the counter reader "
            "is the wrong one, set `coverage.tiers.unit.toolchain.coverage_tool`.\n"
            "lcov --capture failed:\ngeninfo: ERROR: no data"
        )

    @pytest.mark.asyncio
    async def test_a_version_mismatch_names_the_tier_setting(self, tmp_path, monkeypatch, tools):
        from otto.coverage.errors import CoverageToolVersionError
        from otto.coverage.merge import merger as merger_mod

        project = self._project(tmp_path, monkeypatch)

        async def refusing_capture(self, directory, output, toolchain=None, includes=None):
            raise CoverageToolVersionError("geninfo: Incompatible GCC/GCOV version")

        monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", refusing_capture)

        with pytest.raises(CoverageToolVersionError) as excinfo:
            await run_coverage_report([], tmp_path / "report", _toolchain_inputs(project))

        message = str(excinfo.value)
        assert "set `coverage.tiers.unit.toolchain.coverage_tool` to an llvm-cov path" in message
        assert "If the tested code was built with clang --coverage" in message
        assert "host" not in message
        assert "product" not in message
        assert message.endswith("geninfo: Incompatible GCC/GCOV version")


def _write_meta(cov: Path, project: Path) -> None:
    (cov / ".otto_cov_meta.json").write_text(
        json.dumps(
            {
                "project_name": "proj",
                "project_root": str(project),
                "tier": "system",
                "base_commit": head_commit(project),
            }
        )
    )


def _spy_produce(monkeypatch) -> list[dict]:
    from otto.coverage.capture.produce import ProduceResult

    calls: list[dict] = []

    async def fake(cov_dir, **kwargs):
        calls.append({"cov_dir": cov_dir, **kwargs})
        return ProduceResult([])

    monkeypatch.setattr("otto.coverage.capture.produce.produce_captures", fake)
    return calls


def _needs(*tools: str) -> pytest.MarkDecorator:
    missing = [t for t in tools if shutil.which(t) is None]
    return pytest.mark.skipif(bool(missing), reason=f"not installed: {', '.join(missing)}")


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


_ADD_C = "int add(int a) { return a + 1; }\n"
_TEST_ADD_C = "int add(int);\nint main(void) { return add(1) == 2 ? 0 : 1; }\n"


def _c_unit_project(tmp_path: Path, cc: str) -> Path:
    """Build a C unit test beside a product build of the same source, both with *cc*."""
    project = _init_git_repo(tmp_path)
    (project / "src/core").mkdir(parents=True)
    (project / "src/core/add.c").write_text(_ADD_C)
    (project / "tests").mkdir()
    (project / "tests/test_add.c").write_text(_TEST_ADD_C)
    unit = project / "build" / "unit"
    unit.mkdir(parents=True)
    _run([cc, "--coverage", "-c", "../../src/core/add.c", "-o", "add.c.o"], unit)
    _run([cc, "--coverage", "-c", "../../tests/test_add.c", "-o", "test_add.c.o"], unit)
    _run([cc, "--coverage", "add.c.o", "test_add.c.o", "-o", "unit_tests"], unit)
    _run(["./unit_tests"], unit)
    prod = project / "build" / "prod"
    prod.mkdir()
    _run([cc, "--coverage", "-c", "../../src/core/add.c", "-o", "add.c.o"], prod)
    return project


_REAL_UNIT_COV = {
    "tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["build/unit"]}}
}


class TestUnitHarvestRealTools:
    """The unit capture in place with the real lcov, gcov and compilers."""

    @_needs("lcov", "clang", "llvm-cov")
    @pytest.mark.asyncio
    async def test_real_clang_unit_capture_in_place(self, tmp_path, monkeypatch):
        """The built-in ``local``: the notes' clang stamp picks ``llvm-cov`` over ``gcov``."""
        project = _c_unit_project(tmp_path, "clang")
        core = FakeProduct("core", artifact=str(project / "build/prod/add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])

        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, _REAL_UNIT_COV)
        )

        raw = (tmp_path / "report/_work/unit_unit_0.raw.info").read_text()
        assert "SF:" in raw
        assert "test_add.c" not in raw
        assert _store_paths(store, project) == ["src/core/add.c"]

    @_needs("lcov", "gcc", "gcov")
    @pytest.mark.asyncio
    async def test_real_gcc_unit_capture_in_place(self, tmp_path, monkeypatch):
        project = _c_unit_project(tmp_path, "gcc")
        core = FakeProduct("core", artifact=str(project / "build/prod/add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])

        store = await run_coverage_report(
            [], tmp_path / "report", _unit_inputs(project, _REAL_UNIT_COV)
        )

        raw = (tmp_path / "report/_work/unit_unit_0.raw.info").read_text()
        assert "SF:" in raw
        assert "test_add.c" not in raw
        assert _store_paths(store, project) == ["src/core/add.c"]

    @_needs("lcov", "gcc", "gcov")
    @pytest.mark.asyncio
    async def test_real_gcc_of_another_major_reads_with_its_gcov(
        self, tmp_path, monkeypatch, caplog
    ):
        from otto.host.toolchain_discovery import system_gcov_major

        system = system_gcov_major()
        other = next((m for m in range(9, 20) if m != system and shutil.which(f"gcc-{m}")), None)
        if other is None or shutil.which(f"gcov-{other}") is None:
            pytest.skip("no second gcc with its gcov installed")
        project = _c_unit_project(tmp_path, f"gcc-{other}")
        core = FakeProduct("core", artifact=str(project / "build/prod/add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])

        with caplog.at_level("INFO"):
            store = await run_coverage_report(
                [], tmp_path / "report", _unit_inputs(project, _REAL_UNIT_COV)
            )

        assert _store_paths(store, project) == ["src/core/add.c"]
        gcov = shutil.which(f"gcov-{other}")
        assert any(
            m.startswith("unit unit: gcov stamp ") and m.endswith(f"reading counters with {gcov}")
            for m in _messages(caplog, "INFO")
        )

    @_needs("lcov", "gcc", "gcov", "llvm-cov")
    @pytest.mark.asyncio
    async def test_real_llvm_cov_over_gcc_data_is_refused_in_the_preflight(
        self, tmp_path, monkeypatch
    ):
        """lcov itself would report success with an empty tracefile here."""
        from otto.coverage.errors import CoverageInputError

        project = _c_unit_project(tmp_path, "gcc")
        core = FakeProduct("core", artifact=str(project / "build/prod/add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])
        cov = {"tiers": {"unit": {**_REAL_UNIT_COV["tiers"]["unit"]}}}
        cov["tiers"]["unit"]["toolchain"] = {"coverage_tool": "llvm-cov"}

        with pytest.raises(CoverageInputError, match="an llvm-cov reads only gcov stamps"):
            await run_coverage_report([], tmp_path / "report", _unit_inputs(project, cov))

        assert not (tmp_path / "report").exists()

    @_needs("lcov", "clang", "llvm-cov")
    @pytest.mark.asyncio
    async def test_real_clang_with_only_lcov_args_into_a_spaced_output_dir(
        self, tmp_path, monkeypatch
    ):
        """The overlay keeps llvm-cov discovery; its wrapper lives outside the output dir,
        whose space would otherwise break geninfo's shell."""
        project = _c_unit_project(tmp_path, "clang")
        core = FakeProduct("core", artifact=str(project / "build/prod/add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])
        cov = {"tiers": {"unit": {**_REAL_UNIT_COV["tiers"]["unit"]}}}
        cov["tiers"]["unit"]["toolchain"] = {"lcov_args": ["--rc", "geninfo_unexecuted_blocks=1"]}
        out = tmp_path / "my report"
        from otto.host.local_host import LocalHost

        commands: list[str] = []
        real_exec = LocalHost.exec

        async def recording_exec(self, cmd, *args, **kwargs):
            commands.append(cmd)
            return await real_exec(self, cmd, *args, **kwargs)

        monkeypatch.setattr(LocalHost, "exec", recording_exec)

        store = await run_coverage_report([], out, _unit_inputs(project, cov))

        assert _store_paths(store, project) == ["src/core/add.c"]
        (capture,) = [c for c in commands if "--capture" in c]
        assert " --rc geninfo_unexecuted_blocks=1 " in capture
        assert "SF:" in (out / "_work/unit_unit_0.raw.info").read_text()
        assert not list(out.rglob("llvm-gcov-*"))

    @_needs("lcov", "gcc", "g++", "gcov")
    @pytest.mark.skipif(not Path("/usr/include/gtest").is_dir(), reason="gtest not installed")
    @pytest.mark.asyncio
    async def test_gtest_file_never_reaches_geninfo(self, tmp_path, monkeypatch, caplog):
        from otto.coverage.errors import CaptureFailedError
        from otto.coverage.merge.merger import LcovMerger
        from otto.host.local_host import LocalHost

        project = _init_git_repo(tmp_path)
        (project / "src/core").mkdir(parents=True)
        (project / "src/core/add.c").write_text(_ADD_C)
        (project / "tests").mkdir()
        # A value-parameterised test: lcov 2 refuses gtest's expansion of it
        # ("mismatched exception tag") when the file reaches geninfo.
        (project / "tests/test_add.cpp").write_text(
            "#include <gtest/gtest.h>\n"
            'extern "C" int add(int);\n'
            "TEST(Add, One) { EXPECT_EQ(add(1), 2); }\n"
            "class AddP : public ::testing::TestWithParam<int> {};\n"
            "TEST_P(AddP, Plus) { EXPECT_EQ(add(GetParam()), GetParam() + 1); }\n"
            "INSTANTIATE_TEST_SUITE_P(Vals, AddP, ::testing::Values(1, 2, 3));\n"
        )
        unit = project / "build" / "unit"
        unit.mkdir(parents=True)
        _run(["gcc", "--coverage", "-c", "../../src/core/add.c", "-o", "add.c.o"], unit)
        _run(["g++", "--coverage", "-c", "../../tests/test_add.cpp", "-o", "test_add.cpp.o"], unit)
        _run(
            [
                *["g++", "--coverage", "add.c.o", "test_add.cpp.o"],
                *["-lgtest", "-lgtest_main", "-pthread", "-o", "unit_tests"],
            ],
            unit,
        )
        _run(["./unit_tests"], unit)
        prod = project / "build" / "prod"
        prod.mkdir()
        _run(["gcc", "--coverage", "-c", "../../src/core/add.c", "-o", "add.c.o"], prod)

        # (a) unfiltered, geninfo fails on the gtest file
        localhost = LocalHost()
        try:
            with pytest.raises(CaptureFailedError):
                await LcovMerger(localhost).capture_dir(unit, tmp_path / "unfiltered.info")
        finally:
            await localhost.close()

        # (b) the filtered unit harvest never hands it to geninfo
        core = FakeProduct("core", artifact=str(prod / "add.c.o"))
        install_lab(monkeypatch, [FakeHost("h1", [core])])
        with caplog.at_level("DEBUG"):
            store = await run_coverage_report(
                [], tmp_path / "report", _unit_inputs(project, _REAL_UNIT_COV)
            )

        assert _store_paths(store, project) == ["src/core/add.c"]
        debug = _messages(caplog, "DEBUG")
        assert "unit unit: drop tests/test_add.cpp (not in any product)" in debug
        assert any(m.startswith("unit unit: drop /usr/include/gtest/") for m in debug)


@pytest.mark.asyncio
async def test_duplicate_capture_across_sources_registers_one_run(tmp_path):
    """The same capture in a cov dir AND the manual store folds in once.

    Dedupe key: (tier, base_commit, board, product, captured_at). The manual-store copy
    (validity-aware anchor chain) wins because its run key is pre-seeded
    into the dedupe set before the cov-dir captures fold.
    """
    from otto.coverage.capture.gitio import blob_sha, head_commit
    from otto.coverage.capture.model import Capture, CaptureFileCov
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import CollectionInputs, CoverageReporter
    from otto.coverage.tiers import load_tiers

    project = _init_git_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"

    cap = Capture(
        tier="manual",
        product="app",
        base_commit=head_commit(project),
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(project, Path("f.c")), lines={1: 2})},
    )
    write_manual_capture(cap, project)  # manual-store copy
    dup = tmp_path / "cov" / "b1" / "capture.json"
    dup.parent.mkdir(parents=True)
    cap.save(dup)  # cov-dir copy of the SAME run

    cov = {"tiers": {"manual": {"kind": "manual", "precedence": 1}}}
    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        collection=CollectionInputs(
            project_root=project,
            tier_configs=load_tiers(cov),
            capture_paths=[dup],
        ),
    )
    store = await reporter.run()

    assert len(store.runs) == 1  # deduped to one run
    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    assert fr.lines[1].hits.for_tier("manual") == 2  # folded once, not twice
    assert fr.lines[1].run_hits == {0: 2}


@pytest.mark.asyncio
async def test_explicit_info_tier_gets_synthetic_run(tmp_path):
    from otto.coverage.reporter import CoverageReporter

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.c").write_text("int a;\n")
    info = tmp_path / "u.info"
    info.write_text(f"TN:\nSF:{src / 'f.c'}\nDA:1,7\nend_of_record\n")

    reporter = CoverageReporter(src, tmp_path / "report", tiers=[("unit", info)])
    store = await reporter.run()

    (rec,) = store.runs
    assert (rec.tier, rec.label, rec.base_commit) == ("unit", "unit", "")
    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    assert fr.lines[1].run_hits == {rec.id: 7}


async def _runs_for_captures(tmp_path: Path, contexts: list[tuple[str, str]]):
    """Fold one capture per ``(board, product)`` at HEAD and return the run table."""
    project = _init_git_repo(tmp_path)
    head = head_commit(project)
    cov = tmp_path / "out" / "cov"
    capture_paths = []
    for board, product in contexts:
        cap = Capture(
            tier="system",
            product=product,
            base_commit=head,
            captured_at="2026-07-01T00:00:00Z",
            board=board,
            files={"f.c": CaptureFileCov(lines={2: 7})},
        )
        path = cov / board / product / "capture.json"
        cap.save(path)
        capture_paths.append(path)

    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        collection=CollectionInputs(
            project_root=project,
            tier_configs=load_tiers(_BASE_COMMIT_GUARD_COV),
            capture_paths=capture_paths,
        ),
    )
    store = await reporter.run()
    return store.runs


@pytest.mark.asyncio
async def test_two_products_on_one_host_are_two_runs(tmp_path):
    """``_run_key`` carries ``product``: two captures identical but for the
    product are two runs, not one deduped run."""
    runs = await _runs_for_captures(tmp_path, [("h1", "agent"), ("h1", "app")])
    assert [(r.host, r.product) for r in runs] == [("h1", "agent"), ("h1", "app")]


@pytest.mark.asyncio
async def test_two_hosts_sharing_a_product_name_are_two_runs(tmp_path):
    """The host component still discriminates: one product name on two hosts
    is two runs."""
    runs = await _runs_for_captures(tmp_path, [("h1", "app"), ("h2", "app")])
    assert [(r.host, r.product) for r in runs] == [("h1", "app"), ("h2", "app")]


@pytest.mark.asyncio
async def test_e2e_hit_suppresses_manual_stale_mark_same_line(tmp_path):
    """Fold-order regression (manual folds LAST): a manual capture whose
    covered line was edited since its base_commit goes stale under the anchor chain,
    but when an e2e capture at the current HEAD also covers that exact line,
    the line itself must NOT read as stale — only the manual run's own
    evidence is recorded as revoked (``stale_runs``), while the e2e
    run's hit is credited normally. Folding manual first (reading the
    stale guard before the e2e hit lands) would incorrectly stale the line.
    """
    from otto.coverage.capture.gitio import blob_sha, head_commit
    from otto.coverage.capture.model import Capture, CaptureFileCov
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import CollectionInputs, CoverageReporter
    from otto.coverage.tiers import load_tiers

    project = _init_git_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"
    head1 = head_commit(project)

    manual_cap = Capture(
        tier="manual",
        product="app",
        base_commit=head1,
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(project, Path("f.c")), lines={3: 1})},
    )
    write_manual_capture(manual_cap, project)

    # Edit + commit line 3 -> stales the manual capture's coverage of it
    # under the blob/diff anchor chain.
    (project / "f.c").write_text("int a;\nint b;\nint CHANGED;\n")
    _git(project, tmp_path, "commit", "-aqm", "edit")
    head2 = head_commit(project)

    # An e2e capture anchored at the new HEAD covers the same (now-current)
    # line 3.
    e2e_cap = Capture(
        tier="system",
        product="app",
        base_commit=head2,
        captured_at="2026-07-02T00:00:00Z",
        board="b2",
        files={"f.c": CaptureFileCov(lines={3: 4})},
    )
    e2e_path = tmp_path / "out" / "cov" / "b2" / "capture.json"
    e2e_cap.save(e2e_path)

    cov_config = {
        "tiers": {
            "manual": {"kind": "manual", "precedence": 1},
            "system": {"kind": "e2e", "precedence": 2},
        }
    }
    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        collection=CollectionInputs(
            project_root=project,
            tier_configs=load_tiers(cov_config),
            capture_paths=[e2e_path],
        ),
    )
    store = await reporter.run()

    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    line3 = fr.lines[3]
    manual_run = next(c.id for c in store.runs if c.tier == "manual")
    e2e_run = next(c.id for c in store.runs if c.tier == "system")

    assert line3.state is None  # e2e hit suppresses the stale mark
    assert line3.stale_runs == [manual_run]  # manual's own evidence still revoked
    assert line3.run_hits.get(e2e_run) == 4  # e2e run credited


@pytest.mark.asyncio
async def test_duplicate_survives_stale_base_commit_via_key_hoist(tmp_path):
    """The dedupe guarantee the key-hoist design advertises: a cov-dir
    duplicate of an already-committed manual run is skipped by *key* before
    the e2e base_commit guard ever runs against it — even once the checkout has
    moved past the duplicate's base_commit. Without cross-source dedupe this raised
    ``CaptureCommitError``; the manual anchor-chain copy stays the
    sole source of truth for the run.
    """
    from otto.coverage.capture.gitio import blob_sha, head_commit
    from otto.coverage.capture.model import Capture, CaptureFileCov
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import CollectionInputs, CoverageReporter
    from otto.coverage.tiers import load_tiers

    project = _init_git_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"
    head1 = head_commit(project)

    cap = Capture(
        tier="manual",
        product="app",
        base_commit=head1,
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(project, Path("f.c")), lines={1: 2})},
    )
    write_manual_capture(cap, project)  # committed manual-store copy, anchored to head1

    dup = tmp_path / "out" / "cov" / "b1" / "capture.json"
    cap.save(dup)  # verbatim cov-dir duplicate of the same run

    # Advance the checkout past head1 -- an unrelated commit so the duplicate's
    # base_commit no longer matches HEAD, but f.c's blob is untouched.
    (project / "unrelated.txt").write_text("x\n")
    _git(project, tmp_path, "add", "unrelated.txt")
    _git(project, tmp_path, "commit", "-qm", "unrelated")
    head2 = head_commit(project)
    assert head2 != head1

    cov_config = {"tiers": {"manual": {"kind": "manual", "precedence": 1}}}
    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        collection=CollectionInputs(
            project_root=project,
            tier_configs=load_tiers(cov_config),
            capture_paths=[dup],
        ),
    )
    # Pre-fix (load-order dedupe): this would raise CaptureCommitError
    # because the cov-dir duplicate is folded via _load_captures against the
    # now-stale base_commit before the manual copy ever gets a chance to dedupe it.
    store = await reporter.run()

    assert len(store.runs) == 1  # deduped to exactly one run
    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    # f.c's blob is unchanged across the unrelated commit -> anchor fast
    # path -> valid, not stale.
    assert fr.lines[1].hits.for_tier("manual") == 2
    assert fr.lines[1].state is None


@pytest.mark.asyncio
async def test_superseded_covdir_duplicate_still_skipped(tmp_path):
    """Same shape as ``test_duplicate_survives_stale_base_commit_via_key_hoist``,
    plus ONE extra committed re-capture of the same (tier, label, host)
    context. Supersede drops the old committed capture from the winners
    list (:func:`select_manual_captures`), so seeding ``seen_runs`` from
    winners alone never recognizes the old run's key: the cov-dir copy of
    the superseded capture then reaches the e2e ``base_commit`` guard and
    raises. ``seen_runs`` must be seeded from *every* committed manual
    capture, not just the winners, so a stale cov-dir duplicate is
    skipped by key regardless of supersede's outcome.
    """
    from otto.coverage.capture.gitio import blob_sha, head_commit
    from otto.coverage.capture.model import Capture, CaptureFileCov
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import CollectionInputs, CoverageReporter
    from otto.coverage.tiers import load_tiers

    project = _init_git_repo(tmp_path)  # f.c: "int a;\nint b;\nint c;\n"
    head1 = head_commit(project)

    cap_old = Capture(
        tier="manual",
        product="app",
        base_commit=head1,
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(project, Path("f.c")), lines={1: 2})},
    )
    write_manual_capture(cap_old, project)  # committed manual-store copy, anchored to head1

    dup = tmp_path / "out" / "cov" / "b1" / "capture.json"
    cap_old.save(dup)  # verbatim cov-dir duplicate of the OLD run

    # Advance the checkout past head1.
    (project / "unrelated.txt").write_text("x\n")
    _git(project, tmp_path, "add", "unrelated.txt")
    _git(project, tmp_path, "commit", "-qm", "unrelated")
    head2 = head_commit(project)
    assert head2 != head1

    # Re-capture the SAME context (tier=manual, label falls back to board=b1)
    # at head2, newer captured_at -> supersedes cap_old.
    cap_new = cap_old.model_copy(
        update={"base_commit": head2, "captured_at": "2026-07-02T00:00:00Z"}
    )
    write_manual_capture(cap_new, project)

    cov_config = {"tiers": {"manual": {"kind": "manual", "precedence": 1}}}
    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        collection=CollectionInputs(
            project_root=project,
            tier_configs=load_tiers(cov_config),
            capture_paths=[dup],
        ),
    )
    # Expected (intent of the key-hoist + supersede): the cov-dir copy of the
    # superseded run is skipped; report runs and folds only cap_new.
    store = await reporter.run()
    assert len(store.runs) == 1


# ── Task 7: CoverageReporter ticket attribution wiring ───────────────────────


def _git_repo_with_ticket_commit(tmp_path: Path) -> Path:
    """Create a one-commit git repository whose commit message names ticket PROJ-7."""
    project = tmp_path / "ticket_sut"
    project.mkdir()
    _git(project, tmp_path, "init", "-q")
    (project / "a.c").write_text("one\ntwo\n")
    _git(project, tmp_path, "add", "-A")
    _git(project, tmp_path, "commit", "-q", "-m", "seed PROJ-7")
    return project


def _store_with_lines(project: Path):
    """A minimal store with one file (project/a.c) and two known line numbers."""
    from otto.coverage.store.model import CoverageStore

    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(project / "a.c")
    record.get_or_create_line(1)
    record.get_or_create_line(2)
    return store


def _reporter_with(**kwargs) -> CoverageReporter:
    """Build a CoverageReporter with the constructor's minimum required args.

    Mirrors ``TestCoverageReporter.test_run_empty_dirs`` (the nearest
    existing ``CoverageReporter(`` construction in this module) — the
    placeholder source_root/output_dir are never touched by
    ``_annotate_tickets``, so *kwargs* only needs to override what a given
    test cares about (here, ``ticket_spec``).
    """
    defaults: dict[str, object] = {
        "source_root": Path("/nonexistent/source"),
        "output_dir": Path("/nonexistent/out"),
    }
    defaults.update(kwargs)
    return CoverageReporter(**defaults)


def test_reporter_annotates_lines_with_tickets(tmp_path):
    """With a ticket_spec configured, _annotate_tickets attributes both the
    per-line ticket ids and the store's ticket -> commits table."""
    from otto.coverage.tickets import build_ticket_spec

    project = _git_repo_with_ticket_commit(tmp_path)
    store = _store_with_lines(project)
    reporter = _reporter_with(ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None))

    reporter._annotate_tickets(store, project)

    assert next(store.files()).lines[1].ticket == ["PROJ-7"]
    assert store.tickets["PROJ-7"].commits


def test_reporter_without_spec_leaves_tickets_empty(tmp_path):
    """ticket_spec=None (the feature-absent default) must run no git walk at
    all and leave every LineRecord.ticket / store.tickets untouched."""
    project = _git_repo_with_ticket_commit(tmp_path)
    store = _store_with_lines(project)
    reporter = _reporter_with(ticket_spec=None)

    reporter._annotate_tickets(store, project)

    assert all(line.ticket == [] for line in next(store.files()).lines.values())
    assert store.tickets == {}


@pytest.mark.asyncio
async def test_run_wires_ticket_attribution_into_the_store(tmp_path):
    """Regression: `run()` must itself call `_annotate_tickets` — not just
    have a correct, independently-tested method sitting unused.

    The two tests above call ``reporter._annotate_tickets(...)`` directly,
    so they prove the method works but say nothing about whether ``run()``
    actually invokes it; a reviewer deleted that one call site from ``run()``
    and the entire cov + cli suite (482 tests) stayed green. This test goes
    through the public ``run()`` entry point with a real one-commit git repository
    (commit message names a ticket), a real ``.info`` tier so the store
    actually holds lines, ``project_root`` set via ``CollectionInputs``, and a
    ``ticket_spec`` — so deleting the ``run()`` call site makes *this* test
    fail even though every other test still passes.
    """
    from otto.coverage.tickets import build_ticket_spec

    project = tmp_path / "sut"
    project.mkdir()
    _git(project, tmp_path, "init", "-q")
    (project / "f.c").write_text("int a;\nint b;\n")
    _git(project, tmp_path, "add", "f.c")
    _git(project, tmp_path, "commit", "-q", "-m", "seed PROJ-9")

    info = tmp_path / "u.info"
    info.write_text(f"TN:\nSF:{project / 'f.c'}\nDA:1,3\nDA:2,1\nend_of_record\n")

    reporter = CoverageReporter(
        project,
        tmp_path / "report",
        tiers=[("unit", info)],
        collection=CollectionInputs(project_root=project),
        ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None),
    )
    store = await reporter.run()

    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    assert fr.lines[1].ticket == ["PROJ-9"]
    assert fr.lines[2].ticket == ["PROJ-9"]
    assert "PROJ-9" in store.tickets
    assert store.tickets["PROJ-9"].commits


# ── Task 14: synthetic (no ticket) / (uncommitted) rows ─────────────────────
#
# reporter.py:721's `if ids and lineno in rec.lines` used to drop both
# rows on the floor: attribution.attribute_tickets already returns an empty
# id list for BOTH a no-ticket-matching commit and an uncommitted line (see
# test_attribution.py's `test_attribute_tickets_exposes_owning_sha_...`), so
# the falsy check silently discarded both instead of just one. These tests
# exercise `_annotate_tickets` directly (like the two tests above) with a
# real one-commit git repository, so a reviewer reverting the sentinel assignment
# would see one of these fail even though every ticket-configured test
# above (which never exercises a no-ticket commit or a dirty line) stays
# green.


def _git_repo_with_no_ticket_commit(tmp_path: Path) -> Path:
    """A one-commit git repository whose message names no ticket at all."""
    project = tmp_path / "no_ticket_sut"
    project.mkdir()
    _git(project, tmp_path, "init", "-q")
    (project / "a.c").write_text("one\ntwo\n")
    _git(project, tmp_path, "add", "-A")
    _git(project, tmp_path, "commit", "-q", "-m", "chore: bump dependency")
    return project


def test_reporter_assigns_no_ticket_sentinel_for_unmatched_commit(tmp_path):
    """A line whose owning commit matched no ticket pattern lands in the
    synthetic `(no ticket)` row instead of being dropped."""
    from otto.coverage.attribution import NO_TICKET
    from otto.coverage.tickets import build_ticket_spec

    project = _git_repo_with_no_ticket_commit(tmp_path)
    store = _store_with_lines(project)
    reporter = _reporter_with(ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None))

    reporter._annotate_tickets(store, project)

    (rec,) = list(store.files())
    assert rec.lines[1].ticket == [NO_TICKET]
    assert rec.lines[2].ticket == [NO_TICKET]
    assert NO_TICKET in store.tickets
    assert store.tickets[NO_TICKET].url is None
    assert store.tickets[NO_TICKET].commits  # the real sha that matched no ticket


def test_reporter_assigns_uncommitted_sentinel_and_does_not_inherit_prior_ticket(tmp_path):
    """A working-tree edit lands in `(uncommitted)` — and, critically, does
    NOT inherit the ticket of whoever last committed that line, which is
    exactly the bug the sentinel exists to prevent (attribution.py's
    `_apply_worktree_overlay` docstring)."""
    from otto.coverage.attribution import UNCOMMITTED_TICKET
    from otto.coverage.tickets import build_ticket_spec

    project = _git_repo_with_ticket_commit(tmp_path)  # commits "one\ntwo\n" as "seed PROJ-7"
    (project / "a.c").write_text("one\nDIRTY\n")  # line 2 edited, never committed
    store = _store_with_lines(project)
    reporter = _reporter_with(ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None))

    reporter._annotate_tickets(store, project)

    (rec,) = list(store.files())
    assert rec.lines[1].ticket == ["PROJ-7"]  # untouched line: still committed to PROJ-7
    assert rec.lines[2].ticket == [UNCOMMITTED_TICKET]  # NOT ["PROJ-7"]
    assert UNCOMMITTED_TICKET in store.tickets
    assert store.tickets[UNCOMMITTED_TICKET].url is None


# ── Final-review fix: no TicketRecord for a ticket owning zero coverable lines ──
#
# `_annotate_tickets`'s `line_counts` is keyed by each file's *highest*
# stored line number (`max(rec.lines)`), not by which specific lines are
# coverable — a covered file storing only lines {1, 3} still asks
# `attribute_tickets` to walk lines 1-3, so a commit whose only change is
# line 2 (the gap) gets attributed and its ticket lands in `commits`, even
# though line 2 itself is filtered out by `if lineno not in rec.lines`
# below and so never reaches any `LineRecord`. Left unfixed, that ticket
# would still get a `TicketRecord` in `store.tickets` — appearing in the
# tickets page and the `tickets.json` export with zero owned lines.


def _git_repo_with_a_non_coverable_gap(tmp_path: Path) -> tuple[Path, str, str]:
    """Two commits against a 3-line file; only the second touches line 2.

    Returns ``(root, first_sha, second_sha)``. Callers register a store
    that knows about lines 1 and 3 only, leaving line 2 as the "gap" a
    real coverage tool would never report (a blank line, a comment, a
    brace-only line).
    """
    project = tmp_path / "gap_sut"
    project.mkdir()
    _git(project, tmp_path, "init", "-q")
    (project / "a.c").write_text("one\ntwo\nthree\n")
    _git(project, tmp_path, "add", "-A")
    _git(project, tmp_path, "commit", "-q", "-m", "seed PROJ-1")
    first = head_commit(project)

    (project / "a.c").write_text("one\nCHANGED\nthree\n")
    _git(project, tmp_path, "add", "-A")
    _git(project, tmp_path, "commit", "-q", "-m", "edit PROJ-2")
    second = head_commit(project)

    return project, first, second


def test_ticket_owning_only_a_non_coverable_line_gets_no_ticket_record(tmp_path):
    """PROJ-2 touched only the gap line (2) — it must not surface as a ticket."""
    from otto.coverage.store.model import CoverageStore
    from otto.coverage.tickets import build_ticket_spec

    project, first, _second = _git_repo_with_a_non_coverable_gap(tmp_path)
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(project / "a.c")
    record.get_or_create_line(1)
    record.get_or_create_line(3)  # line 2 deliberately absent: the gap
    reporter = _reporter_with(ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None))

    reporter._annotate_tickets(store, project)

    (rec,) = list(store.files())
    assert rec.lines[1].ticket == ["PROJ-1"]
    assert rec.lines[3].ticket == ["PROJ-1"]  # untouched by the line-2-only edit
    assert "PROJ-1" in store.tickets
    assert store.tickets["PROJ-1"].commits == [first]
    assert "PROJ-2" not in store.tickets
