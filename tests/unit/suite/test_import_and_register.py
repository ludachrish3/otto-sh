"""
Integration tests for the test-file auto-scan → suite-registration pipeline,
using the real tests/repo1 SUT repo.

These tests cover the bug fix in ``configmodule.repo.import_test_files()``:
the original implementation tried to derive a dotted module name from
``sys.path`` entries and silently skipped files whose parent directory was
not on ``sys.path``.  The fix uses ``importlib.util.spec_from_file_location``
to load files directly by path, independent of ``sys.path``.

Two concerns are exercised end-to-end:

1. **Registration** — ``import_test_files()`` triggers auto-registration (via
   ``OttoSuite.__init_subclass__``) on ``TestDevice`` and the suite lands in
   the ``SUITES`` registry, even though ``tests/repo1/tests`` is not on
   ``sys.path``.

2. **Help-menu fidelity** — the generated Typer command exposes the full set
   of expected options: the five common options, the two ``TestDevice``-specific
   options (``firmware``, ``check_interfaces``), and the two ``RepoOptions``
   repo-wide options (``device_type``, ``lab_env``).
"""

import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from otto.configmodule.repo import Repo
from otto.suite.register import SUITES

runner = CliRunner()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# tests/unit/suite/ → tests/unit/ → tests/ → repo1/
_REPO1_DIR: Path = Path(__file__).parents[2] / "repo1"
_REPO1_TESTS_DIR: Path = _REPO1_DIR / "tests"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_registry():
    """Isolate the repo1 suite world; restore it exactly afterwards.

    The global ``SUITES`` registry and Python's import cache couple these
    tests to any earlier test in the same worker that imported repo1's test
    files: cached ``_otto_suite_*`` modules make ``import_test_files()`` a
    no-op, the decorators never re-run, and delta assertions become
    order-dependent. Park pre-registered suites (with their origins), evict
    every cached ``_otto_suite_*`` module, and restore both at teardown.
    """
    parked = {}
    for name in list(SUITES.names()):
        entry = SUITES.get(name)
        origin = SUITES.origin(name)
        # Two origin flavors both mean "repo1's suite world": the auto-scan
        # `_otto_suite_*` module names, AND pytest's own module names left by
        # an in-process `pytest.main([suite_file])` run (run_suite), which
        # register_suite_class silently same-file-overwrites. Match by source
        # file (all flavors share it) plus the auto-scan prefix for non-repo1
        # repos.
        if origin.startswith("_otto_suite_") or Path(entry.file).is_relative_to(_REPO1_DIR):
            parked[name] = (entry, origin)
            SUITES.unregister(name)
    evicted = {m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("_otto_suite_")}
    before_names = set(SUITES.names())
    yield
    for name in set(SUITES.names()) - before_names:
        SUITES.unregister(name)
    for key in [m for m in sys.modules if m.startswith("_otto_suite_")]:
        sys.modules.pop(key, None)
    sys.modules.update(evicted)
    for name, (obj, origin) in parked.items():
        SUITES.register(name, obj, overwrite=True, origin=origin)


@pytest.fixture
def repo1(clean_registry) -> Repo:
    """Return a Repo for tests/repo1 with libs on sys.path.

    Uses ``clean_registry`` so each test starts with a predictable ``SUITES``
    state and ``sys.modules`` is restored afterwards. The tests-dir sys.path
    precondition is enforced here (mirroring the pylib treatment in
    test_repo.py) so it can't depend on what ran earlier in the worker.

    The polluter is identified and empirically verified: any in-process suite
    execution (``run_suite`` → ``pytest.main([suite_file, ...])``) makes
    pytest PERMANENTLY insert the suite file's parent dirs — including
    ``tests/repo1/tests`` — into the worker's ``sys.path`` (importmode=
    prepend, no ``__init__.py``). The same mechanism also re-registers the
    file's suites under pytest's own module name; see ``clean_registry``.
    """
    while str(_REPO1_TESTS_DIR) in sys.path:
        sys.path.remove(str(_REPO1_TESTS_DIR))

    repo = Repo(sut_dir=_REPO1_DIR)
    # Add repo1/pylib so that `from repo1_common.options import RepoOptions`
    # inside test_device.py resolves correctly.
    repo.add_libs_to_pythonpath()
    return repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_for(suite_name: str) -> typer.Typer:
    """Wrap the named suite's sub-app in a fresh Typer app for CliRunner tests."""
    if suite_name not in SUITES:
        raise LookupError(f"{suite_name!r} not found in SUITES")
    app = typer.Typer(no_args_is_help=True)
    app.add_typer(SUITES.get(suite_name).sub_app)
    return app


