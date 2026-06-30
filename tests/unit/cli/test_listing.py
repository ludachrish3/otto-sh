"""
Tests for the --list-* options on otto test and otto run.

Unit tests exercise panel methods and resolve_suite directly using
pre-built CollectedTest objects and tmp_path-backed Repo instances.

Integration tests create an external SUT repo in tmp_path (outside the
otto project root) and verify the full pipeline:
  - CollectedTest.path is always absolute
  - Panels display paths relative to sut_dir
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
    name: str = "myrepo",
    version: str = "1.0.0",
    extra_toml: str = "",
) -> Path:
    """Create a minimal SUT directory with .otto/settings.toml."""
    sut_dir = base / name
    sut_dir.mkdir(parents=True, exist_ok=True)
    otto_dir = sut_dir / ".otto"
    otto_dir.mkdir()
    (otto_dir / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\ntests = ["${{sut_dir}}/tests"]\n{extra_toml}'
    )
    return sut_dir


def _add_test_file(
    sut_dir: Path, filename: str = "test_example.py", content: str | None = None
) -> Path:
    """Write a test file into the SUT's tests/ directory."""
    tests_dir = sut_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    if content is None:
        content = "def test_pass():\n    assert True\n\ndef test_another():\n    assert True\n"
    p = tests_dir / filename
    p.write_text(content)
    return p


def _item(sut_dir: Path, rel: str, name: str, cls_name: str | None = None) -> CollectedTest:
    """Build a CollectedTest with an absolute path inside sut_dir."""
    return CollectedTest(
        nodeid=f"pytest/root/{rel}::{name}",
        name=name,
        path=(sut_dir / rel).resolve(),
        cls_name=cls_name,
    )


# ---------------------------------------------------------------------------
# get_test_suites_panel — unique suites / files
# ---------------------------------------------------------------------------


