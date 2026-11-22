"""Shared tmp-project builder for ``otto test NAMES`` / ``-m`` selection e2e tests.

Mirrors the tmp-project idiom in ``tests/e2e/test_project_wide_conftest.py``: a
throwaway project with ``.otto/settings.toml`` wired to otto's own JSON lab
fixture data, so a mandatory ``--lab`` flag is satisfiable without a real
host. Two test classes share a marker, plus a plain pytest function —
exercising selection across classes, projects, and non-class tests.
"""

from pathlib import Path

from tests._fixtures.labdata import tech1_lab_source
from tests._fixtures.project_scaffold import make_project

# Reuse otto's own lab fixture data, the bed's cell (it defines the "unix" lab), so a
# throwaway project can satisfy the mandatory --lab flag without touching a real
# host — none of these fixture suites request a host fixture.

# Appended verbatim after make_project's name/version/tests header.
SETTINGS_EXTRA = tech1_lab_source()

SUITE_SRC = """\
import pytest


class TestAlpha:
    @pytest.mark.shared
    async def test_alpha_one(self) -> None:
        assert True

    async def test_alpha_two(self) -> None:
        assert True


class TestBeta:
    @pytest.mark.shared
    async def test_beta_one(self) -> None:
        assert True


def test_plain_function() -> None:
    assert True
"""

# Class-free fixtures for the multi-project tests: one plain function per project,
# so each project's selection is exactly one test.
PLAIN_SUITE_SRC = """\
def test_plain_function() -> None:
    assert True
"""

# A single failing plain function, used by the multi-project worst-exit-code test.
FAILING_SUITE_SRC = """\
def test_plain_function() -> None:
    assert False
"""


def make_selection_project(
    root: Path,
    *,
    name: str = "selrepo",
    suite_src: str = SUITE_SRC,
    with_lab: bool = True,
) -> Path:
    """Build a throwaway project with ``suite_src`` as its one test module.

    Returns the project root (``root / name``).

    ``with_lab`` controls whether this project contributes the tech1 lab to
    the aggregated lab search paths (see ``ensure_lab_context`` in
    ``otto.cli.invoke``, which concatenates every project's ``[[lab.sources]]``
    entries unchanged). Pass ``False`` for every project but one in a multi-project
    test — two projects both pointing at the same lab data dir would load the
    same ``lab.json`` twice and collide on duplicate host IDs.

    The test module is named ``test_selection_<name>.py`` (not a fixed
    ``test_selection.py``): two projects collected in the same otto process
    otherwise import identically-named, package-less modules under the same
    ``sys.modules`` key, and the second project's file loses to the first's
    already-cached module — silently collecting zero tests.
    """
    return make_project(
        root / name,
        name=name,
        version="0.1.0",
        tests=["tests"],
        extra=SETTINGS_EXTRA if with_lab else "",
        files={f"tests/test_selection_{name}.py": suite_src},
    )
