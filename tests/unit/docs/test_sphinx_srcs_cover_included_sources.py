"""Every non-Python source under ``src/otto`` that a docs page includes is a ``make docs`` input.

``SPHINX_SRCS`` globs ``src/otto/**/*.py`` but lists any other file by name. A page
that literalincludes one the list lacks does not rebuild when only that file
changes, so the published page shows the old text.
"""

import re
from pathlib import Path

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

INCLUDE = re.compile(r"^\s*(?:`{3,}|:{3,})\{(?:literal)?include\}\s+(\S+)", re.MULTILINE)


def _included_sources() -> dict[str, Path]:
    """Map each non-``.py`` file under ``src/otto`` that a docs page includes to the page."""
    found: dict[str, Path] = {}
    src = (CHECKOUT_ROOT / "src" / "otto").resolve()
    for page in (CHECKOUT_ROOT / "docs").rglob("*.md"):
        if "_build" in page.parts:
            continue
        for target in INCLUDE.findall(page.read_text(encoding="utf-8")):
            path = (page.parent / target).resolve()
            if path.suffix != ".py" and path.is_relative_to(src):
                found[path.relative_to(CHECKOUT_ROOT.resolve()).as_posix()] = page
    return found


def test_the_scan_sees_the_known_includes() -> None:
    assert "src/otto/kmodcov/kmodcov.h" in _included_sources()


def test_every_included_source_is_a_sphinx_input() -> None:
    makefile = (CHECKOUT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    listed = set(re.findall(r"\bsrc/otto/\S+", srcs.group(0)))
    missing = {path: page for path, page in _included_sources().items() if path not in listed}
    assert not missing, f"add to SPHINX_SRCS in the Makefile: {sorted(missing)}"
