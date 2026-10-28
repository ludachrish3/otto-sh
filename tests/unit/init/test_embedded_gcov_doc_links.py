"""What ``otto init --embedded-gcov`` writes into a user's project points at the published docs.

The consumer starter's ``README.md`` and the commented ``[coverage.embedded]``
block are read inside the USER's project. The generic walker in
``test_init_doc_links`` already holds both templates to "a link into the
published docs naming a page and heading this checkout has"; this module pins
what is specific to the area.
"""

import pytest

from otto.init.config import DEFAULT_EMBEDDED_GCOV_DIR
from otto.init.templates import (
    EMBEDDED_GCOV_SETTINGS_TEMPLATE,
    EMBEDDED_GCOV_STARTER_README_TEMPLATE,
)
from tests._fixtures.doc_links import DOCS_URL


def _texts() -> dict[str, str]:
    return {
        "consumer starter README.md": EMBEDDED_GCOV_STARTER_README_TEMPLATE.format(
            embedded_gcov_dir=DEFAULT_EMBEDDED_GCOV_DIR
        ),
        "commented [coverage.embedded] block": EMBEDDED_GCOV_SETTINGS_TEMPLATE.format(
            embedded_gcov_dir=DEFAULT_EMBEDDED_GCOV_DIR
        ),
    }


@pytest.mark.parametrize("where", list(_texts()))
def test_the_readers_entry_points_link_to_the_embedded_page(where):
    assert any("instrumenting/embedded.html" in url for url in DOCS_URL.findall(_texts()[where]))


@pytest.mark.parametrize("where", list(_texts()))
def test_each_says_it_needs_a_gnu_compiler_and_the_gcov_format(where):
    text = " ".join(_texts()[where].replace("#", " ").split())
    assert "GNU compiler" in text
    assert "gcov format" in text
