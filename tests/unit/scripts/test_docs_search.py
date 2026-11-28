"""``scripts/docs_search.py``: where a page sits in the site search, and where the index lands.

A page's area is the root toctree caption above it, and a page under the API
reference records rather than explains; both are read off the toctree, so the
tests drive the two walks over a small tree. The index itself must land under
the built site, never in the directory the build was started from.
"""

import asyncio
import pathlib
from types import SimpleNamespace

import pytest
from docutils import nodes
from sphinx import addnodes

from scripts.docs_search import (
    API_REFERENCE,
    INDEX_DIR,
    RECORD_WEIGHT,
    _page_context,
    section_of,
    under,
    write_index,
)

# index -> {setup/index, api/index}; setup/index -> setup/a; api/index -> api/x -> api/x/y
PARENTS = {
    "setup/index": "index",
    "setup/a": "setup/index",
    "api/index": "index",
    "api/x": "api/index",
    "api/x/y": "api/x",
}
CAPTIONS = {"setup/index": "Set up a project", "api/index": "Reference"}


@pytest.mark.parametrize(
    ("docname", "section"),
    [
        ("setup/index", "Set up a project"),
        ("setup/a", "Set up a project"),
        ("api/x/y", "Reference"),
        ("orphan", None),
        ("index", None),
    ],
)
def test_section_is_the_root_caption_above_the_page(docname: str, section: str | None) -> None:
    assert section_of(docname, PARENTS, CAPTIONS) == section


@pytest.mark.parametrize(
    ("docname", "expected"),
    [("api/index", True), ("api/x/y", True), ("setup/a", False), ("orphan", False)],
)
def test_under_the_api_reference(docname: str, expected: bool) -> None:
    assert under(docname, API_REFERENCE, PARENTS) is expected


def test_a_toctree_cycle_ends_the_walk() -> None:
    cyclic = {"a": "b", "b": "a"}
    assert section_of("a", cyclic, {}) is None
    assert under("a", "c", cyclic) is False


def test_the_index_lands_under_the_site_not_the_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.html").write_text(
        "<html><body><div data-pagefind-body>"
        "<h1>Hops</h1><p>reach a host through a hop</p>"
        "</div></body></html>"
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    pages = asyncio.run(write_index(site))

    assert pages == 1
    assert (site / INDEX_DIR / "pagefind.js").is_file()
    assert list(cwd.iterdir()) == []


def _app() -> SimpleNamespace:
    """A Sphinx app whose environment holds a root toctree, a guide, the API and a release note."""
    root = nodes.document("", None)
    for caption, entries in [
        ("Set up a project", ["setup/index"]),
        ("Reference", ["api/index", "release-notes/index"]),
    ]:
        root += addnodes.toctree(caption=caption, entries=[(None, e) for e in entries])
    includes = {
        "index": ["setup/index", "api/index", "release-notes/index"],
        "setup/index": ["setup/guide"],
        "api/index": ["api/project"],
        "release-notes/index": ["release-notes/0.16.0"],
    }
    docs = ["index", *[d for children in includes.values() for d in children]]
    env = SimpleNamespace(
        config=SimpleNamespace(root_doc="index"),
        all_docs=dict.fromkeys(docs, 0.0),
        toctree_includes=includes,
        get_doctree=lambda name: root,
        doc2path=lambda name, base=True: pathlib.Path(f"{name}.md"),
    )
    return SimpleNamespace(env=env)


@pytest.mark.parametrize(
    ("pagename", "area", "weight"),
    [
        ("setup/guide", "Set up a project", 1.0),
        ("api/project", "Reference", RECORD_WEIGHT),
        ("release-notes/0.16.0", "Reference", RECORD_WEIGHT),
    ],
)
def test_page_context_reads_the_environment(pagename: str, area: str, weight: float) -> None:
    context: dict = {}
    _page_context(_app(), pagename, "page.html", context, None)
    assert context == {
        "otto_search_indexed": True,
        "otto_search_area": area,
        "otto_search_weight": weight,
    }


def test_a_page_without_source_is_not_indexed() -> None:
    context: dict = {}
    _page_context(_app(), "genindex", "genindex.html", context, None)
    assert context == {"otto_search_indexed": False}
