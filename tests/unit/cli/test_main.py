"""
Unit tests for the main CLI entry-point argument parsing.

Tests cover:
  - Eager options that exit before the main callback (--version, --list-labs)
  - Global options forwarded to initOttoLogger (--verbose, --log-level, --log-days, --xdir)
  - Lab-loading arguments (--lab, --show-lab, --list-hosts)
  - Validation of numeric constraints (--log-days min=0)
  - --field / --debug toggle
"""

import logging
import os
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.logger import getOttoLogger

runner = CliRunner()


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def main_mocks():
    """
    Create main parser mocks

    Patch every external dependency touched by the main callback so tests
    don't need a real lab file, logger, or config module.
    """
    mock_lab = MagicMock()
    mock_lab.hosts = {}
    mock_config = MagicMock()
    mock_config.lab = mock_lab

    # Clear OTTO_* env vars so Typer envvar= defaults aren't overridden
    # by the user's shell environment.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith('OTTO_')}

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch('otto.cli.main.initOttoLogger') as p_logger,
        patch('otto.cli.main.getRepos', return_value=[]),
        patch('otto.cli.main.getLab', return_value=mock_lab) as p_getlab,
        patch('otto.cli.main.setConfigModule'),
        patch('otto.cli.main.getConfigModule', return_value=mock_config),
        patch('otto.cli.callbacks.getConfigModule', return_value=mock_config),
    ):
        yield {
            'initOttoLogger': p_logger,
            'getLab': p_getlab,
            'lab': mock_lab,
            'config': mock_config,
        }


def _invoke(extra_args: list[str]):
    """
    Invoke the main app.

    The required ``--lab`` option pre-filled so tests don't have to repeat it
    everywhere.
    """
    return runner.invoke(app, ['--lab', 'test_lab'] + extra_args)


# ── Eager / early-exit options ────────────────────────────────────────────────

class TestEagerOptions:
    """Options that exit before the main callback body runs."""

    def test_version_exits_zero(self):
        result = runner.invoke(app, ['--version'])
        assert result.exit_code == 0

    def test_version_prints_version_string(self):
        result = runner.invoke(app, ['--version'])
        assert 'version' in result.output.lower()

    def test_help_short_flag(self):
        result = runner.invoke(app, ['-h'])
        assert result.exit_code == 0

    def test_help_long_flag(self):
        result = runner.invoke(app, ['--help'])
        assert result.exit_code == 0

    def test_help_mentions_otto(self):
        result = runner.invoke(app, ['-h'])
        assert 'otto' in result.output.lower() or 'OTTO' in result.output

    def test_list_labs_exits_zero(self):
        # getRepos() returns [] in test env; just verifies the flag is accepted
        result = runner.invoke(app, ['--list-labs'])
        assert result.exit_code == 0


# ── Argument validation ───────────────────────────────────────────────────────

class TestArgumentValidation:
    """Typer/Click constraint enforcement for the main callback options."""

    def test_missing_lab_option_is_rejected(self):
        """--lab is required; without it (and without OTTO_LAB env var) the CLI must fail."""
        result = runner.invoke(app, [], env={'OTTO_LAB': ''})
        assert result.exit_code != 0

    def test_negative_log_days_rejected(self, main_mocks):
        result = _invoke(['--log-days', '-1'])
        assert result.exit_code == 2

    def test_zero_log_days_accepted(self, main_mocks):
        result = _invoke(['--log-days', '0'])
        assert result.exit_code == 0

    def test_positive_log_days_accepted(self, main_mocks):
        result = _invoke(['--log-days', '7'])
        assert result.exit_code == 0


# ── Logger arguments ──────────────────────────────────────────────────────────

