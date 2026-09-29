"""
Tests for the --list-* options on otto test and otto run.

Unit tests exercise panel methods directly on tmp_path-backed Repo
instances; the listings pytest collects are pinned in
``tests/unit/cli/test_test_listings.py``.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console
from typer.testing import CliRunner

from otto.config.repo import Repo
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

runner = CliRunner()


def _test_app():
    """``otto test``'s app, read at call time so it carries the current verb flags."""
    from otto.cli import test as cli_test

    return cli_test.test_app


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
    return make_sut_repo(base / name, name=name, version=version, tests=["tests"], extra=extra_toml)


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


# ---------------------------------------------------------------------------
# get_lab_panel — host-source-backed lab listing + graceful failure
# ---------------------------------------------------------------------------


class TestGetLabPanel:
    def test_lists_lab_names_from_host_source(self, tmp_path):
        """The default json backend's lab names render as bulleted entries."""
        sut_dir = _make_sut(
            tmp_path, extra_toml='[[lab.sources]]\nbackend = "json"\npaths = ["labdata"]\n'
        )
        labdata = sut_dir / "labdata"
        labdata.mkdir()
        write_lab_json(
            labdata / "lab.json",
            [{"ip": "10.0.0.1", "element": "a", "labs": ["alpha", "beta"]}],
        )
        repo = Repo(sut_dir=sut_dir)
        text = _render(repo.get_lab_panel())
        assert "alpha" in text
        assert "beta" in text

    def test_declaring_no_source_says_so_instead_of_rendering_blank(self, tmp_path):
        """A repo with no host source names the missing declaration.

        The empty composite lists no labs, so the bulleted branch would render
        an empty panel — indistinguishable from a source that answered with
        zero labs. The two are different problems and must read differently.
        """
        repo = Repo(sut_dir=_make_sut(tmp_path))
        assert repo.lab_sources == []
        assert "no [[lab.sources]] declared" in _render(repo.get_lab_panel())

    def test_unknown_backend_renders_error_not_traceback(self, tmp_path):
        """A misconfigured [[lab.sources]] backend surfaces in-panel instead of crashing."""
        sut_dir = _make_sut(tmp_path, extra_toml='[[lab.sources]]\nbackend = "does-not-exist"\n')
        repo = Repo(sut_dir=sut_dir)
        # Must not raise — get_lab_panel catches the build failure.
        text = _render(repo.get_lab_panel())
        assert "host source unavailable" in text
        assert "does-not-exist" in text


# ---------------------------------------------------------------------------
# get_markers_panel
# ---------------------------------------------------------------------------


class TestGetMarkersPanel:
    def test_populated_panel_shows_markers(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        text = _render(repo.get_markers_panel(["slow", "smoke"]))
        assert "slow" in text
        assert "smoke" in text

    def test_empty_panel_shows_placeholder(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        text = _render(repo.get_markers_panel([]))
        assert "no markers found" in text


# ---------------------------------------------------------------------------
# --list-markers CLI flag
# ---------------------------------------------------------------------------


class TestListMarkers:
    def test_a_repo_with_no_tests_shows_the_placeholder(self, tmp_path):
        sut = _make_sut(tmp_path)
        with patch("otto.config.get_repos", return_value=[Repo(sut_dir=sut)]):
            result = runner.invoke(_test_app(), ["--list-markers"])
        assert result.exit_code == 0
        assert "no markers found" in result.stdout

    def test_list_markers_includes_otto_builtins(self, tmp_path):
        """`ensure` and `retry` are otto's, not the repo's — they get their own panel."""
        sut = _make_sut(tmp_path)
        with patch("otto.config.get_repos", return_value=[Repo(sut_dir=sut)]):
            result = runner.invoke(_test_app(), ["--list-markers"])
        assert result.exit_code == 0
        assert "ensure(*steps)" in result.stdout
        assert "retry(n)" in result.stdout
        assert "otto (built in)" in result.stdout


# ---------------------------------------------------------------------------
# get_instructions_panel
# ---------------------------------------------------------------------------


class TestGetInstructionsPanel:
    def _fake_registry(self, *entries: tuple[str, str]) -> MagicMock:
        """Build a fake INSTRUCTIONS-shaped registry from (cmd_name, module) pairs.

        Mirrors ``otto.instructions.InstructionEntry``'s ``.name`` / ``.module``
        fields consumed by ``get_instructions_panel``.
        """
        items = []
        for cmd_name, module in entries:
            entry = MagicMock()
            entry.name = cmd_name
            entry.module = module
            items.append((cmd_name, entry))
        registry = MagicMock()
        registry.items.return_value = items
        return registry

    def test_shows_command_from_matching_module(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        fake = self._fake_registry(("do-something", "my_instructions.cmd"))
        with patch("otto.instructions.INSTRUCTIONS", fake):
            text = _render(repo.get_instructions_panel())
        assert "do-something" in text

    def test_excludes_command_from_other_module(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        fake = self._fake_registry(("foreign-command", "other_repo.cmd"))
        with patch("otto.instructions.INSTRUCTIONS", fake):
            text = _render(repo.get_instructions_panel())
        assert "foreign-command" not in text
        assert "no instructions found" in text

    def test_explicit_cmd_name_takes_priority_over_func_name(self, tmp_path):
        """The registered name is already the explicit/derived name at registration time."""
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        fake = self._fake_registry(("explicit-name", "my_instructions.cmd"))
        with patch("otto.instructions.INSTRUCTIONS", fake):
            text = _render(repo.get_instructions_panel())
        assert "explicit-name" in text
        assert "func-name" not in text

    def test_matches_top_level_init_module(self, tmp_path):
        """Module name exactly equal to an init entry (not just a prefix) should match."""
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        fake = self._fake_registry(("top-level-cmd", "my_instructions"))
        with patch("otto.instructions.INSTRUCTIONS", fake):
            text = _render(repo.get_instructions_panel())
        assert "top-level-cmd" in text

    def test_empty_when_no_groups(self, tmp_path):
        sut_dir = _make_sut(tmp_path, extra_toml='init = ["my_instructions"]\n')
        repo = Repo(sut_dir=sut_dir)
        fake = self._fake_registry()
        with patch("otto.instructions.INSTRUCTIONS", fake):
            text = _render(repo.get_instructions_panel())
        assert "no instructions found" in text


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
        with patch("otto.config.get_repos", return_value=[repo]):
            result = runner.invoke(_test_app(), ["--list-tests"])
        assert result.exit_code == 0
        assert "test_alpha" in result.stdout
        assert "test_beta" in result.stdout

    def test_list_tests_filters_by_marker(self, tmp_path: Path) -> None:
        repo = self._repo_with_tests(tmp_path)
        with patch("otto.config.get_repos", return_value=[repo]):
            result = runner.invoke(_test_app(), ["--list-tests", "--markers", "slow"])
        assert result.exit_code == 0
        assert "test_beta" in result.stdout
        assert "test_alpha" not in result.stdout

    def test_no_names_and_no_flags_is_a_usage_error(self) -> None:
        result = runner.invoke(_test_app(), [])
        assert result.exit_code == 2
        assert "at least one test name or -m" in result.output
