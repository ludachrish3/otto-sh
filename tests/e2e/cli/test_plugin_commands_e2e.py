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


class TestDiscoveryContainment:
    """Phase-1 containment: malformed config DATA degrades like broken user CODE."""

    @pytest.fixture
    def bad_toml_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo_bad_toml"
        (repo / ".otto").mkdir(parents=True)
        (repo / ".otto" / "settings.toml").write_text("this is [not valid toml\n")
        return repo

    def test_malformed_settings_degrades_help_with_framed_warning(
        self, tmp_path: Path, bad_toml_repo: Path
    ) -> None:
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{bad_toml_repo}")
        assert r.returncode == 0  # help still renders
        assert "Traceback" not in r.stderr
        assert "settings.toml" in r.stderr  # framed warning names the culprit
        assert "run" in r.stdout  # first-party intact
        assert "e2e-hello" in r.stdout  # healthy repo's plugins intact

    def test_malformed_settings_fails_real_dispatch_loud(
        self, tmp_path: Path, bad_toml_repo: Path
    ) -> None:
        r = run_otto(["run", "noop"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{bad_toml_repo}")
        assert r.returncode != 0
        assert "settings.toml" in r.stderr + r.stdout

    def test_missing_sut_dir_fails_clean_one_liner(self, tmp_path: Path) -> None:
        # Env-level failure: nothing user-specific can load, so there is no
        # "degraded help" to offer — fail loud but CLEAN (no traceback).
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=tmp_path / "nope")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "does not exist" in r.stderr
