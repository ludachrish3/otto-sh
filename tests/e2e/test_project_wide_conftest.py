"""otto test loads conftest.py from the project root, not just the test file's dir."""

from pathlib import Path

import pytest

from tests._fixtures.labdata import lab_data_dir
from tests._fixtures.project_scaffold import make_project
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

# Reuse otto's own JSON lab fixture data (defines the "unix" lab) so this
# throwaway project can satisfy the mandatory --lab flag without touching a real
# host — TestConfcut itself requests no host fixture.
LAB_DATA_DIR = lab_data_dir() / "tech1"

# Appended verbatim after make_project's name/version/tests header.
SETTINGS_EXTRA = """\
[[lab.sources]]
backend = "json"
paths = ["{lab_data_dir}"]
"""

ROOT_CONFTEST = """\
import pytest

@pytest.fixture
def root_marker() -> str:
    return "from-project-root"
"""

SUITE = """\
class TestConfcut:
    async def test_sees_root_fixture(self, root_marker: str) -> None:
        assert root_marker == "from-project-root"
"""


def _make_project(root: Path) -> None:
    make_project(
        root,
        name="confrepo",
        version="0.1.0",
        tests=["tests/sub"],
        extra=SETTINGS_EXTRA.format(lab_data_dir=LAB_DATA_DIR),
        files={"conftest.py": ROOT_CONFTEST, "tests/sub/test_confcut.py": SUITE},
    )


def test_class_in_subdir_sees_project_root_fixture(tmp_path: Path) -> None:
    project = tmp_path / "confproject"
    _make_project(project)
    r = run_otto(
        ["test", "TestConfcut"],
        xdir=tmp_path / "xdir",
        roots=project,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
