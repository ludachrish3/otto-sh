"""
Unit tests for the ``otto cov`` subcommand.

Covers:
  - Help / no-args behaviour
  - ``otto cov report`` happy path
  - ``otto cov report`` validation errors
  - ``otto cov get`` validation errors and happy path (fetch layer stubbed)
  - ``_resolve_tester`` identity defaults (spec decision 15)
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli import cov as cov_module
from otto.cli.cov import cov_app
from otto.coverage.capture import produce as produce_module
from otto.coverage.capture.model import Capture

runner = CliRunner()


@pytest.fixture(autouse=True)
def _suppress_loggers():
    """Prevent logger stream handlers from writing to CliRunner's
    captured stdout after it is closed (causes ValueError on typer.Exit)."""
    loggers = [
        cov_module.logger,
        logging.getLogger("otto.coverage.reporter"),
    ]
    saved = [(lgr, lgr.level) for lgr in loggers]
    for lgr in loggers:
        lgr.setLevel(logging.CRITICAL + 1)
    yield
    for lgr, level in saved:
        lgr.setLevel(level)


# ── Help / no-args behaviour ─────────────────────────────────────────────────


class TestCovHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(cov_app, [])
        assert "Usage" in result.output or "usage" in result.output.lower()

    def test_help_flag(self):
        result = runner.invoke(cov_app, ["--help"])
        assert result.exit_code == 0

    def test_short_help_flag(self):
        result = runner.invoke(cov_app, ["-h"])
        assert result.exit_code == 0

    def test_report_listed_in_help(self):
        result = runner.invoke(cov_app, ["--help"])
        assert "report" in result.output

    def test_report_help(self):
        result = runner.invoke(cov_app, ["report", "--help"])
        assert result.exit_code == 0
        assert "OUTPUT_DIRS" in result.output

    def test_get_listed_in_help(self):
        result = runner.invoke(cov_app, ["--help"])
        assert "get" in result.output

    def test_get_help(self):
        result = runner.invoke(cov_app, ["get", "--help"])
        assert result.exit_code == 0
        assert "--tier" in result.output
        assert "--ticket" in result.output

    def test_clean_listed_in_help(self):
        result = runner.invoke(cov_app, ["--help"])
        assert "clean" in result.output

    def test_clean_help(self):
        result = runner.invoke(cov_app, ["clean", "--help"])
        assert result.exit_code == 0

    def test_only_get_wants_the_per_invocation_output_dir(self):
        """`get` produces artifacts, so it uses the standard per-invocation
        output dir; `report` (e2e-pinned: creates no output dir) and `clean`
        (no artifacts) opt out via the leaf marker the preamble reads."""
        assert getattr(cov_module.get, "__cli_output_dir__", True) is True
        assert cov_module.report.__cli_output_dir__ is False
        assert cov_module.clean.__cli_output_dir__ is False


# ── report command — validation errors ───────────────────────────────────────


class TestCovReportValidation:
    def test_nonexistent_dir_exits_1(self):
        with patch.object(cov_module.logger, "error") as mock_err:
            result = runner.invoke(cov_app, ["report", "/no/such/dir"])
        assert result.exit_code == 1
        mock_err.assert_called_once()
        assert "does not exist" in mock_err.call_args[0][0]

    def test_no_gcda_dirs_exits_1(self, tmp_path):
        """Real directory but no cov/ subdirectory → error (git-less legacy path)."""
        # Pin the git-less scenario: no [coverage] settings resolvable, so the
        # legacy no-data path runs and returns None → exit 1. (Without this the
        # outcome would depend on whatever repo bootstrap resolved globally.)
        with (
            patch.object(cov_module, "_resolve_cov_settings", return_value=(None, None, [])),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", str(tmp_path)])
        assert result.exit_code == 1
        assert "not generated" in mock_err.call_args[0][0]

    def test_source_root_not_found_exits_1(self, tmp_path):
        # Create a cov/ subdir with host dir so discover_gcda_dirs returns
        # entries, but no .otto_cov_meta.json so read_cov_source_root fails.
        # Pin the git-less scenario so only the legacy path is exercised.
        (tmp_path / "cov" / "host1").mkdir(parents=True)
        with (
            patch.object(cov_module, "_resolve_cov_settings", return_value=(None, None, [])),
            patch.object(cov_module.logger, "error"),
        ):
            result = runner.invoke(cov_app, ["report", str(tmp_path)])
        assert result.exit_code == 1


class TestCovReportMergeErrors:
    """Merge-stage failures must exit 1 with a clean message — no traceback."""

    @pytest.fixture
    def cov_dir(self, tmp_path):
        (tmp_path / "cov" / "host1").mkdir(parents=True)
        return tmp_path

    def test_stamp_mismatch_reports_cause_without_traceback(self, cov_dir):
        from otto.coverage.errors import CoverageDataMismatchError

        with (
            patch.object(
                cov_module,
                "run_coverage_report",
                side_effect=CoverageDataMismatchError("x.gcda:stamp mismatch with notes file"),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", str(cov_dir)])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "rebuilt" in message  # names the likely cause
        assert "otto test --cov" in message  # names the remedy

    def test_incompatible_gcov_tool_reports_cause_without_traceback(self, cov_dir):
        """A clang build captured with GNU gcov (geninfo: Incompatible
        GCC/GCOV version) must exit 1 with the cause and fix — no traceback."""
        from otto.coverage.errors import CoverageToolVersionError

        with (
            patch.object(
                cov_module,
                "run_coverage_report",
                side_effect=CoverageToolVersionError("Your test was built with '4.8'."),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", str(cov_dir)])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "clang" in message  # names the likely cause
        assert "llvm-cov" in message  # names the fix

    def test_generic_merge_failure_reports_cleanly(self, cov_dir):
        with (
            patch.object(
                cov_module,
                "run_coverage_report",
                side_effect=RuntimeError("lcov --capture failed:\nsome lcov noise"),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", str(cov_dir)])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "Coverage merge failed" in mock_err.call_args[0][0]

    def test_prefix_option_forwards_to_reporter(self, cov_dir):
        with patch.object(
            cov_module, "run_coverage_report", new=AsyncMock(return_value=None)
        ) as rcr:
            runner.invoke(cov_app, ["report", str(cov_dir), "--prefix", "/repo"])
        assert rcr.call_args.kwargs["prefix"] == Path("/repo")


# ── report command — success ─────────────────────────────────────────────────


class TestCovReportSuccess:
    @pytest.fixture
    def cov_tree(self, tmp_path):
        """Create a minimal output directory with cov/<host>/*.gcda."""
        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")
        return tmp_path

    @pytest.fixture
    def mock_run_report(self):
        """Mock ``run_coverage_report`` at the I/O boundary."""
        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 75.0
        mock_store.file_count.return_value = 3

        mock = AsyncMock(return_value=mock_store)
        with patch.object(cov_module, "run_coverage_report", mock):
            yield mock, mock_store

    def test_report_success(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ["report", str(cov_tree)])
        assert result.exit_code == 0
        mock.assert_called_once()

    def test_report_default_output_dir(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ["report", str(cov_tree)])
        assert result.exit_code == 0
        args, _ = mock.call_args.args, mock.call_args.kwargs
        assert args[1] == Path("./cov_report").resolve()

    def test_report_custom_report_dir(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(cov_tree),
                "--report",
                "/tmp/my_report",
            ],
        )
        assert result.exit_code == 0
        args = mock.call_args.args
        assert args[1] == Path("/tmp/my_report").resolve()

    def test_report_custom_options(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(cov_tree),
                "--project-name",
                "My Project",
            ],
        )
        assert result.exit_code == 0
        assert mock.call_args.kwargs["project_name"] == "My Project"

    def test_report_multiple_output_dirs(self, tmp_path, mock_run_report):
        mock, _ = mock_run_report
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"
        for d in (dir1, dir2):
            host_dir = d / "cov" / "host1"
            host_dir.mkdir(parents=True)
            (host_dir / "main.gcda").write_bytes(b"\x00")

        result = runner.invoke(cov_app, ["report", str(dir1), str(dir2)])
        assert result.exit_code == 0
        mock.assert_called_once()
        # Should have forwarded two cov dirs
        args = mock.call_args.args
        assert args[0] == [dir1 / "cov", dir2 / "cov"]

    def test_report_default_tier_is_system(self, cov_tree, mock_run_report):
        """No --tier → default to system-only."""
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ["report", str(cov_tree)])
        assert result.exit_code == 0
        assert mock.call_args.kwargs["tier_specs"] == [("system", None)]

    def test_report_tier_with_path(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(cov_tree),
                "--tier",
                "unit=/tmp/u.info",
                "--tier",
                "system",
            ],
        )
        assert result.exit_code == 0
        assert mock.call_args.kwargs["tier_specs"] == [
            ("unit", Path("/tmp/u.info")),
            ("system", None),
        ]

    def test_report_tier_order_is_preserved(self, cov_tree, mock_run_report):
        """First --tier flag is highest precedence."""
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(cov_tree),
                "--tier",
                "unit=/u.info",
                "--tier",
                "system",
                "--tier",
                "integration=/i.info",
                "--tier",
                "manual=/m.info",
            ],
        )
        assert result.exit_code == 0
        names = [name for name, _ in mock.call_args.kwargs["tier_specs"]]
        assert names == ["unit", "system", "integration", "manual"]

    def test_report_non_system_tier_without_path_errors(self, cov_tree):
        with patch.object(cov_module.logger, "error"):
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(cov_tree),
                    "--tier",
                    "unit",  # No path → error (only system may omit)
                ],
            )
        assert result.exit_code == 1

    def test_report_duplicate_tier_errors(self, cov_tree):
        with patch.object(cov_module.logger, "error"):
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(cov_tree),
                    "--tier",
                    "unit=/a.info",
                    "--tier",
                    "unit=/b.info",
                ],
            )
        assert result.exit_code == 1


# ── report command — collection-model wiring (Task 10) ──────────────────────


class TestCovReportCollectionModel:
    @pytest.fixture
    def mock_run_report(self):
        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 50.0
        mock_store.file_count.return_value = 1
        mock = AsyncMock(return_value=mock_store)
        with patch.object(cov_module, "run_coverage_report", mock):
            yield mock

    def test_no_tier_resolves_repo_root_and_tier_configs_from_settings(
        self, tmp_path, mock_run_report
    ):
        """No --tier → settings-driven collection path (repo_root + tier_configs)."""
        from otto.coverage.tiers import TierConfig

        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        repo_root = tmp_path / "sut"
        tiers = [TierConfig(name="system", kind="e2e", precedence=1, color="green")]
        with patch.object(cov_module, "_resolve_cov_settings", return_value=(repo_root, tiers, [])):
            result = runner.invoke(cov_app, ["report", str(tmp_path)])

        assert result.exit_code == 0
        kwargs = mock_run_report.call_args.kwargs
        assert kwargs["repo_root"] == repo_root
        assert kwargs["tier_configs"] == tiers
        assert kwargs["tier_specs"] == [("system", None)]

    def test_extra_markers_threaded_from_settings(self, tmp_path, mock_run_report):
        """[coverage.exclusions].markers (via _resolve_cov_settings) reach run_coverage_report."""
        from otto.coverage.tiers import TierConfig

        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        repo_root = tmp_path / "sut"
        tiers = [TierConfig(name="system", kind="e2e", precedence=1, color="green")]
        with patch.object(
            cov_module,
            "_resolve_cov_settings",
            return_value=(repo_root, tiers, ["MYPROJ_NO_COV"]),
        ):
            result = runner.invoke(cov_app, ["report", str(tmp_path)])

        assert result.exit_code == 0
        assert mock_run_report.call_args.kwargs["extra_markers"] == ["MYPROJ_NO_COV"]

    def test_explicit_tier_flags_bypass_settings(self, tmp_path, mock_run_report):
        """--tier escape hatch: no settings resolution, repo_root/tier_configs None."""
        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        with patch.object(
            cov_module, "_resolve_cov_settings", side_effect=AssertionError("must not resolve")
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--tier", "unit=/u.info", "--tier", "system"]
            )

        assert result.exit_code == 0
        kwargs = mock_run_report.call_args.kwargs
        assert kwargs["repo_root"] is None
        assert kwargs["tier_configs"] is None
        assert kwargs["tier_specs"] == [("unit", Path("/u.info")), ("system", None)]

    def test_no_output_dirs_allowed_for_manual_only_report(self, mock_run_report):
        """output_dirs is optional: a manual-store-only report needs no run dirs."""
        repo_root = Path("/some/repo")
        with patch.object(cov_module, "_resolve_cov_settings", return_value=(repo_root, None, [])):
            result = runner.invoke(cov_app, ["report"])

        assert result.exit_code == 0
        args = mock_run_report.call_args.args
        assert args[0] == []  # no cov dirs
        assert mock_run_report.call_args.kwargs["repo_root"] == repo_root


# ── report command — collection-model failure modes & empty-report contract ──


class TestCovReportCollectionModelErrors:
    @staticmethod
    def _tiers():
        from otto.coverage.tiers import TierConfig

        return [TierConfig(name="system", kind="e2e", precedence=1, color="green")]

    def test_malformed_manual_capture_exits_1_no_traceback(self, tmp_path):
        """A committed but corrupt manual capture makes load_manual_captures raise
        ValueError; report must exit 1 with the malformed-capture message, no traceback."""
        repo_root = tmp_path / "sut"
        manual = repo_root / ".otto" / "coverage" / "manual"
        manual.mkdir(parents=True)
        (manual / "bad.json").write_text("{nope")

        with (
            patch.object(
                cov_module, "_resolve_cov_settings", return_value=(repo_root, self._tiers(), [])
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", "--report", str(tmp_path / "report")])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "malformed manual capture" in mock_err.call_args[0][0]

    def test_empty_report_exits_1_naming_searched_inputs(self, tmp_path):
        """A store with zero files is a vacuous success; the CI-friendly loud
        fail is restored — exit 1 naming the inputs it searched."""
        repo_root = tmp_path / "sut"

        with (
            patch.object(
                cov_module, "_resolve_cov_settings", return_value=(repo_root, self._tiers(), [])
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", "--report", str(tmp_path / "report")])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "no coverage data found in" in mock_err.call_args[0][0]
        # Names the committed manual store it searched.
        assert "manual" in str(mock_err.call_args[0][1])

    def test_non_git_repo_root_reports_cleanly(self, tmp_path):
        """A [coverage] repo_root that is not a git repo can't run pinned-capture
        features; report names the cause + the git-less escape hatch, no traceback."""
        not_git = tmp_path / "notgit"
        (not_git / "cov" / "board1").mkdir(parents=True)
        (not_git / "cov" / "board1" / "capture.json").write_text("{}")

        with (
            patch.object(
                cov_module, "_resolve_cov_settings", return_value=(not_git, self._tiers(), [])
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(not_git), "--report", str(tmp_path / "report")]
            )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "not a git repository" in message
        assert "--tier" in message


# ── _resolve_cov_settings — [coverage.exclusions].markers wiring ────────────


class TestResolveCovSettingsExtraMarkers:
    @staticmethod
    def _repo(coverage_cfg, sut_dir=None):
        repo = MagicMock()
        repo.settings = {"coverage": coverage_cfg} if coverage_cfg is not None else {}
        repo.sut_dir = sut_dir or Path("/sut")
        return repo

    def test_reads_exclusion_markers_from_settings(self):
        repo = self._repo(
            {
                "tiers": {"system": {"kind": "e2e", "precedence": 1}},
                "exclusions": {"markers": ["MYPROJ_NO_COV"]},
            }
        )
        with patch("otto.config.get_repos", return_value=[repo]):
            repo_root, tier_configs, extra_markers = cov_module._resolve_cov_settings()
        assert repo_root == repo.sut_dir
        assert tier_configs is not None
        assert extra_markers == ["MYPROJ_NO_COV"]

    def test_no_exclusions_table_yields_empty_markers(self):
        repo = self._repo({"tiers": {"system": {"kind": "e2e", "precedence": 1}}})
        with patch("otto.config.get_repos", return_value=[repo]):
            _repo_root, _tier_configs, extra_markers = cov_module._resolve_cov_settings()
        assert extra_markers == []

    def test_no_cov_repo_yields_empty_markers(self):
        with patch("otto.config.get_repos", return_value=[]):
            repo_root, tier_configs, extra_markers = cov_module._resolve_cov_settings()
        assert repo_root is None
        assert tier_configs is None
        assert extra_markers == []


# ── _resolve_tester — identity defaults (spec decision 15) ──────────────────


class TestResolveTester:
    def test_explicit_overrides_win(self, monkeypatch):
        # Should not even consult getpass/git when both are supplied.
        monkeypatch.setattr("getpass.getuser", lambda: pytest.fail("should not be called"))
        tester = cov_module._resolve_tester("Bob", "bob@x.com")
        assert tester == {"name": "Bob", "email": "bob@x.com"}

    def test_defaults_from_getpass_and_git_config(self, monkeypatch):
        monkeypatch.setattr("getpass.getuser", lambda: "alice")

        class FakeProc:
            returncode = 0
            stdout = "alice@example.com\n"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())
        tester = cov_module._resolve_tester(None, None)
        assert tester == {"name": "alice", "email": "alice@example.com"}

    def test_omits_email_when_git_config_unset(self, monkeypatch):
        monkeypatch.setattr("getpass.getuser", lambda: "alice")

        class FakeProc:
            returncode = 1
            stdout = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())
        tester = cov_module._resolve_tester(None, None)
        assert tester == {"name": "alice"}
        assert "email" not in tester

    def test_name_override_with_default_email(self, monkeypatch):
        class FakeProc:
            returncode = 0
            stdout = "carol@example.com\n"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())
        tester = cov_module._resolve_tester("Carol", None)
        assert tester == {"name": "Carol", "email": "carol@example.com"}


# ── get command — validation errors ──────────────────────────────────────────


class TestCovGetValidation:
    @staticmethod
    def _repo(coverage_cfg, sut_dir=None, name="sut"):
        repo = MagicMock()
        repo.settings = {"coverage": coverage_cfg} if coverage_cfg is not None else {}
        repo.sut_dir = sut_dir or Path("/nonexistent/sut")
        repo.name = name
        return repo

    @pytest.fixture
    def git_sut(self, tmp_path):
        """A real one-commit git repo standing in for the SUT checkout.

        Needed by tests that must get *past* ``_do_get``'s git preflight (a
        non-git sut fails fast before the fetch) to exercise a later path.
        """
        root = tmp_path / "sut"
        root.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                env={
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@x",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@x",
                    "HOME": str(tmp_path),
                    "PATH": "/usr/bin:/bin",
                },
            )

        git("init", "-q")
        (root / "f.c").write_text("int a;\n")
        git("add", "f.c")
        git("commit", "-qm", "init")
        return root

    def test_no_coverage_config_exits_1(self):
        repo = self._repo(None)
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "coverage" in mock_err.call_args[0][0].lower()

    def test_unknown_tier_lists_configured_tiers(self):
        repo = self._repo({"hosts": ".*"})
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "--tier", "bogus"])
        assert result.exit_code == 1
        message = mock_err.call_args[0][0]
        assert "bogus" in message
        assert "system" in message

    def test_manual_tier_without_ticket_exits_1(self):
        repo = self._repo(
            {
                "tiers": {
                    "manual": {"kind": "manual", "precedence": 1},
                    "system": {"kind": "e2e", "precedence": 2},
                }
            }
        )
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "--tier", "manual"])
        assert result.exit_code == 1
        message = mock_err.call_args[0][0]
        assert "ticket" in message.lower()
        assert "requires --ticket" in message

    def test_ambiguous_default_tier_lists_candidates_exits_1(self):
        """No --tier given and more than one e2e-kind tier configured is ambiguous."""
        repo = self._repo(
            {
                "tiers": {
                    "sys_a": {"kind": "e2e", "precedence": 1},
                    "sys_b": {"kind": "e2e", "precedence": 2},
                }
            }
        )
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "sys_a" in message
        assert "sys_b" in message

    def test_zero_counters_exits_1(self, monkeypatch, git_sut):
        repo = self._repo({"hosts": ".*"}, sut_dir=git_sut)

        async def fake_collect(cov_config, staging_root, pattern=None):
            return {}

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([])),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=fake_collect,
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(git_sut.parent / "get_out")])
        assert result.exit_code == 1
        assert "no .gcda" in mock_err.call_args[0][0]

    def test_zero_counters_lists_searched_host_names(self, monkeypatch, git_sut):
        """The zero-counter message names the hosts it searched, not just "no data"."""
        repo = self._repo({"hosts": ".*"}, sut_dir=git_sut)
        host1 = MagicMock()
        host1.id = "sprout"

        async def fake_collect(cov_config, staging_root, pattern=None):
            return {}

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([host1])),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=fake_collect,
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(git_sut.parent / "get_out")])
        assert result.exit_code == 1
        message = mock_err.call_args[0][0]
        assert "no .gcda" in message
        assert "sprout" in message

    def test_zero_counters_after_produce_captures_exits_1(self, tmp_path, git_sut):
        """When produce_captures returns empty list despite non-empty host_dirs → error."""
        repo = self._repo({"hosts": ".*"}, sut_dir=git_sut)

        async def fake_collect(cov_config, staging_root, pattern=None):
            board = staging_root / "board1"
            board.mkdir(parents=True, exist_ok=True)
            (board / "x.gcda").write_bytes(b"")
            return {"board1": board}

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([])),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=fake_collect,
            ),
            patch.object(produce_module, "produce_captures", new=AsyncMock(return_value=[])),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(tmp_path / "cov_out")])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "no .gcda" in message
        assert "board1" in message

    def test_non_git_repo_exits_1(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        repo = self._repo({"hosts": ".*"}, sut_dir=not_a_repo)

        async def fake_collect(cov_config, staging_root, pattern=None):
            board = staging_root / "board1"
            board.mkdir(parents=True)
            (board / "x.gcda").write_bytes(b"")
            return {"board1": board}

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{not_a_repo / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([])),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=fake_collect,
            ),
            patch.object(produce_module.LcovMerger, "capture", fake_capture),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(tmp_path / "get_out")])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert mock_err.called
        assert "not a git repository" in mock_err.call_args[0][0]


# ── rich-markup escaping — literal brackets must survive the console handler ─


class TestCovMarkupEscaping:
    """A ``[coverage]``-bearing error message must render *literally* through
    the real ``RichHandler`` console pipeline.

    ``RichHandler``/``rich.console.Console`` are built with ``markup=True``
    (see ``otto.logger.management.init_cli_logging``), so an unescaped
    ``logger.error(str(e))`` of a message containing a literal ``[coverage]``
    is parsed as an (unknown-style) markup tag and the bracketed text is
    silently eaten — e.g. "No [coverage] section found" renders as "No
    section found". This wires a real ``RichHandler`` onto a
    ``Console(file=StringIO())`` (mirroring ``init_cli_logging``'s kwargs) in
    place of the module logger's handlers, so the assertion exercises actual
    rendering rather than just the string handed to ``logger.error``.
    """

    def test_no_config_error_renders_literal_brackets(self):
        import io

        from rich.console import Console
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        repo = TestCovGetValidation._repo(None)
        buf = io.StringIO()
        handler = RichHandler(
            console=Console(file=buf, width=120, force_terminal=False),
            markup=True,
            highlighter=NullHighlighter(),
            show_time=False,
            show_path=False,
        )
        saved_handlers = list(cov_module.logger.handlers)
        saved_level = cov_module.logger.level
        saved_propagate = cov_module.logger.propagate
        cov_module.logger.handlers = [handler]
        cov_module.logger.setLevel(logging.ERROR)
        cov_module.logger.propagate = False
        try:
            with patch("otto.config.get_repos", return_value=[repo]):
                result = runner.invoke(cov_app, ["get"])
        finally:
            cov_module.logger.handlers = saved_handlers
            cov_module.logger.setLevel(saved_level)
            cov_module.logger.propagate = saved_propagate

        assert result.exit_code == 1
        rendered = buf.getvalue()
        assert "[coverage]" in rendered
        assert "No  section found" not in rendered

    def test_clean_no_config_error_renders_literal_brackets(self):
        import io

        from rich.console import Console
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        repo = TestCovGetValidation._repo(None)
        buf = io.StringIO()
        handler = RichHandler(
            console=Console(file=buf, width=120, force_terminal=False),
            markup=True,
            highlighter=NullHighlighter(),
            show_time=False,
            show_path=False,
        )
        saved_handlers = list(cov_module.logger.handlers)
        saved_level = cov_module.logger.level
        saved_propagate = cov_module.logger.propagate
        cov_module.logger.handlers = [handler]
        cov_module.logger.setLevel(logging.ERROR)
        cov_module.logger.propagate = False
        try:
            with patch("otto.config.get_repos", return_value=[repo]):
                result = runner.invoke(cov_app, ["clean"])
        finally:
            cov_module.logger.handlers = saved_handlers
            cov_module.logger.setLevel(saved_level)
            cov_module.logger.propagate = saved_propagate

        assert result.exit_code == 1
        rendered = buf.getvalue()
        assert "[coverage]" in rendered
        assert "No  section found" not in rendered


# ── get command — success (fetch layer stubbed at the I/O boundary) ─────────


class TestCovGetSuccess:
    @pytest.fixture
    def repo(self, tmp_path):
        """A real tmp_path git repo standing in for the SUT."""
        root = tmp_path / "sut"
        root.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                env={
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@x",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@x",
                    "HOME": str(tmp_path),
                    "PATH": "/usr/bin:/bin",
                },
            )

        git("init", "-q")
        (root / "f.c").write_text("int a;\nint b;\n")
        git("add", "f.c")
        git("commit", "-qm", "init")
        return root

    @staticmethod
    def _repo_mock(sut_dir, coverage_cfg, name="sut"):
        repo = MagicMock()
        repo.settings = {"coverage": coverage_cfg}
        repo.sut_dir = sut_dir
        repo.name = name
        return repo

    @staticmethod
    def _fake_capture(sut_dir):
        async def _capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{sut_dir / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        return _capture

    @staticmethod
    def _fake_collect_one_board():
        async def fake_collect(cov_config, staging_root, pattern=None):
            board = staging_root / "board1"
            board.mkdir(parents=True)
            (board / "x.gcda").write_bytes(b"")
            return {"board1": board}

        return fake_collect

    def test_get_defaults_to_the_per_invocation_output_dir(self, tmp_path, repo, monkeypatch):
        """Without --output, `cov get` writes into the standard per-invocation
        output directory the CLI preamble records on the context — the same
        free output dir every other lab-touching command gets."""
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        cov_repo = self._repo_mock(repo, {"tiers": {"system": {"kind": "e2e", "precedence": 1}}})
        monkeypatch.setattr("otto.config.get_repos", lambda: [cov_repo])
        monkeypatch.setattr("otto.config.all_hosts", lambda pattern=None, **kw: iter([]))
        monkeypatch.setattr(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            self._fake_collect_one_board(),
        )
        monkeypatch.setattr(produce_module.LcovMerger, "capture", self._fake_capture(repo))

        invocation_dir = tmp_path / "xdir" / "cov" / "20260703_120000_000_get"
        invocation_dir.mkdir(parents=True)
        token = set_context(OttoContext(lab=Lab(name="t"), output_dir=invocation_dir))
        try:
            result = runner.invoke(cov_app, ["get"])
        finally:
            reset_context(token)

        assert result.exit_code == 0, result.output
        assert (invocation_dir / "cov" / "board1" / "capture.json").is_file()

    def test_get_without_output_dir_anywhere_exits_1(self, repo, monkeypatch):
        """No --output and no context output dir (e.g. a programmatic call
        outside the CLI preamble) fails with a clean one-line error — after
        config/tier validation, so a config problem is never masked by it."""
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        cov_repo = self._repo_mock(repo, {"tiers": {"system": {"kind": "e2e", "precedence": 1}}})
        monkeypatch.setattr("otto.config.get_repos", lambda: [cov_repo])
        monkeypatch.setattr("otto.config.all_hosts", lambda pattern=None, **kw: iter([]))

        token = set_context(OttoContext(lab=Lab(name="t"), output_dir=None))
        try:
            with patch.object(cov_module.logger, "error") as mock_err:
                result = runner.invoke(cov_app, ["get"])
        finally:
            reset_context(token)

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "--output" in mock_err.call_args[0][0]

    def test_get_manual_tier_writes_capture_and_manual_store(self, tmp_path, repo, monkeypatch):
        cov_repo = self._repo_mock(
            repo,
            {
                "tiers": {
                    "manual": {"kind": "manual", "precedence": 1},
                    "system": {"kind": "e2e", "precedence": 2},
                }
            },
        )

        monkeypatch.setattr("otto.config.get_repos", lambda: [cov_repo])
        monkeypatch.setattr("otto.config.all_hosts", lambda pattern=None, **kw: iter([]))
        monkeypatch.setattr(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            self._fake_collect_one_board(),
        )
        monkeypatch.setattr(produce_module.LcovMerger, "capture", self._fake_capture(repo))

        out_dir = tmp_path / "get_out"
        result = runner.invoke(
            cov_app,
            [
                "get",
                "-o",
                str(out_dir),
                "--tier",
                "manual",
                "--ticket",
                "T-1",
                "--note",
                "session note",
                "--tester-name",
                "Bob",
                "--tester-email",
                "bob@x.com",
            ],
        )
        assert result.exit_code == 0, result.output

        capture_path = out_dir / "cov" / "board1" / "capture.json"
        assert capture_path.is_file()
        cap = Capture.load(capture_path)
        assert cap.tier == "manual"
        assert cap.ticket == "T-1"
        assert cap.note == "session note"
        assert cap.tester == {"name": "Bob", "email": "bob@x.com"}

        manual_dir = repo / ".otto" / "coverage" / "manual"
        manual_files = list(manual_dir.glob("*.json"))
        assert len(manual_files) == 1
        manual_cap = Capture.load(manual_files[0])
        assert manual_cap.ticket == "T-1"

    def test_get_default_tier_no_manual_store(self, tmp_path, repo, monkeypatch):
        cov_repo = self._repo_mock(repo, {"hosts": ".*"})

        monkeypatch.setattr("otto.config.get_repos", lambda: [cov_repo])
        monkeypatch.setattr("otto.config.all_hosts", lambda pattern=None, **kw: iter([]))
        monkeypatch.setattr(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            self._fake_collect_one_board(),
        )
        monkeypatch.setattr(produce_module.LcovMerger, "capture", self._fake_capture(repo))

        out_dir = tmp_path / "get_out2"
        result = runner.invoke(cov_app, ["get", "-o", str(out_dir)])
        assert result.exit_code == 0, result.output

        capture_path = out_dir / "cov" / "board1" / "capture.json"
        assert capture_path.is_file()
        cap = Capture.load(capture_path)
        assert cap.tier == "system"
        assert cap.ticket is None
        assert cap.tester is None

        manual_dir = repo / ".otto" / "coverage" / "manual"
        assert not manual_dir.exists() or not list(manual_dir.glob("*.json"))

    def test_resolve_get_tier_called_once_across_full_get_flow(self, tmp_path, repo, monkeypatch):
        """``_do_get`` resolves the tier once; ``collect_coverage`` must not
        re-resolve it from the name.

        Regression for the double-resolve: before the fix, ``_do_get``
        resolved the tier via ``resolve_get_tier`` and then passed only the
        resolved *name* into ``collect_coverage``, which re-resolved it a
        second time internally. Passing the already-resolved ``TierConfig``
        object through means ``resolve_get_tier`` runs exactly once for the
        whole ``otto cov get`` invocation.
        """
        from otto.coverage import tiers as tiers_module

        cov_repo = self._repo_mock(repo, {"hosts": ".*"})

        monkeypatch.setattr("otto.config.get_repos", lambda: [cov_repo])
        monkeypatch.setattr("otto.config.all_hosts", lambda pattern=None, **kw: iter([]))
        monkeypatch.setattr(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            self._fake_collect_one_board(),
        )
        monkeypatch.setattr(produce_module.LcovMerger, "capture", self._fake_capture(repo))

        resolve_spy = MagicMock(wraps=tiers_module.resolve_get_tier)
        monkeypatch.setattr(tiers_module, "resolve_get_tier", resolve_spy)

        out_dir = tmp_path / "get_out_resolve_once"
        result = runner.invoke(cov_app, ["get", "-o", str(out_dir)])
        assert result.exit_code == 0, result.output
        assert resolve_spy.call_count == 1

    def test_get_clean_calls_clean_remote_when_unix_hosts_fetched(
        self, tmp_path, repo, monkeypatch
    ):
        """``--clean`` zeroes remote counters via the same fetcher used to fetch."""
        cov_repo = self._repo_mock(repo, {"hosts": ".*", "gcda_remote_dir": "/remote"})

        unix_host = MagicMock()
        unix_host.id = "host1"
        unix_host.name = "host1"

        from otto.host import UnixHost

        unix_host.__class__ = UnixHost

        out_dir = tmp_path / "get_out3"
        # _do_get resolves cov_dir = output_dir / "cov"; the mocked fetcher
        # doesn't touch disk, so the board dir + meta parent must exist here.
        board = out_dir / "cov" / "host1"
        board.mkdir(parents=True)
        (board / "x.gcda").write_bytes(b"")

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={"host1": board})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        async def fake_embedded(cov_config, staging_root, pattern=None):
            return {}

        with (
            patch("otto.config.get_repos", return_value=[cov_repo]),
            patch("otto.config.all_hosts", return_value=[unix_host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=fake_embedded,
            ),
            patch.object(produce_module.LcovMerger, "capture", self._fake_capture(repo)),
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(out_dir), "--clean"])

        assert result.exit_code == 0, result.output
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")

    def test_get_clean_scopes_clean_pattern_to_unix_hosts_only(self, tmp_path, repo, monkeypatch):
        """Mixed lab: ``get --clean`` must zero only the Unix hosts, never the
        embedded board — the post-fetch clean uses a second fetcher scoped to
        the unix ids (clean_remote re-derives its own host set with no
        EmbeddedHost guard, the exact bug already fixed for `cov clean`)."""
        cov_repo = self._repo_mock(repo, {"hosts": ".*", "gcda_remote_dir": "/remote"})

        from otto.host import UnixHost
        from otto.host.embedded_host import EmbeddedHost

        unix_host = MagicMock()
        unix_host.id = "sprout_cov"
        unix_host.name = "sprout_cov"
        unix_host.__class__ = UnixHost
        embedded_host = MagicMock()
        embedded_host.id = "zeph1"
        embedded_host.name = "zeph1"
        embedded_host.__class__ = EmbeddedHost

        out_dir = tmp_path / "get_out_clean"
        board = out_dir / "cov" / "sprout_cov"
        board.mkdir(parents=True)
        (board / "x.gcda").write_bytes(b"")

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={"sprout_cov": board})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        async def fake_embedded(cov_config, staging_root, pattern=None):
            return {}

        with (
            patch("otto.config.get_repos", return_value=[cov_repo]),
            patch("otto.config.all_hosts", return_value=[unix_host, embedded_host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as mock_fetcher_cls,
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=fake_embedded),
            patch.object(produce_module.LcovMerger, "capture", self._fake_capture(repo)),
        ):
            result = runner.invoke(cov_app, ["get", "-o", str(out_dir), "--clean"])

        assert result.exit_code == 0, result.output
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")
        # The clean fetcher (second construction) is scoped to unix ids only.
        clean_pattern = mock_fetcher_cls.call_args_list[-1].kwargs["pattern"]
        assert clean_pattern.search("sprout_cov")
        assert not clean_pattern.search("zeph1")


# ── clean command — validation errors ────────────────────────────────────────


class TestCovCleanValidation:
    @staticmethod
    def _repo(coverage_cfg, name="sut"):
        repo = MagicMock()
        repo.settings = {"coverage": coverage_cfg} if coverage_cfg is not None else {}
        repo.name = name
        return repo

    def test_no_coverage_config_exits_1(self):
        repo = self._repo(None)
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "coverage" in mock_err.call_args[0][0].lower()

    def test_missing_gcda_remote_dir_exits_1(self):
        repo = self._repo({"hosts": ".*"})
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([])),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "gcda_remote_dir" in mock_err.call_args[0][0]

    def test_no_matching_hosts_exits_1(self):
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", lambda pattern=None, **kw: iter([])),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert mock_err.called


# ── clean command — success (fetch layer stubbed at the I/O boundary) ───────


class TestCovCleanSuccess:
    @staticmethod
    def _repo(coverage_cfg, name="sut"):
        repo = MagicMock()
        repo.settings = {"coverage": coverage_cfg}
        repo.name = name
        return repo

    @staticmethod
    def _unix_host(host_id="host1"):
        from otto.host import UnixHost

        host = MagicMock()
        host.id = host_id
        host.__class__ = UnixHost
        return host

    @staticmethod
    def _embedded_host(host_id="board1"):
        from otto.host.embedded_host import EmbeddedHost

        host = MagicMock()
        host.id = host_id
        host.__class__ = EmbeddedHost
        return host

    def test_clean_calls_clean_remote_with_configured_dir(self):
        """The required TDD case: stubbed fetcher, clean_remote invoked, exit 0."""
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        unix_host = self._unix_host()

        fetcher_instance = MagicMock()
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", return_value=[unix_host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
        ):
            result = runner.invoke(cov_app, ["clean"])

        assert result.exit_code == 0, result.output
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")

    def test_clean_embedded_only_logs_note_and_skips_clean_remote(self):
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        embedded_host = self._embedded_host()

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", return_value=[embedded_host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher") as mock_fetcher_cls,
            patch.object(cov_module.logger, "info") as mock_info,
        ):
            result = runner.invoke(cov_app, ["clean"])

        assert result.exit_code == 0, result.output
        mock_fetcher_cls.assert_not_called()
        assert any(
            "embedded boards not cleaned" in str(c.args[0]) for c in mock_info.call_args_list
        )

    def test_clean_mixed_hosts_cleans_unix_and_notes_embedded(self):
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        unix_host = self._unix_host()
        embedded_host = self._embedded_host()

        fetcher_instance = MagicMock()
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", return_value=[unix_host, embedded_host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
            patch.object(cov_module.logger, "info") as mock_info,
        ):
            result = runner.invoke(cov_app, ["clean"])

        assert result.exit_code == 0, result.output
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")
        assert any(
            "embedded boards not cleaned" in str(c.args[0]) for c in mock_info.call_args_list
        )

    def test_clean_scopes_fetcher_pattern_to_unix_hosts_only(self):
        """Mixed lab: the fetcher's pattern (which clean_remote()'s own
        do_for_all_hosts() call re-matches against every lab host, with no
        EmbeddedHost guard) must only match the unix host, never the
        embedded one — even though both matched [coverage].hosts."""
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        unix_host = self._unix_host("sprout_cov")
        embedded_host = self._embedded_host("zeph1")

        fetcher_instance = MagicMock()
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", return_value=[unix_host, embedded_host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as mock_fetcher_cls,
            patch.object(cov_module.logger, "info") as mock_info,
        ):
            result = runner.invoke(cov_app, ["clean"])

        assert result.exit_code == 0, result.output
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")

        used_pattern = mock_fetcher_cls.call_args.kwargs["pattern"]
        assert used_pattern.search("sprout_cov")
        assert not used_pattern.search("zeph1")
        assert any(
            "embedded boards not cleaned" in str(c.args[0]) for c in mock_info.call_args_list
        )

    def test_clean_pattern_does_not_let_prefix_id_collide(self):
        """A unix host id that is a prefix of another host's id (e.g.
        "sprout" vs. "sprout2") must not accidentally match the longer id
        through an unanchored regex search."""
        repo = self._repo({"hosts": ".*", "gcda_remote_dir": "/remote"})
        unix_host = self._unix_host("sprout")
        other_host = self._embedded_host("sprout2")

        fetcher_instance = MagicMock()
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch("otto.config.get_repos", return_value=[repo]),
            patch("otto.config.all_hosts", return_value=[unix_host, other_host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as mock_fetcher_cls,
        ):
            result = runner.invoke(cov_app, ["clean"])

        assert result.exit_code == 0, result.output
        used_pattern = mock_fetcher_cls.call_args.kwargs["pattern"]
        assert used_pattern.search("sprout")
        assert not used_pattern.search("sprout2")


# ── _capture_annotations — tier-aware annotation resolution ────────────────────


class TestCaptureAnnotations:
    """ticket/note annotate every tier kind; tester attribution stays manual-only."""

    def test_e2e_kind_keeps_ticket_and_note_but_no_tester(self):
        from otto.cli.cov import _capture_annotations

        tester, ticket, note = _capture_annotations("e2e", "CI-77", "nightly run", "Al", "al@x")
        assert tester is None
        assert (ticket, note) == ("CI-77", "nightly run")

    def test_manual_kind_resolves_tester(self, monkeypatch):
        import otto.cli.cov as cov_mod

        monkeypatch.setattr(cov_mod, "_resolve_tester", lambda n, e: {"name": n, "email": e})
        tester, ticket, note = cov_mod._capture_annotations("manual", "T-1", None, "Al", "al@x")
        assert tester == {"name": "Al", "email": "al@x"}
        assert (ticket, note) == ("T-1", None)