class TestLoggerArguments:
    """Verify that parsed CLI values flow through initOttoLogger to real logger state.

    initOttoLogger runs for real here.  Assertions check observable logger
    state (level, xdir, rich_logging) and the I/O-boundary mocks
    (RichHandler constructor args, removeOldLogs call args).
    """

    def test_verbose_default_is_false(self, real_main_mocks):
        _invoke([])
        real_main_mocks['RichHandler'].assert_called_once_with(
            level=ANY, console=ANY, show_time=False,
            tracebacks_max_frames=ANY, tracebacks_show_locals=ANY,
            markup=ANY, highlighter=ANY, show_path=ANY,
            enable_link_path=ANY, log_time_format=ANY,
            omit_repeated_times=ANY,
        )

    def test_verbose_long_flag(self, real_main_mocks):
        _invoke(['--verbose'])
        real_main_mocks['RichHandler'].assert_called_once_with(
            level=ANY, console=ANY, show_time=True,
            tracebacks_max_frames=ANY, tracebacks_show_locals=ANY,
            markup=ANY, highlighter=ANY, show_path=ANY,
            enable_link_path=ANY, log_time_format=ANY,
            omit_repeated_times=ANY,
        )

    def test_verbose_short_flag(self, real_main_mocks):
        _invoke(['-v'])
        real_main_mocks['RichHandler'].assert_called_once_with(
            level=ANY, console=ANY, show_time=True,
            tracebacks_max_frames=ANY, tracebacks_show_locals=ANY,
            markup=ANY, highlighter=ANY, show_path=ANY,
            enable_link_path=ANY, log_time_format=ANY,
            omit_repeated_times=ANY,
        )

    def test_rich_log_file_default_is_false(self, real_main_mocks):
        _invoke([])
        assert getOttoLogger().rich_logging is False

    def test_rich_log_file_true(self, real_main_mocks):
        _invoke(['--rich-log-file'])
        assert getOttoLogger().rich_logging is True

    def test_rich_log_file_explicit_false(self, real_main_mocks):
        _invoke(['--no-rich-log-file'])
        assert getOttoLogger().rich_logging is False

    def test_log_level_default_is_info(self, real_main_mocks):
        _invoke([])
        assert getOttoLogger().level == logging.INFO

    def test_log_level_custom(self, real_main_mocks):
        _invoke(['--log-level', 'DEBUG'])
        assert getOttoLogger().level == logging.DEBUG

    def test_log_level_custom_lower_case(self, real_main_mocks):
        _invoke(['--log-level', 'debug'])
        assert getOttoLogger().level == logging.DEBUG

    def test_log_days_default(self, real_main_mocks):
        _invoke([])
        real_main_mocks['removeOldLogs'].assert_called_once_with(
            seconds=30 * 24 * 60 * 60,
        )

    def test_log_days_custom(self, real_main_mocks):
        _invoke(['--log-days', '14'])
        real_main_mocks['removeOldLogs'].assert_called_once_with(
            seconds=14 * 24 * 60 * 60,
        )

    def test_xdir_default_is_empty_path(self, real_main_mocks):
        _invoke([])
        assert getOttoLogger().xdir == Path()

    def test_xdir_custom_path(self, real_main_mocks, tmp_path):
        custom_xdir = tmp_path / 'custom_xdir'
        custom_xdir.mkdir()
        _invoke(['--xdir', str(custom_xdir)])
        assert getOttoLogger().xdir == custom_xdir


# ── Lab loading ───────────────────────────────────────────────────────────────

class TestLabLoading:
    """Verify lab loading produces real Lab objects with correct hosts.

    getLab runs for real here, reading hosts.json from the tmp_path fixture.
    The fixture data has three hosts across two labs:
      - test_lab: host1, host2
      - lab2: host2, host3
    """

    def test_single_lab_loads_correct_hosts(self, real_main_mocks):
        result = _invoke([])
        assert result.exit_code == 0
        from otto.configmodule import getConfigModule
        lab = getConfigModule().lab
        assert lab.name == 'test_lab'
        assert set(lab.hosts.keys()) == {'host1', 'host2'}

    def test_multiple_labs_split_on_comma(self, real_main_mocks):
        result = runner.invoke(app, ['--lab', 'test_lab,lab2'])
        assert result.exit_code == 0
        from otto.configmodule import getConfigModule
        lab = getConfigModule().lab
        assert set(lab.hosts.keys()) == {'host1', 'host2', 'host3'}

    def test_multiple_lab_flags(self, real_main_mocks):
        result = runner.invoke(app, ['--lab', 'test_lab', '--lab', 'lab2'])
        assert result.exit_code == 0
        from otto.configmodule import getConfigModule
        lab = getConfigModule().lab
        assert set(lab.hosts.keys()) == {'host1', 'host2', 'host3'}

    def test_host_objects_have_correct_ip(self, real_main_mocks):
        _invoke([])
        from otto.configmodule import getConfigModule
        lab = getConfigModule().lab
        assert lab.hosts['host1'].ip == '10.0.0.1'
        assert lab.hosts['host2'].ip == '10.0.0.2'

    def test_show_lab_exits_zero(self, real_main_mocks):
        result = _invoke(['--show-lab'])
        assert result.exit_code == 0

    def test_list_hosts_exits_zero(self, real_main_mocks):
        result = _invoke(['--list-hosts'])
        assert result.exit_code == 0

    def test_list_hosts_output_contains_host_ids(self, real_main_mocks):
        result = _invoke(['--list-hosts'])
        assert 'host1' in result.output
        assert 'host2' in result.output


# ── Field / debug product mode ────────────────────────────────────────────────

class TestFieldDebugMode:
    """--field/--debug is a boolean toggle; verify both flags are accepted."""

    def test_default_mode_exits_zero(self, main_mocks):
        result = _invoke([])
        assert result.exit_code == 0

    def test_field_flag_accepted(self, main_mocks):
        result = _invoke(['--field'])
        assert result.exit_code == 0

    def test_debug_flag_accepted(self, main_mocks):
        result = _invoke(['--debug'])
        assert result.exit_code == 0


# ── Dry-run mode ─────────────────────────────────────────────────────────────

class TestDryRunMode:
    """Verify --dry-run flag is accepted and propagates to hosts."""

    def test_dry_run_flag_accepted(self, main_mocks):
        result = _invoke(['--dry-run'])
        assert result.exit_code == 0

    def test_dry_run_short_flag_accepted(self, main_mocks):
        result = _invoke(['-n'])
        assert result.exit_code == 0

    def test_dry_run_sets_global_flag(self, main_mocks):
        """--dry-run should enable the global dry-run flag."""
        from otto.host.host import isDryRun, setDryRun
        setDryRun(False)  # ensure clean state
        _invoke(['--dry-run'])
        assert isDryRun() is True
        setDryRun(False)  # cleanup
