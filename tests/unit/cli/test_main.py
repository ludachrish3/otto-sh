"""
Unit tests for the main CLI entry-point argument parsing.

Tests cover:
  - Eager options that exit before the main callback (--version, --list-labs)
  - Global options forwarded to init_cli_logging (--show-time, --log-level, --log-days, --xdir)
  - Lab-loading arguments (--lab, --show-lab, --list-hosts)
  - Validation of numeric constraints (--log-days min=0, --lab-depth min=0)
  - --field / --debug toggle
"""

import logging
import os
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.cli.registry import register_cli_command
from otto.logger import get_logger, management
from otto.result import CommandResult
from otto.utils import Status

runner = CliRunner()


# Task 7: lab loading is now lazy — it runs in the leaf-invoke preamble, not the
# root callback. Tests that assert lab-load / logging side-effects must therefore
# dispatch a real (non-help, non-lab-free) leaf so the preamble fires. This
# scratch command is that leaf: its body is a no-op, but invoking `otto --lab X
# _main_probe` drives ensure_cli_session (banner/logging) + ensure_lab_context
# (lab load + reservation state) + the per-command output dir + the gate.
async def _main_probe() -> CommandResult:
    return CommandResult(Status.Success, value="", command="probe", retcode=0)


# gate=False: these tests exercise logging/lab/context, not the reservation
# gate (which has its own coverage); keeping it off avoids building a real
# reservation backend in a mock-lab environment.
register_cli_command("_main_probe", _main_probe, help="internal test probe", gate=False)


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def main_mocks(tmp_path):
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
    # by the user's shell environment; point OTTO_XDIR at tmp_path so logger
    # side-effects land there instead of the project root (--xdir is optional
    # and defaults to CWD, which we don't want tests writing to).
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    clean_env["OTTO_XDIR"] = str(tmp_path)

    from otto import bootstrap as bs

    bs._reset()
    # The lab / session work is lazy (Task 7) and lives in otto.cli.invoke, which
    # imports get_repos / load_lab from otto.configmodule at call time — patch the
    # source so both the root-callback (--show-lab) and preamble paths see mocks.
    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("otto.logger.management.init_cli_logging") as p_logger,
        patch("otto.configmodule.get_repos", return_value=[]),
        patch("otto.configmodule.load_lab", return_value=mock_lab) as p_getlab,
    ):
        yield {
            "init_cli_logging": p_logger,
            "load_lab": p_getlab,
            "lab": mock_lab,
            "config": mock_config,
        }
    bs._reset()


def _invoke(extra_args: list[str]):
    """
    Invoke the main app, driving a real leaf so the lazy preamble fires.

    ``--lab test_lab`` is pre-filled, and the ``_main_probe`` leaf is appended so
    that ensure_cli_session / ensure_lab_context run (they are lazy since Task 7
    and no longer live in the root callback). Root action flags like ``--show-lab``
    / ``--list-hosts`` still short-circuit in the root callback before the probe
    dispatches, so those tests observe only the lab load, as before.
    """
    return runner.invoke(app, ["--lab", "test_lab", *extra_args, "_main_probe"])


# ── Eager / early-exit options ────────────────────────────────────────────────


