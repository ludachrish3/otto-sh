import importlib.util

from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "relativize_docs_links.py"


def _load():
    spec = importlib.util.spec_from_file_location("relativize_docs_links", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_page_gets_single_updir():
    mod = _load()
    html = '<a href="https://docs.python.org/3/library/pathlib.html#pathlib.Path">Path</a>'
    out = mod.rewrite_html(html, depth=0)
    assert 'href="../python/library/pathlib.html#pathlib.Path"' in out


def test_nested_page_gets_depth_correct_updirs():
    mod = _load()
    html = '<a href="https://docs.pytest.org/en/stable/how-to/fixtures.html">f</a>'
    out = mod.rewrite_html(html, depth=2)
    assert 'href="../../../pytest/how-to/fixtures.html"' in out


def test_url_shown_as_text_is_not_rewritten():
    # The installation page DISPLAYS mirror URLs in prose/code; only href
    # attributes may be rewritten, or the page would misquote itself offline.
    mod = _load()
    html = "<code>https://docs.python.org/3/archives/</code>"
    assert mod.rewrite_html(html, depth=0) == html


def test_unmapped_host_is_untouched():
    mod = _load()
    html = '<a href="https://typer.tiangolo.com/tutorial/">t</a>'
    assert mod.rewrite_html(html, depth=1) == html


def test_tree_conversion(tmp_path):
    mod = _load()
    src = tmp_path / "html"
    (src / "guide").mkdir(parents=True)
    (src / "index.html").write_text('<a href="https://docs.python.org/3/library/os.html">os</a>')
    (src / "guide" / "test.html").write_text(
        '<a href="https://rich.readthedocs.io/en/stable/console.html">c</a>'
    )
    dst = tmp_path / "offline"
    assert mod.main([str(src), str(dst)]) == 0
    assert 'href="../python/library/os.html"' in (dst / "index.html").read_text()
    assert 'href="../../rich/console.html"' in (dst / "guide" / "test.html").read_text()


def test_refuses_existing_destination(tmp_path):
    mod = _load()
    src = tmp_path / "html"
    src.mkdir()
    dst = tmp_path / "offline"
    dst.mkdir()
    assert mod.main([str(src), str(dst)]) == 2
