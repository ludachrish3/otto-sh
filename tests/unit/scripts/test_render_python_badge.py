"""The overview's supported-Python badge: a local SVG rendered from ``pyproject.toml``."""

import re
import xml.etree.ElementTree as ET

import pytest

from scripts import render_python_badge as rpb
from tests._fixtures.docs_conf import assert_conf_renders
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

PYPROJECT = """
[project]
name = "x"
classifiers = [
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.10",
  "License :: OSI Approved :: MIT License",
]
"""


def test_the_value_lists_the_declared_minors_in_order() -> None:
    assert rpb.versions_text(PYPROJECT) == "3.10 | 3.12"


def test_a_pyproject_without_minor_classifiers_is_an_error() -> None:
    with pytest.raises(ValueError, match="no 'Programming Language"):
        rpb.versions_text('[project]\nname = "x"\nclassifiers = []\n')


def test_the_badge_is_self_contained_svg_that_names_the_versions() -> None:
    """Parses as SVG, carries the versions as text, and references nothing remote."""
    svg = rpb.render(PYPROJECT)
    root = ET.fromstring(svg)  # noqa: S314 — parsing our own renderer's output
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert texts == ["python", "3.10 | 3.12"]
    assert root.get("aria-label") == "python: 3.10 | 3.12"
    # The SVG namespace is the only URL; no href, no external font or image.
    assert re.findall(r"https?://[^\"']+", svg) == ["http://www.w3.org/2000/svg"]
    assert "href" not in svg


def test_the_default_render_reads_the_checkout_pyproject() -> None:
    expected = rpb.versions_text((CHECKOUT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert f">{expected}</text>" in rpb.render()


def test_main_writes_the_badge_to_the_output_path(tmp_path) -> None:
    out = tmp_path / "badge.svg"
    assert rpb.main(["--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == rpb.render()


def test_the_docs_build_renders_the_badge_and_fails_when_the_renderer_does() -> None:
    assert_conf_renders("scripts.render_python_badge", "docs/_static/generated/python-badge.svg")


def test_sphinx_srcs_names_pyproject_and_the_renderer() -> None:
    """``make docs`` must re-trigger when a Python classifier is added or dropped."""
    makefile = (CHECKOUT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    assert "pyproject.toml" in srcs.group(0)
    assert "scripts/render_python_badge.py" in srcs.group(0)
