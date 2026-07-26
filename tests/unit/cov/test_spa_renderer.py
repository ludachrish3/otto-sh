"""Tests for SpaRenderer: bundle copy, data-chunk emission, warn-and-continue degrade.

Carries forward the pins from the retired Jinja-era renderer tests that must
survive the SPA swap at the Python level (the vitest suites cover the DOM
level) — the shapes these mirror were originally pinned in the deleted
``test_html_renderer_dist.py`` / ``test_renderer.py``.
"""

import json
from pathlib import Path

import pytest

from otto.coverage.renderer import spa_renderer
from otto.coverage.renderer.spa_data import mangle_path
from otto.coverage.renderer.spa_renderer import SpaRenderer
from otto.coverage.store.model import CoverageStore, LineHits, LineRecord


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _index_payload(out: Path) -> dict:
    text = (out / "cov_data" / "index.js").read_text()
    assert text.startswith("window.__OTTO_COV__ = {")
    assert text.endswith("};\n")
    return json.loads(text[len("window.__OTTO_COV__ = ") : -2])


def _file_chunk(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("window.__OTTO_COV_FILE__(")
    assert text.endswith(");\n")
    return json.loads(text[len("window.__OTTO_COV_FILE__(") : -3])


def _find_file(node: dict, name: str) -> dict | None:
    for f in node["files"]:
        if f["name"] == name:
            return f
    for d in node["dirs"]:
        found = _find_file(d, name)
        if found is not None:
            return found
    return None


class TestBundleCopy:
    """LEDGER CARRY-FORWARD (Task 2 -> 8): the copy must exclude *.map or every
    emitted report grows ~4.8MB (the real built bundle carries a hidden
    sourcemap for the TS coverage fold)."""

    def test_present_bundle_copies_index_and_dist_with_no_sourcemaps(self, tmp_path):
        out = tmp_path / "report"
        SpaRenderer(out).render(CoverageStore(tier_order=["system"]))
        assert (out / "index.html").exists()
        assert (out / "dist" / "covapp.js").exists()
        assert (out / "dist" / "covapp.css").exists()
        assert not list(out.rglob("*.map")), "SpaRenderer must exclude *.map from the bundle copy"

    def test_missing_bundle_warns_names_make_web_and_still_emits_cov_data(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(spa_renderer, "STATIC_DIR", tmp_path / "does_not_exist")
        out = tmp_path / "report"

        with caplog.at_level("WARNING"):
            SpaRenderer(out).render(CoverageStore(tier_order=["system"]))

        assert not (out / "index.html").exists()
        assert (out / "cov_data" / "index.js").exists()
        assert any("make web" in r.message for r in caplog.records)

    def test_bundle_dir_present_but_no_index_html_still_warns(self, tmp_path, monkeypatch, caplog):
        """A partial/stray covapp dir with no index.html degrades the same as absent."""
        bare = tmp_path / "static_src"
        (bare / "dist").mkdir(parents=True)
        monkeypatch.setattr(spa_renderer, "STATIC_DIR", bare)
        out = tmp_path / "report"

        with caplog.at_level("WARNING"):
            SpaRenderer(out).render(CoverageStore(tier_order=["system"]))

        assert any("make web" in r.message for r in caplog.records)

    def test_present_dist_does_not_warn(self, tmp_path, caplog):
        out = tmp_path / "report"
        with caplog.at_level("WARNING"):
            SpaRenderer(out).render(CoverageStore(tier_order=["system"]))
        assert not [r for r in caplog.records if "make web" in r.message]


class TestEmptyStoreReport:
    def test_empty_store_still_emits_index_js_and_copies_bundle(self, tmp_path):
        out = tmp_path / "report"
        SpaRenderer(out).render(CoverageStore(tier_order=["system"]))
        payload = _index_payload(out)
        assert payload["tree"]["files"] == []
        assert (out / "index.html").exists()


class TestPrefixAndChunkNaming:
    def test_prefix_strips_display_path_but_chunk_name_uses_full_path(self, tmp_path):
        src = _write(tmp_path, "proj/f.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 1}))

        out = tmp_path / "report"
        SpaRenderer(out, prefix=tmp_path / "proj").render(store)

        payload = _index_payload(out)
        (file_node,) = payload["tree"]["files"]
        assert file_node["path"] == "f.c"  # display path stripped by prefix
        assert file_node["chunk"] == mangle_path(fr.path)  # chunk name = full path, unaffected
        assert (out / "cov_data" / "files" / f"{file_node['chunk']}.js").exists()


class TestOutOfRangeTolerance:
    def test_out_of_range_line_included_in_chunk_without_crash(self, tmp_path):
        src = _write(tmp_path, "f.c", "a;\nb;\nc;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[999] = LineRecord(line_number=999, state="stale")

        out = tmp_path / "report"
        SpaRenderer(out).render(store)  # must not raise (no IndexError)

        chunk_path = next((out / "cov_data" / "files").glob("*.js"))
        chunk = _file_chunk(chunk_path)
        assert "999" in chunk["lines"]

        payload = _index_payload(out)
        file_node = _find_file(payload["tree"], "f.c")
        assert file_node is not None
        assert file_node["stats"]["lines"]["total"] == 1  # the out-of-range line still counts


class TestExcludedLinesRoundTripThroughReporter:
    """spec §9 frontend contract: SpaRenderer's per-file exclusion scan
    annotates the store, and the reporter renders BEFORE it saves store.json —
    so the same annotation must survive into the persisted JSON."""

    @pytest.mark.asyncio
    async def test_excluded_lines_persist_to_store_json_after_render(self, tmp_path):
        from otto.coverage.reporter import CoverageReporter

        src = tmp_path / "src"
        src.mkdir()
        (src / "f.c").write_text("int a;\nint b; // LCOV_EXCL_LINE\nint c;\n")
        info = tmp_path / "u.info"
        info.write_text(f"TN:\nSF:{src / 'f.c'}\nDA:1,7\nend_of_record\n")

        out = tmp_path / "report"
        reporter = CoverageReporter([], src, out, tiers=[("unit", info)])
        store = await reporter.run()

        (fr,) = [f for f in store.files() if f.path.name == "f.c"]
        assert fr.excluded_lines == {2}  # annotated in-memory during render()

        raw = json.loads((out / "store.json").read_text())
        (file_entry,) = [f for f in raw["files"] if f["path"].endswith("f.c")]
        assert file_entry["excluded_lines"] == [2]  # ...and persisted afterward

    @pytest.mark.asyncio
    async def test_settings_driven_collection_path_renders_spa_and_saves_store(
        self, tmp_path, monkeypatch
    ):
        """Mirrors ``test_cov.py``'s settings-driven (repo_root + tier_configs)
        collection-model wiring end to end: the real ``run_coverage_report``
        entry point, through ``CoverageReporter.run()``, must land on the SPA
        artifacts (not the retired Jinja ones) and still save store.json last."""
        from otto.coverage.merge import merger as merger_mod
        from otto.coverage.reporter import run_coverage_report
        from otto.coverage.tiers import load_tiers

        repo = tmp_path / "sut"
        repo.mkdir()
        (repo / "f.c").write_text("int a;\nint b; // LCOV_EXCL_LINE\nint c;\n")

        hdir = tmp_path / "unit_build"
        hdir.mkdir()
        (hdir / "f.gcda").write_bytes(b"")
        (hdir / "f.gcno").write_bytes(b"")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,7\nend_of_record\n")
            return output

        monkeypatch.setattr(merger_mod.LcovMerger, "capture", fake_capture)

        cov_config = {
            "tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]}},
        }
        out = tmp_path / "report"
        store = await run_coverage_report(
            [], out, repo_root=repo, tier_configs=load_tiers(cov_config)
        )

        assert store is not None
        assert (out / "index.html").exists()  # SpaRenderer's bundle copy ran
        assert (out / "cov_data" / "index.js").exists()
        assert not (out / "files").exists()  # not the retired Jinja per-file dir

        raw = json.loads((out / "store.json").read_text())
        (file_entry,) = [f for f in raw["files"] if f["path"].endswith("f.c")]
        assert file_entry["excluded_lines"] == [2]
