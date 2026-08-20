"""otto test loads conftest.py from the repo root, not just the suite's dir."""

from pathlib import Path

import pytest

from tests._fixtures.labdata import lab_data_dir
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

# Reuse otto's own JSON lab fixture data (defines the "veggies" lab) so this
# throwaway repo can satisfy the mandatory --lab flag without touching a real
# host — TestConfcut itself requests no host fixture.
LAB_DATA_DIR = lab_data_dir() / "tech1"

# Appended verbatim after make_sut_repo's name/version/tests header.
SETTINGS_EXTRA = """\
[[lab.sources]]
backend = "json"
paths = ["{lab_data_dir}"]
"""

ROOT_CONFTEST = """\
import pytest

@pytest.fixture
def root_marker() -> str:
    return "from-repo-root"
"""

SUITE = """\
from otto.suite import OttoSuite


class TestConfcut(OttoSuite):
    async def test_sees_root_fixture(self, root_marker: str) -> None:
        assert root_marker == "from-repo-root"
"""


def _make_repo(root: Path) -> None:
    make_sut_repo(
        root,
        name="confrepo",
        version="0.1.0",
        tests=["tests/sub"],
        extra=SETTINGS_EXTRA.format(lab_data_dir=LAB_DATA_DIR),
        files={"conftest.py": ROOT_CONFTEST, "tests/sub/test_confcut.py": SUITE},
    )


def test_suite_in_subdir_sees_repo_root_fixture(tmp_path: Path) -> None:
    repo = tmp_path / "confrepo"
    _make_repo(repo)
    r = run_otto(
        ["test", "TestConfcut"],
        xdir=tmp_path / "xdir",
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
