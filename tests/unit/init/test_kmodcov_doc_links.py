"""What ``otto init --kmodcov`` writes into a user's project points at the published docs (#675).

The vendored ``README.md``, the consumer starter's ``README.md`` and the
commented ``[[dev_tools]]`` entry are read inside the USER's project, where a
relative ``../../../docs/...`` link or a ``docs/...md`` source path names a
file that is not there. Each pointer must be a link into the published docs,
and each link must name a page (and heading) this checkout's ``docs/`` has.
"""

import pytest

from otto.init.templates import KMODCOV_DEV_TOOL_TEMPLATE, KMODCOV_STARTER_README_TEMPLATE
from otto.kmodcov.library import LIBRARY_DIR, SHIPPED_FILES
from tests._fixtures.doc_links import DOCS_URL, broken_links, source_paths


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


@pytest.mark.parametrize("where", list(_texts()))
def test_no_pointer_is_a_path_into_otto_s_source_tree(where):
    assert source_paths(_texts()[where]) == []


@pytest.mark.parametrize("where", list(_texts()))
def test_every_docs_link_names_a_published_page_and_heading(where):
    assert broken_links(_texts()[where]) == []


@pytest.mark.parametrize(
    "where", ["vendored README.md", "consumer starter README.md", "commented [[dev_tools]] entry"]
)
def test_the_readers_entry_points_link_to_the_docs(where):
    assert DOCS_URL.findall(_texts()[where]), f"{where} links to no docs page at all"
