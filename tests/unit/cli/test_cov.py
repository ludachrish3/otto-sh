"""
Unit tests for the ``otto cov`` subcommand.

Covers:
  - Help / no-args behaviour
  - ``otto cov report`` happy path
  - ``otto cov report`` validation errors
  - ``otto cov get`` and ``otto cov clean``: the flags they pass to the library,
    how they render its report, and how its refusals become exit codes (the
    rules themselves are tested in ``tests/unit/cov/test_get.py`` and
    ``tests/unit/cov/test_clean.py``)
"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli import cov as cov_module
from otto.cli.cov import cov_app
from otto.config.coverage_settings import CoverageConfigError
from otto.config.scope import EmptySelectionError
from otto.coverage import reporter as reporter_module
from otto.coverage.capture.gitio import NotAGitRepoError
from otto.coverage.errors import (
    CoverageDataMismatchError,
    CoverageInputError,
    CoverageNotInstrumentedError,
    NoCoverageDataError,
    NoCoverageHostsError,
)
from otto.coverage.report_inputs import ReportInputs
from otto.coverage.reports import CleanReport, GetReport
from otto.host.errors import HostUnreachableError
from otto.result import Result
from otto.utils import Status
from tests.unit.cli.conftest import _flat

runner = CliRunner()


def _product_double(name="app", *, instrumented=True):
    """A stand-in :class:`otto.host.product.Product` with a known verdict.

    ``otto cov get`` runs the local instrumentation scan over the coverage
    hosts before it fetches anything, so every host double in this file needs
    products it can read a verdict off.
    """
    product = MagicMock()
    product.name = name
    product.instrumented.return_value = instrumented
    return product


def _host_double(host_id="host1", *, products=("app",), cls=None, instrumented=True):
    """A lab host double carrying instrumentation-scannable products."""
    host = MagicMock()
    host.id = host_id
    host.name = host_id
    host.products = [_product_double(p, instrumented=instrumented) for p in products]
    if cls is not None:
        host.__class__ = cls
    return host


def _embedded_board(host_id="board1", *, products=("app",), instrumented=True):
    """An embedded host double: no filesystem to fetch, so no fetcher runs."""
    from otto.host.embedded_host import EmbeddedHost

    return _host_double(host_id, products=products, cls=EmbeddedHost, instrumented=instrumented)


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
        # typer 0.27 renders the positional's metavar as the param name
        # (`output_dirs`); 0.26 upcased it — compare case-insensitively.
        assert "output_dirs" in result.output.lower()

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

    def test_get_and_report_want_the_per_invocation_output_dir_and_clean_does_not(self):
        assert getattr(cov_module.get, "__cli_output_dir__", True) is True
        assert getattr(cov_module.report, "__cli_output_dir__", True) is True
        assert cov_module.clean.__cli_output_dir__ is False


# ── report command — validation errors ───────────────────────────────────────


class TestCovReportValidation:
    """The report's input rules are ``run_coverage_report``'s; the leaf only
    splits ``--tier NAME[=PATH]`` (syntax) and spells a refusal in its flags."""

    @pytest.fixture
    def run_dir(self, tmp_path):
        run = tmp_path / "run"
        (run / "cov").mkdir(parents=True)
        return run

    def test_a_missing_run_dir_is_a_usage_error_naming_output_dirs(self, tmp_path):
        missing = tmp_path / "no_such_run"
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()
        ):
            result = runner.invoke(
                cov_app, ["report", str(missing), "--dir", str(tmp_path / "out")]
            )
        assert result.exit_code == 2
        out = _flat(result.output)
        assert "Invalid value for OUTPUT_DIRS" in out
        assert "output directory does not exist" in out
        assert not (tmp_path / "out").exists()

    def test_a_non_system_tier_without_a_path_is_a_usage_error(self, run_dir, tmp_path):
        result = runner.invoke(
            cov_app,
            ["report", str(run_dir), "--tier", "unit", "--dir", str(tmp_path / "out")],
        )
        assert result.exit_code == 2
        out = _flat(result.output)
        assert "Invalid value for --tier" in out
        assert "Tier 'unit' requires a path (only the 'system' tier may omit a path)" in out

    def test_a_duplicate_tier_name_is_a_usage_error(self, run_dir, tmp_path):
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(run_dir),
                "--tier",
                "a=x.info",
                "--tier",
                "a=y.info",
                "--dir",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code == 2
        out = _flat(result.output)
        assert "Invalid value for --tier" in out
        assert "Duplicate tier name: 'a'" in out

    @pytest.mark.parametrize("raw", ["=x.info", "unit=", ""])
    def test_a_malformed_tier_value_is_a_usage_error(self, run_dir, tmp_path, raw):
        report_mock = AsyncMock()
        with patch.object(reporter_module, "run_coverage_report", report_mock):
            result = runner.invoke(
                cov_app,
                ["report", str(run_dir), "--tier", raw, "--dir", str(tmp_path / "out")],
            )
        assert result.exit_code == 2
        assert "Invalid value for --tier" in _flat(result.output)
        report_mock.assert_not_called()

    def test_no_gcda_dirs_exits_1(self, tmp_path):
        """Real directory but no cov/ subdirectory → error (git-less legacy path)."""
        # Pin the git-less scenario: no [coverage] settings resolvable, so the
        # legacy no-data path runs and returns None → exit 1. (Without this the
        # outcome would depend on whatever repo bootstrap resolved globally.)
        with (
            patch("otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--dir", str(tmp_path / "report_out")]
            )
        assert result.exit_code == 1
        assert "not generated" in mock_err.call_args[0][0]

    def test_source_root_not_found_exits_1(self, tmp_path):
        # Create a cov/ subdir with host dir so discover_gcda_dirs returns
        # entries, but no .otto_cov_meta.json so read_cov_source_root fails.
        # Pin the git-less scenario so only the legacy path is exercised.
        (tmp_path / "cov" / "host1").mkdir(parents=True)
        with (
            patch("otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()),
            patch.object(cov_module.logger, "error"),
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--dir", str(tmp_path / "report_out")]
            )
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
                reporter_module,
                "run_coverage_report",
                side_effect=CoverageDataMismatchError("x.gcda:stamp mismatch with notes file"),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(cov_dir), "--dir", str(cov_dir / "report")]
            )
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
                reporter_module,
                "run_coverage_report",
                side_effect=CoverageToolVersionError("Your test was built with '4.8'."),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(cov_dir), "--dir", str(cov_dir / "report")]
            )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "clang" in message  # names the likely cause
        assert "llvm-cov" in message  # names the fix

    def test_missing_gcov_tool_reports_cause_without_traceback(self, cov_dir):
        """A stamp naming a gcov not on PATH must exit 1 with the message
        as-is — no traceback, and never relabeled as a merge failure."""
        from otto.host.errors import CoverageToolMissingError

        with (
            patch.object(
                reporter_module,
                "run_coverage_report",
                side_effect=CoverageToolMissingError(
                    "Coverage data for product 'app' on host 'test1' was written by "
                    "gcc 12 (gcov stamp '12.2.0') and needs `gcov-12` to read it, but no "
                    "`gcov-12` is on PATH. Install it (`apt install gcc-12`), or name the "
                    "gcov to use in the host's `toolchain.gcov`."
                ),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(cov_dir), "--dir", str(cov_dir / "report")]
            )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "gcov-12" in message  # names the missing tool
        assert "Coverage merge failed" not in message

    def test_generic_merge_failure_reports_cleanly(self, cov_dir):
        with (
            patch.object(
                reporter_module,
                "run_coverage_report",
                side_effect=RuntimeError("lcov --capture failed:\nsome lcov noise"),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(cov_dir), "--dir", str(cov_dir / "report")]
            )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "Coverage merge failed" in mock_err.call_args[0][0]

    def test_prefix_option_forwards_to_reporter(self, cov_dir):
        with patch.object(
            reporter_module, "run_coverage_report", new=AsyncMock(return_value=None)
        ) as rcr:
            runner.invoke(
                cov_app,
                ["report", str(cov_dir), "--prefix", "/repo", "--dir", str(cov_dir / "report")],
            )
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
        with patch.object(reporter_module, "run_coverage_report", mock):
            yield mock, mock_store

    def test_report_success(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app, ["report", str(cov_tree), "--dir", str(cov_tree / "report")]
        )
        assert result.exit_code == 0
        mock.assert_called_once()

    def test_report_defaults_into_the_invocation_output_dir(
        self, cov_tree, mock_run_report, tmp_path
    ):
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        mock, _ = mock_run_report
        run_dir = tmp_path / "xdir" / "cov" / "20260703_120000_000_report"
        run_dir.mkdir(parents=True)
        token = set_context(OttoContext(lab=Lab(name="t"), output_dir=run_dir))
        try:
            result = runner.invoke(cov_app, ["report", str(cov_tree)])
        finally:
            reset_context(token)
        assert result.exit_code == 0, result.output
        assert mock.call_args.args[1] == run_dir / "cov_report"
        assert mock.call_args.kwargs["overwrite"] is False

    def test_overwrite_dir_flag_is_passed_through_to_run_coverage_report(
        self, cov_tree, mock_run_report, tmp_path
    ):
        """``--overwrite-dir`` must reach ``run_coverage_report(overwrite=...)``
        as ``True`` — without this test, wiring it to a hardcoded ``False``
        (or dropping the flag's effect entirely) would still pass every
        other test in this file."""
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            ["report", str(cov_tree), "--dir", str(tmp_path / "out"), "--overwrite-dir"],
        )
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["overwrite"] is True

    def test_report_without_an_output_dir_or_dir_fails_naming_dir(self, cov_tree, mock_run_report):
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        mock, _ = mock_run_report
        token = set_context(OttoContext(lab=Lab(name="t"), output_dir=None))
        try:
            result = runner.invoke(cov_app, ["report", str(cov_tree)])
        finally:
            reset_context(token)
        assert result.exit_code == 2
        assert "--dir" in result.output
        mock.assert_not_called()

    def test_explicit_dir_is_refused_when_non_empty(self, cov_tree, tmp_path):
        target = tmp_path / "out"
        target.mkdir()
        (target / "stale.html").write_text("stale")
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()
        ):
            result = runner.invoke(cov_app, ["report", str(cov_tree), "--dir", str(target)])
        assert result.exit_code == 2
        assert "pass --overwrite-dir to clear it" in result.output
        assert (target / "stale.html").exists()

    def test_report_custom_report_dir(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app,
            [
                "report",
                str(cov_tree),
                "--dir",
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
                "--dir",
                str(cov_tree / "report"),
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

        result = runner.invoke(
            cov_app, ["report", str(dir1), str(dir2), "--dir", str(tmp_path / "report")]
        )
        assert result.exit_code == 0
        mock.assert_called_once()
        # Should have forwarded two cov dirs
        args = mock.call_args.args
        assert args[0] == [dir1 / "cov", dir2 / "cov"]

    def test_report_default_tier_is_system(self, cov_tree, mock_run_report):
        """No --tier → default to system-only."""
        mock, _ = mock_run_report
        result = runner.invoke(
            cov_app, ["report", str(cov_tree), "--dir", str(cov_tree / "report")]
        )
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
                "--dir",
                str(cov_tree / "report"),
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
                "--dir",
                str(cov_tree / "report"),
            ],
        )
        assert result.exit_code == 0
        names = [name for name, _ in mock.call_args.kwargs["tier_specs"]]
        assert names == ["unit", "system", "integration", "manual"]


# ── report command — --tickets-json export ───────────────────────────────────


class TestCovReportTicketsJson:
    @pytest.fixture
    def cov_tree(self, tmp_path):
        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")
        return tmp_path

    @pytest.fixture
    def mock_run_report(self):
        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 75.0
        mock_store.file_count.return_value = 3
        mock = AsyncMock(return_value=mock_store)
        with patch.object(reporter_module, "run_coverage_report", mock):
            yield mock, mock_store

    @pytest.fixture
    def mock_resolve_settings(self, tmp_path):
        """A resolved repo_root -- --tickets-json requires one (to emit
        repo-relative paths), and the settings-driven collection-model path
        (not --tier) is what normally resolves it."""
        from otto.coverage.tickets import build_ticket_spec
        from otto.coverage.tiers import TierConfig

        repo_root = tmp_path / "sut"
        tiers = [TierConfig(name="system", kind="e2e", precedence=1, color="green")]
        spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            return_value=ReportInputs(repo_root=repo_root, tier_configs=tiers, ticket_spec=spec),
        ):
            yield repo_root

    def test_no_flag_never_writes_export(self, cov_tree, mock_run_report):
        with patch("otto.coverage.ticket_export.write_ticket_export") as mock_write:
            result = runner.invoke(
                cov_app, ["report", str(cov_tree), "--dir", str(cov_tree / "report")]
            )
        assert result.exit_code == 0
        mock_write.assert_not_called()

    def test_tickets_json_writes_export_with_expected_args(
        self, cov_tree, mock_run_report, mock_resolve_settings
    ):
        _mock, mock_store = mock_run_report
        repo_root = mock_resolve_settings
        target = cov_tree / "tickets.json"
        with patch("otto.coverage.ticket_export.write_ticket_export") as mock_write:
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(cov_tree),
                    "--tickets-json",
                    str(target),
                    "--project-name",
                    "My App",
                    "--dir",
                    str(cov_tree / "report"),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_write.assert_called_once()
        args, kwargs = mock_write.call_args
        assert args[0] is mock_store
        assert args[1] == target
        assert kwargs["repo_root"] == repo_root
        assert kwargs["project"] == "My App"
        assert isinstance(kwargs["otto_version"], str)
        assert isinstance(kwargs["generated"], str)

    def test_tickets_json_no_ticket_data_exits_1_clean(
        self, cov_tree, mock_run_report, mock_resolve_settings
    ):
        """--tickets-json was explicitly requested: no ticket data must abort
        the whole command (loud-fail), unlike otto test's swallow policy."""
        target = cov_tree / "tickets.json"
        with (
            patch(
                "otto.coverage.ticket_export.write_ticket_export",
                side_effect=ValueError("no ticket data in this report — [coverage.tickets] ..."),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(cov_tree),
                    "--tickets-json",
                    str(target),
                    "--dir",
                    str(cov_tree / "report"),
                ],
            )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "[coverage.tickets]" in mock_err.call_args[0][0]

    def test_tickets_json_without_repo_root_exits_1_clean(self, cov_tree, mock_run_report):
        """The --tier legacy path never resolves a repo_root (or a
        ticket_spec) -- store.tickets is guaranteed empty there, so this
        must fail with the same clean "no ticket data" message rather than
        crashing on a None repo_root inside write_ticket_export."""
        target = cov_tree / "tickets.json"
        with patch.object(cov_module.logger, "error") as mock_err:
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(cov_tree),
                    "--tickets-json",
                    str(target),
                    "--tier",
                    "system",
                    "--dir",
                    str(cov_tree / "report"),
                ],
            )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "[coverage.tickets]" in mock_err.call_args[0][0]


