"""Every file ``otto init`` writes points at the published docs, never at otto's source (#700).

The settings file, the lab data, the lab README, the init module and the
notices ``otto init`` prints are read inside the USER's project, where a
``docs/...md`` path names a file that is not there. The checks are the ones
the vendored kmodcov files use (#675); this guard walks every template the
scaffolder has, so a new template is covered the day it lands.
"""

import json

import pytest

from otto.init import areas, templates
from tests._fixtures.doc_links import OUTSIDE_THE_REPO, broken_links, source_paths


def _texts() -> dict[str, str]:
    """Return every template in :mod:`otto.init.templates` as written, and every string
    :mod:`otto.init.areas` holds at module level (the notices it prints among them)."""
    texts = {}
    for name, value in vars(templates).items():
        if not name.isupper():
            continue
        if isinstance(value, str):
            texts[name] = value
        elif isinstance(value, dict):
            texts[name] = json.dumps(value, indent=4)
    for name, value in vars(areas).items():
        if isinstance(value, str) and not name.startswith("__"):
            texts[f"areas.{name}"] = value
    return texts


def test_the_guard_sees_the_templates_that_carry_pointers():
    assert {"SETTINGS_TEMPLATE", "LAB_JSON_TEMPLATE", "LAB_README_TEMPLATE"} <= set(_texts())
    assert {"INSTRUCTIONS_TEMPLATE", "areas._VSCODE_SETTINGS_KEPT"} <= set(_texts())


@pytest.mark.parametrize("where", list(_texts()))
def test_no_pointer_is_a_path_into_otto_s_source_tree(where):
    assert source_paths(_texts()[where]) == [], OUTSIDE_THE_REPO


@pytest.mark.parametrize("where", list(_texts()))
def test_every_docs_link_names_a_published_page_and_heading(where):
    assert broken_links(_texts()[where]) == [], OUTSIDE_THE_REPO


@pytest.mark.parametrize(
    "text",
    [
        "Reference: docs/configuration/settings.md.",
        "see `docs/configuration/lab-config.md`",
        "reservation gate; see docs/cli/reservation/ ---",
        "[link](../../../docs/cli/run/defaults.md)",
    ],
)
def test_a_source_path_is_caught(text):
    assert source_paths(text)


_BASE = "https://otto-sh.readthedocs.io/en/stable/"


@pytest.mark.parametrize(
    "link",
    [
        _BASE + "configuration/no-such-page.html",
        _BASE + "configuration/lab-config.html#no-such-heading",
        "https://otto-sh.readthedocs.io/en/latest/configuration/lab-config.html",
        _BASE + "configuration/lab-config.md",
        "http://otto-sh.readthedocs.io/en/stable/configuration/lab-config.html",
        "otto-sh.readthedocs.io/en/stable/configuration/lab-config.html",
        # MyST's heading_anchors slug: resolves [](#...) at build time, is no HTML id.
        _BASE + "configuration/lab-config.html#host-identity--naming",
        # `# .otto/settings.toml` there sits inside a fenced block: not a heading.
        _BASE + "configuration/lab-config.html#otto-settings-toml",
        # A repeated title's second section gets a numbered id, not a suffixed one.
        _BASE + "extend/extending-backends.html#the-callable-contract-1",
    ],
)
def test_a_link_to_a_missing_page_or_heading_is_caught(link):
    assert broken_links(f"See {link}.") == [link]


@pytest.mark.parametrize(
    "link",
    [
        _BASE + "configuration/lab-config.html#migrating-from-the-hosts-array",
        # "Host identity & naming": the HTML id joins the '&' run with one '-'.
        _BASE + "configuration/lab-config.html#host-identity-naming",
        # Every level has an id, an h4 included.
        _BASE + "configuration/host-options.html#port-forwarding",
        # A `(lab-links)=` label is an id too.
        _BASE + "configuration/lab-config.html#lab-links",
        # A repeated title keeps its id on the first section.
        _BASE + "extend/extending-backends.html#the-callable-contract",
    ],
)
def test_a_link_to_a_real_page_and_heading_passes(link):
    assert broken_links(f"See {link}.") == []
