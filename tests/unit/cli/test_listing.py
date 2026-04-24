"""
Tests for the --list-* options on otto test and otto run.

Unit tests exercise panel methods and resolve_suite directly using
pre-built CollectedTest objects and tmp_path-backed Repo instances.

Integration tests create an external SUT repo in tmp_path (outside the
otto project root) and verify the full pipeline:
  - CollectedTest.path is always absolute
  - Panels display paths relative to sutDir
  - resolve_suite maps those display paths back to absolute pytest paths
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from otto.cli.test import resolve_suite, suite_app
from otto.configmodule.repo import CollectedTest, Repo, _test_run_syntax

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(renderable) -> str:
    """Render a Rich renderable to a plain string for assertion."""
    buf = io.StringIO()
    Console(file=buf, width=300, highlight=False).print(renderable)
    return buf.getvalue()


def _make_sut(
    base: Path,
    name: str = 'myrepo',
    version: str = '1.0.0',
    extra_toml: str = '',
) -> Path:
    """Create a minimal SUT directory with .otto/settings.toml."""
    sut_dir = base / name
    sut_dir.mkdir(parents=True, exist_ok=True)
    otto_dir = sut_dir / '.otto'
    otto_dir.mkdir()
    (otto_dir / 'settings.toml').write_text(
        f'name = "{name}"\nversion = "{version}"\ntests = ["${{sutDir}}/tests"]\n{extra_toml}'
    )
    return sut_dir


def _add_test_file(sut_dir: Path, filename: str = 'test_example.py', content: str | None = None) -> Path:
    """Write a test file into the SUT's tests/ directory."""
    tests_dir = sut_dir / 'tests'
    tests_dir.mkdir(exist_ok=True)
    if content is None:
        content = 'def test_pass():\n    assert True\n\ndef test_another():\n    assert True\n'
    p = tests_dir / filename
    p.write_text(content)
    return p


def _item(sut_dir: Path, rel: str, name: str, cls_name: str | None = None) -> CollectedTest:
    """Build a CollectedTest with an absolute path inside sut_dir."""
    return CollectedTest(
        nodeid=f'pytest/root/{rel}::{name}',
        name=name,
        path=(sut_dir / rel).resolve(),
        cls_name=cls_name,
    )


# ---------------------------------------------------------------------------
# getTestSuitesPanel — unique suites / files
# ---------------------------------------------------------------------------

