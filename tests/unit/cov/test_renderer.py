"""Tests for HtmlRenderer: tier colors, legend, state rows, provenance table."""

from pathlib import Path

from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import CoverageStore, LineHits, LineRecord


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


class TestFilePageRowPrecedence:
    """One covered/tier-0 line, one stale line, one excluded line."""

    def _render(self, tmp_path: Path):
        src = _write(
            tmp_path,
            "f.c",
            "int a;\nint b; // LCOV_EXCL_LINE\nint c;\n",
        )
        store = CoverageStore(tier_order=["system"])
        store.tier_colors["system"] = "#112233"
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 3}))
        fr.lines[3] = LineRecord(line_number=3, state="stale")

        out_dir = tmp_path / "report"
        renderer = HtmlRenderer(out_dir)
        renderer.render(store)

        return (out_dir / HtmlRenderer._file_link(fr)).read_text()

    def test_covered_line_gets_tier_0_class(self, tmp_path):
        html = self._render(tmp_path)
        assert 'class="line tier-0"' in html

    def test_stale_line_gets_state_stale_class(self, tmp_path):
        html = self._render(tmp_path)
        assert 'class="line state-stale"' in html

    def test_excluded_line_gets_state_excluded_class(self, tmp_path):
        html = self._render(tmp_path)
        assert 'class="line state-excluded"' in html

    def test_inline_style_declares_tier_0_color(self, tmp_path):
        html = self._render(tmp_path)
        assert "--tier-0: #112233" in html


class TestExcludedLinesPersisted:
    """spec §9 frontend contract: the renderer's per-file exclusion scan is
    annotated onto the store so store.json carries the excluded line set."""

    def test_store_json_roundtrip_has_excluded_lines(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b; // LCOV_EXCL_LINE\nint c;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 3}))

        out_dir = tmp_path / "report"
        renderer = HtmlRenderer(out_dir)
        renderer.render(store)  # scans source → annotates fr.excluded_lines

        # The renderer annotates the store; the reporter renders before saving,
        # so a save here reflects the same flow store.json consumers see.
        store_json = out_dir / "store.json"
        store.save(store_json)
        reloaded = CoverageStore.load(store_json)

        (frec,) = [f for f in reloaded.files() if f.path.name == "f.c"]
        assert frec.excluded_lines == {2}

    def test_load_tolerates_absent_excluded_lines_key(self, tmp_path):
        """Legacy store.json with no excluded_lines key loads to an empty set."""
        import json

        store_json = tmp_path / "store.json"
        store_json.write_text(
            json.dumps(
                {
                    "tier_order": ["system"],
                    "files": [{"path": "/x/f.c", "lines": {}}],
                }
            )
        )
        reloaded = CoverageStore.load(store_json)
        (frec,) = list(reloaded.files())
        assert frec.excluded_lines == set()


class TestExcludedAlwaysWins:
    """A line that is both covered and inside an LCOV_EXCL block renders excluded."""

    def test_covered_and_excluded_line_is_state_excluded_not_tier(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a; // LCOV_EXCL_LINE\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 5}))

        out_dir = tmp_path / "report"
        renderer = HtmlRenderer(out_dir)
        renderer.render(store)

        html = (out_dir / HtmlRenderer._file_link(fr)).read_text()
        assert 'class="line state-excluded"' in html
        assert 'class="line tier-0"' not in html


class TestIndexProvenanceAndLegend:
    def test_provenance_ticket_and_legend_tier_name_appear(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 1}))
        store.provenance.append(
            {
                "tier": "manual",
                "board": "b1",
                "labs": ["lab1"],
                "date": "2026-07-01T00:00:00Z",
                "tester": {"name": "Alice"},
                "ticket": "T-42",
                "note": "note text",
                "dirty_remap": True,
                "pin": "f" * 40,
            }
        )

        out_dir = tmp_path / "report"
        renderer = HtmlRenderer(out_dir)
        renderer.render(store)

        index_html = (out_dir / "index.html").read_text()
        assert "T-42" in index_html
        # Extract legend div and verify tier name + state label appear within it
        legend_start = index_html.find('<div class="legend">')
        legend_end = index_html.find("</div>", legend_start) + len("</div>")
        assert legend_start >= 0, "Legend div not found"
        legend_content = index_html[legend_start:legend_end]
        assert "System" in legend_content, "System tier label missing from legend"
        assert "Stale" in legend_content, "State label missing from legend"
        assert "✎" in index_html  # dirty_remap renders as the pencil glyph


class TestOutOfRangeStaleTolerance:
    """A stale LineRecord beyond the current file's EOF must not crash rendering."""

    def test_stale_line_past_eof_renders_and_counts(self, tmp_path):
        src = _write(tmp_path, "f.c", "a;\nb;\nc;\n")  # 3 lines
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.lines[999] = LineRecord(line_number=999, state="stale")

        out_dir = tmp_path / "report"
        renderer = HtmlRenderer(out_dir)
        renderer.render(store)  # must not raise (no IndexError)

        file_html = (out_dir / HtmlRenderer._file_link(fr)).read_text()
        assert "Stale: 1" in file_html

        index_html = (out_dir / "index.html").read_text()
        # Verify stale count (1) appears in the file's row in the files table
        files_section = index_html.split('<section class="files">')[1].split("</section>")[0]
        assert 'data-sort="1">1</td>' in files_section
