"""Tests for spa_data: IndexPayload/FileChunk emission for the coverage SPA."""

import json
import re
from pathlib import Path

from otto.coverage.renderer.spa_data import (
    build_index_payload,
    emit_chunks,
    make_stamp,
    mangle_path,
)
from otto.coverage.store.model import CoverageStore, Thresholds


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _find_file(node: dict, name: str) -> dict | None:
    for f in node["files"]:
        if f["name"] == name:
            return f
    for d in node["dirs"]:
        found = _find_file(d, name)
        if found is not None:
            return found
    return None


class TestMakeStampAndManglePath:
    def test_make_stamp_matches_expected_shape(self):
        stamp = make_stamp()
        assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", stamp)

    def test_mangle_path_replaces_separators_and_strips_leading_underscore(self, tmp_path):
        mangled = mangle_path(tmp_path / "a" / "b.c")
        assert "/" not in mangled
        assert "\\" not in mangled
        assert not mangled.startswith("_")


class TestIndexPayload:
    def test_format_and_stamp_and_config_keys(self, tmp_path):
        store = CoverageStore(tier_order=["system", "manual"])
        store.thresholds = Thresholds(high=90, medium=75)
        payload = build_index_payload(
            store,
            project_name="Proj",
            prefix=None,
            stamp="20260725T140200Z-1a2b3c4d",
        )
        assert payload["format"] == 1
        assert payload["stamp"] == "20260725T140200Z-1a2b3c4d"
        assert payload["thresholds"] == {"high": 90.0, "medium": 75.0}
        assert payload["stat_types"] == ["line", "branch", "decision"]
        assert payload["state_colors"]["stale"] == "violet"

    def test_empty_store_tree_is_bare_root(self, tmp_path):
        store = CoverageStore(tier_order=["system"])
        payload = build_index_payload(store, project_name="Empty", prefix=None, stamp="S")
        tree = payload["tree"]
        assert tree["name"] == "Empty"
        assert tree["dirs"] == []
        assert tree["files"] == []
        assert tree["stats"]["lines"] == {"total": 0, "hit": 0, "per_tier": {"system": 0}}
        assert payload["total_lines"] == 0

    def test_zero_line_file_still_appears_in_tree(self, tmp_path):
        src = _write(tmp_path, "empty.c", "")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)  # no lines added
        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "empty.c")
        assert node is not None
        assert node["stats"]["lines"] == {"total": 0, "hit": 0, "per_tier": {"system": 0}}


class TestTreeRollupAndCtxLines:
    def test_root_totals_nested_dirs_and_ctx_lines(self, tmp_path):
        x = _write(tmp_path, "a/x.c", "int a;\nint b;\n")
        y = _write(tmp_path, "a/b/y.c", "int c;\nint d;\n")
        store = CoverageStore(tier_order=["system"])
        run_r1 = store.add_run(tier="system", label="r1")

        fr_x = store.get_or_create_file(x)
        for lineno in (1, 2):
            lr = fr_x.get_or_create_line(lineno)
            lr.hits.add("system", 1)
            lr.run_hits[run_r1] = 1

        fr_y = store.get_or_create_file(y)
        lr_hit = fr_y.get_or_create_line(1)
        lr_hit.hits.add("system", 1)
        lr_hit.run_hits[run_r1] = 1
        fr_y.get_or_create_line(2)  # miss, no run credit

        payload = build_index_payload(store, project_name="Proj", prefix=tmp_path, stamp="S")
        root = payload["tree"]

        assert root["name"] == "Proj"
        assert root["stats"]["lines"]["total"] == 4
        assert root["stats"]["lines"]["hit"] == 3
        assert root["stats"]["ctx_lines"]["r1"] == 3

        dir_a = next(d for d in root["dirs"] if d["name"] == "a")
        assert any(d["name"] == "b" for d in dir_a["dirs"])
        dir_b = next(d for d in dir_a["dirs"] if d["name"] == "b")
        assert dir_b["stats"]["lines"]["total"] == 2
        assert dir_b["stats"]["lines"]["hit"] == 1