class TestGetTestSuitesPanel:

    def test_class_based_shows_class_name_only(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        items = [_item(sut_dir, 'tests/test_foo.py', 'test_bar', cls_name='TestSuite')]
        text = _render(repo.getTestSuitesPanel(items))
        assert 'TestSuite' in text
        assert 'tests/test_foo.py' not in text

    def test_bare_function_excluded(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        items = [_item(sut_dir, 'tests/test_foo.py', 'test_bar')]
        text = _render(repo.getTestSuitesPanel(items))
        # Bare functions are not registered suites — they must not appear
        assert 'test_foo' not in text
        assert 'no tests found' in text

    def test_deduplicates_same_suite(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        items = [
            _item(sut_dir, 'tests/test_foo.py', 'test_one', cls_name='TestSuite'),
            _item(sut_dir, 'tests/test_foo.py', 'test_two', cls_name='TestSuite'),
        ]
        text = _render(repo.getTestSuitesPanel(items))
        assert text.count('TestSuite') == 1

    def test_empty_shows_placeholder(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        assert 'no tests found' in _render(repo.getTestSuitesPanel([]))


# ---------------------------------------------------------------------------
# resolve_suite
# ---------------------------------------------------------------------------

class TestResolveSuite:

    def test_sut_relative_file_resolves_to_absolute(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sutDir=sut_dir)
        result = resolve_suite('tests/test_example.py', [repo])
        assert result == str(test_file.resolve())

    def test_sut_relative_nodeid_resolves_to_absolute(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sutDir=sut_dir)
        result = resolve_suite('tests/test_example.py::test_pass', [repo])
        assert result == f'{test_file.resolve()}::test_pass'

    def test_class_nodeid_resolves_correctly(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sutDir=sut_dir)
        result = resolve_suite('tests/test_example.py::TestSuite::test_method', [repo])
        assert result == f'{test_file.resolve()}::TestSuite::test_method'

    def test_absolute_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sutDir=sut_dir)
        abs_path = str(test_file.resolve())
        assert resolve_suite(abs_path, [repo]) == abs_path

    def test_cwd_relative_existing_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        # pyproject.toml exists relative to the otto project CWD
        assert resolve_suite('pyproject.toml', [repo]) == 'pyproject.toml'

    def test_unresolvable_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sutDir=sut_dir)
        assert resolve_suite('nonexistent/test_file.py', [repo]) == 'nonexistent/test_file.py'

    def test_first_matching_repo_wins(self, tmp_path):
        sut_a = _make_sut(tmp_path / 'a', name='repo_a')
        sut_b = _make_sut(tmp_path / 'b', name='repo_b')
        file_a = _add_test_file(sut_a, 'test_shared.py')
        _add_test_file(sut_b, 'test_shared.py')
        repo_a = Repo(sutDir=sut_a)
        repo_b = Repo(sutDir=sut_b)
        result = resolve_suite('tests/test_shared.py::test_pass', [repo_a, repo_b])
        assert result == f'{file_a.resolve()}::test_pass'

    def test_falls_through_to_second_repo_if_first_has_no_match(self, tmp_path):
        sut_a = _make_sut(tmp_path / 'a', name='repo_a')  # no test file
        sut_b = _make_sut(tmp_path / 'b', name='repo_b')
        file_b = _add_test_file(sut_b, 'test_only_in_b.py')
        repo_a = Repo(sutDir=sut_a)
        repo_b = Repo(sutDir=sut_b)
        result = resolve_suite('tests/test_only_in_b.py', [repo_a, repo_b])
        assert result == str(file_b.resolve())


# ---------------------------------------------------------------------------
# --list-* CLI callbacks
# ---------------------------------------------------------------------------

class TestListCallbacks:
    """Verify callbacks invoke the correct panel method and exit cleanly."""

    def _mock_repo(self, method_name: str) -> MagicMock:
        repo = MagicMock()
        repo.collectTests.return_value = []
        getattr(repo, method_name).return_value = ""
        return repo

    def test_list_suites_calls_correct_panel(self):
        repo = self._mock_repo('getTestSuitesPanel')
        with patch('otto.cli.test.getRepos', return_value=[repo]):
            result = runner.invoke(suite_app, ['--list-suites'])
        assert result.exit_code == 0
        repo.collectTests.assert_called_once()
        repo.getTestSuitesPanel.assert_called_once()


# ---------------------------------------------------------------------------
# getInstructionsPanel
# ---------------------------------------------------------------------------

class TestGetInstructionsPanel:

    def _fake_group(self, module: str, func_name: str, cmd_name: str | None = None) -> MagicMock:
        cb = MagicMock()
        cb.__module__ = module
        cb.__name__ = func_name
        cmd = MagicMock()
        cmd.name = cmd_name
        cmd.callback = cb
        ti = MagicMock()
        ti.registered_commands = [cmd]
        group = MagicMock()
        group.typer_instance = ti
        return group

    def test_shows_command_from_matching_module(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sutDir=sut_dir)
        group = self._fake_group('my_instructions.cmd', 'do_something')
        with patch('otto.cli.run.run_app') as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.getInstructionsPanel())
        assert 'do-something' in text

    def test_excludes_command_from_other_module(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sutDir=sut_dir)
        group = self._fake_group('other_repo.cmd', 'foreign_command')
        with patch('otto.cli.run.run_app') as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.getInstructionsPanel())
        assert 'foreign-command' not in text
        assert 'no instructions found' in text

    def test_explicit_cmd_name_takes_priority_over_func_name(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sutDir=sut_dir)
        group = self._fake_group('my_instructions.cmd', 'func_name', cmd_name='explicit-name')
        with patch('otto.cli.run.run_app') as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.getInstructionsPanel())
        assert 'explicit-name' in text
        assert 'func-name' not in text

    def test_matches_top_level_init_module(self, tmp_path):
        """Module name exactly equal to an init entry (not just a prefix) should match."""
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sutDir=sut_dir)
        group = self._fake_group('my_instructions', 'top_level_cmd')
        with patch('otto.cli.run.run_app') as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.getInstructionsPanel())
        assert 'top-level-cmd' in text

    def test_empty_when_no_groups(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sutDir=sut_dir)
        with patch('otto.cli.run.run_app') as mock_app:
            mock_app.registered_groups = []
            text = _render(repo.getInstructionsPanel())
        assert 'no instructions found' in text


