"""Text otto shows a user at runtime points at the published docs, never at otto's source (#700).

Error, warning and refusal messages, check-report hints and CLI help are read
wherever otto is installed, where a ``docs/...`` path names a file that is not
there. This guard collects every string constant in ``src/otto`` that is not
a bare string statement (module, class, function and attribute docstrings are
read in source or rendered by Sphinx; constants are what otto prints) and
holds each to the checks ``otto init``'s templates use: no path into otto's
``docs/`` tree, and every docs link names a published page and heading.

A docstring that DOES reach a user (a Typer command's help, an options
class's field help) is not a constant and so is not seen here; those were
checked by hand when this guard landed.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.doc_links import DOCS_URL, OUTSIDE_THE_REPO, broken_links, source_paths
from tests._fixtures.paths import CHECKOUT_ROOT

SRC = CHECKOUT_ROOT / "src" / "otto"

ALLOWED_LINKS = {
    "https://otto-sh.readthedocs.io/schemas": (
        "the JSON-schema $id namespace (models/jsonschema.py), an identifier, not a page"
    ),
}
"""Docs-host strings that are not links to a page, each with the reason it may stay."""


def _constants(path: Path, text: str | None = None) -> list[tuple[int, str]]:
    """Return ``(line, text)`` for every string constant in *path* that is not a bare statement."""
    tree = ast.parse(path.read_text() if text is None else text, filename=str(path))
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare
    ]


def _offenders(check) -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text()
        if "docs" not in source:  # every check looks for docs/ or the readthedocs host
            continue
        where = path.relative_to(CHECKOUT_ROOT)
        for line, text in _constants(path, source):
            found.extend(f"{where}:{line}: {hit}" for hit in check(text))
    return found


def test_the_guard_reads_otto_s_messages():
    texts = [text for _, text in _constants(SRC / "instruction_catalog.py")]
    assert any("ProjectActions subclass instead" in text for text in texts)


def test_no_message_points_into_otto_s_docs_source_tree():
    assert _offenders(source_paths) == [], OUTSIDE_THE_REPO


def test_every_docs_link_in_a_message_names_a_published_page_and_heading():
    def check(text: str) -> list[str]:
        return [link for link in broken_links(text) if link not in ALLOWED_LINKS]

    assert _offenders(check) == [], OUTSIDE_THE_REPO


@pytest.mark.parametrize("link", list(ALLOWED_LINKS))
def test_every_allowed_link_is_still_in_the_source(link):
    def check(text: str) -> list[str]:
        return [hit for hit in DOCS_URL.findall(text) if hit == link]

    assert _offenders(check), f"{link} is allowed but no longer used; drop its entry"


@pytest.mark.parametrize(
    "text",
    [
        "see docs/cli/link/check (When a row isn't pass)",
        "and docs/cli/tunnel/add",
        "(docs/setup/host-scopes.md).",
        "docs/configuration/lab-config (Per-host fields: creds)",
    ],
)
def test_a_bare_page_path_is_caught(text):
    assert source_paths(text)
