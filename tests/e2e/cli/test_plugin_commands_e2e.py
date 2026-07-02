"""Hostless e2e: third-party top-level commands + bootstrap containment."""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import PROJECT_ROOT, REPO_E2E, assert_no_output_dir, run_otto

REPO_BROKEN = PROJECT_ROOT / "tests" / "repo_broken"

pytestmark = pytest.mark.hostless


class TestPluginCommands:
    def test_plugin_leaf_dispatches(self, tmp_path: Path) -> None:
        r = run_otto(["e2e-hello", "--who", "otto"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "hello otto" in r.stdout
        assert_no_output_dir(tmp_path)  # lab_free + no output dir declared

    def test_plugin_group_dispatches(self, tmp_path: Path) -> None:
        r = run_otto(["e2etool", "ping"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "pong" in r.stdout

    def test_plugin_commands_listed_in_root_help(self, tmp_path: Path) -> None:
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0
        assert "e2e-hello" in r.stdout
        assert "e2etool" in r.stdout


class TestBootstrapContainment:
    def test_broken_repo_degrades_help_with_framed_warning(self, tmp_path: Path) -> None:
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode == 0  # help still renders
        assert "failed to load test_syntax_error.py" in r.stderr
        assert "run" in r.stdout  # first-party intact

    def test_broken_repo_fails_real_dispatch_loud(self, tmp_path: Path) -> None:
        r = run_otto(["run", "noop"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode != 0
        assert "failed to load test_syntax_error.py" in r.stderr + r.stdout
