"""The fixture report is consumed by the browser suite AND the docs-media
screenshot — it must render hermetically and deterministically."""

from tests._fixtures._report_fixture import build_fixture_report


def test_fixture_report_renders(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    index = (report_dir / "index.html").read_text()
    assert "otto example product" in index
    assert "main.c" in index
    assert "System %" in index
    assert "Unit %" in index
    file_pages = list((report_dir / "files").glob("*.html"))
    assert len(file_pages) == 2
    assert (report_dir / "static" / "report.css").exists()


def test_fixture_report_has_branch_pills(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    # _file_link mangles only path SEPARATORS to "_" — "product/main.c"
    # becomes "..._product_main.c.html" (the basename keeps its dot).
    main_page = next(p for p in (report_dir / "files").glob("*main.c.html"))
    html = main_page.read_text()
    assert "branch-taken" in html
    assert "branch-not-taken" in html


def test_display_paths_are_short_and_deterministic(tmp_path):
    """The builder renders with prefix=base_dir — the screenshot and the
    browser pins both rely on the exact strings product/main.c|utils.c."""
    report_dir = build_fixture_report(tmp_path)
    index = (report_dir / "index.html").read_text()
    assert "product/main.c" in index
    assert "product/utils.c" in index
    assert str(tmp_path) not in index
