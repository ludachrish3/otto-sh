#!/usr/bin/env python3
"""Derive the relocatable offline docs tree from a stock Sphinx HTML build.

Copies the tree, then rewrites external documentation links whose targets have
an offline source (see docs/installation.md's "Offline documentation") into
depth-correct relative paths onto sibling directories, so the result works
from file:// or any static server at any sub-path. Sphinx's own internal
links are already relative; only these absolute external prefixes change.

Only ``href="..."`` attributes are rewritten — the same URLs appear as
DISPLAYED TEXT in the installation page (mirror instructions), and rewriting
those would make the offline copy misquote itself.

typer.tiangolo.com and docs.pydantic.dev are deliberately absent: no official
offline archive exists, so those links stay live (dead offline, correct
online), as the installation page documents.

Usage: python scripts/relativize_docs_links.py docs/_build/html docs/_build/offline
"""

import shutil
import sys
from pathlib import Path

PREFIXES = {
    "https://docs.python.org/3/": "python/",
    "https://docs.pytest.org/en/stable/": "pytest/",
    "https://rich.readthedocs.io/en/stable/": "rich/",
    "https://asyncssh.readthedocs.io/en/stable/": "asyncssh/",
    "https://telnetlib3.readthedocs.io/en/latest/": "telnetlib3/",
}


def rewrite_html(html: str, depth: int) -> str:
    """Rewrite mapped external hrefs for a page ``depth`` directories below root."""
    up = "../" * (depth + 1)
    for prefix, local in PREFIXES.items():
        html = html.replace(f'href="{prefix}', f'href="{up}{local}')
    return html


def main(argv: list[str]) -> int:
    """Copy the HTML tree at ``argv[0]`` to ``argv[1]``, rewriting mapped hrefs."""
    if len(argv) != 2:  # noqa: PLR2004 — the CLI takes exactly two positional paths (src, dst)
        print("usage: relativize_docs_links.py <html-src> <dst>", file=sys.stderr)
        return 2
    src, dst = Path(argv[0]), Path(argv[1])
    if dst.exists():
        print(f"refusing to overwrite existing {dst}", file=sys.stderr)
        return 2
    shutil.copytree(src, dst)
    for page in dst.rglob("*.html"):
        depth = len(page.relative_to(dst).parts) - 1
        page.write_text(rewrite_html(page.read_text(encoding="utf-8"), depth), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
