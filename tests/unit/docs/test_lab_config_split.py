"""The lab configuration reference: an index page plus one page per area.

A reader who arrives from an old bookmark lands on ``lab-config.html``; every
section that page used to hold must still exist on the index or an area page,
and the index must name every area page so the reader can find it.
"""

from tests._fixtures.doc_links import heading_ids
from tests._fixtures.paths import CHECKOUT_ROOT

_CONFIG = CHECKOUT_ROOT / "docs" / "configuration"
_INDEX = _CONFIG / "lab-config.md"
_AREA_DIR = _CONFIG / "lab"

# Every heading id and label lab-config.md published before the split.
FORMER_IDS = {
    "common-optional",
    "coverage-toolchain",
    "declaring-toolchain-tools-in-lab-data",
    "docker-capable-hosts",
    "elements",
    "embedded-only-fields",
    "example",
    "host-identity",
    "host-identity-naming",
    "host-preferences",
    "host-type-os",
    "lab-configuration",
    "lab-files",
    "lab-links",
    "lab-power-control",
    "links",
    "merged-labs-and-containers",
    "migrating-from-the-hosts-array",
    "network-interfaces",
    "option-tables",
    "per-host-fields",
    "power-control",
    "product-host-preferences",
    "project-scope",
    "project-scope-required",
    "project-scope-the-labs-and-hosts-a-project-targets",
    "referencing-the-inventory",
    "required",
    "required-once-a-project-provides-products-or-dev-tools",
    "snmp-monitoring",
    "splitting-a-lab-across-files",
    "the-labs-table",
    "what-the-declaration-changes",
}

AREA_PAGES = {
    "coverage-toolchain",
    "docker",
    "elements",
    "files-and-membership",
    "hops-and-embedded",
    "host-fields",
    "host-identity",
    "host-preferences",
    "inventory-references",
    "links",
    "migrating",
    "network-interfaces",
    "power",
    "project-scope",
    "snmp",
    "toolchain-tools",
}


def _area_pages():
    return sorted(_AREA_DIR.glob("*.md"))


def test_every_former_heading_still_has_a_home():
    found = heading_ids(_INDEX)
    for page in _area_pages():
        found |= heading_ids(page)
    assert FORMER_IDS - found == set()


def test_the_index_names_every_area_page():
    assert {page.stem for page in _area_pages()} == AREA_PAGES
    text = _INDEX.read_text()
    assert sorted(stem for stem in AREA_PAGES if f"lab/{stem}" not in text) == []
