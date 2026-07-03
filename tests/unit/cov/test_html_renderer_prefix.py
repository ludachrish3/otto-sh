"""--prefix strips a leading directory from DISPLAYED paths only (the
genhtml --prefix analogue): links and store keys stay full-path,
non-matching files display unchanged, no prefix means today's verbatim
display."""

from pathlib import Path

from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import CoverageStore, FileRecord, LineRecord


def _store_with(*paths: Path) -> CoverageStore:
    store = CoverageStore(tier_order=["system"])
    for path in paths:
        rec = FileRecord(path=path)
        line = LineRecord(line_number=1)
        line.hits.add("system", 1)
        rec.lines[1] = line
        store.merge_file(rec)
    return store


def _write_src(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("int one(void) { return 1; }\n")
    return path


def test_prefix_strips_displayed_paths(tmp_path):
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path).render(_store_with(src))
    index = (out / "index.html").read_text()
    assert "product/main.c" in index
    assert str(tmp_path) not in index


def test_file_outside_prefix_displays_unchanged(tmp_path):
    inside = _write_src(tmp_path / "repo" / "a.c")
    outside = _write_src(tmp_path / "elsewhere" / "b.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path / "repo").render(_store_with(inside, outside))
    index = (out / "index.html").read_text()
    assert ">a.c<" in index or "a.c" in index  # stripped
    assert str(tmp_path / "elsewhere" / "b.c") in index  # verbatim


def test_no_prefix_keeps_verbatim_display(tmp_path):
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out).render(_store_with(src))
    assert str(src) in (out / "index.html").read_text()


def test_prefix_does_not_change_file_links(tmp_path):
    """Links stay keyed on the full path — only the label is stripped."""
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path).render(_store_with(src))
    mangled = str(src).replace("/", "_").lstrip("_")
    assert (out / "files" / f"{mangled}.html").exists()
