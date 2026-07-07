"""Shared tmp-repo builder for ``otto test --tests`` / ``-m`` selection e2e tests.

Mirrors the tmp-repo idiom in ``tests/e2e/test_repo_wide_conftest.py``: a
throwaway SUT repo with ``.otto/settings.toml`` wired to otto's own JSON lab
fixture data, so a mandatory ``--lab`` flag is satisfiable without a real
host. Two ``OttoSuite`` classes share a marker, plus a plain pytest function
— exercising suite-less selection across suites, repos, and non-class tests.
"""

from pathlib import Path

# Reuse otto's own JSON lab fixture data (defines the "veggies" lab) so a
# throwaway repo can satisfy the mandatory --lab flag without touching a real
# host — none of these fixture suites request a host fixture.
LAB_DATA_DIR = Path(__file__).resolve().parents[1] / "_fixtures" / "lab_data" / "tech1"

SETTINGS = """\
name = "{name}"
version = "0.1.0"
lab_data_type = "json"
{labs_line}
tests = ["${{sut_dir}}/tests"]
"""

SUITE_SRC = """\
import pytest
from otto.suite import OttoSuite


class TestAlpha(OttoSuite):
    @pytest.mark.shared
    async def test_alpha_one(self) -> None:
        assert True

    async def test_alpha_two(self) -> None:
        assert True


class TestBeta(OttoSuite):
    @pytest.mark.shared
    async def test_beta_one(self) -> None:
        assert True


def test_plain_function() -> None:
    assert True
"""

# Suite-class-free fixtures for the multi-repo tests: OttoSuite subclasses
# auto-register into the process-wide SUITES registry keyed by class name, so
# reusing SUITE_SRC's TestAlpha/TestBeta across two repos in the same otto
# process would collide. A plain function has no such global registration.
PLAIN_SUITE_SRC = """\
def test_plain_function() -> None:
    assert True
"""

# A single failing plain function, used by the multi-repo worst-exit-code test.
FAILING_SUITE_SRC = """\
def test_plain_function() -> None:
    assert False
"""


def make_selection_repo(
    root: Path,
    *,
    name: str = "selrepo",
    suite_src: str = SUITE_SRC,
    with_lab: bool = True,
) -> Path:
    """Build a throwaway SUT repo with ``suite_src`` as its one test module.

    Returns the repo root (``root / name``). Non-recursive suite discovery
    means the test file must sit directly in the listed ``tests`` dir.

    ``with_lab`` controls whether this repo contributes ``LAB_DATA_DIR`` to
    the aggregated lab search paths (see ``ensure_lab_context`` in
    ``otto.cli.invoke``, which concatenates every repo's ``labs`` list
    unchanged). Pass ``False`` for every repo but one in a multi-repo test —
    two repos both pointing at the same lab data dir would load the same
    ``lab.json`` twice and collide on duplicate host IDs.

    The test module is named ``test_selection_<name>.py`` (not a fixed
    ``test_selection.py``): two repos collected in the same otto process
    otherwise import identically-named, package-less modules under the same
    ``sys.modules`` key, and the second repo's file loses to the first's
    already-cached module — silently collecting zero tests.
    """
    repo = root / name
    (repo / ".otto").mkdir(parents=True)
    labs_line = f'labs = ["{LAB_DATA_DIR}"]' if with_lab else ""
    (repo / ".otto" / "settings.toml").write_text(SETTINGS.format(name=name, labs_line=labs_line))
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / f"test_selection_{name}.py").write_text(suite_src)
    return repo
