"""The docs' Jsonnet templates render the documents their goldens hold.

The bed case study's two example projects read their labs from Jsonnet
templates (``lab_data/lab.jsonnet``) that replaced hand-written ``lab.json``
files, and two reference pages show a template. Each template is held to a
golden under ``tests/_fixtures/goldens/`` (:data:`tests._fixtures.paths.GOLDENS`).
Three goldens are frozen copies of what a template replaced: the two
``lab.json`` files (``getting_started_lab.json`` and
``getting_started_inventory_lab.json``) and the reservation page's rig
block. The fourth, ``pages/lab-config-devboxes.json``, was written with the
lab-config page's new templating section, which replaced no JSON; it is the
document that section's template must render.

* ``getting-started`` writes its hosts inline, so its template renders the
  copy's document itself: the two read by otto's one lab-file reader give the
  same sections, as ``tests/unit/labs/test_testbed_templates.py`` holds the
  bed cell to its golden, ``_``-prefixed comment keys aside (the copy's
  ``_doc_*`` markers and ``_comment`` are ``//`` comments in the template, and
  the loader drops them);
* ``getting-started-inventory`` references its inventory, qualified
  (``{"name": "default", "key": ...}``) where the copy wrote the bare key, so
  its labs are compared once built: every lab, host by host, against what
  ``tests/unit/labs/test_unchanged_loading.py`` recorded for the copy before
  templating existed;
* a page's template is the ``jsonnet`` block after a
  ``<!-- template: NAME -->`` marker, rendered as a project's
  ``lab_data/lab.jsonnet`` under the source table in the ``toml`` block after
  ``<!-- template-source: NAME -->`` (a plain ``jsonnet`` source over
  ``lab_data`` when the page shows none), and its golden is
  ``goldens/pages/NAME.json``.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from otto.config.project import Project
from otto.models.base import strip_comment_keys
from scripts._lab_document import read_cell_document
from tests._fixtures.gs_example import EXAMPLE
from tests._fixtures.lab_projection import lab_projection, register_fixture_host_extensions
from tests._fixtures.marked_blocks import marked_blocks
from tests._fixtures.paths import CHECKOUT_ROOT, GETTING_STARTED_GOLDEN, GOLDENS
from tests.unit.labs.test_unchanged_loading import LOADING_GOLDEN
from tests.unit.templating import conftest as _templating
from tests.unit.templating._lab_project import listed_labs, project, write

# The templating conftest's fixtures: the binding proof every templated test
# file carries, and a private OTTO_HOME.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test
otto_home = _templating.otto_home

TWIN = CHECKOUT_ROOT / "docs" / "examples" / "getting-started-inventory"
PAGES = {
    "lab-config-devboxes": CHECKOUT_ROOT / "docs" / "configuration" / "lab-config.md",
    "reservation-rig": CHECKOUT_ROOT / "docs" / "cli" / "reservation" / "index.md",
}
"""Every page template, by name, and the page that shows it."""

PLAIN_SOURCE = '[[lab.sources]]\nbackend = "jsonnet"\npaths = ["lab_data"]\n'


def _comment_free(value: Any) -> Any:
    """*value* with every ``_``-prefixed key dropped, at every level, as the loader drops them."""
    if isinstance(value, dict):
        return {k: _comment_free(v) for k, v in strip_comment_keys(value).items()}
    if isinstance(value, list):
        return [_comment_free(v) for v in value]
    return value


def _rendered(root: Path) -> Any:
    """The document *root*'s ``lab_data/lab.jsonnet`` renders, as ``otto lab render`` renders it."""
    from otto.inventory import build_inventory_view
    from otto.labs.inspect import render_entry

    projects = [Project(root=root)]
    return render_entry(
        entry=root / "lab_data" / "lab.jsonnet",
        source=None,
        projects=projects,
        view=build_inventory_view(projects=projects),
    ).document


def test_the_bed_template_renders_its_former_lab_json(otto_home: Path) -> None:
    from otto.labs.documents import read_lab_document

    rendered = read_cell_document(EXAMPLE / "lab_data" / "lab.jsonnet")
    golden = read_lab_document(GETTING_STARTED_GOLDEN).sections
    assert _comment_free(rendered) == _comment_free(golden)


def test_the_twin_template_composes_the_labs_its_former_lab_json_composed(
    otto_home: Path,
) -> None:
    from otto.session import build_lab

    former = json.loads(LOADING_GOLDEN.read_text())["getting-started-inventory"]
    register_fixture_host_extensions()
    twin = Project(root=TWIN)
    projects = [twin]
    labs = listed_labs(twin)  # through the inventory view, as --list-labs lists them
    assert labs == sorted(former)
    for lab in labs:
        assert lab_projection(build_lab(projects, labs=[lab])) == former[lab], lab


def _marked_block(page: str, marker: str, lang: str) -> "str | None":
    """The ``lang`` block after ``<!-- marker -->`` on *page*, or ``None``; at most one."""
    found = marked_blocks(page, re.escape(marker), lang)
    assert len(found) <= 1, f"the page marks {marker!r} {len(found)} times"
    return found[0].body if found else None


def test_every_page_template_is_marked_once_and_only_where_listed() -> None:
    docs = CHECKOUT_ROOT / "docs"
    marked = sorted(
        (block.name, page.relative_to(docs).as_posix())
        for page in docs.rglob("*.md")
        if "_build" not in page.parts
        for block in marked_blocks(page.read_text(), r"template: (?P<name>[\w-]+)", "jsonnet")
    )
    assert marked == sorted(
        (name, page.relative_to(docs).as_posix()) for name, page in PAGES.items()
    )


@pytest.mark.parametrize("name", sorted(PAGES))
def test_a_page_template_renders_its_golden(name: str, tmp_path: Path, otto_home: Path) -> None:
    page = PAGES[name].read_text()
    template = _marked_block(page, f"template: {name}", "jsonnet")
    assert template is not None, f"{PAGES[name]} shows no template {name!r}"
    source = _marked_block(page, f"template-source: {name}", "toml") or PLAIN_SOURCE
    root = tmp_path / name
    project(root, source)
    write(root / "lab_data" / "lab.jsonnet", template)
    golden = json.loads((GOLDENS / "pages" / f"{name}.json").read_text())
    assert _rendered(root) == _comment_free(golden)
