"""The docs site's search: a Pagefind index built over the finished HTML.

Sphinx's own search index matches single words and ranks whole pages, so a
reader's question returns every page or none. Pagefind (https://pagefind.app)
indexes the built HTML instead: it ranks by relevance, matches word stems, and
answers with the section that holds the words.

Two hooks do the work, both registered by :func:`sphinx_setup`:

- ``html-page-context`` tells the template, per page, its **area** (the
  search's filter) and the **weight** its words get. ``_templates/base.html``
  writes both onto the element Pagefind indexes. A page Sphinx generates with
  no source (the general and module indexes) has no indexed element, so the
  search leaves it out.
- ``build-finished`` runs Pagefind over the output directory, writing the index
  and the search UI's files to ``<outdir>/pagefind/``.

Neither hook keeps a list of its own:

- A page's area is the caption of the root toctree that holds it, in
  ``docs/index.rst`` (*Set up a project*, *Reference*, ...). The site's sections
  are decided there, so a new page or directory is placed by the toctree it is
  listed in.
- A page ranks lower when it records rather than explains: the Python API
  reference (every page under :data:`API_REFERENCE`, autodoc output) and the
  pages a renderer writes at build time (:data:`scripts.docs_tree.RENDERED`:
  the release notes, the support matrices). They name every user-facing term in
  passing, so at full weight they crowd the page that explains the term out of
  the first results.

Every HTML build gets the index this way, whoever runs ``sphinx-build``: the
Makefile, CI and Read the Docs build the same search.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Iterator, Mapping
from typing import Any

from sphinx import addnodes
from sphinx.util import logging

from scripts import docs_tree

API_REFERENCE = "api/index"
"""The toctree entry the Python API reference hangs from."""

RECORD_WEIGHT = 0.4
"""The weight of a page that records rather than explains (Pagefind's default is 1)."""

INCLUDE_CHARACTERS = "-_"
"""Characters indexed as part of a word, so a flag or an identifier is found
whole: ``--list-products``, ``ensure_installed``."""

INDEX_DIR = "pagefind"
"""The index's directory under the build's output directory."""

_logger = logging.getLogger(__name__)


def _ancestry(docname: str, parents: Mapping[str, str]) -> Iterator[str]:
    """Yield *docname*, then each document whose toctree lists the one before."""
    seen: set[str] = set()
    current: str | None = docname
    while current is not None and current not in seen:
        yield current
        seen.add(current)
        current = parents.get(current)


def section_of(docname: str, parents: Mapping[str, str], captions: Mapping[str, str]) -> str | None:
    """Return the root toctree caption *docname* is listed under, or None.

    *captions* maps each entry of the root document's toctrees to its caption;
    *parents* maps a document to the document whose toctree lists it. A page
    in no toctree (an orphan) has no section.
    """
    return next((captions[doc] for doc in _ancestry(docname, parents) if doc in captions), None)


def under(docname: str, entry: str, parents: Mapping[str, str]) -> bool:
    """Return whether *docname* is *entry* or is listed, at any depth, under it."""
    return entry in _ancestry(docname, parents)


def _root_captions(env: Any) -> dict[str, str]:
    root = env.get_doctree(env.config.root_doc)
    return {
        ref: toctree.get("caption") or ""
        for toctree in root.findall(addnodes.toctree)
        for _title, ref in toctree["entries"]
    }


def _parents(env: Any) -> dict[str, str]:
    # A document listed in two toctrees keeps the last parent Sphinx recorded.
    return {
        child: parent for parent, children in env.toctree_includes.items() for child in children
    }


def _page_context(
    app: Any, pagename: str, _templatename: str, context: dict, _doctree: Any
) -> None:
    env = app.env
    if pagename not in env.all_docs:
        context["otto_search_indexed"] = False
        return
    parents, captions = _parents(env), _root_captions(env)
    source = pathlib.PurePosixPath(env.doc2path(pagename, base=False)).as_posix()
    records = under(pagename, API_REFERENCE, parents) or not docs_tree.is_source(source)
    context["otto_search_indexed"] = True
    context["otto_search_area"] = section_of(pagename, parents, captions)
    context["otto_search_weight"] = RECORD_WEIGHT if records else 1.0


async def write_index(outdir: pathlib.Path) -> int:
    """Index the HTML pages under *outdir* into *outdir*/``pagefind``; return the page count."""
    from pagefind.index import IndexConfig, PagefindIndex

    config = IndexConfig(
        include_characters=INCLUDE_CHARACTERS,
        # The heading anchor glyph and the "view source" button are not content.
        exclude_selectors=[".headerlink", "a.md-content__button"],
        # The index writes its files when the `async with` closes. A relative
        # path, or none, resolves against the current directory, not the site,
        # so the path is absolute.
        output_path=str(outdir / INDEX_DIR),
    )
    async with PagefindIndex(config=config) as index:
        result = await index.add_directory(path=str(outdir))
    return int(result["page_count"])


def _build_index(app: Any, exception: BaseException | None) -> None:
    if exception is not None or app.builder.format != "html":
        return
    outdir = pathlib.Path(app.outdir)
    pages = asyncio.run(write_index(outdir))
    _logger.info(f"pagefind: indexed {pages} pages into {outdir / INDEX_DIR}")


def sphinx_setup(app: Any) -> None:
    """Register the per-page search context and the post-build index."""
    app.connect("html-page-context", _page_context)
    app.connect("build-finished", _build_index)