class TestGetTestSuitesPanel:
    def _seed(self, sut_dir, *names):
        from otto.suite import register as reg

        for n in names:
            reg._SUITE_REGISTRY.append((n, __import__("typer").Typer()))
            reg._SUITE_FILES[n] = str((sut_dir / "tests" / f"{n}.py").resolve())

    def _cleanup(self, *names):
        from otto.suite import register as reg

        reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] not in names]
        for n in names:
            reg._SUITE_FILES.pop(n, None)

    def test_lists_registered_suite_names(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        self._seed(sut_dir, "TestAlpha", "TestBeta")
        try:
            text = _render(repo.get_test_suites_panel())
        finally:
            self._cleanup("TestAlpha", "TestBeta")
        assert "TestAlpha" in text
        assert "TestBeta" in text

    def test_empty_when_no_suites(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        assert "no tests found" in _render(repo.get_test_suites_panel())


# ---------------------------------------------------------------------------
# Repo.registered_suites() — registry-based suite attribution
# ---------------------------------------------------------------------------


class TestRegisteredSuites:
    def test_attributes_suites_under_sut_dir(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        suite_file = str((sut_dir / "tests" / "test_thing.py").resolve())
        # Seed the registry + companion map directly (no real import needed).
        reg._SUITE_REGISTRY.append(("TestThing", __import__("typer").Typer()))
        reg._SUITE_FILES["TestThing"] = suite_file
        try:
            assert repo.registered_suites() == ["TestThing"]
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "TestThing"]
            reg._SUITE_FILES.pop("TestThing", None)

    def test_excludes_suites_outside_sut_dir(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        reg._SUITE_REGISTRY.append(("Foreign", __import__("typer").Typer()))
        reg._SUITE_FILES["Foreign"] = str((tmp_path / "other" / "test_x.py").resolve())
        try:
            assert repo.registered_suites() == []
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "Foreign"]
            reg._SUITE_FILES.pop("Foreign", None)


# ---------------------------------------------------------------------------
# get_lab_panel — host-source-backed lab listing + graceful failure
# ---------------------------------------------------------------------------


class TestGetLabPanel:
    def test_lists_lab_names_from_host_source(self, tmp_path):
        """The default json backend's lab names render as bulleted entries."""
        sut_dir = _make_sut(tmp_path, extra_toml='labs = ["${sut_dir}/labdata"]\n')
        labdata = sut_dir / "labdata"
        labdata.mkdir()
        (labdata / "hosts.json").write_text('[{"labs": ["alpha", "beta"]}]')
        repo = Repo(sut_dir=sut_dir)
        text = _render(repo.get_lab_panel())
        assert "alpha" in text
        assert "beta" in text

    def test_unknown_backend_renders_error_not_traceback(self, tmp_path):
        """A misconfigured [lab] backend surfaces in-panel instead of crashing."""
        sut_dir = _make_sut(tmp_path, extra_toml='[lab]\nbackend = "does-not-exist"\n')
        repo = Repo(sut_dir=sut_dir)
        # Must not raise — get_lab_panel catches the build failure.
        text = _render(repo.get_lab_panel())
        assert "host source unavailable" in text
        assert "does-not-exist" in text


# ---------------------------------------------------------------------------
# resolve_suite
# ---------------------------------------------------------------------------


class TestResolveSuite:
    def test_sut_relative_file_resolves_to_absolute(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sut_dir=sut_dir)
        result = resolve_suite("tests/test_example.py", [repo])
        assert result == str(test_file.resolve())

    def test_sut_relative_nodeid_resolves_to_absolute(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sut_dir=sut_dir)
        result = resolve_suite("tests/test_example.py::test_pass", [repo])
        assert result == f"{test_file.resolve()}::test_pass"

    def test_class_nodeid_resolves_correctly(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sut_dir=sut_dir)
        result = resolve_suite("tests/test_example.py::TestSuite::test_method", [repo])
        assert result == f"{test_file.resolve()}::TestSuite::test_method"

    def test_absolute_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        test_file = _add_test_file(sut_dir)
        repo = Repo(sut_dir=sut_dir)
        abs_path = str(test_file.resolve())
        assert resolve_suite(abs_path, [repo]) == abs_path

    def test_cwd_relative_existing_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        # pyproject.toml exists relative to the otto project CWD
        assert resolve_suite("pyproject.toml", [repo]) == "pyproject.toml"

    def test_unresolvable_path_returned_unchanged(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        assert resolve_suite("nonexistent/test_file.py", [repo]) == "nonexistent/test_file.py"

    def test_first_matching_repo_wins(self, tmp_path):
        sut_a = _make_sut(tmp_path / "a", name="repo_a")
        sut_b = _make_sut(tmp_path / "b", name="repo_b")
        file_a = _add_test_file(sut_a, "test_shared.py")
        _add_test_file(sut_b, "test_shared.py")
        repo_a = Repo(sut_dir=sut_a)
        repo_b = Repo(sut_dir=sut_b)
        result = resolve_suite("tests/test_shared.py::test_pass", [repo_a, repo_b])
        assert result == f"{file_a.resolve()}::test_pass"

    def test_falls_through_to_second_repo_if_first_has_no_match(self, tmp_path):
        sut_a = _make_sut(tmp_path / "a", name="repo_a")  # no test file
        sut_b = _make_sut(tmp_path / "b", name="repo_b")
        file_b = _add_test_file(sut_b, "test_only_in_b.py")
        repo_a = Repo(sut_dir=sut_a)
        repo_b = Repo(sut_dir=sut_b)
        result = resolve_suite("tests/test_only_in_b.py", [repo_a, repo_b])
        assert result == str(file_b.resolve())


# ---------------------------------------------------------------------------
# --list-* CLI callbacks
# ---------------------------------------------------------------------------


class TestListCallbacks:
    """Verify callbacks invoke the correct panel method and exit cleanly."""

    def test_list_suites_renders_registry_names(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        reg._SUITE_REGISTRY.append(("TestRealSuite", __import__("typer").Typer()))
        reg._SUITE_FILES["TestRealSuite"] = str((sut_dir / "tests" / "test_real.py").resolve())
        try:
            with patch("otto.cli.test.get_repos", return_value=[Repo(sut_dir=sut_dir)]):
                result = runner.invoke(suite_app, ["--list-suites"])
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "TestRealSuite"]
            reg._SUITE_FILES.pop("TestRealSuite", None)
        assert result.exit_code == 0
        assert "TestRealSuite" in result.stdout


# ---------------------------------------------------------------------------
# configured_markers + get_markers_panel
# ---------------------------------------------------------------------------


class TestConfiguredMarkers:
    def test_reads_pyproject_markers(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = ["slow: heavy", "smoke: quick"]\n'
        )
        repo = Repo(sut_dir=sut)
        assert repo.configured_markers() == ["slow", "smoke"]

    def test_returns_empty_when_no_pyproject(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        assert repo.configured_markers() == []

    def test_returns_empty_when_no_markers_key(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        repo = Repo(sut_dir=sut)
        assert repo.configured_markers() == []

    def test_strips_paren_form(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = ["timeout(n): time limit"]\n'
        )
        repo = Repo(sut_dir=sut)
        assert repo.configured_markers() == ["timeout"]


class TestGetMarkersPanel:
    def test_populated_panel_shows_markers(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = ["slow: heavy", "smoke: quick"]\n'
        )
        repo = Repo(sut_dir=sut)
        text = _render(repo.get_markers_panel())
        assert "slow" in text
        assert "smoke" in text

    def test_empty_panel_shows_placeholder(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        text = _render(repo.get_markers_panel())
        assert "no markers configured" in text


# ---------------------------------------------------------------------------
# --list-markers CLI flag
# ---------------------------------------------------------------------------


class TestListMarkers:
    def test_list_markers_renders_configured(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = ["smoke: quick"]\n'
        )
        with patch("otto.cli.test.get_repos", return_value=[Repo(sut_dir=sut)]):
            result = runner.invoke(suite_app, ["--list-markers"])
        assert result.exit_code == 0
        assert "smoke" in result.stdout

    def test_list_markers_empty_shows_placeholder(self, tmp_path):
        sut = _make_sut(tmp_path)
        with patch("otto.cli.test.get_repos", return_value=[Repo(sut_dir=sut)]):
            result = runner.invoke(suite_app, ["--list-markers"])
        assert result.exit_code == 0
        assert "no markers configured" in result.stdout


# ---------------------------------------------------------------------------
# get_instructions_panel
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
        repo = Repo(sut_dir=sut_dir)
        group = self._fake_group("my_instructions.cmd", "do_something")
        with patch("otto.cli.run.run_app") as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.get_instructions_panel())
        assert "do-something" in text

    def test_excludes_command_from_other_module(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        group = self._fake_group("other_repo.cmd", "foreign_command")
        with patch("otto.cli.run.run_app") as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.get_instructions_panel())
        assert "foreign-command" not in text
        assert "no instructions found" in text

    def test_explicit_cmd_name_takes_priority_over_func_name(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        group = self._fake_group("my_instructions.cmd", "func_name", cmd_name="explicit-name")
        with patch("otto.cli.run.run_app") as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.get_instructions_panel())
        assert "explicit-name" in text
        assert "func-name" not in text

    def test_matches_top_level_init_module(self, tmp_path):
        """Module name exactly equal to an init entry (not just a prefix) should match."""
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        group = self._fake_group("my_instructions", "top_level_cmd")
        with patch("otto.cli.run.run_app") as mock_app:
            mock_app.registered_groups = [group]
            text = _render(repo.get_instructions_panel())
        assert "top-level-cmd" in text

    def test_empty_when_no_groups(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        with patch("otto.cli.run.run_app") as mock_app:
            mock_app.registered_groups = []
            text = _render(repo.get_instructions_panel())
        assert "no instructions found" in text


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
      - Panel display must be relative to sut_dir
      - resolve_suite must map display path back to an existing absolute path
    """

    @pytest.fixture
    def sut(self, tmp_path) -> tuple[Path, Repo]:
        sut_dir = tmp_path / "external_sut"
        sut_dir.mkdir()
        (sut_dir / ".otto").mkdir()
        (sut_dir / ".otto" / "settings.toml").write_text(
            'name = "external"\nversion = "0.1.0"\ntests = ["${sut_dir}/tests"]\n'
        )
        tests_dir = sut_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_suite.py").write_text(
            "def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n"
        )
        return sut_dir, Repo(sut_dir=sut_dir)

    def test_sut_dir_is_outside_otto_root(self, sut: tuple[Path, Repo]):
        """Confirm the fixture actually creates an external repo."""
        sut_dir, _ = sut
        otto_root = Path(__file__).parents[3]  # tests/unit/cli/ → project root
        assert not sut_dir.is_relative_to(otto_root)

    def test_collected_paths_are_absolute(self, sut: tuple[Path, Repo]):
        _, repo = sut
        items = repo.collect_tests()
        assert len(items) == 2
        for item in items:
            assert item.path.is_absolute()

    def test_collected_paths_are_under_sut_dir(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        items = repo.collect_tests()
        for item in items:
            assert item.path.is_relative_to(sut_dir)

    def test_suites_panel_excludes_bare_functions(self, sut: tuple[Path, Repo]):
        """Bare functions have no 'otto test' subcommand and must not appear."""
        _, repo = sut
        # No suites registered for this repo — panel must show placeholder.
        text = _render(repo.get_test_suites_panel())
        assert "test_suite" not in text
        assert "no tests found" in text

    def test_class_based_suites_panel_shows_class_name(self, tmp_path):
        """Class-based suites show just ClassName — the 'otto test ClassName' subcommand."""
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        _add_test_file(
            sut_dir,
            "test_class.py",
            "class TestMyDevice:\n    def test_ping(self):\n        assert True\n",
        )
        repo = Repo(sut_dir=sut_dir)
        reg._SUITE_REGISTRY.append(("TestMyDevice", __import__("typer").Typer()))
        reg._SUITE_FILES["TestMyDevice"] = str((sut_dir / "tests" / "test_class.py").resolve())
        try:
            text = _render(repo.get_test_suites_panel())
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "TestMyDevice"]
            reg._SUITE_FILES.pop("TestMyDevice", None)
        assert "TestMyDevice" in text
        assert "test_class.py" not in text
        assert str(sut_dir) not in text

    def test_resolve_suite_maps_display_path_to_absolute(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        resolved = resolve_suite("tests/test_suite.py::test_alpha", [repo])
        expected = str((sut_dir / "tests" / "test_suite.py").resolve()) + "::test_alpha"
        assert resolved == expected

    def test_resolve_suite_directory_resolves_to_absolute(self, sut: tuple[Path, Repo]):
        sut_dir, repo = sut
        resolved = resolve_suite("tests", [repo])
        assert resolved == str((sut_dir / "tests").resolve())

    def test_round_trip_display_to_resolve(self, sut: tuple[Path, Repo]):
        """
        Simulate the user workflow: collect → read display path from panel →
        pass back to resolve_suite → verify the result is an existing absolute path.
        """
        sut_dir, repo = sut
        items = repo.collect_tests()
        for item in items:
            display_path = _test_run_syntax(item, sut_dir)
            resolved = resolve_suite(display_path, [repo])
            file_part, _, _ = resolved.partition("::")
            resolved_path = Path(file_part)
            assert resolved_path.is_absolute(), f"{file_part!r} is not absolute"
            assert resolved_path.exists(), f"{file_part!r} does not exist"


# ---------------------------------------------------------------------------
# --list-tests CLI flag
# ---------------------------------------------------------------------------


class TestListTests:
    def _repo_with_tests(self, tmp_path: Path) -> Repo:
        sut = _make_sut(tmp_path)
        _add_test_file(
            sut,
            "test_device.py",
            "import pytest\n"
            "class TestDevice:\n"
            "    def test_alpha(self):\n        assert True\n"
            "    @pytest.mark.slow\n    def test_beta(self):\n        assert True\n",
        )
        (sut / "tests" / "conftest.py").write_text(
            "def pytest_configure(config):\n    config.addinivalue_line('markers','slow: x')\n"
        )
        return Repo(sut_dir=sut)

    def test_list_tests_lists_all_and_exits(self, tmp_path: Path) -> None:
        repo = self._repo_with_tests(tmp_path)
        with patch("otto.cli.test.get_repos", return_value=[repo]):
            result = runner.invoke(suite_app, ["--list-tests"])
        assert result.exit_code == 0
        assert "test_alpha" in result.stdout
        assert "test_beta" in result.stdout

    def test_list_tests_filters_by_marker(self, tmp_path: Path) -> None:
        repo = self._repo_with_tests(tmp_path)
        with patch("otto.cli.test.get_repos", return_value=[repo]):
            result = runner.invoke(suite_app, ["--list-tests", "--markers", "slow"])
        assert result.exit_code == 0
        assert "test_beta" in result.stdout
        assert "test_alpha" not in result.stdout

    def test_no_subcommand_no_flags_shows_help(self) -> None:
        result = runner.invoke(suite_app, [])
        assert result.exit_code == 0
        assert "Usage" in result.stdout or "Commands" in result.stdout