# ---------------------------------------------------------------------------
# Integration tests — external SUT repo in tmp_path
# ---------------------------------------------------------------------------

class TestExternalRepoIntegration:
    """
    Full-pipeline tests using a real SUT repo created in tmp_path.

    tmp_path resolves to a directory outside the otto project root (typically
    /tmp/pytest-*), confirming that otto handles external SUT repos correctly.
    The tests cover the absolute-vs-relative invariant end-to-end:
      - CollectedTest.path must be absolute
      - Panel display must be relative to sutDir
      - resolve_suite must map display path back to an existing absolute path
    """

    @pytest.fixture
    def sut(self, tmp_path) -> tuple[Path, Repo]:
        sut_dir = tmp_path / 'external_sut'
        sut_dir.mkdir()
        (sut_dir / '.otto').mkdir()
        (sut_dir / '.otto' / 'settings.toml').write_text(
            'name = "external"\nversion = "0.1.0"\ntests = ["${sutDir}/tests"]\n'
        )
        tests_dir = sut_dir / 'tests'
        tests_dir.mkdir()
        (tests_dir / 'test_suite.py').write_text(
            'def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n'
        )
        return sut_dir, Repo(sutDir=sut_dir)

    def test_sut_dir_is_outside_otto_root(self, sut: tuple[Path, Repo]):
        """Confirm the fixture actually creates an external repo."""
        sut_dir, _ = sut
        otto_root = Path(__file__).parents[3]  # tests/unit/cli/ → project root
        assert not sut_dir.is_relative_to(otto_root)

    def test_collected_paths_are_absolute(self, sut: tuple[Path, Repo]):
        _, repo = sut
        items = repo.collectTests()
        assert len(items) == 2
        for item in items:
            assert item.path.is_absolute()

    def test_collected_paths_are_under_sut_dir(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        items = repo.collectTests()
        for item in items:
            assert item.path.is_relative_to(sut_dir)

    def test_suites_panel_excludes_bare_functions(self, sut: tuple[Path, Repo]):
        """Bare functions have no 'otto test' subcommand and must not appear."""
        _, repo = sut
        items = repo.collectTests()
        text = _render(repo.getTestSuitesPanel(items))
        assert 'test_suite' not in text
        assert 'no tests found' in text

    def test_class_based_suites_panel_shows_class_name(self, tmp_path):
        """Class-based suites show just ClassName — the 'otto test ClassName' subcommand."""
        sut_dir = _make_sut(tmp_path)
        _add_test_file(sut_dir, 'test_class.py',
                       'class TestMyDevice:\n    def test_ping(self):\n        assert True\n')
        repo = Repo(sutDir=sut_dir)
        items = repo.collectTests()
        text = _render(repo.getTestSuitesPanel(items))
        assert 'TestMyDevice' in text
        assert 'test_class.py' not in text
        assert str(sut_dir) not in text

    def test_resolve_suite_maps_display_path_to_absolute(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        resolved = resolve_suite('tests/test_suite.py::test_alpha', [repo])
        expected = str((sut_dir / 'tests' / 'test_suite.py').resolve()) + '::test_alpha'
        assert resolved == expected

    def test_resolve_suite_directory_resolves_to_absolute(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        resolved = resolve_suite('tests', [repo])
        assert resolved == str((sut_dir / 'tests').resolve())

    def test_round_trip_display_to_resolve(self, sut: tuple[Path, Repo]):
        """
        Simulate the user workflow: collect → read display path from panel →
        pass back to resolve_suite → verify the result is an existing absolute path.
        """
        sut_dir, repo = sut
        items = repo.collectTests()
        for item in items:
            display_path = _test_run_syntax(item, sut_dir)
            resolved = resolve_suite(display_path, [repo])
            file_part, _, _ = resolved.partition('::')
            resolved_path = Path(file_part)
            assert resolved_path.is_absolute(), f'{file_part!r} is not absolute'
            assert resolved_path.exists(), f'{file_part!r} does not exist'
