"""
Unit tests for the ``otto cov`` subcommand.

Covers:
  - Help / no-args behaviour
  - ``otto cov report`` happy path
  - ``otto cov report`` validation errors
"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli import cov as cov_module
from otto.cli.cov import cov_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _suppress_loggers():
    """Prevent logger stream handlers from writing to CliRunner's
    captured stdout after it is closed (causes ValueError on typer.Exit)."""
    loggers = [
        cov_module.logger,
        logging.getLogger('otto.coverage.reporter'),
    ]
    saved = [(l, l.level) for l in loggers]
    for l in loggers:
        l.setLevel(logging.CRITICAL + 1)
    yield
    for l, level in saved:
        l.setLevel(level)


# ── Help / no-args behaviour ─────────────────────────────────────────────────

class TestCovHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(cov_app, [])
        assert 'Usage' in result.output or 'usage' in result.output.lower()

    def test_help_flag(self):
        result = runner.invoke(cov_app, ['--help'])
        assert result.exit_code == 0

    def test_short_help_flag(self):
        result = runner.invoke(cov_app, ['-h'])
        assert result.exit_code == 0

    def test_report_listed_in_help(self):
        result = runner.invoke(cov_app, ['--help'])
        assert 'report' in result.output

    def test_report_help(self):
        result = runner.invoke(cov_app, ['report', '--help'])
        assert result.exit_code == 0
        assert 'OUTPUT_DIRS' in result.output


# ── report command — validation errors ───────────────────────────────────────

class TestCovReportValidation:
    def test_nonexistent_dir_exits_1(self):
        with patch.object(cov_module.logger, 'error') as mock_err:
            result = runner.invoke(cov_app, ['report', '/no/such/dir'])
        assert result.exit_code == 1
        mock_err.assert_called_once()
        assert 'does not exist' in mock_err.call_args[0][0]

    def test_no_gcda_dirs_exits_1(self, tmp_path):
        """Real directory but no cov/ subdirectory → error."""
        with patch.object(cov_module.logger, 'error') as mock_err:
            result = runner.invoke(cov_app, ['report', str(tmp_path)])
        assert result.exit_code == 1
        assert 'not generated' in mock_err.call_args[0][0]

    def test_source_root_not_found_exits_1(self, tmp_path):
        # Create a cov/ subdir with host dir so discover_gcda_dirs returns
        # entries, but no .otto_cov_meta.json so read_cov_source_root fails.
        (tmp_path / 'cov' / 'host1').mkdir(parents=True)
        with patch.object(cov_module.logger, 'error'):
            result = runner.invoke(cov_app, ['report', str(tmp_path)])
        assert result.exit_code == 1


# ── report command — success ─────────────────────────────────────────────────

class TestCovReportSuccess:
    @pytest.fixture
    def cov_tree(self, tmp_path):
        """Create a minimal output directory with cov/<host>/*.gcda."""
        host_dir = tmp_path / 'cov' / 'host1'
        host_dir.mkdir(parents=True)
        (host_dir / 'main.gcda').write_bytes(b'\x00')
        return tmp_path

    @pytest.fixture
    def mock_run_report(self):
        """Mock ``run_coverage_report`` at the I/O boundary."""
        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 75.0
        mock_store.file_count.return_value = 3

        mock = AsyncMock(return_value=mock_store)
        with patch.object(cov_module, 'run_coverage_report', mock):
            yield mock, mock_store

    def test_report_success(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ['report', str(cov_tree)])
        assert result.exit_code == 0
        mock.assert_called_once()

    def test_report_default_output_dir(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ['report', str(cov_tree)])
        assert result.exit_code == 0
        args, _ = mock.call_args.args, mock.call_args.kwargs
        assert args[1] == Path('./cov_report').resolve()

    def test_report_custom_report_dir(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, [
            'report', str(cov_tree), '--report', '/tmp/my_report',
        ])
        assert result.exit_code == 0
        args = mock.call_args.args
        assert args[1] == Path('/tmp/my_report').resolve()

    def test_report_custom_options(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, [
            'report', str(cov_tree),
            '--project-name', 'My Project',
        ])
        assert result.exit_code == 0
        assert mock.call_args.kwargs['project_name'] == 'My Project'

    def test_report_multiple_output_dirs(self, tmp_path, mock_run_report):
        mock, _ = mock_run_report
        dir1 = tmp_path / 'run1'
        dir2 = tmp_path / 'run2'
        for d in (dir1, dir2):
            host_dir = d / 'cov' / 'host1'
            host_dir.mkdir(parents=True)
            (host_dir / 'main.gcda').write_bytes(b'\x00')

        result = runner.invoke(cov_app, ['report', str(dir1), str(dir2)])
        assert result.exit_code == 0
        mock.assert_called_once()
        # Should have forwarded two cov dirs
        args = mock.call_args.args
        assert args[0] == [dir1 / 'cov', dir2 / 'cov']

    def test_report_default_tier_is_system(self, cov_tree, mock_run_report):
        """No --tier → default to system-only."""
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, ['report', str(cov_tree)])
        assert result.exit_code == 0
        assert mock.call_args.kwargs['tier_specs'] == [('system', None)]

    def test_report_tier_with_path(self, cov_tree, mock_run_report):
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, [
            'report', str(cov_tree),
            '--tier', 'unit=/tmp/u.info',
            '--tier', 'system',
        ])
        assert result.exit_code == 0
        assert mock.call_args.kwargs['tier_specs'] == [
            ('unit', Path('/tmp/u.info')),
            ('system', None),
        ]

    def test_report_tier_order_is_preserved(self, cov_tree, mock_run_report):
        """First --tier flag is highest precedence."""
        mock, _ = mock_run_report
        result = runner.invoke(cov_app, [
            'report', str(cov_tree),
            '--tier', 'unit=/u.info',
            '--tier', 'system',
            '--tier', 'integration=/i.info',
            '--tier', 'manual=/m.info',
        ])
        assert result.exit_code == 0
        names = [name for name, _ in mock.call_args.kwargs['tier_specs']]
        assert names == ['unit', 'system', 'integration', 'manual']

    def test_report_non_system_tier_without_path_errors(self, cov_tree):
        with patch.object(cov_module.logger, 'error'):
            result = runner.invoke(cov_app, [
                'report', str(cov_tree),
                '--tier', 'unit',  # No path → error (only system may omit)
            ])
        assert result.exit_code == 1

    def test_report_duplicate_tier_errors(self, cov_tree):
        with patch.object(cov_module.logger, 'error'):
            result = runner.invoke(cov_app, [
                'report', str(cov_tree),
                '--tier', 'unit=/a.info',
                '--tier', 'unit=/b.info',
            ])
        assert result.exit_code == 1