class TestRunContrib:
    def test_lines_revoked_and_top_files_sorted_desc(self, tmp_path):
        x = _write(tmp_path, "a.c", "int a;\nint b;\n")
        y = _write(tmp_path, "b.c", "int c;\n")
        store = CoverageStore(tier_order=["system"])
        run_id = store.add_run(tier="system", label="r1")
        other_run = store.add_run(tier="system", label="r2")

        fr_x = store.get_or_create_file(x)
        l1 = fr_x.get_or_create_line(1)
        l1.hits.add("system", 1)
        l1.run_hits[run_id] = 1
        l2 = fr_x.get_or_create_line(2)
        l2.state = "stale"
        l2.stale_runs.append(run_id)

        fr_y = store.get_or_create_file(y)
        l3 = fr_y.get_or_create_line(1)
        l3.hits.add("system", 1)
        l3.run_hits[run_id] = 1

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        contrib = payload["run_contrib"][str(run_id)]
        assert contrib["lines"] == 2
        assert contrib["revoked"] == 1
        assert contrib["files"] == [["a.c", 1], ["b.c", 1]]

        other_contrib = payload["run_contrib"][str(other_run)]
        assert other_contrib == {"lines": 0, "revoked": 0, "files": []}


class TestDisplayPathPrefixStripAndFallback:
    def test_prefix_strips_matching_path(self, tmp_path):
        src = _write(tmp_path, "product/main.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "main.c")
        assert node is not None
        assert node["path"] == "product/main.c"

    def test_file_outside_prefix_falls_back_to_full_path(self, tmp_path):
        inside_dir = tmp_path / "repo"
        src = _write(tmp_path, "elsewhere/b.c", "int b;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        payload = build_index_payload(store, project_name="P", prefix=inside_dir, stamp="S")
        node = _find_file(payload["tree"], "b.c")
        assert node is not None
        assert node["path"] == str(src)


class TestOutOfRangeLines:
    def test_emit_chunks_handles_line_past_eof_without_crash(self, tmp_path):
        src = _write(tmp_path, "f.c", "a;\nb;\nc;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.get_or_create_line(999).state = "stale"

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        chunk_path = out_dir / "cov_data" / "files" / f"{mangle_path(src)}.js"
        text = chunk_path.read_text()
        assert '"999"' in text

        payload = build_index_payload(store, project_name="P", prefix=None, stamp="S")
        assert payload["tree"]["stats"]["lines"]["total"] == 1
        assert payload["tree"]["stats"]["flags"]["stale"] == 1


class TestEmitChunks:
    def test_index_js_is_classic_assignment(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )
        text = (out_dir / "cov_data" / "index.js").read_text()
        assert text.startswith("window.__OTTO_COV__ = {")
        assert text.endswith("};\n")
        # the slice between them is valid JSON
        json.loads(text[len("window.__OTTO_COV__ = ") : -2])

    def test_file_chunk_wraps_call_and_matches_store_line_json(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b;\nint c;\n")
        store = CoverageStore(tier_order=["system", "manual"])
        run_a = store.add_run(tier="system", label="r1")
        run_b = store.add_run(tier="manual", label="r2")
        fr = store.get_or_create_file(src)

        lr1 = fr.get_or_create_line(1)
        lr1.hits.add("system", 3)
        lr1.run_hits[run_a] = 3

        lr2 = fr.get_or_create_line(2)
        lr2.state = "stale"
        lr2.stale_runs.append(run_b)

        fr.get_or_create_line(3)  # no run hits, no stale runs

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        chunk_path = out_dir / "cov_data" / "files" / f"{mangle_path(src)}.js"
        text = chunk_path.read_text()
        assert text.startswith("window.__OTTO_COV_FILE__(")
        assert text.endswith(");\n")
        body = text[len("window.__OTTO_COV_FILE__(") : -len(");\n")]
        chunk = json.loads(body)

        expected_lines = fr.to_dict()["lines"]
        for lineno_str in ("1", "2", "3"):
            assert chunk["lines"][lineno_str] == expected_lines[lineno_str]
        assert "run" in chunk["lines"]["1"]
        assert "run" not in chunk["lines"]["2"]
        assert "run" not in chunk["lines"]["3"]
        assert "stale_run" in chunk["lines"]["2"]
        assert "stale_run" not in chunk["lines"]["1"]
        assert "stale_run" not in chunk["lines"]["3"]

    def test_excluded_lines_annotated_on_store(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b; // LCOV_EXCL_LINE\nint c;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.get_or_create_line(1).hits.add("system", 1)

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        assert fr.excluded_lines == {2}

    def test_empty_store_still_writes_index_js(self, tmp_path):
        store = CoverageStore(tier_order=["system"])
        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )
        assert (out_dir / "cov_data" / "index.js").exists()