# ---------------------------------------------------------------------------
# TestSuiteAutoScan
# ---------------------------------------------------------------------------


class TestSuiteAutoScan:
    """``import_test_files()`` registers suites from external paths (bug-fix coverage)."""

    def test_test_device_registered(self, repo1: Repo):
        """TestDevice must appear in SUITES after import_test_files()."""
        repo1.import_test_files()
        assert "TestDevice" in SUITES

    def test_tests_dir_not_on_syspath_before_import(self, repo1: Repo):
        """Pre-condition: repo1's tests directory must NOT be on sys.path.

        This confirms the bug scenario: the old code would have silently skipped
        test_device.py and left SUITES empty.
        """
        assert str(_REPO1_TESTS_DIR) not in sys.path

    def test_registration_survives_missing_syspath_entry(self, repo1: Repo):
        """Suite registers even though tests dir is absent from sys.path (bug regression)."""
        assert str(_REPO1_TESTS_DIR) not in sys.path  # confirm bug scenario

        repo1.import_test_files()

        assert "TestDevice" in SUITES, (
            "TestDevice not registered — import_test_files() may have fallen back "
            "to the broken sys.path-relative logic"
        )

    def test_plain_test_file_without_suite_class_not_registered(self, repo1: Repo):
        """test_example.py has no OttoSuite subclass and must not add to the registry."""
        before_names = set(SUITES.names())
        repo1.import_test_files()
        added_names = set(SUITES.names()) - before_names
        assert "test_example" not in added_names
        # test_device.py adds TestDevice; test_coverage_product.py adds TestCoverageProduct
        assert "TestDevice" in added_names
        assert len(added_names) >= 1

    def test_duplicate_import_is_skipped(self, repo1: Repo):
        """Calling import_test_files() twice must not double-register suites."""
        repo1.import_test_files()
        count_after_first = len(SUITES)
        repo1.import_test_files()
        assert len(SUITES) == count_after_first


# ---------------------------------------------------------------------------
# TestSuiteOptionsInHelp
# ---------------------------------------------------------------------------


class TestSuiteOptionsInHelp:
    """The TestDevice --help output must list the complete, correct set of options."""

    @pytest.fixture(autouse=True)
    def _import(self, repo1: Repo):
        repo1.import_test_files()

    def test_runner_options_absent_from_suite_help(self):
        """Runner options live on the parent callback, not individual suites."""
        app = _app_for("TestDevice")
        result = runner.invoke(app, ["TestDevice", "--help"])
        assert result.exit_code == 0
        for flag in ("--markers", "--iterations", "--duration", "--threshold", "--results"):
            assert flag not in result.output, (
                f"{flag!r} should live on `otto test --help`, not suite help"
            )

    def test_suite_specific_options_present(self):
        """Options declared in TestDevice.Options must appear."""
        app = _app_for("TestDevice")
        result = runner.invoke(app, ["TestDevice", "--help"])
        assert result.exit_code == 0
        assert "--firmware" in result.output
        assert "--check-interfaces" in result.output

    def test_inherited_repo_wide_options_present(self):
        """Fields from RepoOptions (the parent dataclass) must also appear."""
        app = _app_for("TestDevice")
        result = runner.invoke(app, ["TestDevice", "--help"])
        assert result.exit_code == 0
        assert "--device-type" in result.output
        assert "--lab-env" in result.output

    def test_bool_option_generates_flag_pair(self):
        """check_interfaces: bool must produce --check-interfaces / --no-check-interfaces.

        The negative flag may be truncated by Rich's column layout when the help
        table is wide, so we check for the unambiguous prefix '--no-check-inter'.
        """
        app = _app_for("TestDevice")
        result = runner.invoke(app, ["TestDevice", "--help"])
        assert result.exit_code == 0
        assert "--check-interfaces" in result.output
        # Rich may truncate the pair to '--no-check-inter…' in narrow columns
        assert "--no-check-inter" in result.output

    def test_all_options_present(self):
        """Full regression: all suite-specific options (2 suite + 2 repo-wide) must be listed."""
        app = _app_for("TestDevice")
        result = runner.invoke(app, ["TestDevice", "--help"])
        assert result.exit_code == 0
        expected = (
            "--firmware",
            "--check-interfaces",  # suite-specific
            "--device-type",
            "--lab-env",  # repo-wide
        )
        missing = [f for f in expected if f not in result.output]
        assert not missing, f"Options missing from --help: {missing}"
