"""Checks that a file otto writes into a USER's project points at the published docs.

Such a file is read inside the user's project, where a ``docs/...md`` source
path (or a relative ``../docs/...`` link) names a file that is not there. Each
pointer must instead be a link into the published docs, naming a page (and
heading) this checkout's ``docs/`` has. ``otto init``'s templates and the
vendored kmodcov files share these checks.
"""

import re
from pathlib import Path

from docutils.nodes import make_id

from tests._fixtures.paths import CHECKOUT_ROOT

DOCS_BASE = "https://otto-sh.readthedocs.io/en/stable/"
"""The published docs, as ``pyproject.toml``'s Documentation URL names them, at the release tag."""

DOCS_URL = re.compile(
    r"(?:\w+://)?(?<![\w.-])otto-sh\.readthedocs\.io(?:/[^\s)>\"'`]*[^\s)>\"'`.,;:])?"
)
"""Any mention of the published docs host, with or without a scheme; a sentence's closing
punctuation is not part of it. :func:`broken_links` holds each to :data:`DOCS_BASE`."""

OUTSIDE_THE_REPO = (
    "otto shows this outside the repository: "
    "use https://otto-sh.readthedocs.io/en/stable/<page>.html#<heading-id>"
)
"""What a guard says when text otto shows a user points anywhere but a published page."""

REPO_DOC_PATH = re.compile(r"(?<![\w/.:-])(?:\.\./)*docs/[\w.-]+(?:/[\w.-]+)*/?")
"""A path into otto's own ``docs/`` source tree: a ``.md`` page, a bare page, or a directory."""

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r" {0,3}#{1,6}(?:\s+(.*?))?(?:\s+#+)?\s*$")
_LABEL = re.compile(r"\(([^()\s]+)\)=\s*$")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def heading_ids(page: Path) -> set[str]:
    """Return the ids the published HTML gives *page*'s headings, which a ``#anchor`` can name.

    A browser scrolls to an HTML ``id``, and Sphinx gives each section the
    docutils id of its title (:func:`docutils.nodes.make_id`, which keeps
    ``a-z0-9`` and joins every other run with one '-'), at every heading
    level, plus the id of any ``(label)=`` target above it. MyST's own
    ``heading_anchors`` slugs (``host-identity--naming``) are a different
    thing: they resolve ``[](#slug)`` references between pages at build
    time and never reach the HTML. A title used twice gets its id once; the
    repeat gets a numbered ``idN`` no link should name. A line inside a
    fenced block is not a heading.
    """
    ids: set[str] = set()
    fence = ""
    for line in page.read_text().splitlines():
        opened = _FENCE.match(line)
        if fence:
            closes = (
                opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence)
            )
            if closes and not line.strip().strip(fence[0]):
                fence = ""
            continue
        if opened:
            fence = opened.group(1)
            continue
        label = _LABEL.match(line)
        if label:
            ids.add(make_id(label.group(1)))
            continue
        heading = _ATX.match(line)
        if heading:
            title = re.sub(r"[`*]", "", _LINK.sub(r"\1", heading.group(1) or ""))
            ids.add(make_id(title))
    return ids


def source_paths(text: str) -> list[str]:
    """Return every pointer in *text* that is a path into otto's ``docs/`` source tree."""
    return REPO_DOC_PATH.findall(text)


def broken_links(text: str) -> list[str]:
    """Return every docs link in *text* that names no published page or heading.

    A link is good when it starts at :data:`DOCS_BASE` (so an ``http://`` or
    scheme-less mention of the docs host is broken), names an ``.html``
    page whose ``.md`` source this checkout's ``docs/`` has, and, when it
    carries an ``#anchor``, that page has a heading with that id.
    """
    broken = []
    for link in DOCS_URL.findall(text):
        if not link.startswith(DOCS_BASE):
            broken.append(link)
            continue
        page, _, anchor = link.removeprefix(DOCS_BASE).partition("#")
        source = CHECKOUT_ROOT / "docs" / page.removesuffix(".html")
        source = source.with_name(source.name + ".md")
        if (
            not page.endswith(".html")
            or not source.is_file()
            or (anchor and anchor not in heading_ids(source))
        ):
            broken.append(link)
    return broken
