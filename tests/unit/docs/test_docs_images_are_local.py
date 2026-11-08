"""Every image the docs show ships inside the docs build.

The release attaches an offline docs tarball for air-gapped labs
(docs/installation.md, "Offline documentation"). A hotlinked image — the
img.shields.io Python badge was one — renders as a broken image there, and
scripts/relativize_docs_links.py only rewrites ``href`` links, never ``src``.
So a page's images must be files in the tree: a Markdown image, a
reST image/figure directive, or a raw ``<img>`` whose target is an
http(s) URL fails here.
"""

import re

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

DOCS = CHECKOUT_ROOT / "docs"
SKIP_PARTS = {"_build", "superpowers", "_inventories"}

REMOTE_IMAGE = re.compile(
    r"!\[[^\]]*\]\(\s*https?://"  # Markdown ![alt](https://…)
    r"|\.\.\s+(?:image|figure)::\s*https?://"  # reST .. image:: https://…
    r"|```\{(?:image|figure)\}\s*https?://"  # MyST ```{image} https://…
    r"|<img\b[^>]*\bsrc=[\"']https?://",  # raw HTML <img src="https://…">
    re.IGNORECASE,
)


def _sources() -> "list":
    return sorted(
        p
        for p in DOCS.rglob("*")
        if p.suffix in {".md", ".rst"} and not SKIP_PARTS.intersection(p.relative_to(DOCS).parts)
    )


def test_the_scan_sees_the_docs_tree() -> None:
    names = {p.relative_to(DOCS).as_posix() for p in _sources()}
    assert {"index.rst", "overview.md"} <= names


@pytest.mark.parametrize(
    "line",
    [
        "![Python](https://img.shields.io/badge/python-3.10-blue)",
        ".. image:: https://example.com/x.png",
        "```{image} http://example.com/x.png",
        '<img alt="x" src="https://example.com/x.png">',
    ],
)
def test_the_pattern_catches_each_remote_image_spelling(line: str) -> None:
    assert REMOTE_IMAGE.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "![Supported Python versions](_static/generated/python-badge.svg)",
        '<img src="_static/otto-hero.png" alt="otto-sh">',
        "[a link, not an image](https://example.com/x.png)",
    ],
)
def test_the_pattern_passes_local_images_and_plain_links(line: str) -> None:
    assert not REMOTE_IMAGE.search(line)


def test_no_docs_page_hotlinks_an_image() -> None:
    offenders = [
        f"{path.relative_to(CHECKOUT_ROOT)}:{lineno}: {line.strip()}"
        for path in _sources()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if REMOTE_IMAGE.search(line)
    ]
    assert not offenders, "remote images break the offline docs:\n" + "\n".join(offenders)
