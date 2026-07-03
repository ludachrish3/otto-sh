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
        assert "System" in index_html  # legend + summary both label the "system" tier
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
        assert index_html  # index render also completed without crashing
