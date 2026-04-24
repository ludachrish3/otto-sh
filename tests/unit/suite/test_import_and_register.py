"""
Integration tests for the test-file auto-scan → suite-registration pipeline,
using the real tests/repo1 SUT repo.

These tests cover the bug fix in ``configmodule.repo.importTestFiles()``:
the original implementation tried to derive a dotted module name from
``sys.path`` entries and silently skipped files whose parent directory was
not on ``sys.path``.  The fix uses ``importlib.util.spec_from_file_location``
to load files directly by path, independent of ``sys.path``.

Two concerns are exercised end-to-end:

1. **Registration** — ``importTestFiles()`` triggers ``@register_suite()`` on
   ``TestDevice`` and the suite lands in ``_SUITE_REGISTRY``, even though
   ``tests/repo1/tests`` is not on ``sys.path``.

2. **Help-menu fidelity** — the generated Typer command exposes the full set
   of expected options: the five common options, the two ``TestDevice``-specific
   options (``firmware``, ``check_interfaces``), and the two ``RepoOptions``
   repo-wide options (``device_type``, ``lab_env``).
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.configmodule.repo import Repo
from otto.suite.register import _SUITE_REGISTRY

runner = CliRunner()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# tests/unit/suite/ → tests/unit/ → tests/ → repo1/
_REPO1_DIR: Path = Path(__file__).parents[2] / 'repo1'
_REPO1_TESTS_DIR: Path = _REPO1_DIR / 'tests'

# Stable module names used by importTestFiles for the two repo1 test files
_MOD_DEVICE  = '_otto_suite_test_device'
_MOD_EXAMPLE = '_otto_suite_test_example'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_registry():
    """Snapshot and restore _SUITE_REGISTRY; remove injected sys.modules entries."""
    before_len = len(_SUITE_REGISTRY)
    before_mods = set(sys.modules)
    yield
    del _SUITE_REGISTRY[before_len:]
    for key in set(sys.modules) - before_mods:
        if key.startswith('_otto_suite_'):
            sys.modules.pop(key, None)


@pytest.fixture()
def repo1(clean_registry) -> Repo:  # noqa: F811
    """Return a Repo for tests/repo1 with libs on sys.path.

    Uses ``clean_registry`` so each test starts with a predictable
    ``_SUITE_REGISTRY`` state and ``sys.modules`` is restored afterwards.
    """
    # Evict any previously cached module so we get a fresh import each test
    sys.modules.pop(_MOD_DEVICE,  None)
    sys.modules.pop(_MOD_EXAMPLE, None)

    repo = Repo(sutDir=_REPO1_DIR)
    # Add repo1/pylib so that `from repo1_common.options import RepoOptions`
    # inside test_device.py resolves correctly.
    repo.addLibsToPythonpath()
    return repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app_for(suite_name: str) -> typer.Typer:
    """Wrap the named suite's sub-app in a fresh Typer app for CliRunner tests."""
    for name, sub_app in reversed(_SUITE_REGISTRY):
        if name == suite_name:
            app = typer.Typer(no_args_is_help=True)
            app.add_typer(sub_app)
            return app
    raise LookupError(f'{suite_name!r} not found in _SUITE_REGISTRY')


# ---------------------------------------------------------------------------
# TestSuiteAutoScan
# ---------------------------------------------------------------------------

class TestSuiteAutoScan:
    """``importTestFiles()`` registers suites from external paths (bug-fix coverage)."""

    def test_test_device_registered(self, repo1: Repo):
        """TestDevice must appear in _SUITE_REGISTRY after importTestFiles()."""
        repo1.importTestFiles()
        names = [n for n, _ in _SUITE_REGISTRY]
        assert 'TestDevice' in names

    def test_tests_dir_not_on_syspath_before_import(self, repo1: Repo):
        """Pre-condition: repo1's tests directory must NOT be on sys.path.

        This confirms the bug scenario: the old code would have silently skipped
        test_device.py and left _SUITE_REGISTRY empty.
        """
        assert str(_REPO1_TESTS_DIR) not in sys.path

    def test_registration_survives_missing_syspath_entry(self, repo1: Repo):
        """Suite registers even though tests dir is absent from sys.path (bug regression)."""
        assert str(_REPO1_TESTS_DIR) not in sys.path  # confirm bug scenario

        repo1.importTestFiles()

        names = [n for n, _ in _SUITE_REGISTRY]
        assert 'TestDevice' in names, (
            'TestDevice not registered — importTestFiles() may have fallen back '
            'to the broken sys.path-relative logic'
        )

    def test_plain_test_file_without_decorator_not_registered(self, repo1: Repo):
        """test_example.py has no @register_suite() and must not add to the registry."""
        before = len(_SUITE_REGISTRY)
        repo1.importTestFiles()
        added_names = [n for n, _ in _SUITE_REGISTRY[before:]]
        assert 'test_example' not in added_names
        # test_device.py adds TestDevice; test_coverage_product.py adds TestCoverageProduct
        assert 'TestDevice' in added_names
        assert len(added_names) >= 1

    def test_duplicate_import_is_skipped(self, repo1: Repo):
        """Calling importTestFiles() twice must not double-register suites."""
        repo1.importTestFiles()
        count_after_first = len(_SUITE_REGISTRY)
        repo1.importTestFiles()
        assert len(_SUITE_REGISTRY) == count_after_first


