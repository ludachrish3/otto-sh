"""What ``otto init --kmodcov`` writes into a user's project points at the published docs (#675).

The vendored ``README.md``, the consumer starter's ``README.md`` and the
commented ``[[dev_tools]]`` entry are read inside the USER's project, where a
relative ``../../../docs/...`` link or a ``docs/...md`` source path names a
file that is not there. Each pointer must be a link into the published docs,
and each link must name a page (and heading) this checkout's ``docs/`` has.
"""

import re
from pathlib import Path

import pytest

from otto.init.templates import KMODCOV_DEV_TOOL_TEMPLATE, KMODCOV_STARTER_README_TEMPLATE
from otto.kmodcov.library import LIBRARY_DIR, SHIPPED_FILES
from tests._fixtures.paths import CHECKOUT_ROOT

DOCS_BASE = "https://otto-sh.readthedocs.io/en/stable/"
"""The published docs, as ``pyproject.toml``'s Documentation URL names them, at the release tag."""

_URL = re.compile(r"https://otto-sh\.readthedocs\.io/[^\s)>\"'`]*")
_REPO_DOC_PATH = re.compile(r"(?<![\w/.:-])(?:\.\./)*docs/[\w/.-]+\.md")


def _texts() -> dict[str, str]:
    return {
        **{f"vendored {name}": (LIBRARY_DIR / name).read_text() for name in SHIPPED_FILES},
        "consumer starter README.md": KMODCOV_STARTER_README_TEMPLATE.format(
            kmodcov_dir="third_party/otto_kmodcov"
        ),
        "commented [[dev_tools]] entry": KMODCOV_DEV_TOOL_TEMPLATE.format(
            kmodcov_dir="third_party/otto_kmodcov"
        ),
    }


def _heading_ids(page: Path) -> set[str]:
    """The ids Sphinx gives the page's headings: lower-case, punctuation dropped, spaces to '-'."""
    ids = set()
    for line in page.read_text().splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            title = re.sub(r"[`*]", "", m.group(1)).strip().lower()
            ids.add(re.sub(r"[^a-z0-9]+", "-", title).strip("-"))
    return ids


@pytest.mark.parametrize("where", list(_texts()))
def test_no_pointer_is_a_path_into_otto_s_source_tree(where):
    text = _texts()[where]
    assert not _REPO_DOC_PATH.findall(text), (where, _REPO_DOC_PATH.findall(text))


@pytest.mark.parametrize("where", list(_texts()))
def test_every_docs_link_names_a_published_page_and_heading(where):
    links = _URL.findall(_texts()[where])
    for link in links:
        assert link.startswith(DOCS_BASE), link
        page, _, anchor = link[len(DOCS_BASE) :].partition("#")
        assert page.endswith(".html"), link
        source = CHECKOUT_ROOT / "docs" / page.replace(".html", ".md")
        assert source.is_file(), link
        if anchor:
            assert anchor in _heading_ids(source), link


@pytest.mark.parametrize(
    "where", ["vendored README.md", "consumer starter README.md", "commented [[dev_tools]] entry"]
)
def test_the_readers_entry_points_link_to_the_docs(where):
    assert _URL.findall(_texts()[where]), f"{where} links to no docs page at all"