# ── report command — collection-model wiring (Task 10) ──────────────────────


class TestCovReportCollectionModel:
    @pytest.fixture
    def mock_run_report(self):
        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 50.0
        mock_store.file_count.return_value = 1
        mock = AsyncMock(return_value=mock_store)
        with patch.object(reporter_module, "run_coverage_report", mock):
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
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            return_value=ReportInputs(repo_root=repo_root, tier_configs=tiers),
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--dir", str(tmp_path / "report")]
            )

        assert result.exit_code == 0
        inputs = mock_run_report.call_args.args[2]
        assert inputs.repo_root == repo_root
        assert inputs.tier_configs == tiers
        assert mock_run_report.call_args.kwargs["tier_specs"] == [("system", None)]

    def test_ticket_spec_threaded_from_settings(self, tmp_path, mock_run_report):
        """[coverage.tickets] (via resolve_report_inputs) reaches run_coverage_report."""
        from otto.coverage.tickets import build_ticket_spec
        from otto.coverage.tiers import TierConfig

        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        repo_root = tmp_path / "sut"
        tiers = [TierConfig(name="system", kind="e2e", precedence=1, color="green")]
        spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            return_value=ReportInputs(repo_root=repo_root, tier_configs=tiers, ticket_spec=spec),
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--dir", str(tmp_path / "report")]
            )

        assert result.exit_code == 0
        assert mock_run_report.call_args.args[2].ticket_spec is spec

    def test_explicit_tier_flags_never_thread_ticket_spec(self, tmp_path, mock_run_report):
        """--tier bypasses settings resolution entirely, so ticket_spec stays None
        even when [coverage.tickets] would otherwise resolve one."""
        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            side_effect=AssertionError("must not resolve"),
        ):
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(tmp_path),
                    "--tier",
                    "unit=/u.info",
                    "--tier",
                    "system",
                    "--dir",
                    str(tmp_path / "report"),
                ],
            )

        assert result.exit_code == 0
        assert mock_run_report.call_args.args[2].ticket_spec is None

    def test_exclusion_rules_threaded_from_settings(self, tmp_path, mock_run_report):
        """[coverage.exclusions].rules reach run_coverage_report as compiled rules."""
        from otto.coverage.exclusions.rules import MarkerRule
        from otto.coverage.tiers import TierConfig

        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        repo_root = tmp_path / "sut"
        tiers = [TierConfig(name="system", kind="e2e", precedence=1, color="green")]
        rules = [MarkerRule(stat="line", name="MYPROJ_NO_COV")]
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            return_value=ReportInputs(
                repo_root=repo_root, tier_configs=tiers, exclusion_rules=rules
            ),
        ):
            result = runner.invoke(
                cov_app, ["report", str(tmp_path), "--dir", str(tmp_path / "report")]
            )

        assert result.exit_code == 0
        threaded = mock_run_report.call_args.args[2].exclusion_rules
        assert [r.name for r in threaded] == ["MYPROJ_NO_COV"]

    def test_explicit_tier_flags_bypass_settings(self, tmp_path, mock_run_report):
        """--tier escape hatch: no settings resolution, repo_root/tier_configs None."""
        host_dir = tmp_path / "cov" / "host1"
        host_dir.mkdir(parents=True)
        (host_dir / "main.gcda").write_bytes(b"\x00")

        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            side_effect=AssertionError("must not resolve"),
        ):
            result = runner.invoke(
                cov_app,
                [
                    "report",
                    str(tmp_path),
                    "--tier",
                    "unit=/u.info",
                    "--tier",
                    "system",
                    "--dir",
                    str(tmp_path / "report"),
                ],
            )

        assert result.exit_code == 0
        inputs = mock_run_report.call_args.args[2]
        assert inputs.repo_root is None
        assert inputs.tier_configs is None
        assert mock_run_report.call_args.kwargs["tier_specs"] == [
            ("unit", Path("/u.info")),
            ("system", None),
        ]

    def test_no_output_dirs_allowed_for_manual_only_report(self, mock_run_report, tmp_path):
        """output_dirs is optional: a manual-store-only report needs no run dirs."""
        repo_root = Path("/some/repo")
        with patch(
            "otto.coverage.report_inputs.resolve_report_inputs",
            return_value=ReportInputs(repo_root=repo_root),
        ):
            result = runner.invoke(cov_app, ["report", "--dir", str(tmp_path / "report")])

        assert result.exit_code == 0
        args = mock_run_report.call_args.args
        assert args[0] == []  # no cov dirs
        assert args[2].repo_root == repo_root


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
            patch(
                "otto.coverage.report_inputs.resolve_report_inputs",
                return_value=ReportInputs(repo_root=repo_root, tier_configs=self._tiers()),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", "--dir", str(tmp_path / "report")])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "malformed manual capture" in mock_err.call_args[0][0]

    def test_empty_report_exits_1_naming_searched_inputs(self, tmp_path):
        """A store with zero files is a vacuous success; the CI-friendly loud
        fail is restored — exit 1 naming the inputs it searched."""
        repo_root = tmp_path / "sut"

        with (
            patch(
                "otto.coverage.report_inputs.resolve_report_inputs",
                return_value=ReportInputs(repo_root=repo_root, tier_configs=self._tiers()),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", "--dir", str(tmp_path / "report")])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "no coverage data found in" in mock_err.call_args[0][0]
        # Names the committed manual store it searched.
        assert "manual" in str(mock_err.call_args[0][1])

    def test_non_git_repo_root_reports_cleanly(self, tmp_path):
        """A [coverage] repo_root that is not a git repo can't run pinned-capture
        features; report names the cause + the git-less escape hatch, no traceback."""
        not_git = tmp_path / "notgit"
        (not_git / "cov" / "board1" / "app").mkdir(parents=True)
        (not_git / "cov" / "board1" / "app" / "capture.json").write_text("{}")

        with (
            patch(
                "otto.coverage.report_inputs.resolve_report_inputs",
                return_value=ReportInputs(repo_root=not_git, tier_configs=self._tiers()),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(
                cov_app, ["report", str(not_git), "--dir", str(tmp_path / "report")]
            )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "not a git repository" in message
        assert "--tier" in message

    def test_malformed_overrides_file_exits_1_no_traceback(self, tmp_path):
        """A settings tree with [coverage.tickets] and a malformed override
        file: resolve_report_inputs's real load_override_config call raises
        OverrideConfigError (a ValueError), which report's existing
        `except ValueError` handler must print clean — no traceback."""
        repo = MagicMock()
        repo.sut_dir = tmp_path / "sut"
        repo.sut_dir.mkdir()
        repo.settings = {
            "coverage": {
                "tiers": {"system": {"kind": "e2e", "precedence": 1}},
                "tickets": {"pattern": r"#(?P<n>[0-9]+)"},
            }
        }
        overrides_path = repo.sut_dir / ".otto" / "coverage-overrides.toml"
        overrides_path.parent.mkdir(parents=True)
        overrides_path.write_text("not valid toml {{{")

        with (
            patch("otto.bootstrap.get_repos", return_value=[repo]),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["report", "--dir", str(tmp_path / "report")])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "not valid TOML" in mock_err.call_args[0][0]


# ── get command — a thin leaf over get_coverage ─────────────────────────────
#
# Every get RULE (tier, ticket, instrumentation, repository, destination, the
# manual store, the scoped clean) is the library's, tested in
# tests/unit/cov/test_get.py. These tests own what only the leaf does: pass the
# parsed flags through, render the report, and turn a refusal into an exit code.


def _get_report(*, captures=(), clean=None):
    return GetReport(
        cov_dir=Path("/o/cov"),
        tier="system",
        captures=list(captures),
        manual_captures=[],
        clean=clean,
    )


def _not_instrumented_refusal(instrumented):
    """The refusal ``get_coverage`` raises for a lab of one board with *instrumented*'s verdict."""
    from otto.coverage.instrumentation import decide_coverage, detect

    board = _embedded_board("board1", products=("app",), instrumented=instrumented)
    with pytest.raises(CoverageNotInstrumentedError) as e:
        decide_coverage(True, detect([board]), has_cov_config=True, command="otto cov get")
    return e.value


class TestCovGetLeaf:
    def test_every_parsed_flag_reaches_get_coverage_unchanged(self):
        get_mock = AsyncMock(return_value=_get_report(captures=[Path("/o/cov/b/a/capture.json")]))
        with patch("otto.coverage.get.get_coverage", get_mock):
            result = runner.invoke(
                cov_app,
                [
                    "get",
                    "-o",
                    "/o",
                    "--tier",
                    "manual",
                    "--ticket",
                    "T-1",
                    "--note",
                    "n",
                    "--tester-name",
                    "Bob",
                    "--tester-email",
                    "bob@x.com",
                    "--clean",
                ],
            )
        assert result.exit_code == 0, result.output
        get_mock.assert_awaited_once_with(
            Path("/o"),
            tier="manual",
            ticket="T-1",
            note="n",
            tester_name="Bob",
            tester_email="bob@x.com",
            clean=True,
        )

    def test_no_flags_leave_every_default_to_the_library(self):
        get_mock = AsyncMock(return_value=_get_report(captures=[Path("c")]))
        with patch("otto.coverage.get.get_coverage", get_mock):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 0, result.output
        get_mock.assert_awaited_once_with(
            None,
            tier=None,
            ticket=None,
            note=None,
            tester_name=None,
            tester_email=None,
            clean=False,
        )

    def test_an_input_refusal_is_a_usage_error_in_flag_spelling(self):
        err = CoverageInputError(
            "tier 'manual' is a manual-kind tier and requires a ticket", field="ticket"
        )
        with patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=err)):
            result = runner.invoke(cov_app, ["get", "--tier", "manual"])
        assert result.exit_code == 2
        assert "Invalid value for --ticket" in _flat(result.output)
        assert "requires a ticket" in _flat(result.output)

    def test_ambiguous_default_tier_spells_the_remedy_as_a_flag(self):
        err = CoverageInputError(
            "cannot pick a default tier: 2 e2e-kind tiers configured (sys_a, sys_b); "
            "name one of them",
            field="tier",
        )
        with patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=err)):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 2
        out = _flat(result.output)
        assert "Invalid value for --tier" in out
        assert "sys_a, sys_b" in out

    def test_no_output_dir_anywhere_names_the_output_flag(self):
        err = CoverageInputError(
            "no output directory was given and none is set for this invocation",
            field="output_dir",
        )
        with patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=err)):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 2
        assert "Invalid value for --output" in _flat(result.output)

    def test_the_input_refusal_message_passes_through_byte_identical(self):
        """A message embedding user text is never rewritten: only the hint names the flag."""
        message = "unknown tier 'tier_name[x]'; configured tiers: system"
        err = CoverageInputError(message, field="tier")
        with patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=err)):
            result = runner.invoke(cov_app, ["get", "--tier", "tier_name[x]"])
        assert result.exit_code == 2
        assert message in _flat(result.output)

    def test_a_library_refusal_prints_clean_and_exits_1(self):
        with (
            patch(
                "otto.coverage.get.get_coverage",
                AsyncMock(side_effect=CoverageConfigError("No [coverage] section found")),
            ),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "[coverage]" in mock_err.call_args[0][0]  # bracket survives escaping

    @pytest.mark.parametrize(
        ("error", "logged"),
        [
            (EmptySelectionError("sensor", 3), "fullmatches none of the 3 host(s)"),
            (NotAGitRepoError("not a git repository: /sut"), "not a git repository: /sut"),
            (
                NoCoverageDataError("no .gcda counters retrieved from any product (b1:app)"),
                "no .gcda counters retrieved from any product (b1:app)",
            ),
            (CoverageDataMismatchError("stamp"), "Coverage data does not match"),
            (HostUnreachableError("board1 is down"), "board1 is down"),
            (RuntimeError("lcov exploded"), "Coverage merge failed: lcov exploded"),
        ],
        ids=[
            "empty-selection",
            "not-a-git-repo",
            "no-coverage-data",
            "data-mismatch",
            "host-unreachable",
            "bare-runtime-error",
        ],
    )
    def test_each_library_refusal_is_one_clean_line_and_exit_1(self, error, logged):
        with (
            patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=error)),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert not isinstance(result.exception, type(error)), "reached typer unframed"
        assert logged in " ".join(mock_err.call_args[0][0].split())

    def test_no_instrumented_product_shows_the_verdict_table(self):
        """The verdicts reach the console as the instrumentation table; the logged
        line is the HEADLINE only — the rest of the message is the same verdicts
        in plain text, and printing it under the table would show them twice."""
        refusal = _not_instrumented_refusal(instrumented=False)
        with (
            patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=refusal)),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        message = mock_err.call_args[0][0]
        assert "no instrumented product" in message
        assert "\n" not in message
        assert "coverage instrumentation" in result.output
        for cell in ("host", "product", "instrumented", "board1", "app", "no"):
            assert cell in result.output

    def test_the_refusal_table_names_the_override_for_an_unknown_verdict(self):
        """An `unknown` verdict is the one the reader can DO something about."""
        refusal = _not_instrumented_refusal(instrumented=None)
        with (
            patch("otto.coverage.get.get_coverage", AsyncMock(side_effect=refusal)),
            patch.object(cov_module.logger, "error"),
        ):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 1
        assert "unknown" in result.output
        assert "Product.instrumented" in result.output
        # The caption's `[[products]]` must survive rich's markup parser.
        assert "[[products]]" in result.output

    def test_success_prints_each_capture_and_the_summary(self):
        report = _get_report(captures=[Path("/o/cov/t1/app/capture.json")])
        with patch("otto.coverage.get.get_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["get", "-o", "/o"])
        assert result.exit_code == 0, result.output
        out = _flat(result.output)
        assert "/o/cov/t1/app/capture.json" in out
        assert "Coverage captured: 1 product(s) -> /o/cov" in out

    def test_a_capture_path_with_brackets_prints_literally(self):
        report = _get_report(captures=[Path("/o/cov/[t1]/app/capture.json")])
        with patch("otto.coverage.get.get_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["get"])
        assert result.exit_code == 0, result.output
        assert "/o/cov/[t1]/app/capture.json" in _flat(result.output)

    def test_a_successful_clean_prints_each_reset_and_exits_0(self):
        cleaned = CleanReport(hosts={"t1": {"app": Result(Status.Success)}})
        report = _get_report(captures=[Path("c")], clean=cleaned)
        with patch("otto.coverage.get.get_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["get", "--clean"])
        assert result.exit_code == 0, result.output
        assert "t1/app: counters cleared" in _flat(result.output)

    def test_a_failed_clean_prints_each_reset_and_exits_1(self):
        bad = CleanReport(hosts={"t1": {"app": Result(Status.Error, msg="denied")}})
        report = _get_report(captures=[Path("c")], clean=bad)
        with patch("otto.coverage.get.get_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["get", "--clean"])
        assert result.exit_code == 1
        out = _flat(result.output)
        assert "Coverage captured: 1 product(s)" in out  # the captures were still written
        assert "t1/app: denied" in out


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

    @staticmethod
    def _render_refusal(verb, target):
        """Invoke *verb* with *target* raising the no-config refusal; return (exit, rendered)."""
        import io

        from rich.console import Console
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        refusal = CoverageConfigError("No [coverage] section found in .otto/settings.toml")
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
            with patch(target, AsyncMock(side_effect=refusal)):
                result = runner.invoke(cov_app, [verb])
        finally:
            cov_module.logger.handlers = saved_handlers
            cov_module.logger.setLevel(saved_level)
            cov_module.logger.propagate = saved_propagate
        return result.exit_code, buf.getvalue()

    def test_no_config_error_renders_literal_brackets(self):
        exit_code, rendered = self._render_refusal("get", "otto.coverage.get.get_coverage")
        assert exit_code == 1
        assert "[coverage]" in rendered
        assert "No  section found" not in rendered

    def test_clean_no_config_error_renders_literal_brackets(self):
        exit_code, rendered = self._render_refusal("clean", "otto.coverage.collect.clean_coverage")
        assert exit_code == 1
        assert "[coverage]" in rendered
        assert "No  section found" not in rendered


# ── clean command — a thin leaf over clean_coverage ─────────────────────────
#
# Which hosts are walked, how each product is reset, and every refusal are the
# library's (tests/unit/cov/test_clean.py). The leaf calls it with no
# arguments and renders the report.


class TestCovCleanLeaf:
    def test_clean_coverage_is_called_with_no_arguments(self):
        clean_mock = AsyncMock(return_value=CleanReport(hosts={}))
        with patch("otto.coverage.collect.clean_coverage", clean_mock):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 0, result.output
        clean_mock.assert_awaited_once_with()

    def test_every_reset_gets_a_line_and_a_failure_exits_1(self):
        report = CleanReport(
            hosts={
                "t1": {"app": Result(Status.Success)},
                "zephyr": {"ext": Result(Status.Error, msg="reset_fn 'cov_reset' failed")},
            }
        )
        with patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["clean"])
        out = _flat(result.output)
        assert "t1/app: counters cleared" in out
        assert "zephyr/ext: reset_fn 'cov_reset' failed" in out
        assert result.exit_code == 1

    def test_every_success_exits_0(self):
        report = CleanReport(
            hosts={"t1": {"app": Result(Status.Success), "lib": Result(Status.Success)}}
        )
        with patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 0, result.output
        out = _flat(result.output)
        assert "t1/app: counters cleared" in out
        assert "t1/lib: counters cleared" in out

    def test_a_dry_run_prints_not_run_and_exits_0(self):
        report = CleanReport(hosts={"t1": {"app": Result(Status.NotRun)}})
        with patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 0, result.output
        assert "t1/app: not run (dry run)" in _flat(result.output)

    def test_a_host_with_no_instrumented_product_gets_one_line(self):
        report = CleanReport(hosts={"t1": {}})
        with patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 0, result.output
        assert "t1: no instrumented products" in _flat(result.output)

    def test_a_failure_reason_with_brackets_prints_literally(self):
        report = CleanReport(hosts={"t1": {"app": Result(Status.Error, msg="perm [denied]")}})
        with patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report)):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert "t1/app: perm [denied]" in _flat(result.output)

    def test_no_hosts_prints_clean_and_exits_1(self):
        err = NoCoverageHostsError("No coverage host matched [coverage].hosts — nothing to clean")
        with (
            patch("otto.coverage.collect.clean_coverage", AsyncMock(side_effect=err)),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "[coverage].hosts" in mock_err.call_args[0][0]

    def test_no_coverage_section_prints_clean_and_exits_1(self):
        err = CoverageConfigError("No [coverage] section found in .otto/settings.toml")
        with (
            patch("otto.coverage.collect.clean_coverage", AsyncMock(side_effect=err)),
            patch.object(cov_module.logger, "error") as mock_err,
        ):
            result = runner.invoke(cov_app, ["clean"])
        assert result.exit_code == 1
        assert not isinstance(result.exception, CoverageConfigError)
        assert "[coverage]" in mock_err.call_args[0][0]


# ── kmodcov subgroup — export/check are thin over otto.kmodcov, lab-free ────────


class TestKmodcov:
    """``otto cov kmodcov export|check``: thin over otto.kmodcov, lab-free, no output dir."""

    def test_listed_in_help(self):
        result = runner.invoke(cov_app, ["--help"])
        assert result.exit_code == 0
        assert "kmodcov" in result.output

    def test_export_writes_the_library_and_reports_what_changed(self, tmp_path):
        from otto import kmodcov

        result = runner.invoke(cov_app, ["kmodcov", "export", str(tmp_path / "lib")])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "lib" / "kmodcov.h").is_file()
        assert f"{len(kmodcov.SHIPPED_FILES) + 1} file(s) written" in result.output
        again = runner.invoke(cov_app, ["kmodcov", "export", str(tmp_path / "lib")])
        assert again.exit_code == 0
        assert "already current" in again.output

    def test_check_exits_zero_on_a_current_copy(self, tmp_path):
        runner.invoke(cov_app, ["kmodcov", "export", str(tmp_path)])
        result = runner.invoke(cov_app, ["kmodcov", "check", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "current" in result.output

    def test_check_exits_one_and_names_the_files_on_a_differing_copy(self, tmp_path):
        runner.invoke(cov_app, ["kmodcov", "export", str(tmp_path)])
        (tmp_path / "kmodcov.c").write_text("// edited\n")
        (tmp_path / "Kbuild").unlink()
        result = runner.invoke(cov_app, ["kmodcov", "check", str(tmp_path)])
        assert result.exit_code == 1
        assert "kmodcov.c" in result.output and "Kbuild" in result.output  # noqa: PT018
        assert "otto cov kmodcov export" in result.output

    def test_check_exits_two_where_there_is_no_library(self, tmp_path):
        result = runner.invoke(cov_app, ["kmodcov", "check", str(tmp_path / "nowhere")])
        assert result.exit_code == 2
        assert "no otto_kmodcov" in result.output

    def test_the_leaves_are_lab_free_and_make_no_output_dir(self):
        from otto.cli.cov import kmodcov_check, kmodcov_export

        for leaf in (kmodcov_export, kmodcov_check):
            assert leaf.__cli_lab_free__ is True
            assert leaf.__cli_output_dir__ is False


def test_kmodcov_check_runs_through_the_bridge_without_a_lab(tmp_path, monkeypatch):
    """The per-leaf marker, end to end: the cov group is lab-bound, this leaf is not.

    ``lab_free=False`` on the bridge is what makes this a real test of the
    marker: it drives the synthetic ``CommandSpec`` itself to lab-bound (the
    same as the real ``cov`` group's registration), so the preamble's
    ``not spec.lab_free`` arm is TRUE and only ``kmodcov_check``'s own
    ``__cli_lab_free__`` marker can still skip the lab slice. Confirmed by the
    negative control right below, which sends a leaf with no such marker
    through the same lab_free=False bridge and gets the missing-lab error.
    """
    from tests._fixtures.dispatch import DispatchRunner

    monkeypatch.delenv("OTTO_LAB", raising=False)
    bridge = DispatchRunner()
    result = bridge.invoke(
        cov_app, ["kmodcov", "check", str(tmp_path)], spec_name="cov", lab_free=False
    )
    assert "Missing option '--lab'" not in result.output
    assert result.exit_code == 2  # absent: the marker let it run, and it answered


def test_cov_report_through_the_bridge_without_a_lab_is_refused(monkeypatch):
    """Negative control for the marker test above: a lab-bound leaf with NO
    ``__cli_lab_free__`` marker (``cov report``) must hit the lab slice and
    fail with the missing-lab error under the identical ``lab_free=False``
    bridge — proving the harness genuinely enforces the lab slice rather than
    the marker test passing by some other coincidence."""
    from tests._fixtures.dispatch import DispatchRunner

    monkeypatch.delenv("OTTO_LAB", raising=False)
    bridge = DispatchRunner()
    result = bridge.invoke(cov_app, ["report"], spec_name="cov", lab_free=False)
    assert "Missing option '--lab'" in result.output
    assert result.exit_code == 2
