"""Where otto registers OttoSuites from, and why it is narrower than pytest.

`Repo.iter_test_files` is the third and narrowest reader of a repo's tests
dirs, and the only one that EXECUTES what it returns — `import_test_files`
runs at bootstrap, on every otto command. So it reads only the top level of
each configured directory: recursion would hand otto the whole test tree,
where one import error becomes a startup error.

That is a contract, not an oversight, which is why it is pinned from both
sides here: a nested suite is NOT registered, listing its directory DOES
register it, and pytest collection is unaffected either way.
"""

import shutil
import sys
from pathlib import Path

import pytest

from otto.config.repo import Repo
from otto.suite.register import SUITES
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.sutrepo import make_sut_repo

_REPO1_DIR = TESTS_ROOT / "repo1"


@pytest.fixture
def nested_repo(tmp_path: Path):
    """A copy of repo1 with a second suite one directory deeper.

    A copy, not repo1 itself: this writes a file into the tests dir, and
    repo1 is shared with every other suite test in the worker.
    """
    root = tmp_path / "repo1"
    shutil.copytree(_REPO1_DIR, root)
    top = root / "tests" / "test_device.py"
    nested_dir = root / "tests" / "device"
    nested_dir.mkdir()
    # Stem, not just class, distinct from every file in repo1: `import_test_file`
    # keys its module on `_otto_suite_<stem>` alone, so a repo1 file of the same
    # name would make this one a silent no-op and the failure would read as
    # "the escape hatch is broken".
    (nested_dir / "test_nested_probe.py").write_text(
        top.read_text().replace("class TestDevice", "class TestNestedProbe")
    )

    # Evict UP FRONT, not just at teardown: `import_test_file` keys the module
    # on `_otto_suite_<stem>`, which is global by stem, so repo1's own
    # `test_device.py` — imported by any earlier test in this worker — makes
    # the copy's import a silent no-op and the positive controls below
    # vacuously false. Same hazard `test_import_and_register.clean_registry`
    # exists for.
    evicted = {m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("_otto_suite_")}
    before = set(SUITES.names())
    yield root
    for name in set(SUITES.names()) - before:
        SUITES.unregister(name)
    for mod in [m for m in sys.modules if m.startswith("_otto_suite_")]:
        sys.modules.pop(mod, None)
    sys.modules.update(evicted)
    # sys.path needs no bookkeeping here: the root conftest's `_isolate_sys_path`
    # and this package's autouse `_isolate_suites` already roll back both.


def _registered(repo: Repo, before: set[str]) -> set[str]:
    repo.import_test_files()
    return set(SUITES.names()) - before


def test_a_nested_suite_is_not_registered_from_its_parent_dir(nested_repo: Path) -> None:
    """The boundary. `tests/device/test_nested_probe.py` is invisible to the importer."""
    repo = Repo(sut_dir=nested_repo)
    repo.add_libs_to_pythonpath()
    repo.tests = [nested_repo / "tests"]
    before = set(SUITES.names())

    added = _registered(repo, before)
    assert "TestDevice" in added, "positive control: the top-level suite must register"
    assert "TestNestedProbe" not in added


def test_listing_the_subdirectory_registers_it(nested_repo: Path) -> None:
    """The escape hatch, and the reason the boundary is acceptable: `tests` is a
    LIST, so nesting is opt-in rather than impossible."""
    repo = Repo(sut_dir=nested_repo)
    repo.add_libs_to_pythonpath()
    repo.tests = [nested_repo / "tests", nested_repo / "tests" / "device"]
    before = set(SUITES.names())

    added = _registered(repo, before)
    assert "TestNestedProbe" in added
    assert "TestDevice" in added


def test_the_boundary_is_registration_only_not_collection(tmp_path: Path) -> None:
    """A plain `test_*` function under a subdirectory still RUNS.

    Without this the boundary reads as "otto ignores nested tests", which is
    a much bigger claim than the one being made — `collect_tests` hands the
    directory to pytest, which recurses as usual.

    A bare tmp repo rather than the repo1 copy: `collect_tests` runs a nested
    `pytest.main` IN PROCESS, so pointing it at repo1's tests would import
    them and register their suites as a side effect of COLLECTION. (This
    worker's conftests roll that back afterwards, so it would not leak — it
    would just make the assertion below depend on machinery this test is not
    about.) Deliberately no suite here, and worth stating what that avoids: a
    nested suite file IS registered by that import, under pytest's own module
    name, far too late for the `otto test` group already built.
    """
    make_sut_repo(
        tmp_path,
        name="nested_probe",
        version="0.0.0",
        tests=["tests"],
        files={"tests/device/test_plain.py": "def test_plain_nested():\n    assert True\n"},
    )
    repo = Repo(sut_dir=tmp_path)
    repo.tests = [tmp_path / "tests"]

    assert repo.iter_test_files() == [], "positive control: nothing here is registrable"
    assert "test_plain_nested" in {t.name for t in repo.collect_tests()}


def test_iter_test_files_reads_only_the_top_level(nested_repo: Path) -> None:
    """The unit statement of the same rule, so a change to the glob fails here
    and not only through the two registration paths above."""
    repo = Repo(sut_dir=nested_repo)
    repo.tests = [nested_repo / "tests"]
    found = {p.relative_to(nested_repo).as_posix() for p in repo.iter_test_files()}

    assert "tests/test_device.py" in found
    assert "tests/device/test_nested_probe.py" not in found
    assert all(p.count("/") == 1 for p in found), found