# ---------------------------------------------------------------------------
# TestSuiteOptionsInHelp
# ---------------------------------------------------------------------------

class TestSuiteOptionsInHelp:
    """The TestDevice --help output must list the complete, correct set of options."""

    @pytest.fixture(autouse=True)
    def _import(self, repo1: Repo):
        repo1.importTestFiles()

    def test_runner_options_absent_from_suite_help(self):
        """Runner options live on the parent callback, not individual suites."""
        app = _app_for('TestDevice')
        result = runner.invoke(app, ['TestDevice', '--help'])
        assert result.exit_code == 0
        for flag in ('--markers', '--iterations', '--duration', '--threshold', '--results'):
            assert flag not in result.output, (
                f'{flag!r} should live on `otto test --help`, not suite help'
            )

    def test_suite_specific_options_present(self):
        """Options declared in TestDevice.Options must appear."""
        app = _app_for('TestDevice')
        result = runner.invoke(app, ['TestDevice', '--help'])
        assert result.exit_code == 0
        assert '--firmware' in result.output
        assert '--check-interfaces' in result.output

    def test_inherited_repo_wide_options_present(self):
        """Fields from RepoOptions (the parent dataclass) must also appear."""
        app = _app_for('TestDevice')
        result = runner.invoke(app, ['TestDevice', '--help'])
        assert result.exit_code == 0
        assert '--device-type' in result.output
        assert '--lab-env' in result.output

    def test_bool_option_generates_flag_pair(self):
        """check_interfaces: bool must produce --check-interfaces / --no-check-interfaces.

        The negative flag may be truncated by Rich's column layout when the help
        table is wide, so we check for the unambiguous prefix '--no-check-inter'.
        """
        app = _app_for('TestDevice')
        result = runner.invoke(app, ['TestDevice', '--help'])
        assert result.exit_code == 0
        assert '--check-interfaces' in result.output
        # Rich may truncate the pair to '--no-check-inter…' in narrow columns
        assert '--no-check-inter' in result.output

    def test_all_options_present(self):
        """Full regression: all suite-specific options (2 suite + 2 repo-wide) must be listed."""
        app = _app_for('TestDevice')
        result = runner.invoke(app, ['TestDevice', '--help'])
        assert result.exit_code == 0
        expected = (
            '--firmware', '--check-interfaces',  # suite-specific
            '--device-type', '--lab-env',        # repo-wide
        )
        missing = [f for f in expected if f not in result.output]
        assert not missing, f'Options missing from --help: {missing}'


# ---------------------------------------------------------------------------
# TestSuiteOptionsPassthrough
# ---------------------------------------------------------------------------

class TestSuiteOptionsPassthrough:
    """Options reach test methods via the suite_options fixture from OttoOptionsPlugin."""

    @pytest.mark.integration
    def test_options_passed_through_to_suite(self, tmp_path: Path) -> None:
        """End-to-end: CLI options propagate to the suite_options fixture.

        Runs a minimal inner pytest session carrying a custom Options instance via
        OttoOptionsPlugin, then asserts that the suite_options fixture provides it
        correctly.
        """
        import otto.suite.suite as suite_module
        from otto.suite.plugin import OttoPlugin
        from otto.suite.register import OttoOptionsPlugin

        @dataclass
        class Opts:
            device_type: str = "router"

        opts = Opts(device_type="switch")

        capture_file = tmp_path / "captured.txt"
        test_file = tmp_path / "test_capture.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestCapture(OttoSuite):
    async def test_capture_suite_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(
            suite_options.device_type if suite_options is not None else "NONE"  # type: ignore
        )
        assert suite_options is not None, "suite_options was not provided by OttoOptionsPlugin"
        assert suite_options.device_type == "switch"  # type: ignore
""")

        mock_logger = MagicMock()
        mock_logger.output_dir = tmp_path

        with patch.object(suite_module, "logger", mock_logger):
            exit_code = pytest.main(
                [str(test_file), "-o", "asyncio_mode=auto",
                 "-o", "asyncio_default_fixture_loop_scope=function",
                 "--no-cov", "--override-ini", "addopts="],
                plugins=[OttoPlugin(), OttoOptionsPlugin(opts)],
            )

        assert capture_file.exists(), "test_capture_suite_options never ran"
        assert capture_file.read_text() == "switch"
        assert exit_code == pytest.ExitCode.OK