class TestEagerOptions:
    """Options that exit before the main callback body runs."""

    def test_version_exits_zero(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0

    def test_version_prints_version_string(self):
        result = runner.invoke(app, ["--version"])
        assert "version" in result.output.lower()

    def test_help_short_flag(self):
        result = runner.invoke(app, ["-h"])
        assert result.exit_code == 0

    def test_help_long_flag(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_help_mentions_otto(self):
        result = runner.invoke(app, ["-h"])
        assert "otto" in result.output.lower() or "OTTO" in result.output

    def test_list_labs_exits_zero(self):
        # get_repos() returns [] in test env; just verifies the flag is accepted
        result = runner.invoke(app, ["--list-labs"])
        assert result.exit_code == 0


# ── Argument validation ───────────────────────────────────────────────────────


class TestArgumentValidation:
    """Typer/Click constraint enforcement for the main callback options."""

    def test_missing_lab_option_is_rejected(self):
        """--lab is required; without it (and without OTTO_LAB env var) the CLI must fail."""
        result = runner.invoke(app, [], env={"OTTO_LAB": ""})
        assert result.exit_code != 0

    def test_lab_needing_path_without_lab_reports_missing_option(self):
        """A non-lab-free invocation without --lab errors with the clear message."""
        result = runner.invoke(app, ["--show-lab"], env={"OTTO_LAB": ""})
        assert result.exit_code != 0
        # click 8.2+ (Typer 0.26) routes usage errors to stderr, not stdout.
        assert "Missing option '--lab'" in result.stderr

    def test_negative_log_days_rejected(self, main_mocks):
        result = _invoke(["--log-days", "-1"])
        assert result.exit_code == 2

    def test_zero_log_days_accepted(self, main_mocks):
        result = _invoke(["--log-days", "0"])
        assert result.exit_code == 0

    def test_positive_log_days_accepted(self, main_mocks):
        result = _invoke(["--log-days", "7"])
        assert result.exit_code == 0


# ── Lab-free subcommands ──────────────────────────────────────────────────────


class TestLabFreeSubcommands:
    """`otto schema` introspects otto itself and must run without a lab."""

    def test_schema_export_runs_without_lab(self, tmp_path):
        out = tmp_path / "schemas"
        result = runner.invoke(
            app,
            ["schema", "export", "--out", str(out), "--builtins-only"],
            env={"OTTO_LAB": ""},
        )
        assert result.exit_code == 0, result.output
        assert (out / "hosts.schema.json").is_file()


# ── Lab-free flags: --help and --list-* discovery ────────────────────────────


class TestLabFreeFlags:
    """Subcommand --help and --list-* discovery flags must work without --lab.

    These flags inspect otto itself (registered suites / instructions) and
    touch no host resources, so forcing --lab would be a pointless barrier.

    Boundary: actual command execution (``otto test <Suite>``) and
    ``otto --list-hosts`` still require --lab because they operate on real
    lab state.
    """

    def test_test_help_exits_zero_without_lab(self):
        """``otto test --help`` must succeed with no --lab."""
        result = runner.invoke(app, ["test", "--help"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_test_short_help_exits_zero_without_lab(self):
        """``otto test -h`` must succeed with no --lab."""
        result = runner.invoke(app, ["test", "-h"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_run_help_exits_zero_without_lab(self):
        """``otto run --help`` must succeed with no --lab."""
        result = runner.invoke(app, ["run", "--help"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_test_list_suites_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-suites`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-suites"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_test_list_tests_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-tests`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-tests"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_test_list_markers_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-markers`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-markers"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_run_list_instructions_exits_zero_without_lab(self, tmp_path):
        """``otto run --list-instructions`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["run", "--list-instructions"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    # ── Boundary: these still require --lab ───────────────────────────────────

    def test_actual_suite_run_still_requires_lab(self):
        """``otto test <Suite>`` (no --help/--list-*) must still exit 2 without --lab.

        With lazy loading (Task 7), the ``--lab`` requirement is enforced by the
        leaf-invoke preamble rather than the root callback, so this must exercise a
        *real* registered suite leaf (an unknown name would error at parse time with
        "No such command", never reaching the preamble). A registered suite reaches
        the preamble, which enforces ``--lab`` and exits 2.
        """
        from otto.suite.register import register_suite

        @register_suite()
        class _LabReqSuite:
            pass

        # suite_app resolves _LabReqSuite lazily from the SUITES registry —
        # no explicit attach step needed.
        result = runner.invoke(app, ["test", "_LabReqSuite"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2, result.output
        assert "--lab" in result.stderr or "--lab" in result.output

    def test_list_hosts_still_requires_lab(self):
        """``otto --list-hosts`` queries lab state; must still exit 2 without --lab."""
        result = runner.invoke(app, ["--list-hosts"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2
        assert "--lab" in result.stderr or "--lab" in result.output


# ── Logger arguments ──────────────────────────────────────────────────────────


class TestLoggerArguments:
    """Verify that parsed CLI values flow through init_cli_logging to real logger state.

    init_cli_logging runs for real here.  Assertions check observable logger
    state (level, xdir, rich_logging) and the I/O-boundary mocks
    (RichHandler constructor args, remove_old_logs call args).
    """

    def test_show_time_default_is_false(self, real_main_mocks):
        _invoke([])
        real_main_mocks["RichHandler"].assert_called_once_with(
            level=ANY,
            console=ANY,
            show_time=False,
            tracebacks_max_frames=ANY,
            tracebacks_show_locals=ANY,
            markup=ANY,
            highlighter=ANY,
            show_path=ANY,
            enable_link_path=ANY,
            log_time_format=ANY,
            omit_repeated_times=ANY,
        )

    def test_show_time_flag(self, real_main_mocks):
        _invoke(["--show-time"])
        real_main_mocks["RichHandler"].assert_called_once_with(
            level=ANY,
            console=ANY,
            show_time=True,
            tracebacks_max_frames=ANY,
            tracebacks_show_locals=ANY,
            markup=ANY,
            highlighter=ANY,
            show_path=ANY,
            enable_link_path=ANY,
            log_time_format=ANY,
            omit_repeated_times=ANY,
        )

    def test_show_time_flag_replaces_verbose(self):
        result = runner.invoke(app, ["--help"])
        assert "--show-time" in result.output
        assert "--verbose" not in result.output

    def test_lab_depth_flag_present(self):
        result = runner.invoke(app, ["--help"])
        assert "--lab-depth" in result.output

    def test_rich_log_file_default_is_false(self, real_main_mocks):
        _invoke([])
        assert management._state.rich_log_file is False

    def test_rich_log_file_true(self, real_main_mocks):
        _invoke(["--rich-log-file"])
        assert management._state.rich_log_file is True

    def test_rich_log_file_explicit_false(self, real_main_mocks):
        _invoke(["--no-rich-log-file"])
        assert management._state.rich_log_file is False

    def test_log_level_default_is_info(self, real_main_mocks):
        _invoke([])
        assert get_logger().level == logging.INFO

    def test_log_level_custom(self, real_main_mocks):
        _invoke(["--log-level", "DEBUG"])
        assert get_logger().level == logging.DEBUG

    def test_log_level_custom_lower_case(self, real_main_mocks):
        _invoke(["--log-level", "debug"])
        assert get_logger().level == logging.DEBUG

    def test_log_days_default(self, real_main_mocks):
        _invoke([])
        assert management._state.keep_seconds == 30 * 24 * 60 * 60

    def test_log_days_custom(self, real_main_mocks):
        _invoke(["--log-days", "14"])
        assert management._state.keep_seconds == 14 * 24 * 60 * 60

    def test_xdir_from_env(self, real_main_mocks):
        # real_main_mocks pre-sets OTTO_XDIR to tmp_path; the callback should
        # pick that up without an explicit --xdir on the command line.
        _invoke([])
        assert management._state.xdir == real_main_mocks["tmp_path"]

    def test_xdir_custom_path(self, real_main_mocks, tmp_path):
        custom_xdir = tmp_path / "custom_xdir"
        custom_xdir.mkdir()
        _invoke(["--xdir", str(custom_xdir)])
        assert management._state.xdir == custom_xdir

    def test_xdir_default_when_neither_flag_nor_env(self, real_main_mocks, monkeypatch):
        """--xdir is optional: with neither flag nor OTTO_XDIR it defaults to CWD.

        The CWD default is safe because ``remove_old_logs`` only rmtree's entries
        matching otto's timestamped log-dir name pattern (see management.py), so a
        CWD-pointed xdir can no longer walk foreign trees at startup.
        """
        monkeypatch.delenv("OTTO_XDIR", raising=False)
        result = _invoke([])
        assert result.exit_code == 0
        assert management._state.xdir == Path()


# ── Lab loading ───────────────────────────────────────────────────────────────


class TestLabLoading:
    """Verify lab loading produces real Lab objects with correct hosts.

    load_lab runs for real here, reading hosts.json from the tmp_path fixture.
    The fixture data has three hosts across two labs:
      - test_lab: host1, host2
      - lab2: host2, host3
    """

    def test_single_lab_loads_correct_hosts(self, real_main_mocks):
        result = _invoke([])
        assert result.exit_code == 0
        from otto.configmodule import get_lab

        lab = get_lab()
        assert lab.name == "test_lab"
        # `local` is the built-in host injected into every lab by load_lab.
        assert set(lab.hosts.keys()) == {"host1", "host2", "local"}

    def test_multiple_labs_split_on_comma(self, real_main_mocks):
        # Append the probe leaf so the lazy preamble loads the lab (Task 7).
        result = runner.invoke(app, ["--lab", "test_lab,lab2", "_main_probe"])
        assert result.exit_code == 0
        from otto.configmodule import get_lab

        lab = get_lab()
        assert set(lab.hosts.keys()) == {"host1", "host2", "host3", "local"}

    def test_multiple_lab_flags(self, real_main_mocks):
        # Append the probe leaf so the lazy preamble loads the lab (Task 7).
        result = runner.invoke(app, ["--lab", "test_lab", "--lab", "lab2", "_main_probe"])
        assert result.exit_code == 0
        from otto.configmodule import get_lab

        lab = get_lab()
        assert set(lab.hosts.keys()) == {"host1", "host2", "host3", "local"}

    def test_host_objects_have_correct_ip(self, real_main_mocks):
        _invoke([])
        from otto.configmodule import get_lab

        lab = get_lab()
        assert lab.hosts["host1"].ip == "10.0.0.1"
        assert lab.hosts["host2"].ip == "10.0.0.2"

    def test_show_lab_exits_zero(self, real_main_mocks):
        result = _invoke(["--show-lab"])
        assert result.exit_code == 0

    def test_show_lab_lab_depth_zero_maps_to_unlimited(self, real_main_mocks):
        """--lab-depth 0 must reach pprint as max_depth=None (unlimited)."""
        # The --show-lab block does `from rich.pretty import pprint` locally,
        # so patch where it is looked up.
        with patch("rich.pretty.pprint") as spy:
            result = _invoke(["--show-lab", "--lab-depth", "0"])
        assert result.exit_code == 0
        spy.assert_called_once()
        assert spy.call_args.kwargs["max_depth"] is None

    def test_show_lab_lab_depth_value_passed_to_pprint(self, real_main_mocks):
        """--lab-depth N (N > 0) must reach pprint as max_depth=N."""
        with patch("rich.pretty.pprint") as spy:
            result = _invoke(["--show-lab", "--lab-depth", "2"])
        assert result.exit_code == 0
        spy.assert_called_once()
        assert spy.call_args.kwargs["max_depth"] == 2

    def test_list_hosts_exits_zero(self, real_main_mocks):
        result = _invoke(["--list-hosts"])
        assert result.exit_code == 0

    def test_list_hosts_output_contains_host_ids(self, real_main_mocks):
        result = _invoke(["--list-hosts"])
        assert "host1" in result.output
        assert "host2" in result.output


# ── Field / debug product mode ────────────────────────────────────────────────


class TestFieldDebugMode:
    """--field/--debug is a boolean toggle; verify both flags are accepted."""

    def test_default_mode_exits_zero(self, main_mocks):
        result = _invoke([])
        assert result.exit_code == 0

    def test_field_flag_accepted(self, main_mocks):
        result = _invoke(["--field"])
        assert result.exit_code == 0

    def test_debug_flag_accepted(self, main_mocks):
        result = _invoke(["--debug"])
        assert result.exit_code == 0


# ── Dry-run mode ─────────────────────────────────────────────────────────────


class TestDryRunMode:
    """Verify --dry-run flag is accepted and propagates to hosts."""

    def test_dry_run_flag_accepted(self, main_mocks):
        result = _invoke(["--dry-run"])
        assert result.exit_code == 0

    def test_dry_run_short_flag_accepted(self, main_mocks):
        result = _invoke(["-n"])
        assert result.exit_code == 0

    def test_dry_run_sets_context_flag(self, main_mocks):
        """--dry-run should enable dry_run on the active OttoContext."""
        from otto.host.host import is_dry_run

        _invoke(["--dry-run"])
        assert is_dry_run() is True
